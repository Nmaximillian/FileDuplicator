"""
Duplicate file scanner engine.

Uses a progressive strategy to minimize I/O:
  1. Group files by chosen criteria (name, size)
  2. For size-matched groups, compute a PARTIAL hash (first + last 64 KB)
  3. Only compute FULL hash for files whose partial hashes collide

This avoids reading entire multi-GB files unless truly necessary.
"""

from __future__ import annotations

import os
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable

import xxhash

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PARTIAL_CHUNK = 64 * 1024        # 64 KB for partial hash
FULL_CHUNK    = 4 * 1024 * 1024  # 4 MB read buffer for full hash (larger = fewer syscalls)

# Directories to always skip (case-insensitive on Windows)
SKIP_DIRS: set[str] = {
    "$recycle.bin", "system volume information", "$windows.~bt",
    "$windows.~ws", "windows", "windows.old",
    "recovery", "config.msi", "msocache",
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache",
    ".venv", "venv", "env",
}

# File extensions to always skip (temp / OS junk)
SKIP_EXTENSIONS: set[str] = {
    ".tmp", ".temp", ".log", ".bak",
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
    partial_hash: str = ""
    full_hash: str = ""


@dataclass
class DuplicateGroup:
    """A group of files considered duplicates."""
    mode: DuplicateMode
    key: str                           # the shared value (name / size / hash)
    files: list[FileEntry] = field(default_factory=list)


# Callback type:  (phase_label, current, total)
ProgressCallback = Callable[[str, int, int], None]
# Callback to check if scan was cancelled
CancelCheck = Callable[[], bool]


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


def _walk_fast(root: str, min_size: int, skip_dirs: set[str],
               cancelled: CancelCheck, progress: ProgressCallback) -> list[FileEntry]:
    """Walk directory tree using os.scandir for speed, with directory filtering."""
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
                                ))
                                count += 1
                                if count % 10000 == 0:
                                    progress("Enumerating files…", count, 0)
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            continue

    return all_files


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

    Parameters
    ----------
    root : str
        Top-level directory to scan.
    by_name : bool
        Group duplicates by file name.
    by_size : bool
        Group duplicates by file size (prerequisite for hash).
    by_hash : bool
        Refine size groups with content hash.
    recursive : bool
        Walk subdirectories.
    min_size : int
        Ignore files smaller than this (bytes).  0 = include empty files.
    progress : callable, optional
        ``(phase, current, total)`` – called periodically.
    cancelled : callable, optional
        Return ``True`` to abort early.
    extra_skip_dirs : set[str], optional
        Additional directory names to skip (lowercase).

    Returns
    -------
    list[DuplicateGroup]
    """
    _progress = progress or (lambda *_: None)
    _cancelled = cancelled or (lambda: False)
    skip = SKIP_DIRS | (extra_skip_dirs or set())

    # ------------------------------------------------------------------
    # Phase 1 – enumerate files (using fast os.scandir walker)
    # ------------------------------------------------------------------
    _progress("Enumerating files…", 0, 0)

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
                                    path=entry.path, name=entry.name, size=st.st_size))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

    total_files = len(all_files)
    _progress("Enumerating files…", total_files, total_files)

    if _cancelled():
        return []

    results: list[DuplicateGroup] = []

    # ------------------------------------------------------------------
    # Phase 2 – group by name
    # ------------------------------------------------------------------
    if by_name:
        _progress("Grouping by name…", 0, total_files)
        name_map: dict[str, list[FileEntry]] = defaultdict(list)
        for i, fe in enumerate(all_files):
            name_map[fe.name.lower()].append(fe)
            if i % 50000 == 0:
                _progress("Grouping by name…", i, total_files)
                if _cancelled():
                    return []
        for key, group in name_map.items():
            if len(group) >= 2:
                results.append(DuplicateGroup(DuplicateMode.NAME, key, group))
        _progress("Grouping by name…", total_files, total_files)

    if not by_size and not by_hash:
        return results

    # ------------------------------------------------------------------
    # Phase 3 – group by size
    # ------------------------------------------------------------------
    _progress("Grouping by size…", 0, total_files)
    size_map: dict[int, list[FileEntry]] = defaultdict(list)
    for fe in all_files:
        size_map[fe.size].append(fe)

    # Keep only groups with 2+ files
    size_groups = {sz: grp for sz, grp in size_map.items() if len(grp) >= 2}
    del size_map  # free memory
    _progress("Grouping by size…", total_files, total_files)

    if not by_hash:
        for sz, grp in size_groups.items():
            results.append(
                DuplicateGroup(DuplicateMode.SIZE, f"{sz:,} bytes", grp)
            )
        return results

    # ------------------------------------------------------------------
    # Phase 4 – partial hash (fast pre-filter)
    # ------------------------------------------------------------------
    candidates = [fe for grp in size_groups.values() for fe in grp]
    del size_groups  # free memory
    total_candidates = len(candidates)
    _progress("Partial hashing…", 0, total_candidates)

    # Sort by size descending – hash small files quickly first is nice,
    # but grouping by size improves OS I/O scheduling
    partial_map: dict[str, list[FileEntry]] = defaultdict(list)
    for i, fe in enumerate(candidates):
        if _cancelled():
            return []
        fe.partial_hash = _partial_hash(fe.path)
        if fe.partial_hash:
            key = f"{fe.size}:{fe.partial_hash}"
            partial_map[key].append(fe)
        if i % 2000 == 0:
            _progress("Partial hashing…", i, total_candidates)

    del candidates  # free memory
    partial_groups = {k: g for k, g in partial_map.items() if len(g) >= 2}
    del partial_map
    _progress("Partial hashing…", total_candidates, total_candidates)

    # ------------------------------------------------------------------
    # Phase 5 – full hash (only for partial-hash collisions)
    # Sort by size ascending so small files resolve quickly and progress
    # moves fast at the start.
    # ------------------------------------------------------------------
    full_candidates = sorted(
        [fe for grp in partial_groups.values() for fe in grp],
        key=lambda fe: fe.size,
    )
    del partial_groups
    total_full = len(full_candidates)
    _progress("Full hashing…", 0, total_full)

    full_map: dict[str, list[FileEntry]] = defaultdict(list)
    for i, fe in enumerate(full_candidates):
        if _cancelled():
            return []
        fe.full_hash = _full_hash(fe.path)
        if fe.full_hash:
            key = f"{fe.size}:{fe.full_hash}"
            full_map[key].append(fe)
        if i % 200 == 0:
            _progress("Full hashing…", i, total_full)

    for key, grp in full_map.items():
        if len(grp) >= 2:
            results.append(DuplicateGroup(DuplicateMode.HASH, key, grp))

    # Sort result groups by total wasted space (largest first)
    results.sort(key=lambda g: g.files[0].size * (len(g.files) - 1), reverse=True)

    _progress("Done", total_full, total_full)
    return results
