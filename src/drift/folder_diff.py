import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, List

if TYPE_CHECKING:
    from .ignore import DriftIgnore

logger = logging.getLogger(__name__)

@dataclass
class FolderDiff:
    added: List[Path] = field(default_factory=list)
    modified: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)

def compare_folders(
    src_dir: Path,
    dst_dir: Path,
    ignore_handler: Optional['DriftIgnore'] = None,
    resolve_symlinks: bool = True
) -> FolderDiff:
    """
    Recursively compares src_dir against dst_dir. 
    Returns a FolderDiff of relative paths for added, modified, and deleted files/symlinks/directories.
    The ignore_handler is used on files from src_dir.

    Natively supports single-file comparison: if src_dir is a file or symlink,
    it is compared against dst_dir and returned as Path("") in the FolderDiff.
    """
    from .file_utils import file_contents_differ

    diff = FolderDiff()

    def add_all_as_deleted(p_dst: Path, rel: Path):
        if p_dst.is_symlink() or p_dst.is_file():
            diff.deleted.append(rel)
        elif p_dst.is_dir():
            if ignore_handler is None:
                try:
                    if not any(p_dst.iterdir()):
                        diff.deleted.append(rel)
                except Exception:
                    pass
            for child in p_dst.iterdir():
                add_all_as_deleted(child, rel / child.name)

    def add_all_as_added(p_src: Path, rel: Path):
        if ignore_handler and ignore_handler.match_path(rel):
            return

        if p_src.is_symlink():
            if not resolve_symlinks:
                diff.added.append(rel)
            else:
                try:
                    real_target = p_src.resolve()
                    if not real_target.exists():
                        diff.added.append(rel)
                    else:
                        add_all_as_added(real_target, rel)
                except Exception:
                    diff.added.append(rel)
        elif p_src.is_file():
            diff.added.append(rel)
        elif p_src.is_dir():
            if ignore_handler is None:
                try:
                    if not any(p_src.iterdir()):
                        diff.added.append(rel)
                except Exception:
                    pass
            for child in p_src.iterdir():
                add_all_as_added(child, rel / child.name)

    def _compare_recursive(p_src: Path, p_dst: Path, rel: Path):
        is_src_ignored = ignore_handler and ignore_handler.match_path(rel)

        src_exists = p_src.exists() or p_src.is_symlink()
        dst_exists = p_dst.exists() or p_dst.is_symlink()

        if is_src_ignored:
            if dst_exists:
                add_all_as_deleted(p_dst, rel)
            return

        if not src_exists:
            if dst_exists:
                add_all_as_deleted(p_dst, rel)
            return

        if not dst_exists:
            add_all_as_added(p_src, rel)
            return

        # Both exist
        if p_src.is_symlink() and resolve_symlinks:
            try:
                real_target = p_src.resolve()
                if not real_target.exists():
                    _compare_broken_symlink(p_src, p_dst, rel)
                else:
                    _compare_recursive(real_target, p_dst, rel)
            except Exception:
                _compare_broken_symlink(p_src, p_dst, rel)
            return

        if p_src.is_symlink() and not resolve_symlinks:
            _compare_symlink_raw(p_src, p_dst, rel)
            return

        if p_dst.is_symlink():
            diff.modified.append(rel)
            return

        # Type mismatch check
        if p_src.is_dir() and p_dst.is_file():
            diff.deleted.append(rel)
            add_all_as_added(p_src, rel)
            return
        elif p_src.is_file() and p_dst.is_dir():
            add_all_as_deleted(p_dst, rel)
            diff.added.append(rel)
            return

        if p_src.is_dir() and p_dst.is_dir():
            names = sorted(list({c.name for c in p_src.iterdir()} | {c.name for c in p_dst.iterdir()}))
            for name in names:
                _compare_recursive(p_src / name, p_dst / name, rel / name)
        elif p_src.is_file() and p_dst.is_file():
            if file_contents_differ(p_src, p_dst):
                diff.modified.append(rel)
        else:
            diff.modified.append(rel)

    def _compare_broken_symlink(p_src: Path, p_dst: Path, rel: Path):
        try:
            link_val = os.readlink(p_src)
            if not p_dst.is_symlink() or os.readlink(p_dst) != link_val:
                diff.modified.append(rel)
        except Exception:
            diff.modified.append(rel)

    def _compare_symlink_raw(p_src: Path, p_dst: Path, rel: Path):
        try:
            link_val = os.readlink(p_src)
            if not p_dst.is_symlink() or os.readlink(p_dst) != link_val:
                diff.modified.append(rel)
        except Exception:
            diff.modified.append(rel)

    _compare_recursive(src_dir, dst_dir, Path(""))

    diff.added = sorted(list(set(diff.added)))
    diff.modified = sorted(list(set(diff.modified)))
    diff.deleted = sorted(list(set(diff.deleted)))
    return diff
