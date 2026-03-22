"""
Duplicate file scanner engine.

Uses a progressive strategy to minimize I/O:
  1. Enumerate files (os.scandir, skipping system/junk dirs + cloud files)
  2. Group by chosen criteria (name, size)
  3. Partial hash (first + last 64 KB) – parallel, batched with timeout
  4. Full hash (streamed, parallel, batched with timeout)

Cloud / placeholder files (OneDrive, iCloud, etc.) are detected via
Windows file attributes and skipped automatically so they never block I/O.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

import csv as _csv
from datetime import datetime as _datetime

import xxhash
import hashlib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PARTIAL_CHUNK = 64 * 1024        # 64 KB for partial hash
FULL_CHUNK    = 4 * 1024 * 1024  # 4 MB read buffer for full hash
HASH_BATCH    = 5_000            # files per parallel batch
BATCH_TIMEOUT_MIN = 120          # minimum batch timeout (seconds)
# Timeout scales with total data in the batch:
#   100 MB/s is conservative for SHA-256 on NAS SSDs.
#   Applied per-worker, so total batch throughput = workers × this rate.
BATCH_TIMEOUT_RATE = 100 * 1024 * 1024  # bytes/sec assumed throughput per worker

# Directories to always skip (case-insensitive on Windows)
SKIP_DIRS: set[str] = {
    # Windows system (never contain user duplicates)
    "$recycle.bin", "system volume information", "$windows.~bt",
    "$windows.~ws", "windows", "windows.old",
    "recovery", "config.msi", "msocache",
    # Dev tooling (version-controlled / generated)
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv",
}

# Windows file attributes that indicate cloud / placeholder / offline files.
# Reading these files can trigger a download from OneDrive / iCloud / etc.
# and block for minutes or forever.  We skip them during enumeration.
#   FILE_ATTRIBUTE_OFFLINE              = 0x0000_1000
#   FILE_ATTRIBUTE_RECALL_ON_OPEN       = 0x0004_0000
#   FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS= 0x0040_0000
#   FILE_ATTRIBUTE_VIRTUAL              = 0x0001_0000
_WIN_CLOUD_ATTRS = 0
if sys.platform == "win32":
    _WIN_CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000 | 0x10000


class DuplicateMode(Enum):
    NAME = auto()
    SIZE = auto()
    HASH = auto()


@dataclass(slots=True)
class FileEntry:
    path: str
    name: str
    size: int
    mtime: float = 0.0
    partial_hash: str = ""
    full_hash: str = ""


@dataclass
class DuplicateGroup:
    """A group of files considered duplicates."""
    mode: DuplicateMode
    key: str
    files: list[FileEntry] = field(default_factory=list)


@dataclass
class ScanStats:
    """Statistics about a completed scan."""
    total_files_scanned: int = 0
    total_size_scanned: int = 0       # bytes
    cloud_files_skipped: int = 0
    size_groups: int = 0
    partial_groups: int = 0
    duplicate_groups: int = 0
    duplicate_files: int = 0
    reclaimable_bytes: int = 0
    hash_algorithm: str = "xxhash"
    index_path: str = ""          # path to saved file index CSV, if any


@dataclass(slots=True)
class DirectoryRule:
    """A rule that controls how files under a directory are treated.

    *path*      – absolute directory path
    *rule_type* – ``"preserve"`` or ``"expendable"``

    **Preserve**:   files here are canonical – delete matching copies elsewhere.
    **Expendable**: files here are junk copies – delete them if copies exist elsewhere.
    """
    path: str
    rule_type: str          # "preserve" | "expendable"

    PRESERVE   = "preserve"
    EXPENDABLE = "expendable"


# Callback types
ProgressCallback = Callable[[str, int, int], None]
CancelCheck = Callable[[], bool]


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _new_hasher(use_sha256: bool = False):
    """Return a fresh hash object (xxh128 or sha256)."""
    return hashlib.sha256() if use_sha256 else xxhash.xxh128()


def _make_partial_hash(use_sha256: bool = False):
    """Return a partial-hash function bound to the chosen algorithm."""
    def _partial_hash(path: str) -> str:
        try:
            h = _new_hasher(use_sha256)
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                h.update(f.read(PARTIAL_CHUNK))
                if size > PARTIAL_CHUNK * 2:
                    f.seek(-PARTIAL_CHUNK, os.SEEK_END)
                    h.update(f.read(PARTIAL_CHUNK))
            return h.hexdigest()
        except (OSError, PermissionError):
            return ""
    return _partial_hash


def _make_full_hash(use_sha256: bool = False):
    """Return a full-hash function bound to the chosen algorithm."""
    def _full_hash(path: str) -> str:
        try:
            h = _new_hasher(use_sha256)
            with open(path, "rb") as f:
                while chunk := f.read(FULL_CHUNK):
                    h.update(chunk)
            return h.hexdigest()
        except (OSError, PermissionError):
            return ""
    return _full_hash


# macOS: iCloud placeholder flags (UF_DATALESS / SF_DATALESS)
_MAC_CLOUD_FLAGS = 0
if sys.platform == "darwin":
    _MAC_CLOUD_FLAGS = 0x0040 | 0x40000000  # UF_DATALESS | SF_DATALESS


def _is_cloud_file(st) -> bool:
    """Return True if the file is a cloud placeholder / offline file.

    Supports Windows (OneDrive / iCloud for Windows) and macOS (iCloud Drive).
    On Linux there are no known cloud placeholder attributes.
    """
    if sys.platform == "win32" and _WIN_CLOUD_ATTRS:
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & _WIN_CLOUD_ATTRS:
            return True
    elif sys.platform == "darwin" and _MAC_CLOUD_FLAGS:
        flags = getattr(st, "st_flags", 0)
        if flags & _MAC_CLOUD_FLAGS:
            return True
    return False


# ---------------------------------------------------------------------------
# Fast directory walker
# ---------------------------------------------------------------------------

def _walk_fast(
    root: str,
    min_size: int,
    skip_dirs: set[str],
    cancelled: CancelCheck,
    progress: ProgressCallback,
) -> tuple[list[FileEntry], int]:
    """Walk directory tree using os.scandir with filtering.

    Returns (files, skipped_cloud_count).
    """
    all_files: list[FileEntry] = []
    count = 0
    skipped_cloud = 0
    dirs_to_walk = [root]

    while dirs_to_walk:
        if cancelled():
            return [], 0
        current = dirs_to_walk.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in skip_dirs:
                                dirs_to_walk.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            # Skip cloud / placeholder files
                            if _is_cloud_file(st):
                                skipped_cloud += 1
                                continue
                            if st.st_size >= min_size:
                                all_files.append(FileEntry(
                                    path=entry.path,
                                    name=entry.name,
                                    size=st.st_size,
                                    mtime=st.st_mtime,
                                ))
                                count += 1
                                if count % 5000 == 0:
                                    progress(
                                        f"Enumerating files… {count:,}"
                                        + (f" ({skipped_cloud:,} cloud skipped)" if skipped_cloud else ""),
                                        count, 0,
                                    )
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    return all_files, skipped_cloud


# ---------------------------------------------------------------------------
# Batched parallel hashing  (the key to never freezing)
# ---------------------------------------------------------------------------

def _parallel_hash_phase(
    files: list[FileEntry],
    hash_fn: Callable[[str], str],
    assign_fn: Callable[[FileEntry, str], None],
    workers: int,
    cancelled: CancelCheck,
    progress: ProgressCallback,
    phase_label: str,
) -> int:
    """
    Hash *files* in parallel, in batches with timeouts.

    *hash_fn*:   takes a path, returns a hash string (or "")
    *assign_fn*: called with (fe, hash) for each successful result
    *workers*:   thread count

    Returns the number of files skipped (timed-out or errored).
    """
    total = len(files)
    done = 0
    skipped = 0

    for batch_start in range(0, total, HASH_BATCH):
        if cancelled():
            return skipped

        batch = files[batch_start : batch_start + HASH_BATCH]
        # Timeout scales with the total bytes in this batch so large
        # files (1 GB+) get enough time even with slower algorithms.
        batch_bytes = sum(fe.size for fe in batch)
        bytes_per_worker = batch_bytes / max(workers, 1)
        size_timeout = (bytes_per_worker / BATCH_TIMEOUT_RATE) * 2  # 2× safety
        batch_timeout = max(BATCH_TIMEOUT_MIN, size_timeout)

        pool = ThreadPoolExecutor(max_workers=workers)
        futs = {pool.submit(hash_fn, fe.path): fe for fe in batch}

        try:
            for future in as_completed(futs, timeout=batch_timeout):
                if cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return skipped
                try:
                    h = future.result(timeout=0)  # already complete
                    fe = futs[future]
                    if h:
                        assign_fn(fe, h)
                except Exception:
                    skipped += 1
                done += 1
                if done % 2000 == 0:
                    progress(
                        f"{phase_label} {done:,}/{total:,}"
                        + (f" ({skipped} skipped)" if skipped else ""),
                        done, total,
                    )
        except TimeoutError:
            # Some futures in this batch are stuck — count and abandon them
            n_stuck = sum(1 for f in futs if not f.done())
            skipped += n_stuck
            done += n_stuck
            progress(
                f"{phase_label} {done:,}/{total:,} ({skipped} skipped – batch timeout)",
                done, total,
            )

        # Shut down pool WITHOUT waiting for stuck threads
        pool.shutdown(wait=False, cancel_futures=True)

    progress(
        f"{phase_label} {done:,}/{total:,}"
        + (f" ({skipped} skipped)" if skipped else "") + " ✓",
        total, total,
    )
    return skipped


# ---------------------------------------------------------------------------
# File index writer
# ---------------------------------------------------------------------------

def _index_human_size(n: int | float) -> str:
    """Compact human-readable file size for the index CSV."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def write_file_index(all_files: list[FileEntry], path: str) -> None:
    """Write a full file-system index of every enumerated file to a CSV.

    Columns: path, directory, filename, size_bytes, size, last_modified

    This is separate from the duplicate report – it covers ALL files that
    were seen during the scan, not just the ones that had duplicates.  It
    acts like a lightweight IYF / Everything snapshot so you can grep for
    a filename later without re-scanning the whole drive.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["# FileDuplicator File Index"])
        w.writerow(["# Generated", _datetime.now().isoformat()])
        w.writerow(["# Total Files", len(all_files)])
        w.writerow([])
        w.writerow(["path", "directory", "filename", "size_bytes", "size", "last_modified"])
        for fe in all_files:
            try:
                mtime_str = _datetime.fromtimestamp(fe.mtime).isoformat() if fe.mtime else ""
            except (OSError, ValueError, OverflowError):
                mtime_str = ""
            w.writerow([
                fe.path,
                os.path.dirname(fe.path),
                fe.name,
                fe.size,
                _index_human_size(fe.size),
                mtime_str,
            ])


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_directory(
    root: str | list[str],
    *,
    by_name: bool = False,
    by_size: bool = True,
    by_hash: bool = True,
    recursive: bool = True,
    min_size: int = 1,
    use_sha256: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
    extra_skip_dirs: set[str] | None = None,
    index_path: str | None = None,
) -> tuple[list[DuplicateGroup], ScanStats]:
    """
    Scan one or more directories for duplicate files.

    *root* may be a single path string or a list of path strings.
    Returns (groups, stats) where groups is sorted by wasted space (largest first).
    """
    roots = [root] if isinstance(root, str) else list(root)
    _progress = progress or (lambda *_: None)
    _cancelled = cancelled or (lambda: False)
    skip = SKIP_DIRS | (extra_skip_dirs or set())
    workers = min(4, (os.cpu_count() or 2))
    hash_name = "SHA-256" if use_sha256 else "xxHash (xxh128)"
    stats = ScanStats(hash_algorithm=hash_name)

    # ------------------------------------------------------------------
    # Phase 1 – enumerate files
    # ------------------------------------------------------------------
    all_files: list[FileEntry] = []
    cloud_skipped = 0
    for idx, scan_root in enumerate(roots, 1):
        prefix = f"Phase 1 · [{idx}/{len(roots)}] " if len(roots) > 1 else "Phase 1 · "
        _progress(f"{prefix}Enumerating files…", 0, 0)
        if _cancelled():
            return [], stats

        if recursive:
            root_files, root_cloud = _walk_fast(scan_root, min_size, skip, _cancelled, _progress)
            all_files.extend(root_files)
            cloud_skipped += root_cloud
        else:
            try:
                with os.scandir(scan_root) as it:
                    for entry in it:
                        if _cancelled():
                            return [], stats
                        try:
                            if entry.is_file(follow_symlinks=False):
                                st = entry.stat(follow_symlinks=False)
                                if _is_cloud_file(st):
                                    cloud_skipped += 1
                                    continue
                                if st.st_size >= min_size:
                                    all_files.append(FileEntry(
                                        path=entry.path,
                                        name=entry.name,
                                        size=st.st_size,
                                        mtime=st.st_mtime,
                                    ))
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError):
                pass

    total_files = len(all_files)
    total_size = sum(fe.size for fe in all_files)
    stats.total_files_scanned = total_files
    stats.total_size_scanned = total_size
    stats.cloud_files_skipped = cloud_skipped
    cloud_msg = f" ({cloud_skipped:,} cloud files skipped)" if cloud_skipped else ""
    _progress(
        f"Phase 1 · {total_files:,} files found{cloud_msg}",
        total_files, total_files,
    )

    # Write full file index if requested (all files, not just duplicates)
    if index_path:
        try:
            write_file_index(all_files, index_path)
            stats.index_path = index_path
            _progress(
                f"Phase 1 · File index saved ({total_files:,} files) → {index_path}",
                total_files, total_files,
            )
        except Exception as _exc:
            _progress(
                f"Phase 1 · Warning: could not write file index: {_exc}",
                total_files, total_files,
            )

    if _cancelled() or total_files == 0:
        return [], stats

    results: list[DuplicateGroup] = []

    # ------------------------------------------------------------------
    # Phase 2 – group by name (optional, no hash needed)
    # ------------------------------------------------------------------
    if by_name and not by_size and not by_hash:
        _progress("Phase 2 · Grouping by name…", 0, total_files)
        name_map: dict[str, list[FileEntry]] = defaultdict(list)
        for fe in all_files:
            name_map[fe.name.lower()].append(fe)
        for key, group in name_map.items():
            if len(group) >= 2:
                results.append(DuplicateGroup(DuplicateMode.NAME, key, group))
        results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)
        stats.duplicate_groups = len(results)
        stats.duplicate_files = sum(len(g.files) for g in results)
        stats.reclaimable_bytes = sum(g.files[0].size * (len(g.files) - 1) for g in results)
        _progress("Phase 2 · Done", total_files, total_files)
        return results, stats

    # ------------------------------------------------------------------
    # Phase 2 – group by size
    # ------------------------------------------------------------------
    _progress("Phase 2 · Grouping by size…", 0, total_files)
    size_map: dict[int, list[FileEntry]] = defaultdict(list)
    for fe in all_files:
        size_map[fe.size].append(fe)

    size_groups = {sz: grp for sz, grp in size_map.items() if len(grp) >= 2}
    del size_map

    candidates_count = sum(len(g) for g in size_groups.values())
    stats.size_groups = len(size_groups)
    _progress(
        f"Phase 2 · {len(size_groups):,} size groups ({candidates_count:,} files)",
        total_files, total_files,
    )

    if _cancelled():
        return [], stats

    if not by_hash:
        for sz, grp in size_groups.items():
            results.append(DuplicateGroup(DuplicateMode.SIZE, f"{sz:,} bytes", grp))
        results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)
        stats.duplicate_groups = len(results)
        stats.duplicate_files = sum(len(g.files) for g in results)
        stats.reclaimable_bytes = sum(g.files[0].size * (len(g.files) - 1) for g in results)
        return results, stats

    # ------------------------------------------------------------------
    # Phase 3 – partial hash (parallel, batched with timeout)
    # ------------------------------------------------------------------
    candidates = [fe for grp in size_groups.values() for fe in grp]
    del size_groups

    partial_map: dict[str, list[FileEntry]] = defaultdict(list)
    partial_hash_fn = _make_partial_hash(use_sha256)

    def _assign_partial(fe: FileEntry, h: str):
        fe.partial_hash = h
        partial_map[f"{fe.size}:{h}"].append(fe)

    _parallel_hash_phase(
        candidates,
        hash_fn=partial_hash_fn,
        assign_fn=_assign_partial,
        workers=workers,
        cancelled=_cancelled,
        progress=_progress,
        phase_label=f"Phase 3 · Partial hash ({hash_name})",
    )

    del candidates
    partial_groups = {k: g for k, g in partial_map.items() if len(g) >= 2}
    del partial_map
    stats.partial_groups = len(partial_groups)

    _progress(
        f"Phase 3 · {len(partial_groups):,} groups need full hash",
        1, 1,
    )

    if _cancelled():
        return [], stats

    # ------------------------------------------------------------------
    # Phase 4 – full hash (parallel, batched with timeout)
    # ------------------------------------------------------------------
    full_candidates = sorted(
        [fe for grp in partial_groups.values() for fe in grp],
        key=lambda fe: fe.size,
    )
    del partial_groups

    full_map: dict[str, list[FileEntry]] = defaultdict(list)
    full_hash_fn = _make_full_hash(use_sha256)

    def _assign_full(fe: FileEntry, h: str):
        fe.full_hash = h
        full_map[f"{fe.size}:{h}"].append(fe)

    _parallel_hash_phase(
        full_candidates,
        hash_fn=full_hash_fn,
        assign_fn=_assign_full,
        workers=workers,
        cancelled=_cancelled,
        progress=_progress,
        phase_label=f"Phase 4 · Full hash ({hash_name})",
    )

    for key, grp in full_map.items():
        if len(grp) >= 2:
            results.append(DuplicateGroup(DuplicateMode.HASH, key, grp))

    results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)

    stats.duplicate_groups = len(results)
    stats.duplicate_files = sum(len(g.files) for g in results)
    stats.reclaimable_bytes = sum(g.files[0].size * (len(g.files) - 1) for g in results)

    _progress(f"Done · {len(results):,} duplicate groups", 1, 1)
    return results, stats


# ---------------------------------------------------------------------------
# Directory rule engine
# ---------------------------------------------------------------------------

def apply_directory_rules(
    groups: list[DuplicateGroup],
    rules: list[DirectoryRule],
) -> dict[str, str]:
    """Apply directory priority rules to duplicate groups.

    For each group the logic is:

    1. **Preserve wins**: if any file lives under a *preserve* path, keep it
       and mark everything else for deletion.
    2. **Conflict**: if *two or more* preserve paths both claim a copy, flag
       the whole group for manual ``"review"`` (never auto-delete).
    3. **Expendable**: if a file is under an *expendable* path *and* a safe
       copy exists outside expendable paths, delete the expendable copy.
       If *only* expendable copies exist, keep the oldest one.

    Returns ``{filepath: "keep" | "delete" | "review"}`` for every file that
    is affected by at least one rule.  Files in rule-free groups are omitted.
    """
    if not rules or not groups:
        return {}

    # Normalise rule paths once for fast prefix matching
    norm_rules = [
        (os.path.normcase(os.path.normpath(r.path)) + os.sep, r.rule_type)
        for r in rules
    ]

    def _classify(filepath: str) -> str | None:
        fp = os.path.normcase(os.path.normpath(filepath))
        for rp, rt in norm_rules:
            if fp.startswith(rp):
                return rt
        return None

    decisions: dict[str, str] = {}

    for group in groups:
        preserve: list[FileEntry] = []
        expendable: list[FileEntry] = []
        neutral: list[FileEntry] = []

        for fe in group.files:
            c = _classify(fe.path)
            if c == DirectoryRule.PRESERVE:
                preserve.append(fe)
            elif c == DirectoryRule.EXPENDABLE:
                expendable.append(fe)
            else:
                neutral.append(fe)

        # Skip groups where no rules apply
        if not preserve and not expendable:
            continue

        if len(preserve) > 1:
            # Conflict – multiple preserve paths claim the same content
            for fe in group.files:
                decisions[fe.path] = "review"
        elif preserve:
            # Clear canonical copy exists
            for fe in preserve:
                decisions[fe.path] = "keep"
            for fe in expendable + neutral:
                decisions[fe.path] = "delete"
        elif expendable:
            if neutral:
                # Safe copies exist outside expendable paths
                for fe in expendable:
                    decisions[fe.path] = "delete"
                for fe in neutral:
                    decisions[fe.path] = "keep"
            elif len(expendable) > 1:
                # ALL copies are expendable – keep the oldest
                sorted_exp = sorted(expendable, key=lambda f: f.mtime or 0)
                decisions[sorted_exp[0].path] = "keep"
                for fe in sorted_exp[1:]:
                    decisions[fe.path] = "delete"
            # else: single expendable copy with no safe copy elsewhere → keep

    return decisions
