import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Tuple
from .ignore import IgnoreHandler

logger = logging.getLogger(__name__)

@dataclass
class FolderDiff:
    added: List[Path] = field(default_factory=list)
    modified: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)
    matches: List[Path] = field(default_factory=list)

def compare_folders(
    src_dir: Path,
    dst_dir: Path,
    ignore_handler: Optional[IgnoreHandler] = None,
    resolve_symlinks: bool = True,
    translate_mode: Optional[str] = None,
    src_only: bool = False
) -> FolderDiff:
    """
    Recursively compares src_dir against dst_dir. 
    Returns a FolderDiff of relative paths for added, modified, and deleted files/symlinks/directories.
    The ignore_handler is applied on files from src_dir.

    Natively supports single-file comparison: if src_dir is a file or symlink,
    it is compared against dst_dir and returned as Path("") in the FolderDiff.

    translate_mode: 
      - "forward": src uses dot- prefixes, dst uses leading dots. (repo -> system)
      - "reverse": src uses leading dots, dst uses dot- prefixes. (system -> repo)
    Translation is applied to relative paths in src during comparison.
      
    src_only: If True, only paths that exist in src_dir are checked in dst_dir. 
              Loop 2 (dst items not in src) is skipped.
    """
    from .file_utils import (
        file_contents_differ, 
        translate_dot_prefixes, 
        translate_dot_prefixes_reverse,
        is_relative_to
    )

    diff = FolderDiff()

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

    def add_children_as_deleted(p_dst: Path, rel: Path):
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
                add_children_as_deleted(child, new_src_rel)

    def add_children_as_added(p_src: Path, rel: Path):
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
                        add_children_as_added(real_target, rel)
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
                add_children_as_added(child, rel / child.name)

    def _resolve_target(p: Path) -> Tuple[Path, bool]:
        """Resolves symlink target safely and returns (target_path, is_broken)."""
        if not p.is_symlink():
            return p, False
        try:
            target = p.resolve()
            return target, not target.exists()
        except Exception:
            return p, True

    def _compare_symlink_entry(p_src: Path, p_dst: Path, rel: Path) -> None:
        src_is_symlink = p_src.is_symlink()
        dst_is_symlink = p_dst.is_symlink()

        if resolve_symlinks:
            # 1a. Resolve both and check for broken symlinks
            src_target, src_broken = _resolve_target(p_src)
            dst_target, dst_broken = _resolve_target(p_dst)

            if src_broken or dst_broken:
                if src_is_symlink and dst_is_symlink:
                    try:
                        if os.readlink(p_src) == os.readlink(p_dst):
                            diff.matches.append(rel)
                        else:
                            diff.modified.append(rel)
                    except Exception:
                        diff.modified.append(rel)
                else:
                    diff.modified.append(rel)
                return

            # Both resolved targets exist, recursively compare resolved paths
            _compare_recursive(src_target, dst_target, rel)
        else:
            # 1b. Raw comparison (resolve_symlinks=False)
            if src_is_symlink and dst_is_symlink:
                try:
                    if os.readlink(p_src) == os.readlink(p_dst):
                        diff.matches.append(rel)
                    else:
                        diff.modified.append(rel)
                except Exception:
                    diff.modified.append(rel)
                return

            # Exactly one is a symlink: check for directory type mismatches
            if src_is_symlink:
                if p_dst.is_dir():
                    add_children_as_deleted(p_dst, rel)
                    diff.added.append(rel)
                else:
                    diff.modified.append(rel)
                return

            if dst_is_symlink:
                if p_src.is_dir():
                    diff.deleted.append(rel)
                    add_children_as_added(p_src, rel)
                else:
                    diff.modified.append(rel)
                return

    def _compare_physical_entry(p_src: Path, p_dst: Path, rel: Path) -> None:
        # Both are physical non-symlinks: compare types and contents
        if p_src.is_dir() and p_dst.is_file():
            diff.deleted.append(rel)
            add_children_as_added(p_src, rel)
            return

        if p_src.is_file() and p_dst.is_dir():
            add_children_as_deleted(p_dst, rel)
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
                d_name = new_dst_rel.name
                claimed_dst_names.add(d_name)
                _compare_recursive(p_src / s_name, p_dst / d_name, new_rel)
                
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
        else:
            diff.modified.append(rel)

    def _compare_recursive(p_src: Path, p_dst: Path, rel: Path):
        # rel is relative to src_dir
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = translate_dot_prefixes_reverse(rel)
            
        is_src_ignored = ignore_handler and ignore_handler.match_path(repo_rel)
        if is_src_ignored:
            return

        src_exists = p_src.exists() or p_src.is_symlink()
        dst_exists = p_dst.exists() or p_dst.is_symlink()

        if not src_exists:
            if dst_exists:
                add_children_as_deleted(p_dst, rel)
            return

        if not dst_exists:
            add_children_as_added(p_src, rel)
            return

        # 1. If at least one of them is a symlink:
        if p_src.is_symlink() or p_dst.is_symlink():
            _compare_symlink_entry(p_src, p_dst, rel)
            return

        # 2. Both are physical non-symlinks: compare types and contents
        _compare_physical_entry(p_src, p_dst, rel)

    _compare_recursive(src_dir, dst_dir, Path(""))

    diff.added = sorted(list(set(diff.added)))
    diff.modified = sorted(list(set(diff.modified)))
    diff.deleted = sorted(list(set(diff.deleted)))
    return diff


def list_folder_paths(
    src_dir: Path,
    base_rel: Optional[Path] = None,
    ignore_handler: Optional[IgnoreHandler] = None,
    resolve_symlinks: bool = True,
    translate_mode: Optional[str] = None
) -> List[Path]:
    """
    Recursively lists all relative paths within src_dir.
    If base_rel is provided, paths returned (and matched against ignore_handler)
    are prefixed by base_rel.
    """
    from .file_utils import translate_dot_prefixes_reverse

    results: List[Path] = []
    prefix = base_rel if base_rel is not None else Path("")

    def _walk(p_src: Path, rel: Path, visited: set):
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = translate_dot_prefixes_reverse(rel)

        if ignore_handler and ignore_handler.match_path(repo_rel):
            return

        if p_src.is_symlink() and not resolve_symlinks:
            results.append(rel)
            return

        if p_src.is_file() or not p_src.exists():
            results.append(rel)
            return

        if p_src.is_dir():
            try:
                real_key = p_src.resolve()
                if real_key in visited:
                    return
                visited.add(real_key)
                children = list(p_src.iterdir())
                if not children and rel != Path(""):
                    results.append(rel)
                else:
                    for child in children:
                        child_rel = rel / child.name if rel != Path("") else Path(child.name)
                        _walk(child, child_rel, visited.copy())
            except Exception:
                results.append(rel)

    _walk(src_dir, prefix, set())
    return sorted(list(set(results)))


def find_links_pointing_into(
    search_path: Path,
    target_dir: Path,
    follow_symlinks: bool = False
) -> List[Path]:
    """
    Recursively scans search_path for symlinks whose resolved targets lie within target_dir.
    """
    from .file_utils import is_relative_to

    results: List[Path] = []
    abs_target = target_dir.resolve()
    visited: set = set()

    def _scan(current: Path):
        if current.is_symlink():
            try:
                if is_relative_to(current.resolve(), abs_target):
                    results.append(current)
            except Exception:
                pass

            if not follow_symlinks:
                return

        if current.is_dir():
            try:
                real_key = current.resolve()
                if real_key in visited:
                    return
                visited.add(real_key)
                for child in current.iterdir():
                    _scan(child)
            except Exception:
                pass

    if search_path.exists() or search_path.is_symlink():
        _scan(search_path)

    return sorted(results)
