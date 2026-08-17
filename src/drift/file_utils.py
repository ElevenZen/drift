"""Utility functions for file and directory operations using pathlib."""

import hashlib
import shutil
from pathlib import Path
from typing import Optional, List


def _is_relative_to(path: Path, other: Path) -> bool:
    """Robust fallback implementation of Path.is_relative_to for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def tree_relative_files(dir_path: Path) -> List[Path]:
    """Gets all files in dir_path recursively as Path objects relative to dir_path."""
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    relative_files = []
    for entry in dir_path.rglob("*"):
        if entry.is_file():
            relative_files.append(entry.relative_to(dir_path))
    return sorted(relative_files)


def compute_file_hash(file_path: Path) -> str:
    """Computes md5 hash of a file for efficient change tracking."""
    h = hashlib.md5()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_contents_differ(file1: Path, file2: Path) -> bool:
    """Returns True if the contents of file1 and file2 differ using stream comparison."""
    if file1.stat().st_size != file2.stat().st_size:
        return True
    with file1.open("rb") as f1, file2.open("rb") as f2:
        for chunk1, chunk2 in zip(iter(lambda: f1.read(65536), b""), iter(lambda: f2.read(65536), b"")):
            if chunk1 != chunk2:
                return True
    return False


def rmdir_parents(dir_path: Path, limit_dir: Path) -> None:
    """Recursively removes empty directories from dir_path up to limit_dir."""
    curr = dir_path.resolve()
    limit = limit_dir.resolve()
    while curr and curr != limit and _is_relative_to(curr, limit):
        if curr.exists() and curr.is_dir() and not any(curr.iterdir()):
            try:
                curr.rmdir()
            except OSError:
                break
            curr = curr.parent
        else:
            break


def backup_and_delete_file(
    file_path: Path,
    backup_path: Path,
    limit_dir: Optional[Path] = None
) -> None:
    """Backs up a file to backup_path, deletes it, and cleans up empty parent directories up to limit_dir."""
    if file_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, backup_path)
        file_path.unlink()
        if limit_dir:
            rmdir_parents(file_path.parent, limit_dir)
