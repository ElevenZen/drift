"""Primitive 1 Reverse Sync operation (System -> install/ state database)."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .constants import MANAGED_CONFIG_FILES
from .ignore import DriftIgnore
from .file_utils import (
    is_relative_to,
    resolve_system_target,
    translate_dot_prefixes,
    translate_dot_prefixes_reverse,
    remove_file_or_dir,
)
from .folder_diff import compare_folders
from .sync_ops import reverse_sync_file_or_dir
from .install_repo import load_config_for_install
from .result_models import PackageReverseSyncResult, ReverseSyncResult

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
    repo_rel = translate_dot_prefixes_reverse(rel)
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
    normalized_fcds = [translate_dot_prefixes(f) for f in fully_controlled_dirs]
    to_sync = []
    for rel in added_files:
        if rel.name in MANAGED_CONFIG_FILES:
            continue

        in_fcd = any(is_relative_to(rel, fcd_rel) for fcd_rel in normalized_fcds)
        parent_deleted = any(parent in deleted_files for parent in [rel] + list(rel.parents))

        if in_fcd or parent_deleted:
            to_sync.append(rel)
    return to_sync


def reverse_sync_package(pkg: str, install_base: Path, workspace_config: WorkspaceConfig) -> PackageReverseSyncResult:
    """Performs the reverse sync process for a single package."""
    install_pkg_dir = install_base / pkg
    try:
        metadata = load_config_for_install(install_base, pkg)
    except Exception as e:
        logger.warning(f"Skipping package '{pkg}' during reverse sync: {e}")
        return PackageReverseSyncResult(
            package=pkg,
            target_directory="",
            status="FAILED",
            error=str(e)
        )

    if not metadata.enable_install:
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
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

    # Use compare_folders to detect all changes (modifications and wild files in FCD)
    diff = compare_folders(
        src_dir=target_dir_path,
        dst_dir=install_pkg_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=True,
        translate_mode="reverse"
    )

    drifted_files: List[str] = []
    synced_files: List[str] = []

    # 1. Handle system deletions (deleted in system but exist in repo, or blocking dirs)
    for rel in diff.deleted:
        repo_rel = translate_dot_prefixes_reverse(rel)
        if ignore_handler and ignore_handler.match_path(repo_rel):
            continue
        repo_file = install_pkg_dir / repo_rel
        
        if repo_file.exists():
            logger.info(f"System Deletion: '{target_dir_path / rel}' is missing. Deleting counterpart '{repo_file}' from install/...")
            remove_file_or_dir(repo_file)
            drifted_files.append(str(rel))
            synced_files.append(str(repo_rel))

    # 2. Handle system modifications
    for rel in diff.modified:
        drifted_str, synced_str = sync_file_to_install(
            rel=rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        drifted_files.append(drifted_str)
        synced_files.append(synced_str)

    # 3. Handle wild files in FCD and promoted tracked files (added on system but not in repo)
    added_to_sync = filter_added_files_to_sync(
        added_files=diff.added,
        deleted_files=diff.deleted,
        fully_controlled_dirs=metadata.fully_controlled_dirs
    )
    for rel in added_to_sync:
        drifted_str, synced_str = sync_file_to_install(
            rel=rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        drifted_files.append(drifted_str)
        synced_files.append(synced_str)

    return PackageReverseSyncResult(
        package=pkg,
        target_directory=str(target_dir_path),
        drifted_files=drifted_files,
        synced_files=synced_files,
        status="SUCCESS"
    )


def run_primitive_1_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None
) -> ReverseSyncResult:
    """Unconditionally pulls configuration state from host system back to the install/ repository (Primitive 1)."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist. Skipping reverse sync.")
        return ReverseSyncResult(
            status="FAILED",
            error_message=f"Install state database directory '{install_base}' does not exist."
        )

    # Determine packages to process
    discovered_packages = workspace_config.get_discovered_packages(
        custom_dir=install_base,
        target_pkgs=package_names,
    )

    results: List[PackageReverseSyncResult] = []
    for pkg in discovered_packages:
        pkg_res = reverse_sync_package(pkg, install_base, workspace_config)
        results.append(pkg_res)

    return ReverseSyncResult(
        status="SUCCESS",
        packages=results
    )
