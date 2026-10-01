"""Primitive 1 Reverse Sync operation (System -> install/ state database)."""

import logging
from pathlib import Path
from typing import List, Optional, Union, Sequence, Set

from ..config.workspace_config import WorkspaceConfig
from ..core.constants import MANAGED_CONFIG_FILES
from ..core.ignore import DriftIgnore, IgnoreHandler
from ..utils.path_utils import (
    is_relative_to,
    resolve_target_path,
    encode_dot_prefix,
    decode_dot_prefix,
)
from ..utils.file_ops import remove
from ..utils.file_inspect import is_concrete_dir
from ..core.folder_diff import compare_folders
from ..core.sync_ops import reverse_sync_file_or_dir
from ..config.package_config import PackageConfig
from ..core.result_models import PackageReverseSyncResult, ReverseSyncResult

logger = logging.getLogger(__name__)


def sync_file_to_install(
    rel: Path,
    target_dir_path: Path,
    install_pkg_dir: Path,
    ignore_handler: DriftIgnore
) -> tuple[str, str]:
    """Syncs a single modified/added file or directory from the host system back into the package install repository.

    Returns (drifted_file_str, synced_file_str).
    """
    system_file = target_dir_path / rel
    repo_rel = decode_dot_prefix(rel)
    repo_file = install_pkg_dir / repo_rel
    reverse_sync_file_or_dir(system_file, repo_file, ignore_handler=ignore_handler)
    return str(rel), str(repo_rel)


def filter_added_files_to_sync(
    added_files: List[Path],
    deleted_files: List[Path],
    fully_controlled_dirs: List[Path]
) -> List[Path]:
    """Filters added files on the host system to determine which ones should be reverse-synced.

    Files are synced if:
    1. They are inside a fully-controlled directory (FCD).
    2. Or a parent directory was deleted in repo (promoted tracked file).
    Internal managed config files are ignored.
    """
    normalized_fcds = [encode_dot_prefix(f) for f in fully_controlled_dirs]
    to_sync = []
    for rel in added_files:
        if rel.name in MANAGED_CONFIG_FILES:
            continue

        in_fcd = any(is_relative_to(rel, fcd_rel) for fcd_rel in normalized_fcds)
        parent_deleted = any(parent in deleted_files for parent in [rel] + list(rel.parents))

        if in_fcd or parent_deleted:
            to_sync.append(rel)
    return to_sync


def _check_has_synced_ancestor(rel: Path, synced_ancestors: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of rel has already been synced."""
    return any(parent in synced_ancestors for parent in rel.parents if parent != Path("") and parent != Path("."))


def _collect_leaf_paths(root: Path) -> List[Path]:
    """Returns all leaf files and symlinks under root, or [root] if root is not a directory or empty."""
    if not root.is_dir() or root.is_symlink():
        return [root]
    leaves = [p for p in root.rglob("*") if not p.is_dir() or p.is_symlink()]
    return sorted(leaves) if leaves else [root]


def record_sync_result(
    drifted_str: str,
    synced_str: str,
    drifted_files: List[str],
    synced_files: List[str]
) -> None:
    """Helper to append unique drifted and synced file path records."""
    if drifted_str not in drifted_files:
        drifted_files.append(drifted_str)
        synced_files.append(synced_str)


def record_synced_target(
    target_rel: Path,
    target_dir_path: Path,
    drifted_files: List[str],
    synced_files: List[str],
    ignore_handler: Optional[IgnoreHandler] = None,
) -> None:
    """Records synced system target and its leaf files into drifted_files and synced_files."""
    system_target = target_dir_path / target_rel
    if is_concrete_dir(system_target):
        for leaf in _collect_leaf_paths(system_target):
            child_target_rel = leaf.relative_to(target_dir_path) if leaf != target_dir_path else target_rel
            child_repo_rel = decode_dot_prefix(child_target_rel)
            if child_repo_rel.name in MANAGED_CONFIG_FILES:
                continue
            if ignore_handler and ignore_handler.match_path(child_repo_rel, is_dir=leaf.is_dir()):
                continue
            record_sync_result(str(child_target_rel), str(child_repo_rel), drifted_files, synced_files)
    else:
        repo_rel = decode_dot_prefix(target_rel)
        if repo_rel.name not in MANAGED_CONFIG_FILES:
            if not (ignore_handler and ignore_handler.match_path(repo_rel, is_dir=system_target.is_dir())):
                record_sync_result(str(target_rel), str(repo_rel), drifted_files, synced_files)


def record_deleted_repo_target(
    repo_rel: Path,
    install_pkg_dir: Path,
    drifted_files: List[str],
    synced_files: List[str],
    ignore_handler: Optional[IgnoreHandler] = None,
) -> None:
    """Records deleted repo counterpart and its leaf files into drifted_files and synced_files."""
    repo_file = install_pkg_dir / repo_rel
    if is_concrete_dir(repo_file):
        for leaf in _collect_leaf_paths(repo_file):
            child_repo_rel = leaf.relative_to(install_pkg_dir) if leaf != install_pkg_dir else repo_rel
            child_target_rel = encode_dot_prefix(child_repo_rel)
            if child_repo_rel.name in MANAGED_CONFIG_FILES:
                continue
            if ignore_handler and ignore_handler.match_path(child_repo_rel, is_dir=leaf.is_dir()):
                continue
            record_sync_result(str(child_target_rel), str(child_repo_rel), drifted_files, synced_files)
    else:
        target_rel = encode_dot_prefix(repo_rel)
        if repo_rel.name not in MANAGED_CONFIG_FILES:
            if not (ignore_handler and ignore_handler.match_path(repo_rel, is_dir=repo_file.is_dir())):
                record_sync_result(str(target_rel), str(repo_rel), drifted_files, synced_files)


def sync_tracked_files(
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    drifted_files: List[str],
    synced_files: List[str]
) -> None:
    """Probes and synchronizes tracked package files against the host system without scanning the rest of target_dir."""
    diff = compare_folders(
        src_dir=install_pkg_dir,
        dst_dir=target_dir_path,
        ignore_handler=ignore_handler,
        resolve_symlinks=True,
        translate_mode="forward",
        src_only=True
    )

    synced_ancestors: Set[Path] = set()

    # 1. Handle diff.added (items in install/ where host counterpart differs in type or is missing)
    # Sorted shallowest-to-deepest so parent folders are processed before children.
    for repo_rel in sorted(diff.added, key=lambda p: (len(p.parts), p)):
        if _check_has_synced_ancestor(repo_rel, synced_ancestors):
            continue
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        repo_file = install_pkg_dir / repo_rel
        if ignore_handler and ignore_handler.match_path(repo_rel, is_dir=repo_file.is_dir()):
            continue
        target_rel = encode_dot_prefix(repo_rel)
        system_target = target_dir_path / target_rel

        if system_target.exists() or system_target.is_symlink():
            # Host target exists (e.g. type changed from file to directory)
            sync_file_to_install(
                rel=target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_synced_target(target_rel, target_dir_path, drifted_files, synced_files, ignore_handler=ignore_handler)
            if is_concrete_dir(system_target):
                if repo_rel != Path("") and repo_rel != Path("."):
                    synced_ancestors.add(repo_rel)
        else:
            # Host target is missing -> System Deletion
            if repo_file.exists() or repo_file.is_symlink():
                logger.info(f"System Deletion: '{system_target}' is missing. Deleting counterpart '{repo_file}' from install/...")
                record_deleted_repo_target(repo_rel, install_pkg_dir, drifted_files, synced_files, ignore_handler=ignore_handler)
                remove(repo_file)
                if repo_rel != Path("") and repo_rel != Path("."):
                    synced_ancestors.add(repo_rel)

    # 2. Handle system modifications (items modified on host)
    for repo_rel in sorted(diff.modified, key=lambda p: (len(p.parts), p)):
        if _check_has_synced_ancestor(repo_rel, synced_ancestors):
            continue
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        repo_file = install_pkg_dir / repo_rel
        if ignore_handler and ignore_handler.match_path(repo_rel, is_dir=repo_file.is_dir()):
            continue
        target_rel = encode_dot_prefix(repo_rel)
        sync_file_to_install(
            rel=target_rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        record_synced_target(target_rel, target_dir_path, drifted_files, synced_files, ignore_handler=ignore_handler)
        system_target = target_dir_path / target_rel
        if is_concrete_dir(system_target):
            if repo_rel != Path("") and repo_rel != Path("."):
                synced_ancestors.add(repo_rel)

    # 3. Handle diff.deleted (sub-items created inside directories on host when repo was a file)
    for target_rel in sorted(diff.deleted, key=lambda p: (len(p.parts), p)):
        repo_rel = decode_dot_prefix(target_rel)
        if _check_has_synced_ancestor(repo_rel, synced_ancestors):
            continue
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        system_target = target_dir_path / target_rel
        if ignore_handler and ignore_handler.match_path(repo_rel, is_dir=system_target.is_dir()):
            continue
        if system_target.exists() or system_target.is_symlink():
            sync_file_to_install(
                rel=target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_synced_target(target_rel, target_dir_path, drifted_files, synced_files, ignore_handler=ignore_handler)
            if is_concrete_dir(system_target):
                if repo_rel != Path("") and repo_rel != Path("."):
                    synced_ancestors.add(repo_rel)


def sync_single_fcd(
    fcd: Path,
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    drifted_files: List[str],
    synced_files: List[str]
) -> None:
    """Reverse-syncs wild additions, modifications, and deletions within a single Fully-Controlled Directory."""
    fcd_repo_rel = decode_dot_prefix(fcd)
    fcd_target_rel = encode_dot_prefix(fcd)
    fcd_system_dir = target_dir_path / fcd_target_rel
    fcd_install_dir = install_pkg_dir / fcd_repo_rel

    if not fcd_system_dir.exists() and not fcd_system_dir.is_symlink():
        return

    # Handle file or broken symlink at FCD root
    if fcd_system_dir.is_file() or (fcd_system_dir.is_symlink() and not fcd_system_dir.is_dir()):
        if not ignore_handler.match_path(fcd_repo_rel, is_dir=False):
            sync_file_to_install(
                rel=fcd_target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_synced_target(fcd_target_rel, target_dir_path, drifted_files, synced_files, ignore_handler=ignore_handler)
        return

    class ScopedIgnore(IgnoreHandler):
        def match_path(self, rel_path: Path, is_dir: bool = False) -> bool:
            repo_sub = fcd_repo_rel / decode_dot_prefix(rel_path) if rel_path != Path("") else fcd_repo_rel
            return ignore_handler.match_path(repo_sub, is_dir=is_dir)

    fcd_diff = compare_folders(
        src_dir=fcd_system_dir,
        dst_dir=fcd_install_dir,
        ignore_handler=ScopedIgnore(),
        resolve_symlinks=True,
        translate_mode="reverse"
    )

    # 1. Process deletions first to resolve multi-level type changes and clear obsolete paths
    synced_deleted_ancestors: Set[Path] = set()
    for sub_rel in sorted(fcd_diff.deleted, key=lambda p: (len(p.parts), p)):
        if _check_has_synced_ancestor(sub_rel, synced_deleted_ancestors):
            continue
        full_repo_rel = fcd_repo_rel / decode_dot_prefix(sub_rel) if sub_rel != Path("") else fcd_repo_rel
        repo_file = install_pkg_dir / full_repo_rel
        if full_repo_rel.name in MANAGED_CONFIG_FILES or ignore_handler.match_path(full_repo_rel, is_dir=repo_file.is_dir()):
            continue
        if repo_file.exists() or repo_file.is_symlink():
            full_target_rel = fcd_target_rel / sub_rel if sub_rel != Path("") else fcd_target_rel
            logger.info(f"System Deletion (FCD): '{target_dir_path / full_target_rel}' is missing. Deleting counterpart '{repo_file}' from install/...")
            record_deleted_repo_target(full_repo_rel, install_pkg_dir, drifted_files, synced_files, ignore_handler=ignore_handler)
            remove(repo_file)
            if sub_rel != Path("") and sub_rel != Path("."):
                synced_deleted_ancestors.add(sub_rel)

    # 2. Process additions and modifications after deletions
    synced_added_ancestors: Set[Path] = set()
    for sub_rel in sorted(fcd_diff.added + fcd_diff.modified, key=lambda p: (len(p.parts), p)):
        if _check_has_synced_ancestor(sub_rel, synced_added_ancestors):
            continue
        if sub_rel.name in MANAGED_CONFIG_FILES:
            continue
        full_target_rel = fcd_target_rel / sub_rel if sub_rel != Path("") else fcd_target_rel
        full_repo_rel = fcd_repo_rel / decode_dot_prefix(sub_rel) if sub_rel != Path("") else fcd_repo_rel
        system_item = target_dir_path / full_target_rel
        if ignore_handler.match_path(full_repo_rel, is_dir=system_item.is_dir()):
            continue
        sync_file_to_install(
            rel=full_target_rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        record_synced_target(full_target_rel, target_dir_path, drifted_files, synced_files, ignore_handler=ignore_handler)
        if is_concrete_dir(system_item):
            if sub_rel != Path("") and sub_rel != Path("."):
                synced_added_ancestors.add(sub_rel)


def sync_fully_controlled_dirs(
    fully_controlled_dirs: List[Path],
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    drifted_files: List[str],
    synced_files: List[str]
) -> None:
    """Iterates and synchronizes all designated Fully-Controlled Directories."""
    for fcd in fully_controlled_dirs:
        sync_single_fcd(
            fcd=fcd,
            install_pkg_dir=install_pkg_dir,
            target_dir_path=target_dir_path,
            ignore_handler=ignore_handler,
            drifted_files=drifted_files,
            synced_files=synced_files
        )


def reverse_sync_package(pkg: str, install_base: Path, workspace_config: WorkspaceConfig) -> PackageReverseSyncResult:
    """Performs the reverse sync process for a single package without scanning the entire target_dir."""
    install_pkg_dir = install_base / pkg
    try:
        metadata = PackageConfig.from_install_dir(install_pkg_dir, workspace_config)
    except Exception as e:
        logger.warning(f"Skipping package '{pkg}' during reverse sync: {e}")
        return PackageReverseSyncResult(
            package=pkg,
            target_directory="",
            status="FAILED",
            error=str(e)
        )

    if not metadata.package.enable_install:
        logger.info(f"Reverse sync is disabled for package '{pkg}' (enable_install = false). Skipping.")
        return PackageReverseSyncResult(
            package=pkg,
            target_directory=str(metadata.get_target_directory(workspace_config)),
            status="SKIPPED"
        )

    target_dir_path = metadata.get_target_directory(workspace_config)
    assert target_dir_path.is_absolute(), f"Target directory '{target_dir_path}' for package '{pkg}' must be an absolute path."
    if not target_dir_path.exists():
        logger.warning(f"Target directory '{target_dir_path}' for package '{pkg}' does not exist. Skipping reverse sync.")
        return PackageReverseSyncResult(
            package=pkg,
            target_directory=str(target_dir_path),
            status="SKIPPED"
        )

    # Load ignore patterns
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)

    drifted_files: List[str] = []
    synced_files: List[str] = []

    # 1. Sync tracked package files
    sync_tracked_files(
        install_pkg_dir=install_pkg_dir,
        target_dir_path=target_dir_path,
        ignore_handler=ignore_handler,
        drifted_files=drifted_files,
        synced_files=synced_files
    )

    # 2. Sync Fully-Controlled Directories
    sync_fully_controlled_dirs(
        fully_controlled_dirs=metadata.package.fully_controlled_dirs,
        install_pkg_dir=install_pkg_dir,
        target_dir_path=target_dir_path,
        ignore_handler=ignore_handler,
        drifted_files=drifted_files,
        synced_files=synced_files
    )

    return PackageReverseSyncResult(
        package=pkg,
        target_directory=str(target_dir_path),
        drifted_files=drifted_files,
        synced_files=synced_files,
        status="SUCCESS"
    )


def run_primitive_1_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = ()
) -> ReverseSyncResult:
    """Unconditionally pulls configuration state from host system back to the install/ repository (Primitive 1)."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist. Skipping reverse sync.")
        return ReverseSyncResult(
            status="FAILED",
            error_message=f"Install state database directory '{install_base}' does not exist."
        )

    # Determine packages to process from install directory
    discovered_packages = workspace_config.filter_install_packages_by_target(
        target_packages=package_names or None,
    )

    results: List[PackageReverseSyncResult] = []
    for pkg in discovered_packages:
        pkg_res = reverse_sync_package(pkg, install_base, workspace_config)
        results.append(pkg_res)

    return ReverseSyncResult(
        status="SUCCESS",
        packages=results
    )
