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
import hashlib
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
FULL_CHUNK    = 1 * 1024 * 1024  # 1 MB read buffer for full hash


class DuplicateMode(Enum):
    NAME = auto()
    SIZE = auto()
    HASH = auto()


@dataclass
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

    Returns
    -------
    list[DuplicateGroup]
    """
    _progress = progress or (lambda *_: None)
    _cancelled = cancelled or (lambda: False)

    # ------------------------------------------------------------------
    # Phase 1 – enumerate files
    # ------------------------------------------------------------------
    _progress("Enumerating files…", 0, 0)
    all_files: list[FileEntry] = []

    if recursive:
        for dirpath, _dirs, filenames in os.walk(root):
            if _cancelled():
                return []
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    if st.st_size < min_size:
                        continue
                    all_files.append(FileEntry(path=fp, name=fn, size=st.st_size))
                except (OSError, PermissionError):
                    continue
    else:
        for fn in os.listdir(root):
            if _cancelled():
                return []
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size < min_size:
                    continue
                all_files.append(FileEntry(path=fp, name=fn, size=st.st_size))
            except (OSError, PermissionError):
                continue

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
            if i % 5000 == 0:
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
    for i, fe in enumerate(all_files):
        size_map[fe.size].append(fe)
        if i % 5000 == 0:
            _progress("Grouping by size…", i, total_files)
            if _cancelled():
                return []

    # Keep only groups with 2+ files
    size_groups = {sz: grp for sz, grp in size_map.items() if len(grp) >= 2}
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
    total_candidates = len(candidates)
    _progress("Partial hashing…", 0, total_candidates)

    partial_map: dict[str, list[FileEntry]] = defaultdict(list)
    for i, fe in enumerate(candidates):
        if _cancelled():
            return []
        fe.partial_hash = _partial_hash(fe.path)
        if fe.partial_hash:
            key = f"{fe.size}:{fe.partial_hash}"
            partial_map[key].append(fe)
        if i % 200 == 0:
            _progress("Partial hashing…", i, total_candidates)

    partial_groups = {k: g for k, g in partial_map.items() if len(g) >= 2}
    _progress("Partial hashing…", total_candidates, total_candidates)

    # ------------------------------------------------------------------
    # Phase 5 – full hash (only for partial-hash collisions)
    # ------------------------------------------------------------------
    full_candidates = [fe for grp in partial_groups.values() for fe in grp]
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
        if i % 50 == 0:
            _progress("Full hashing…", i, total_full)

    for key, grp in full_map.items():
        if len(grp) >= 2:
            results.append(DuplicateGroup(DuplicateMode.HASH, key, grp))

    _progress("Done", total_full, total_full)
    return results
