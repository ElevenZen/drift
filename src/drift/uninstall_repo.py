"""Primitive 7: Uninstall package from system and restore backups."""

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry, save_state_registry
from .package_config import load_package_config_static
from .file_utils import (
    remove_file_or_dir_with_sudo,
    rmdir_parents,
    copy_or_move_file_or_dir_external,
    tree_relative_files,
    resolve_system_target,
    is_relative_to
)
from .constants import PACKAGE_CONFIG_FILE_NAME

logger = logging.getLogger(__name__)

def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: List[str],
    force: bool = False
) -> None:
    """
    Uninstalls one or more packages from the system.
    Safeguard: Aborts if the package is still enabled in workspace config unless force=True.
    """
    if not package_names:
        return

    # 1. Load state registry
    state_file = workspace_config.install_path / "state.toml"
    registry = load_state_registry(state_file)

    successfully_uninstalled = []

    for pkg in package_names:
        # 2. Safeguard: Check if package is enabled in workspace_config
        # Discovered active packages are those enabled in drift.toml
        if workspace_config.is_package_enabled(pkg) and not force:
            logger.error(f"🛡️  [SAFEGUARD] Package '{pkg}' is still active/enabled in workspace configuration.")
            logger.error(f"   To safely uninstall, first disable it in drift.toml or use --force.")
            raise RuntimeError(f"Safeguard abort: Package '{pkg}' is active.")

        # 3. Get package state and deployed files
        pkg_state = registry.packages.get(pkg)
        if not pkg_state:
            logger.warning(f"⚠️  Package '{pkg}' is not registered as installed. Skipping.")
            continue

        logger.info(f"🗑️  Uninstalling package: {pkg}")

        # 4. Load package config from install/pkg/package.toml
        install_pkg_dir = workspace_config.install_path / pkg
        config_file = install_pkg_dir / PACKAGE_CONFIG_FILE_NAME
        
        target_dir = workspace_config.default_target_path
        sudo = False
        
        if config_file.exists():
            try:
                pkg_config = load_package_config_static(config_file, default_name=pkg)
                target_dir = pkg_config.target_directory or workspace_config.default_target_path
                sudo = pkg_config.sudo
            except Exception as e:
                logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        else:
            logger.warning(f"   Missing package config in {install_pkg_dir}. Using defaults.")

        # 5. Remove deployed files
        deployed_files = pkg_state.deployed_files
        # Sort in reverse to handle nested files/dirs (files before their parent dirs)
        for rel_file in sorted(deployed_files, reverse=True):
            system_target = resolve_system_target(rel_file, target_dir)
            
            if system_target.exists() or system_target.is_symlink():
                logger.debug(f"   Removing: {system_target}")
                remove_file_or_dir_with_sudo(system_target, sudo)
                # Cleanup empty parent dirs up to target_dir
                rmdir_parents(system_target.parent, target_dir)
        
        logger.info(f"🧹 Cleaned up deployed files for {pkg}")

        # 6. Restore backups (from overwritten folder)
        backup_pkg_overwritten = workspace_config.backup_path / pkg / "overwritten"
        if backup_pkg_overwritten.exists():
            logger.info(f"🔄 Restoring backups for {pkg}...")
            backup_files = tree_relative_files(backup_pkg_overwritten)
            for rel_backup in backup_files:
                src = backup_pkg_overwritten / rel_backup
                system_target = resolve_system_target(rel_backup, target_dir)
                logger.debug(f"   Restoring: {system_target}")
                # Use move=True to clean up backup as we restore it
                copy_or_move_file_or_dir_external(src, system_target, sudo, move=True)
            logger.info(f"✨ Restored {len(backup_files)} file(s) for {pkg}")

        # 7. Clean up backup directory if empty
        backup_pkg_dir = workspace_config.backup_path / pkg
        if backup_pkg_dir.exists():
            # If overwritten was empty/restored, we might still have deleted_files
            # We don't restore deleted_files, but we should probably keep them or delete them?
            # Design doc says "clean traces", so let's delete them if they aren't restored.
            try:
                shutil.rmtree(backup_pkg_dir)
            except Exception as e:
                logger.warning(f"   Failed to clean up backup directory {backup_pkg_dir}: {e}")

        # 8. Clean up install/pkg directory
        if install_pkg_dir.exists():
            try:
                shutil.rmtree(install_pkg_dir)
            except Exception as e:
                logger.warning(f"   Failed to clean up install directory {install_pkg_dir}: {e}")

        # 9. Update state registry
        registry.remove_package(pkg)
        successfully_uninstalled.append(pkg)

    # 10. Save state registry
    save_state_registry(state_file, registry)

    # 11. Commit changes in install repo
    if successfully_uninstalled:
        from .install_repo import run_primitive_6_commit_install_repo
        commit_msg = f"Uninstall: Removed package(s) {', '.join(successfully_uninstalled)}"
        run_primitive_6_commit_install_repo(workspace_config, commit_msg, successfully_uninstalled)
        logger.info(f"✨ Successfully uninstalled {len(successfully_uninstalled)} package(s)!")
    else:
        logger.info("Nothing to uninstall.")
