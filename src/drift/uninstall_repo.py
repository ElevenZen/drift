"""Primitive 7: Uninstall package from system and restore backups.

Note:
    Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
    triggered if the package configuration file ('drift_package.toml') is available
    in the install/<pkg>/ directory.
"""

import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry, save_state_registry, PackageState, StateRegistry
from .file_utils import (
    remove_file_or_dir_with_sudo,
    rmdir_parents,
    copy_or_move_file_or_dir_external,
    tree_relative_files,
    resolve_system_target,
)
from .constants import PACKAGE_CONFIG_FILE_NAME, UNINSTALL_HOOK_NAMES
from .result_models import PackageUninstallResult, RestoredBackup, UninstallResult

logger = logging.getLogger(__name__)

def filter_uninstallable_packages(
    workspace_config: WorkspaceConfig,
    registry: StateRegistry,
    package_names: Optional[List[str]],
    force: bool = False
) -> Tuple[Dict[str, PackageState], List[str]]:
    """
    Filters which packages are safe to uninstall according to the workspace configuration and registry.
    Returns (safe_pkg_state_map, active_but_rejected_names).
    Force will allow uninstalling even if the package is still enabled in workspace config.
    """
    installed_packages = registry.packages

    if package_names is None:
        # If no packages specified, target all installed packages that are NOT enabled in config (orphans)
        package_names = [pkg for pkg in installed_packages if not workspace_config.is_package_enabled(pkg)]
        if not package_names:
            return {}, []

    safe_map = {}
    rejected = []

    for pkg in package_names:
        # Check active status FIRST for safeguard
        if workspace_config.is_package_enabled(pkg) and not force:
            rejected.append(pkg)
            continue
            
        if pkg not in installed_packages:
            logger.warning(f"⚠️  Package '{pkg}' is not registered as installed. Skipping.")
            continue
            
        safe_map[pkg] = installed_packages[pkg]
            
    return safe_map, rejected


def get_uninstall_metadata(workspace_config: WorkspaceConfig, pkg: str) -> Tuple[Path, bool]:
    """Retrieves target directory and sudo settings for a package uninstallation."""
    from .install_repo import load_config_for_install
    
    try:
        pkg_config = load_config_for_install(workspace_config.install_path, pkg)
        target_dir = pkg_config.get_target_directory(workspace_config)
        sudo = pkg_config.sudo
        return target_dir, sudo
    except Exception as e:
        logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        return workspace_config.default_target_path, False


def remove_deployed_files(
    pkg: str,
    deployed_files: List[Path],
    target_dir: Path,
    sudo: bool,
    dry_run: bool = False
) -> List[Path]:
    """Removes deployed files from the system. Returns list of removed paths."""
    removed = []
    # Sort in reverse to handle nested files/dirs (files before their parent dirs)
    for rel_file in sorted(deployed_files, reverse=True):
        system_target = resolve_system_target(rel_file, target_dir)
        
        if system_target.exists() or system_target.is_symlink():
            if dry_run:
                logger.info(f"🔍 [DRY RUN] Would remove: {system_target}")
            else:
                logger.debug(f"   Removing: {system_target}")
                remove_file_or_dir_with_sudo(system_target, sudo)
                # Cleanup empty parent dirs up to target_dir
                rmdir_parents(system_target.parent, target_dir)
            removed.append(system_target)
    
    if not dry_run and removed:
        logger.info(f"🧹 Cleaned up {len(removed)} deployed file(s) for {pkg}")
    return removed


def restore_backups(
    workspace_config: WorkspaceConfig,
    pkg: str,
    target_dir: Path,
    sudo: bool,
    dry_run: bool = False
) -> List[Path]:
    """Restores backups for a package. Returns list of restored paths."""
    restored = []
    backup_pkg_overwritten = workspace_config.backup_path / pkg / "overwritten"
    
    if not backup_pkg_overwritten.exists():
        return restored  # No backups to restore

    # We assume we can just move symlinks in the backup without resolving,
    # so we can safely use tree_relative_files
    backup_files = tree_relative_files(backup_pkg_overwritten)

    if not backup_files:
        return restored  # No backups to restore
    if not dry_run:
        logger.info(f"🔄 Restoring backups for {pkg}...")
    
    for rel_backup in backup_files:
        src = backup_pkg_overwritten / rel_backup
        system_target = resolve_system_target(rel_backup, target_dir)
        
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would restore: {system_target}")
        else:
            logger.debug(f"   Restoring: {system_target}")
            # Use move=True to clean up backup as we restore it
            copy_or_move_file_or_dir_external(src, system_target, sudo, move=True)
        restored.append(system_target)
    
    if not dry_run:
        logger.info(f"✨ Restored {len(restored)} file(s) for {pkg}")
        # Clean up the 'overwritten' directory if it's now empty
        if backup_pkg_overwritten.exists() and not any(backup_pkg_overwritten.iterdir()):
            try:
                backup_pkg_overwritten.rmdir()
            except Exception:
                pass

    return restored


def clean_up_package_directories(workspace_config: WorkspaceConfig, pkg: str) -> None:
    """Cleans up the package directory under install_path and empty package directory under backup_path."""
    # Clean up install/pkg directory
    install_pkg_dir = workspace_config.install_path / pkg
    if install_pkg_dir.exists():
        try:
            shutil.rmtree(install_pkg_dir)
        except Exception as e:
            logger.warning(f"   Failed to clean up install directory {install_pkg_dir}: {e}")

    # Clean up backup/pkg directory if empty
    backup_pkg_dir = workspace_config.backup_path / pkg
    if backup_pkg_dir.exists() and not any(backup_pkg_dir.iterdir()):
        try:
            backup_pkg_dir.rmdir()
        except Exception:
            pass


def detach_single_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_state: PackageState,
    dry_run: bool = False
) -> bool:
    """Decouples/detaches a single package from Drift, replacing symlinks with physical copies."""
    if not dry_run:
        logger.info(f"🔌 Detaching package: {pkg} (converting to independent system config)")
    else:
        logger.info(f"🔍 [DRY RUN] Would detach package: {pkg} (replacing symlinks with copies)")

    target_dir, sudo = get_uninstall_metadata(workspace_config, pkg)

    for rel_file in pkg_state.deployed_files:
        system_target = resolve_system_target(rel_file, target_dir)
        if system_target.is_symlink():
            # Log the file that will be replaced in both dry-run and live modes
            if dry_run:
                logger.info(f"🔍 [DRY RUN] Would replace symlink with actual copy: {system_target}")
            else:
                logger.info(f"   Replacing symlink with actual copy: {system_target}")
                remove_file_or_dir_with_sudo(system_target, sudo)
                src_file = workspace_config.install_path / pkg / rel_file
                if src_file.is_file():
                    system_target.parent.mkdir(parents=True, exist_ok=True)
                    copy_or_move_file_or_dir_external(src_file, system_target, sudo, move=False)

    if not dry_run:
        logger.info(f"🔌 Successfully detached and converted {pkg} files to independent configurations on the host.")

    if dry_run:
        return True

    clean_up_package_directories(workspace_config, pkg)
    return True


def uninstall_single_package_standard(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_state: PackageState,
    dry_run: bool = False,
    no_hooks: bool = False
) -> bool:
    """Orchestrates standard uninstallation of a single package.

    Note:
        Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
        triggered if the package configuration file ('drift_package.toml') is available
        in the install/<pkg>/ directory.
    """
    if not dry_run:
        logger.info(f"🗑️  Uninstalling package: {pkg}")
    else:
        logger.info(f"🔍 [DRY RUN] Would uninstall package: {pkg}")

    install_pkg_dir = workspace_config.install_path / pkg

    from .install_repo import load_config_for_install
    try:
        pkg_config = load_config_for_install(workspace_config.install_path, pkg)
        target_dir = pkg_config.get_target_directory(workspace_config)
        sudo = pkg_config.sudo
    except Exception as e:
        logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        pkg_config = None
        target_dir = workspace_config.default_target_path
        sudo = False

    # Check uninstall hook files exist before attempting uninstallation
    if not dry_run and not no_hooks and pkg_config and pkg_config.hooks:
        pkg_config.hooks.check_hook_files(install_pkg_dir, hook_names=UNINSTALL_HOOK_NAMES)

    # 1. Trigger pre_uninstall hook (only if drift_package.toml is available)
    if not dry_run and pkg_config and pkg_config.pre_uninstall:
        with pkg_config.package_envs(workspace_config):
            pkg_config.hooks.trigger_pre_uninstall(install_dir=install_pkg_dir, cwd=install_pkg_dir, no_hooks=no_hooks)

    # 2. Remove deployed files
    remove_deployed_files(pkg, pkg_state.deployed_files, target_dir, sudo, dry_run=dry_run)

    # 3. Restore backups
    restore_backups(workspace_config, pkg, target_dir, sudo, dry_run=dry_run)

    # 4. Trigger post_uninstall hook (only if drift_package.toml is available)
    if not dry_run and pkg_config and pkg_config.post_uninstall:
        with pkg_config.package_envs(workspace_config):
            pkg_config.hooks.trigger_post_uninstall(install_dir=install_pkg_dir, cwd=target_dir, no_hooks=no_hooks)

    if dry_run:
        return True

    clean_up_package_directories(workspace_config, pkg)
    return True


def uninstall_single_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_state: PackageState,
    dry_run: bool = False,
    detach: bool = False,
    no_hooks: bool = False
) -> bool:
    """Orchestrates the uninstallation or detachment of a single package."""
    if detach:
        return detach_single_package(workspace_config, pkg, pkg_state, dry_run=dry_run)
    else:
        return uninstall_single_package_standard(workspace_config, pkg, pkg_state, dry_run=dry_run, no_hooks=no_hooks)


def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    force: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    no_hooks: bool = False
) -> UninstallResult:
    """
    Uninstalls or detaches one or more packages from the system.
    Safeguard: Aborts if the package is still enabled in workspace config unless force=True.
    If package_names is None, uninstalls all orphans.
    Returns the UninstallResult containing details of uninstalled packages.

    Note:
        Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
        triggered if the package configuration file ('drift_package.toml') is available
        in the install/<pkg>/ directory.
    """
    # 1. Load state registry (if exists, otherwise empty)
    state_file = workspace_config.install_path / "state.toml"
    if state_file.exists():
        registry = load_state_registry(state_file)
    else:
        registry = StateRegistry({})

    # 2. Filter packages
    safe_map, rejected_pkgs = filter_uninstallable_packages(workspace_config, registry, package_names, force)

    if rejected_pkgs:
        for pkg in rejected_pkgs:
            logger.error(f"🛡️  [SAFEGUARD] Package '{pkg}' is still active/enabled in workspace configuration.")
        logger.error("   To safely uninstall, first disable it in drift.toml or use --force.")
        raise RuntimeError(f"Safeguard abort: Package(s) {', '.join(rejected_pkgs)} are active.")

    if not safe_map:
        if package_names is not None:
              logger.info("Nothing to uninstall.")
        return UninstallResult(status="SUCCESS", detach_mode=detach, packages=[])

    # Pre-check uninstall hook files and sudo privileges for all packages to be uninstalled
    if not dry_run and not detach:
        from .install_repo import load_config_for_install
        pkg_config_map = { pkg: load_config_for_install(workspace_config.install_path, pkg)
                          for pkg in safe_map
                          if (workspace_config.install_path / pkg / PACKAGE_CONFIG_FILE_NAME).exists() }

        needs_sudo = any(pkg_cfg.sudo for pkg_cfg in pkg_config_map.values() if pkg_cfg)
        if needs_sudo:
            from .file_utils import check_sudo_privilege
            check_sudo_privilege(True)

        if not no_hooks:
            for pkg, pkg_config in pkg_config_map.items():
                if pkg_config and pkg_config.hooks:
                    pkg_config.hooks.check_hook_files(
                            workspace_config.install_path / pkg, hook_names=UNINSTALL_HOOK_NAMES)

    package_results: List[PackageUninstallResult] = []
    successfully_uninstalled: List[str] = []

    for pkg, pkg_state in safe_map.items():
        target_dir, sudo = get_uninstall_metadata(workspace_config, pkg)
        if uninstall_single_package(workspace_config, pkg, pkg_state, dry_run=dry_run, detach=detach, no_hooks=no_hooks):
            if not dry_run:
                registry.remove_package(pkg)
                successfully_uninstalled.append(pkg)
            package_results.append(
                PackageUninstallResult(
                    package=pkg,
                    install_method=pkg_state.install_method or "stow",
                    target_directory=str(target_dir),
                    detach_mode=detach,
                    removed_files=[str(x) for x in pkg_state.deployed_files] if not detach else [],
                    converted_symlinks=[str(x) for x in pkg_state.deployed_files] if detach else [],
                    status="SUCCESS"
                )
            )

    if dry_run:
        return UninstallResult(
            status="SUCCESS",
            detach_mode=detach,
            packages=package_results
        )

    # 3. Save state registry
    save_state_registry(state_file, registry)

    # 4. Commit changes in install repo
    if successfully_uninstalled:
        from .install_repo import run_primitive_6_commit_install_repo
        action_name = "Detach" if detach else "Uninstall"
        commit_msg = f"{action_name}: Removed package(s) {', '.join(successfully_uninstalled)}"
        run_primitive_6_commit_install_repo(workspace_config, commit_msg, successfully_uninstalled)
        if detach:
            logger.info(f"✨ Successfully detached {len(successfully_uninstalled)} package(s)!")
        else:
            logger.info(f"✨ Successfully uninstalled {len(successfully_uninstalled)} package(s)!")
    else:
        logger.info("Nothing was uninstalled.")
        
    return UninstallResult(
        status="SUCCESS",
        detach_mode=detach,
        packages=package_results
    )
