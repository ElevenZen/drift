"""Utility functions for file and directory operations."""

import hashlib
import os


def tree_relative_files(dir_path: str) -> list:
    """Gets all files in dir_path recursively as paths relative to dir_path."""
    if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
        return []
    relative_files = []
    for root, _, files in os.walk(dir_path):
        for file in files:
            abs_path = os.path.join(root, file)
            relative_files.append(os.path.relpath(abs_path, dir_path))
    return sorted(relative_files)


def compute_file_hash(file_path: str) -> str:
    """Computes md5 hash of a file for efficient change tracking."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_contents_differ(file1: str, file2: str) -> bool:
    """Returns True if the contents of file1 and file2 differ using hashes."""
    if os.path.getsize(file1) != os.path.getsize(file2):
        return True
    return compute_file_hash(file1) != compute_file_hash(file2)


def rmdir_parents(dir_path: str, limit_dir: str) -> None:
    """Recursively removes empty directories from dir_path up to limit_dir."""
    curr = os.path.abspath(dir_path)
    limit = os.path.abspath(limit_dir)
    while curr and curr != limit and curr.startswith(limit):
        if os.path.exists(curr) and os.path.isdir(curr) and not os.listdir(curr):
            try:
                os.rmdir(curr)
            except OSError:
                break
            curr = os.path.dirname(curr)
        else:
            break
