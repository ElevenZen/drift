"""Primitive 1 Reverse Sync operation (System -> install/ state database)."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .constants import IGNORED_FILENAMES
from .ignore import DriftIgnore
from .file_utils import (
    tree_relative_files,
    resolve_system_target,
    translate_dot_prefixes_reverse,
)
from .sync_ops import reverse_sync_file_or_dir
from .install_repo import load_config_for_install

logger = logging.getLogger(__name__)


def should_skip_fcd_path(local_rel_path: Path, system_file: Path, ignore_handler: DriftIgnore, install_pkg_dir: Path) -> bool:
    """Checks if a given FCD path should be skipped during reverse sync."""
    if not system_file.exists() and not system_file.is_symlink():
        return True
    if ignore_handler.match_path(local_rel_path):
        return True
    local_install_file = install_pkg_dir / local_rel_path
    if local_install_file.exists():
        return True
    return False


def reverse_sync_fcd_dir(
    fcd_rel_path: Path,
    target_dir_path: Path,
    install_pkg_dir: Path,
    ignore_handler: DriftIgnore
) -> None:
    """Audits a single Fully-Controlled Directory subfolder/file for wild/untracked files and syncs them back."""
    target_sub_dir = target_dir_path / fcd_rel_path
    local_rel_path = translate_dot_prefixes_reverse(fcd_rel_path)
    local_install_file = install_pkg_dir / local_rel_path

    if should_skip_fcd_path(local_rel_path, target_sub_dir, ignore_handler, install_pkg_dir):
        return

    # If the FCD target is not a directory (is a file or a broken link), sync it back
    if not target_sub_dir.is_dir():
        reverse_sync_file_or_dir(target_sub_dir, local_install_file, ignore_handler=ignore_handler)
        return

    # If it is a directory, process its children recursively
    if target_sub_dir.is_dir():
        # Scan all files recursively inside the target subdirectory
        host_files = tree_relative_files(target_sub_dir)
        for rel_file in host_files:
            # Map '.' to 'dot-' using utility function
            translated_rel_file = translate_dot_prefixes_reverse(rel_file)
            child_local_rel_path = fcd_rel_path / translated_rel_file
            child_system_file = target_sub_dir / rel_file

            if should_skip_fcd_path(child_local_rel_path, child_system_file, ignore_handler, install_pkg_dir):
                continue

            child_local_install_file = install_pkg_dir / child_local_rel_path
            reverse_sync_file_or_dir(child_system_file, child_local_install_file, ignore_handler=ignore_handler)


def reverse_sync_package(pkg: str, install_base: Path, workspace_config: WorkspaceConfig) -> None:
    """Performs the reverse sync process for a single package."""
    install_pkg_dir = install_base / pkg
    try:
        metadata = load_config_for_install(install_base, pkg)
    except Exception as e:
        logger.warning(f"Skipping package '{pkg}' during reverse sync: {e}")
        return

    if not metadata.enable_install:
        logger.info(f"Reverse sync is disabled for package '{pkg}' (enable_install = false). Skipping.")
        return

    target_dir_path = (metadata.target_directory or workspace_config.default_target_directory).expanduser()
    assert target_dir_path.is_absolute(), f"Target directory '{target_dir_path}' for package '{pkg}' must be an absolute path."
    if not target_dir_path.exists():
        logger.warning(f"Target directory '{target_dir_path}' for package '{pkg}' does not exist. Skipping reverse sync.")
        return

    # Load ignore patterns
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

    # A. Traverse and sync all files currently tracked by our database (under install/<pkg>)
    deployable_files = ignore_handler.filter_deployable_files(install_pkg_dir)

    for relative_path in deployable_files:
        system_target = resolve_system_target(relative_path, target_dir_path)
        local_install_file = install_pkg_dir / relative_path
        reverse_sync_file_or_dir(system_target, local_install_file, ignore_handler=ignore_handler)

    # B. Audit Fully-Controlled Directory subfolders for wild/untracked files
    for fcd_rel_path in metadata.fully_controlled_dirs:
        reverse_sync_fcd_dir(
            fcd_rel_path=fcd_rel_path,
            target_dir_path=target_dir_path,
            install_pkg_dir=install_pkg_dir,
            ignore_handler=ignore_handler
        )


def run_primitive_1_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None
) -> None:
    """Unconditionally pulls configuration state from host system back to the install/ repository (Primitive 1)."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist. Skipping reverse sync.")
        return

    # Determine packages to process
    discovered_packages = workspace_config.get_discovered_packages(
        custom_dir=install_base,
        target_pkgs=package_names,
    )

    for pkg in discovered_packages:
        reverse_sync_package(pkg, install_base, workspace_config)
