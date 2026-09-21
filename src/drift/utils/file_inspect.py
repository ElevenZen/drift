"""Read-only file inspection utilities for content comparison, hashing, and classification.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

All functions in this module are read-only — they inspect filesystem state
without modifying it.

    is_binary_file(file_path) — Null-byte heuristic for binary detection.
    file_hash(file_path) — MD5 hash for change tracking.
    normalize_newlines(content, line_ending) — LF/CRLF byte-level conversion.
    contents_differ(file1, file2, convert_line_endings) — Byte/text content comparison.
    permissions_differ(file1, file2) — POSIX executable bit comparison.
    is_mode_only_change(file1, file2) — Content same + permissions different.
    tree_files(dir_path) — Recursive glob returning sorted relative paths.
    is_temp_file(file_name_or_path) — Editor/OS temp file pattern matching.
    find_symlink_ancestor(file_path, link_target_range) — Walks up to find symlink pointing into range.

===============================================================================
"""

import os
import sys
import hashlib
import fnmatch
import logging
from pathlib import Path
from typing import Optional, Union, List

from ..core.constants import LineEnding, TEMPORARY_FILE_PATTERNS
from .path_utils import is_relative_to

logger = logging.getLogger(__name__)


def is_binary_file(file_path: Path) -> bool:
    """Detects whether a file is binary by scanning for null bytes in the first 8KB."""
    try:
        with file_path.open("rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except Exception:
        return False


def file_hash(file_path: Path) -> str:
    """Computes md5 hash of a file for efficient change tracking."""
    h = hashlib.md5()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_newlines(content: bytes, line_ending: LineEnding = LineEnding.LF) -> bytes:
    """Translates newlines between LF, CRLF, or PRESERVE at the byte level.

    Preserves raw encoding and special characters without Unicode decoding errors.
    """
    if line_ending == LineEnding.CRLF:
        return content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    elif line_ending == LineEnding.LF:
        return content.replace(b"\r\n", b"\n")
    else:
        return content


def contents_differ(file1: Path, file2: Path, convert_line_endings: Optional[bool] = None) -> bool:
    """Returns True if the contents of file1 and file2 differ using stream comparison.

    If convert_line_endings is True (default on Windows), ignores LF vs CRLF line ending differences on text files.
    """
    if not file1.exists() and not file2.exists():
        return False
    if not file1.exists() or not file2.exists():
        return True
    if not file1.is_file() or not file2.is_file():
        raise ValueError("Both paths must be files for content comparison.")
    if file1.resolve() == file2.resolve():
        return False

    if convert_line_endings is None:
        convert_line_endings = (sys.platform == "win32")

    if convert_line_endings:
        # If either file is binary, compare byte sizes and stream chunks
        if is_binary_file(file1) or is_binary_file(file2):
            if file1.stat().st_size != file2.stat().st_size:
                return True
            with file1.open("rb") as f1, file2.open("rb") as f2:
                for chunk1, chunk2 in zip(iter(lambda: f1.read(65536), b""), iter(lambda: f2.read(65536), b"")):
                    if chunk1 != chunk2:
                        return True
            return False
        else:
            # Text files: compare LF-normalized bytes
            b1 = normalize_newlines(file1.read_bytes(), line_ending=LineEnding.LF)
            b2 = normalize_newlines(file2.read_bytes(), line_ending=LineEnding.LF)
            return b1 != b2
    else:
        if file1.stat().st_size != file2.stat().st_size:
            return True
        with file1.open("rb") as f1, file2.open("rb") as f2:
            for chunk1, chunk2 in zip(iter(lambda: f1.read(65536), b""), iter(lambda: f2.read(65536), b"")):
                if chunk1 != chunk2:
                    return True
        return False


def permissions_differ(file1: Path, file2: Path) -> bool:
    """Returns True if the executable permissions of file1 and file2 differ on POSIX.

    Always returns False on Windows (win32) or if either file does not exist.
    """
    if sys.platform == "win32":
        return False
    if not file1.exists() or not file2.exists():
        return False
    try:
        mode1 = file1.stat().st_mode
        mode2 = file2.stat().st_mode
        return bool(mode1 & 0o111) != bool(mode2 & 0o111)
    except Exception:
        return False


def is_mode_only_change(file1: Path, file2: Path, convert_line_endings: Optional[bool] = None) -> bool:
    """Returns True if the byte contents of file1 and file2 match, but their executable permissions differ."""
    return not contents_differ(file1, file2, convert_line_endings=convert_line_endings) and permissions_differ(file1, file2)


def tree_files(dir_path: Path) -> List[Path]:
    """Gets all files in dir_path recursively as Path objects relative to dir_path.

    tree_files() is used only when there is no symlink and no comparison.
    Otherwise, compare_folders() is used to detect untracked files and resolve links.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    return sorted(
        entry.relative_to(dir_path)
        for entry in dir_path.rglob("*")
        if entry.is_file()
    )


def is_temp_file(file_name_or_path: Union[str, Path]) -> bool:
    """Checks if a file is an editor temporary/swap/backup file or OS metadata."""
    name = Path(file_name_or_path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in TEMPORARY_FILE_PATTERNS)


def find_symlink_ancestor(file_path: Path, link_target_range: Path) -> Optional[Path]:
    """Finds the nearest ancestor (or self) that is a symlink pointing into link_target_range.

    Traverses up the directory tree from file_path. Returns the symlink Path itself
    (not its resolved target), or None if no such ancestor is found.
    """
    cursor = file_path
    home_dir = Path.home()
    abs_drift_root = link_target_range.resolve()
    while cursor and cursor != Path("/") and cursor != home_dir:
        if cursor.is_symlink():
            try:
                link_str = cursor.readlink()
                abs_link_target = (cursor.parent / link_str).resolve()

                if is_relative_to(abs_link_target, abs_drift_root):
                    return cursor
            except Exception:
                # because it's a parent dir, very unlikely to fail the resolve process,
                # but if it does, we ignore and continue up the tree
                pass
        # iterate up the directory tree
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return None
