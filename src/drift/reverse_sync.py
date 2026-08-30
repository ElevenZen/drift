"""Primitive 1 Reverse Sync operation (System -> install/ state database)."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .constants import MANAGED_CONFIG_FILES
from .ignore import DriftIgnore, IgnoreHandler
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

    # 1. Handle diff.added (items in install/ where host counterpart differs in type or is missing)
    for repo_rel in diff.added:
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        if ignore_handler and ignore_handler.match_path(repo_rel):
            continue
        target_rel = translate_dot_prefixes(repo_rel)
        system_target = target_dir_path / target_rel
        repo_file = install_pkg_dir / repo_rel

        if system_target.exists() or system_target.is_symlink():
            # Host target exists (e.g. type changed from file to directory)
            drifted_str, synced_str = sync_file_to_install(
                rel=target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_sync_result(drifted_str, synced_str, drifted_files, synced_files)
        else:
            # Host target is missing -> System Deletion
            if repo_file.exists() or repo_file.is_symlink():
                logger.info(f"System Deletion: '{system_target}' is missing. Deleting counterpart '{repo_file}' from install/...")
                remove_file_or_dir(repo_file)
                record_sync_result(str(target_rel), str(repo_rel), drifted_files, synced_files)

    # 2. Handle system modifications (items modified on host)
    for repo_rel in diff.modified:
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        if ignore_handler and ignore_handler.match_path(repo_rel):
            continue
        target_rel = translate_dot_prefixes(repo_rel)
        drifted_str, synced_str = sync_file_to_install(
            rel=target_rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        record_sync_result(drifted_str, synced_str, drifted_files, synced_files)

    # 3. Handle diff.deleted (sub-items created inside directories on host when repo was a file)
    for target_rel in diff.deleted:
        repo_rel = translate_dot_prefixes_reverse(target_rel)
        if repo_rel.name in MANAGED_CONFIG_FILES:
            continue
        if ignore_handler and ignore_handler.match_path(repo_rel):
            continue
        system_target = target_dir_path / target_rel
        if system_target.exists() or system_target.is_symlink():
            drifted_str, synced_str = sync_file_to_install(
                rel=target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_sync_result(drifted_str, synced_str, drifted_files, synced_files)


def sync_single_fcd(
    fcd: Path,
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    drifted_files: List[str],
    synced_files: List[str]
) -> None:
    """Reverse-syncs wild additions, modifications, and deletions within a single Fully-Controlled Directory."""
    fcd_repo_rel = translate_dot_prefixes_reverse(fcd)
    fcd_target_rel = translate_dot_prefixes(fcd)
    fcd_system_dir = target_dir_path / fcd_target_rel
    fcd_install_dir = install_pkg_dir / fcd_repo_rel

    if not fcd_system_dir.exists() and not fcd_system_dir.is_symlink():
        return

    # Handle file or broken symlink at FCD root
    if fcd_system_dir.is_file() or (fcd_system_dir.is_symlink() and not fcd_system_dir.is_dir()):
        if not ignore_handler.match_path(fcd_repo_rel):
            drifted_str, synced_str = sync_file_to_install(
                rel=fcd_target_rel,
                target_dir_path=target_dir_path,
                install_pkg_dir=install_pkg_dir,
                ignore_handler=ignore_handler
            )
            record_sync_result(drifted_str, synced_str, drifted_files, synced_files)
        return

    class ScopedIgnore(IgnoreHandler):
        def match_path(self, rel_path: Path) -> bool:
            repo_sub = fcd_repo_rel / translate_dot_prefixes_reverse(rel_path) if rel_path != Path("") else fcd_repo_rel
            return ignore_handler.match_path(repo_sub)

    fcd_diff = compare_folders(
        src_dir=fcd_system_dir,
        dst_dir=fcd_install_dir,
        ignore_handler=ScopedIgnore(),
        resolve_symlinks=True,
        translate_mode="reverse"
    )

    for sub_rel in fcd_diff.added + fcd_diff.modified:
        if sub_rel.name in MANAGED_CONFIG_FILES:
            continue
        full_target_rel = fcd_target_rel / sub_rel if sub_rel != Path("") else fcd_target_rel
        full_repo_rel = fcd_repo_rel / translate_dot_prefixes_reverse(sub_rel) if sub_rel != Path("") else fcd_repo_rel
        if ignore_handler.match_path(full_repo_rel):
            continue
        drifted_str, synced_str = sync_file_to_install(
            rel=full_target_rel,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )
        record_sync_result(drifted_str, synced_str, drifted_files, synced_files)

    for sub_rel in fcd_diff.deleted:
        full_repo_rel = fcd_repo_rel / translate_dot_prefixes_reverse(sub_rel) if sub_rel != Path("") else fcd_repo_rel
        if full_repo_rel.name in MANAGED_CONFIG_FILES or ignore_handler.match_path(full_repo_rel):
            continue
        repo_file = install_pkg_dir / full_repo_rel
        if repo_file.exists() or repo_file.is_symlink():
            full_target_rel = fcd_target_rel / sub_rel if sub_rel != Path("") else fcd_target_rel
            logger.info(f"System Deletion (FCD): '{target_dir_path / full_target_rel}' is missing. Deleting counterpart '{repo_file}' from install/...")
            remove_file_or_dir(repo_file)
            record_sync_result(str(full_target_rel), str(full_repo_rel), drifted_files, synced_files)


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
        fully_controlled_dirs=metadata.fully_controlled_dirs,
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
