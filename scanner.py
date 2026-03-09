"""
Duplicate file scanner engine.

Uses a progressive strategy to minimize I/O:
  1. Enumerate files (os.scandir, skipping system/junk dirs)
  2. Group by chosen criteria (name, size)
  3. Partial hash (first + last 64 KB) – cheap pre-filter
  4. Full hash (streamed, parallel via ThreadPoolExecutor)

This avoids reading entire multi-GB files unless truly necessary.
"""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import xxhash

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PARTIAL_CHUNK = 64 * 1024        # 64 KB for partial hash
FULL_CHUNK    = 4 * 1024 * 1024  # 4 MB read buffer for full hash

# Directories to always skip (case-insensitive on Windows)
# ONLY truly useless system dirs – no user content lives here
SKIP_DIRS: set[str] = {
    # Windows system (never contain user duplicates)
    "$recycle.bin", "system volume information", "$windows.~bt",
    "$windows.~ws", "windows", "windows.old",
    "recovery", "config.msi", "msocache",
    # Dev tooling (version-controlled / generated – not real duplicates)
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv",
}


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


# Callback types
ProgressCallback = Callable[[str, int, int], None]
CancelCheck = Callable[[], bool]

# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _partial_hash(path: str) -> str:
    """Hash the first and last PARTIAL_CHUNK bytes of a file (fast)."""
    try:
        h = xxhash.xxh128()
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            h.update(f.read(PARTIAL_CHUNK))
            if size > PARTIAL_CHUNK * 2:
                f.seek(-PARTIAL_CHUNK, os.SEEK_END)
                h.update(f.read(PARTIAL_CHUNK))
        return h.hexdigest()
    except (OSError, PermissionError):
        return ""


def _full_hash(path: str) -> str:
    """Stream-hash the entire file in FULL_CHUNK increments."""
    try:
        h = xxhash.xxh128()
        with open(path, "rb") as f:
            while chunk := f.read(FULL_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# Fast directory walker
# ---------------------------------------------------------------------------

def _walk_fast(
    root: str,
    min_size: int,
    skip_dirs: set[str],
    cancelled: CancelCheck,
    progress: ProgressCallback,
) -> list[FileEntry]:
    """Walk directory tree using os.scandir with directory filtering."""
    all_files: list[FileEntry] = []
    count = 0
    dirs_to_walk = [root]

    while dirs_to_walk:
        if cancelled():
            return []
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
                            if st.st_size >= min_size:
                                all_files.append(FileEntry(
                                    path=entry.path,
                                    name=entry.name,
                                    size=st.st_size,
                                    mtime=st.st_mtime,
                                ))
                                count += 1
                                if count % 5000 == 0:
                                    progress("Enumerating files…", count, 0)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    return all_files


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def scan_directory(
    root: str,
    *,
    by_name: bool = False,
    by_size: bool = True,
    by_hash: bool = True,
    recursive: bool = True,
    min_size: int = 1,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
    extra_skip_dirs: set[str] | None = None,
) -> list[DuplicateGroup]:
    """
    Scan *root* for duplicate files.

    Returns a list of DuplicateGroup sorted by wasted space (largest first).
    """
    _progress = progress or (lambda *_: None)
    _cancelled = cancelled or (lambda: False)
    skip = SKIP_DIRS | (extra_skip_dirs or set())

    # ------------------------------------------------------------------
    # Phase 1 – enumerate files
    # ------------------------------------------------------------------
    _progress("Phase 1 · Enumerating files…", 0, 0)

    if recursive:
        all_files = _walk_fast(root, min_size, skip, _cancelled, _progress)
    else:
        all_files = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if _cancelled():
                        return []
                    try:
                        if entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
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
    _progress(f"Phase 1 · {total_files:,} files found", total_files, total_files)

    if _cancelled() or total_files == 0:
        return []

    results: list[DuplicateGroup] = []

    # ------------------------------------------------------------------
    # Phase 2 – group by name (optional)
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
        _progress("Phase 2 · Done", total_files, total_files)
        return results

    # ------------------------------------------------------------------
    # Phase 2 – group by size
    # ------------------------------------------------------------------
    _progress("Phase 2 · Grouping by size…", 0, total_files)
    size_map: dict[int, list[FileEntry]] = defaultdict(list)
    for fe in all_files:
        size_map[fe.size].append(fe)

    # Keep only groups with 2+ files
    size_groups = {sz: grp for sz, grp in size_map.items() if len(grp) >= 2}
    del size_map

    candidates_count = sum(len(g) for g in size_groups.values())
    _progress(
        f"Phase 2 · {len(size_groups):,} size groups ({candidates_count:,} files)",
        total_files, total_files,
    )

    if _cancelled():
        return []

    if not by_hash:
        for sz, grp in size_groups.items():
            results.append(DuplicateGroup(DuplicateMode.SIZE, f"{sz:,} bytes", grp))
        results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)
        return results

    # ------------------------------------------------------------------
    # Phase 3 – partial hash (fast pre-filter)
    # ------------------------------------------------------------------
    candidates = [fe for grp in size_groups.values() for fe in grp]
    del size_groups
    total_candidates = len(candidates)
    _progress(f"Phase 3 · Partial hashing {total_candidates:,} files…", 0, total_candidates)

    partial_map: dict[str, list[FileEntry]] = defaultdict(list)
    for i, fe in enumerate(candidates):
        if _cancelled():
            return []
        fe.partial_hash = _partial_hash(fe.path)
        if fe.partial_hash:
            key = f"{fe.size}:{fe.partial_hash}"
            partial_map[key].append(fe)
        if i % 2000 == 0:
            _progress(f"Phase 3 · Partial hash {i:,}/{total_candidates:,}", i, total_candidates)

    del candidates
    partial_groups = {k: g for k, g in partial_map.items() if len(g) >= 2}
    del partial_map

    _progress(
        f"Phase 3 · {len(partial_groups):,} groups need full hash",
        total_candidates, total_candidates,
    )

    if _cancelled():
        return []

    # ------------------------------------------------------------------
    # Phase 4 – full hash (parallel with ThreadPoolExecutor)
    # ------------------------------------------------------------------
    full_candidates = sorted(
        [fe for grp in partial_groups.values() for fe in grp],
        key=lambda fe: fe.size,
    )
    del partial_groups
    total_full = len(full_candidates)
    _progress(f"Phase 4 · Full hashing {total_full:,} files…", 0, total_full)

    # Parallel full hashing – I/O bound so threads work well
    workers = min(4, (os.cpu_count() or 2))
    done = 0

    def _hash_one(fe: FileEntry) -> tuple[FileEntry, str]:
        return fe, _full_hash(fe.path)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_hash_one, fe): fe for fe in full_candidates}
        for future in as_completed(futures):
            if _cancelled():
                pool.shutdown(wait=False, cancel_futures=True)
                return []
            try:
                fe, h = future.result()
                fe.full_hash = h
            except Exception:
                pass
            done += 1
            if done % 500 == 0 or done == total_full:
                _progress(f"Phase 4 · Full hash {done:,}/{total_full:,}", done, total_full)

    # Re-group by full hash
    full_map: dict[str, list[FileEntry]] = defaultdict(list)
    for fe in full_candidates:
        if fe.full_hash:
            key = f"{fe.size}:{fe.full_hash}"
            full_map[key].append(fe)

    for key, grp in full_map.items():
        if len(grp) >= 2:
            results.append(DuplicateGroup(DuplicateMode.HASH, key, grp))

    # Sort by total wasted space (largest groups first)
    results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)

    _progress(f"Done · {len(results):,} duplicate groups", total_full, total_full)
    return results
