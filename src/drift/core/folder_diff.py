import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Tuple, Union, Set
from .constants import DirMode
from .ignore import IgnoreHandler
from ..utils.path_utils import decode_dot_prefix, is_relative_to

logger = logging.getLogger(__name__)

@dataclass
class FolderDiff:
    added: List[Path] = field(default_factory=list)
    modified: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)
    matches: List[Path] = field(default_factory=list)
    permissions_differ: List[Path] = field(default_factory=list)

    def is_mode_only_change(self, rel_path: Path) -> bool:
        """Checks if a relative path in modified list is a mode-only change (content matches, permissions differ)."""
        return rel_path in self.permissions_differ


def compare_folders(
    src_dir: Path,
    dst_dir: Path,
    ignore_handler: Optional[IgnoreHandler] = None,
    resolve_symlinks: bool = True,
    translate_mode: Optional[str] = None,
    src_only: bool = False,
    dir_mode: Union[str, DirMode] = DirMode.ALL_DIRS,
) -> FolderDiff:
    """
    Recursively compares src_dir against dst_dir. 
    Returns a FolderDiff of relative paths for added, modified, and deleted files/symlinks/directories.
    The ignore_handler is applied on files from src_dir, and is also applied to dst_dir when recording deleted files.

    Natively supports single-file comparison: if src_dir is a file or symlink,
    it is compared against dst_dir and returned as Path("") in the FolderDiff.

    translate_mode: 
      - "forward": src uses dot- prefixes, dst uses leading dots. (repo -> system)
      - "reverse": src uses leading dots, dst uses dot- prefixes. (system -> repo)
    Translation is applied to relative paths in src during comparison.
      
    src_only: If True, only paths that exist in src_dir are checked in dst_dir. 
              Loop 2 (dst items not in src) is skipped.

    dir_mode controls directory inclusion in diff (DirMode enum or string):
      - Note: dir_mode only affects the 'added' and 'deleted' lists. The 'modified' and 'matches'
        lists only contain leaf files and symlinks, never directory nodes.
      - "all-dirs": (default) Files, empty directories, and intermediate directory nodes are returned in added/deleted.
      - "only-empty-dir": Files and empty directory placeholders are returned in added/deleted.
      - "no-dir": Only files and symlinks are returned in added/deleted (no directories).

    Application Order & Multi-Level Type Changes:
      - In folder delivery (both forward installation and reverse sync), additions and updates
        are processed before orphan deletions to prevent race conditions and data loss from symlink aliasing.
        Multi-level type changes (e.g. file replacing a directory tree or vice-versa) are handled
        structurally via ancestor directory inspection and leaf node inspection.
      - Ensure destination directories inside internal state stores do not contain circular symlinks
        pointing into source directories.
    """
    from ..utils.file_inspect import contents_differ, permissions_differ
    from ..utils.path_utils import encode_dot_prefix, decode_dot_prefix, is_relative_to

    mode = DirMode.from_str(dir_mode)
    diff = FolderDiff()

    def _translate(rel: Path) -> Path:
        if translate_mode == "forward":
            return encode_dot_prefix(rel)
        if translate_mode == "reverse":
            return decode_dot_prefix(rel)
        return rel

    def _untranslate(rel: Path) -> Path:
        if translate_mode == "forward":
            return decode_dot_prefix(rel)
        if translate_mode == "reverse":
            return encode_dot_prefix(rel)
        return rel

    def visited_test_add(visited: set, rel: Path) -> Optional[Path]:
        """Returns None if visited, else adds resolved path to visited and returns key."""
        try:
            real_key = rel.resolve()
        except Exception:
            real_key = rel
        if real_key in visited:
            return None
        visited.add(real_key)
        return real_key

    def add_children_as_deleted(p_dst: Path, rel: Path, visited: set):
        """rel is relative to src_dir."""
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = decode_dot_prefix(rel)

        if ignore_handler and ignore_handler.match_path(repo_rel, is_dir=p_dst.is_dir()):
            return

        if p_dst.is_symlink():
            if not resolve_symlinks:
                diff.deleted.append(rel)
            else:
                # resolve and recur
                try:
                    real_target = p_dst.resolve()
                    if not real_target.exists():
                        diff.deleted.append(rel)
                    else:
                        add_children_as_deleted(real_target, rel, visited)
                except Exception:
                    diff.deleted.append(rel)
        elif p_dst.is_file():
            diff.deleted.append(rel)
        elif p_dst.is_dir():
            real_key = visited_test_add(visited, p_dst)
            if real_key is None:
                return
            try:
                is_empty = not any(p_dst.iterdir())
                if mode == DirMode.ALL_DIRS and (rel != Path("") or is_empty):
                    diff.deleted.append(rel)
                elif mode == DirMode.ONLY_EMPTY_DIR and is_empty:
                    diff.deleted.append(rel)
                for child in p_dst.iterdir():
                    # Compute dst_rel and then untranslate to get src_rel
                    dst_rel = _translate(rel) / child.name
                    new_src_rel = _untranslate(dst_rel)
                    add_children_as_deleted(child, new_src_rel, visited)
            finally:
                visited.remove(real_key)

    def add_children_as_added(p_src: Path, rel: Path, visited: set):
        """rel is relative to src_dir."""
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = decode_dot_prefix(rel)

        if ignore_handler and ignore_handler.match_path(repo_rel, is_dir=p_src.is_dir()):
            return

        if p_src.is_symlink():
            if not resolve_symlinks:
                diff.added.append(rel)
            else:
                # resolve and recur
                try:
                    real_target = p_src.resolve()
                    if not real_target.exists():
                        diff.added.append(rel)
                    else:
                        add_children_as_added(real_target, rel, visited)
                except Exception:
                    diff.added.append(rel)
        elif p_src.is_file():
            diff.added.append(rel)
        elif p_src.is_dir():
            real_key = visited_test_add(visited, p_src)
            if real_key is None:
                return
            try:
                is_empty = not any(p_src.iterdir())
                if mode == DirMode.ALL_DIRS and (rel != Path("") or is_empty):
                    diff.added.append(rel)
                elif mode == DirMode.ONLY_EMPTY_DIR and is_empty:
                    diff.added.append(rel)
                for child in p_src.iterdir():
                    add_children_as_added(child, rel / child.name, visited)
            finally:
                visited.remove(real_key)

    def _resolve_target(p: Path) -> Tuple[Path, bool]:
        """Resolves symlink target safely and returns (target_path, is_broken)."""
        if not p.is_symlink():
            return p, False
        try:
            target = p.resolve()
            return target, not target.exists()
        except Exception:
            return p, True

    def _compare_symlink_entry(p_src: Path, p_dst: Path, rel: Path, visited: set) -> None:
        src_is_symlink = p_src.is_symlink()
        dst_is_symlink = p_dst.is_symlink()

        if resolve_symlinks:
            # 1a. Resolve both and check for broken symlinks
            src_target, src_broken = _resolve_target(p_src)
            dst_target, dst_broken = _resolve_target(p_dst)

            if not src_broken and not dst_broken:
                # Both resolved targets exist, recursively compare resolved paths
                _compare_recursive(src_target, dst_target, rel, visited)
                return

            # at least one is broken symlink, compare symlink targets directly
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
                    add_children_as_deleted(p_dst, rel, visited)
                    diff.added.append(rel)
                else:
                    diff.modified.append(rel)
                return

            if dst_is_symlink:
                if p_src.is_dir():
                    diff.deleted.append(rel)
                    add_children_as_added(p_src, rel, visited)
                else:
                    diff.modified.append(rel)
                return

    def _compare_physical_entry(p_src: Path, p_dst: Path, rel: Path, visited: set) -> None:
        # Both are physical non-symlinks: compare types and contents
        if p_src.is_dir() and p_dst.is_file():
            diff.deleted.append(rel)
            add_children_as_added(p_src, rel, visited)
            return

        if p_src.is_file() and p_dst.is_dir():
            add_children_as_deleted(p_dst, rel, visited)
            diff.added.append(rel)
            return

        if p_src.is_dir() and p_dst.is_dir():
            try:
                pair_key = (p_src.resolve(), p_dst.resolve())
            except Exception:
                pair_key = (p_src, p_dst)

            if pair_key in visited:
                return

            visited.add(pair_key)
            try:
                src_names = {c.name for c in p_src.iterdir()}
                dst_names = {c.name for c in p_dst.iterdir()} if not src_only else set()
                
                claimed_dst_names = set()
                
                # 1. Iterate over src children
                for s_name in sorted(list(src_names)):
                    new_rel = rel / s_name
                    new_dst_rel = _translate(new_rel)
                    d_name = new_dst_rel.name
                    claimed_dst_names.add(d_name)
                    _compare_recursive(p_src / s_name, p_dst / d_name,
                                       new_rel, visited)
                    
                # 2. Iterate over unclaimed dst children (if not src_only)
                if not src_only:
                    for d_name in sorted(list(dst_names - claimed_dst_names)):
                        new_dst_rel = _translate(rel) / d_name
                        new_src_rel = _untranslate(new_dst_rel)
                        _compare_recursive(src_dir / new_src_rel, p_dst / d_name,
                                           new_src_rel, visited)
            finally:
                visited.remove(pair_key)

        elif p_src.is_file() and p_dst.is_file():
            if contents_differ(p_src, p_dst):
                diff.modified.append(rel)
            elif permissions_differ(p_src, p_dst):
                diff.modified.append(rel)
                diff.permissions_differ.append(rel)
            else:
                diff.matches.append(rel)
        else:
            diff.modified.append(rel)

    def _compare_recursive(p_src: Path, p_dst: Path, rel: Path, visited: set) -> None:
        # rel is relative to src_dir
        # visited is a set of resolved paths to avoid infinite loops with symlinks
        # two kinds of visited keys: (src_resolved, dst_resolved) for directories, and src_resolved for files/symlinks
        repo_rel = rel
        if translate_mode == "reverse":
            repo_rel = decode_dot_prefix(rel)

        src_exists = p_src.exists() or p_src.is_symlink()
        dst_exists = p_dst.exists() or p_dst.is_symlink()

        is_dir = p_src.is_dir() if src_exists else p_dst.is_dir()
        is_src_ignored = ignore_handler and ignore_handler.match_path(repo_rel, is_dir=is_dir)
        if is_src_ignored:
            return

        if not src_exists and not dst_exists:
            return

        if not src_exists:
            add_children_as_deleted(p_dst, rel, visited)
            return

        if not dst_exists:
            add_children_as_added(p_src, rel, visited)
            return

        # 1. If at least one of them is a symlink:
        if p_src.is_symlink() or p_dst.is_symlink():
            _compare_symlink_entry(p_src, p_dst, rel, visited)
            return

        # 2. Both are physical non-symlinks: compare types and contents
        _compare_physical_entry(p_src, p_dst, rel, visited)

    _compare_recursive(src_dir, dst_dir, Path(""), set())

    diff.added = sorted(list(set(diff.added)))
    diff.modified = sorted(list(set(diff.modified)))
    diff.deleted = sorted(list(set(diff.deleted)))
    diff.permissions_differ = sorted(list(set(diff.permissions_differ)))
    return diff


@dataclass(frozen=True)
class FolderListingContext:
    """Immutable traversal configuration and filters for list_folder_paths."""
    root_rel_prefix: Path = Path("")
    ignore_handler: Optional[IgnoreHandler] = None
    resolve_symlinks: bool = True
    translate_mode: Optional[str] = None
    dir_mode: DirMode = DirMode.ALL_DIRS
    dest_dir: Optional[Path] = None


def check_circular_dest_symlink(
    symlink_path: Path,
    dest_dir: Optional[Path],
    repo_rel: Path,
) -> bool:
    """Read-only inspection returning True if symlink_path is an anomalous circular symlink into dest_dir.

    When dest_dir is provided (e.g. install_pkg_dir during reverse-sync) and symlink_path points into dest_dir:
    - Canonical symlinks: if resolved symlink matches expected counterpart (dest_dir / repo_rel),
      it represents a standard Drift deployment link and returns False.
    - Mismatched symlinks: if resolved symlink points into dest_dir but differs from counterpart,
      it represents an anomalous circular reference (e.g. host subfolder linking back to repo).
      A prominent warning is logged and returns True so traversal skips it, preventing infinite
      duplicate nesting across repeated sync operations.
    """
    if dest_dir is None or not symlink_path.is_symlink():
        return False

    try:
        resolved_symlink = symlink_path.resolve()
    except Exception:
        resolved_symlink = symlink_path

    try:
        if not is_relative_to(resolved_symlink, dest_dir):
            return False

        expected_counterpart = dest_dir / repo_rel
        try:
            expected_counterpart_resolved = expected_counterpart.resolve()
        except Exception:
            expected_counterpart_resolved = expected_counterpart

        if resolved_symlink != expected_counterpart_resolved:
            logger.warning(
                f"⚠️  [REVERSE-SYNC] Circular self-referential directory symlink detected at '{symlink_path}' "
                f"(resolves to '{resolved_symlink}' inside destination repository '{dest_dir}', "
                f"differing from expected counterpart '{expected_counterpart}'). "
                f"Skipping recursive traversal to prevent duplicate nesting."
            )
            return True
    except Exception:
        pass

    return False


def _traverse_directory_children(
    dir_path: Path,
    current_rel: Path,
    resolved_dir: Path,
    visited_dirs: Set[Path],
    collected_paths: List[Path],
    context: FolderListingContext,
) -> None:
    """Safely traverses child entries of a directory, guarding against recursion cycles and collecting empty dirs."""
    if resolved_dir in visited_dirs:
        return

    visited_dirs.add(resolved_dir)
    try:
        child_entries = list(dir_path.iterdir())
        if not child_entries and current_rel != context.root_rel_prefix:
            if context.dir_mode == DirMode.ONLY_EMPTY_DIR:
                collected_paths.append(current_rel)
        else:
            for child_path in child_entries:
                child_rel = (
                    current_rel / child_path.name
                    if current_rel != Path("")
                    else Path(child_path.name)
                )
                _walk_folder_entry(child_path, child_rel, visited_dirs, collected_paths, context)
    finally:
        visited_dirs.remove(resolved_dir)


def _walk_folder_entry(
    current_path: Path,
    current_rel: Path,
    visited_dirs: Set[Path],
    collected_paths: List[Path],
    context: FolderListingContext,
) -> None:
    """Inspects a single filesystem entry, classifying it as ignored, leaf, or directory node."""
    ignore_check_rel = (
        decode_dot_prefix(current_rel)
        if context.translate_mode == "reverse"
        else current_rel
    )

    if context.ignore_handler and context.ignore_handler.match_path(
        ignore_check_rel, is_dir=current_path.is_dir()
    ):
        return

    if current_path.is_symlink() and not context.resolve_symlinks:
        collected_paths.append(current_rel)
        return

    if current_path.is_file() or not current_path.exists():
        collected_paths.append(current_rel)
        return

    if current_path.is_dir():
        if check_circular_dest_symlink(
            symlink_path=current_path,
            dest_dir=context.dest_dir,
            repo_rel=ignore_check_rel,
        ):
            return

        try:
            resolved_dir = current_path.resolve()
        except Exception:
            resolved_dir = current_path

        if context.dir_mode == DirMode.ALL_DIRS and current_rel != context.root_rel_prefix:
            collected_paths.append(current_rel)

        _traverse_directory_children(
            dir_path=current_path,
            current_rel=current_rel,
            resolved_dir=resolved_dir,
            visited_dirs=visited_dirs,
            collected_paths=collected_paths,
            context=context,
        )


def list_folder_paths(
    src_dir: Path,
    base_rel: Optional[Path] = None,
    ignore_handler: Optional[IgnoreHandler] = None,
    resolve_symlinks: bool = True,
    translate_mode: Optional[str] = None,
    dir_mode: Union[str, DirMode] = DirMode.ALL_DIRS,
    dest_dir: Optional[Path] = None,
) -> List[Path]:
    """
    Recursively lists all relative paths within src_dir.
    If base_rel is provided, paths returned (and matched against ignore_handler)
    are prefixed by base_rel.

    dir_mode controls directory inclusion (DirMode enum or string):
      - "all-dirs": (default) Files, empty directories, and intermediate directory nodes are returned.
      - "only-empty-dir": Files and empty directory placeholders are returned.
      - "no-dir": Only files and symlinks are returned (no directory nodes).

    dest_dir (optional):
      Destination store boundary (typically install_pkg_dir during reverse-sync).
      When provided, symlinks resolving into dest_dir are inspected for self-referential loops:
      - Canonical symlinks: if real_target matches expected counterpart (dest_dir / repo_rel),
        it represents a standard Drift deployment link and is traversed normally without warning.
      - Mismatched symlinks: if real_target points into dest_dir but differs from counterpart,
        it represents an anomalous circular reference (e.g. host subfolder linking back to repo).
        A prominent warning is logged and recursive traversal is skipped to prevent infinite duplicate
        nesting across repeated sync operations.
    """
    context = FolderListingContext(
        root_rel_prefix=base_rel if base_rel is not None else Path(""),
        ignore_handler=ignore_handler,
        resolve_symlinks=resolve_symlinks,
        translate_mode=translate_mode,
        dir_mode=DirMode.from_str(dir_mode),
        dest_dir=dest_dir,
    )

    collected_paths: List[Path] = []
    _walk_folder_entry(
        current_path=src_dir,
        current_rel=context.root_rel_prefix,
        visited_dirs=set(),
        collected_paths=collected_paths,
        context=context,
    )
    return sorted(list(set(collected_paths)))
