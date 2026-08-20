import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List
from .ignore import DriftIgnore

logger = logging.getLogger(__name__)

@dataclass
class FolderDiff:
    added: List[Path] = field(default_factory=list)
    modified: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)
    matches: List[Path] = field(default_factory=list)
    # internal_symlinks: paths in dst that are symlinks pointing into drift_root (for safety detection)
    internal_symlinks: List[Path] = field(default_factory=list)

def compare_folders(
    src_dir: Path,
    dst_dir: Path,
    ignore_handler: Optional[DriftIgnore] = None,
    resolve_symlinks: bool = True,
    translate_mode: Optional[str] = None,
    src_only: bool = False,
    drift_root: Optional[Path] = None
) -> FolderDiff:
    """
    Recursively compares src_dir against dst_dir. 
    Returns a FolderDiff of relative paths for added, modified, and deleted files/symlinks/directories.
    The ignore_handler is used on files from src_dir.

    Natively supports single-file comparison: if src_dir is a file or symlink,
    it is compared against dst_dir and returned as Path("") in the FolderDiff.

    translate_mode: 
      - "forward": src uses dot- prefixes, dst uses leading dots. (repo -> system)
      - "reverse": src uses leading dots, dst uses dot- prefixes. (system -> repo)
    Translation is applied to relative paths in src during comparison.
      
    src_only: If True, only paths that exist in src_dir are checked in dst_dir. 
              Loop 2 (dst items not in src) is skipped.
              
    drift_root: If provided, dst symlinks pointing into drift_root are tracked in internal_symlinks.
    """
    from .file_utils import (
        file_contents_differ, 
        translate_dot_prefixes, 
        translate_dot_prefixes_reverse,
        is_relative_to
    )

    diff = FolderDiff()
    abs_drift_root = drift_root.resolve() if drift_root else None

    def _translate(rel: Path) -> Path:
        if translate_mode == "forward":
            return translate_dot_prefixes(rel)
        if translate_mode == "reverse":
            return translate_dot_prefixes_reverse(rel)
        return rel

    def _untranslate(rel: Path) -> Path:
        if translate_mode == "forward":
            return translate_dot_prefixes_reverse(rel)
        if translate_mode == "reverse":
            return translate_dot_prefixes(rel)
        return rel

    def add_all_as_deleted(p_dst: Path, rel: Path):
        """rel is relative to src_dir."""
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
                # Compute dst_rel and then untranslate to get src_rel
                dst_rel = _translate(rel) / child.name
                new_src_rel = _untranslate(dst_rel)
                add_all_as_deleted(child, new_src_rel)

    def add_all_as_added(p_src: Path, rel: Path):
        """rel is relative to src_dir."""
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = translate_dot_prefixes_reverse(rel)

        if ignore_handler and ignore_handler.match_path(repo_rel):
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
        # rel is relative to src_dir
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = translate_dot_prefixes_reverse(rel)
            
        is_src_ignored = ignore_handler and ignore_handler.match_path(repo_rel)

        src_exists = p_src.exists() or p_src.is_symlink()
        dst_exists = p_dst.exists() or p_dst.is_symlink()

        # Safety Check: Internal Symlink detection
        if dst_exists and p_dst.is_symlink() and abs_drift_root:
            try:
                link_target = (p_dst.parent / os.readlink(p_dst)).resolve()
                if is_relative_to(link_target, abs_drift_root):
                    # It points into drift_root. 
                    # If it's a correct stow-link (points to p_src), it's fine.
                    if not (p_src.exists() and link_target == p_src.resolve()):
                        diff.internal_symlinks.append(rel)
            except Exception:
                pass

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

        if p_dst.is_symlink() and not resolve_symlinks:
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
            src_names = {c.name for c in p_src.iterdir()}
            dst_names = {c.name for c in p_dst.iterdir()} if not src_only else set()
            
            claimed_dst_names = set()
            
            # 1. Iterate over src children
            for s_name in sorted(list(src_names)):
                new_rel = rel / s_name
                new_dst_rel = _translate(new_rel)
                
                # Handle potential skipping folders (dot-)
                if translate_mode == "forward" and new_dst_rel == _translate(rel):
                    _compare_recursive(p_src / s_name, p_dst, new_rel)
                    continue
                
                d_name = new_dst_rel.name
                claimed_dst_names.add(d_name)
                _compare_recursive(p_src / s_name, dst_dir / new_dst_rel, new_rel)
                
            # 2. Iterate over unclaimed dst children (if not src_only)
            if not src_only:
                for d_name in sorted(list(dst_names - claimed_dst_names)):
                    new_dst_rel = _translate(rel) / d_name
                    new_src_rel = _untranslate(new_dst_rel)
                    _compare_recursive(src_dir / new_src_rel, p_dst / d_name, new_src_rel)

        elif p_src.is_file() and p_dst.is_file():
            if file_contents_differ(p_src, p_dst):
                diff.modified.append(rel)
            else:
                diff.matches.append(rel)
        elif p_src.is_symlink() and p_dst.is_symlink():
             # Raw comparison (resolve_symlinks=False)
             _compare_symlink_raw(p_src, p_dst, rel)
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
