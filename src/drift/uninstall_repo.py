"""Primitive 7: Uninstall package from system and restore backups.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 4: Primitive Entry Point
    run_primitive_7_uninstall_packages(workspace_config, package_names, force, dry_run, detach, flags)
        1. Discover & Filter Target Packages:
            load_state_registry
            filter_uninstallable_packages [Layer 1]
        2. Pre-flight Validation & Privilege Checks:
            load_package_config_for_uninstall [Layer 1] (gather package configs for all targets)
            check_sudo_privilege (if any package config requires sudo)
            check_hook_files (UNINSTALL_HOOK_NAMES, skipped in detach mode)
        3. Execute Single-Package Actions:
            detach_one_package [Layer 3] (if detach=True)
                replace symlinks with physical copies
                clean_up_package_directories [Layer 1]
            uninstall_one_package [Layer 3] (if detach=False)
                pkg_config.package_envs context
                trigger_pre_uninstall (hook)
                remove_deployed_files [Layer 2]
                    resolve_system_target
                    remove_file_or_dir_with_sudo
                    rmdir_parents
                restore_backups [Layer 2]
                    copy_or_move_file_or_dir_external (move=True)
                    rmdir_parents
                trigger_post_uninstall (hook)
                clean_up_package_directories [Layer 1]
                    shutil.rmtree (install/<pkg>)
                    rmdir_parents (backup/<pkg>)
        4. State Registry & Install Repo Synchronization:
            registry.remove_package
            registry.save
            run_primitive_6_commit_install_repo

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Pre-flight & Metadata / Cleanup Helpers
        filter_uninstallable_packages
        load_package_config_for_uninstall
        clean_up_package_directories
    Layer 2: File Operation Helpers
        remove_deployed_files
        restore_backups
    Layer 3: Single-Package Execution Handlers
        detach_one_package
        uninstall_one_package
    Layer 4: Public Primitive Entry Point
        run_primitive_7_uninstall_packages
===============================================================================
"""

import logging
import shutil
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Sequence

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig
from .state_registry import load_state_registry, PackageState, StateRegistry
from .file_utils import (
    check_sudo_privilege,
    remove_file_or_dir_with_sudo,
    rmdir_parents,
    copy_or_move_file_or_dir_external,
    tree_relative_files,
    resolve_system_target,
)
from .constants import UNINSTALL_HOOK_NAMES
from .lifecycle_hooks import HookExecFlags
from .result_models import PackageUninstallResult, UninstallResult

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Pre-flight & Metadata / Cleanup Helpers
# =====================================================================

def filter_uninstallable_packages(
    workspace_config: WorkspaceConfig,
    registry: StateRegistry,
    package_names: Sequence[str] = (),
    force: bool = False
) -> Tuple[Dict[str, PackageState], List[str]]:
    """
    Filters which packages are safe to uninstall according to the workspace configuration and registry.
    Returns (packages_to_uninstall, active_but_rejected_names).
    Force will allow uninstalling even if the package is still enabled in workspace config.
    """
    installed_packages = registry.packages

    if package_names:
        target_names = list(package_names)
    else:
        # If no packages specified, target all installed packages that are NOT enabled in config (orphans)
        target_names = [pkg for pkg in installed_packages
                        if not workspace_config.is_package_enabled(pkg)]

    packages_to_uninstall = {}
    rejected = []

    for pkg in target_names:
        # Check active status FIRST for safeguard
        if workspace_config.is_package_enabled(pkg) and not force:
            rejected.append(pkg)
            continue
            
        if pkg not in installed_packages:
            logger.warning(f"⚠️  Package '{pkg}' is not registered as installed. Skipping.")
            continue
            
        packages_to_uninstall[pkg] = installed_packages[pkg]
            
    return packages_to_uninstall, rejected


def load_package_config_for_uninstall(
    workspace_config: WorkspaceConfig,
    pkg: str
) -> PackageConfig:
    """Loads package configuration from install base, or constructs a default configuration if missing or invalid."""
    try:
        return PackageConfig.from_install_dir(workspace_config.install_path / pkg)
    except Exception as e:
        logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        return PackageConfig(name=pkg)


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
    rmdir_parents(backup_pkg_dir, workspace_config.backup_path)


# =====================================================================
# Layer 2: File Operation Helpers
# =====================================================================

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
        rmdir_parents(backup_pkg_overwritten, workspace_config.backup_path)

    return restored


# =====================================================================
# Layer 3: Single-Package Execution Handlers
# =====================================================================

def detach_one_package(
    workspace_config: WorkspaceConfig,
    pkg_state: PackageState,
    pkg_config: PackageConfig,
    dry_run: bool = False,
) -> bool:
    """Decouples/detaches a single package from Drift, replacing symlinks with physical copies."""
    pkg = pkg_config.name
    if dry_run:
        logger.info(f"🔍 [DRY RUN] Would detach package: {pkg} (replacing symlinks with copies)")
    else:
        logger.info(f"🔌 Detaching package: {pkg} (converting to independent system config)")

    target_dir = pkg_config.get_target_directory(workspace_config)
    sudo = pkg_config.sudo

    for rel_file in pkg_state.deployed_files:
        system_target = resolve_system_target(rel_file, target_dir)
        if not system_target.is_symlink():
            continue
        # Log the file that will be replaced in both dry-run and live modes
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would replace symlink with actual copy: {system_target}")
            continue
        src_file = workspace_config.install_path / pkg / rel_file
        if not src_file.is_file():
            logger.error(f"❌ Source file not found in install/ directory for package '{pkg}': {src_file}")
            continue
        logger.info(f"   Replacing symlink with actual copy: {system_target}")
        remove_file_or_dir_with_sudo(system_target, sudo)
        system_target.parent.mkdir(parents=True, exist_ok=True)
        copy_or_move_file_or_dir_external(src_file, system_target, sudo, move=False)

    if dry_run:
        return True

    logger.info(f"🔌 Successfully detached and converted {pkg} files to independent configurations on the host.")
    clean_up_package_directories(workspace_config, pkg)
    return True


def uninstall_one_package(
    workspace_config: WorkspaceConfig,
    pkg_state: PackageState,
    pkg_config: PackageConfig,
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> bool:
    """Orchestrates standard uninstallation of a single package.

    Note:
        Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
        triggered if the package configuration file ('drift_package.toml') is available
        in the install/<pkg>/ directory.
    """
    pkg = pkg_config.name
    if dry_run:
        logger.info(f"🔍 [DRY RUN] Would uninstall package: {pkg}")
    else:
        logger.info(f"🗑️  Uninstalling package: {pkg}")

    install_pkg_dir = workspace_config.install_path / pkg
    target_dir = pkg_config.get_target_directory(workspace_config)
    sudo = pkg_config.sudo
    hook_flags = HookExecFlags.resolve(flags)

    # Check uninstall hook files exist before attempting uninstallation
    if not dry_run and not hook_flags.no_hooks:
        pkg_config.hooks.check_hook_files(install_pkg_dir, is_source=False, hook_names=UNINSTALL_HOOK_NAMES)

    with pkg_config.package_envs(workspace_config):
        # 1. Trigger pre_uninstall hook (only if drift_package.toml is available)
        if not dry_run and pkg_config.hooks.pre_uninstall:
            pkg_config.hooks.trigger_pre_uninstall(
                flags=hook_flags
            )

        # 2. Remove deployed files
        remove_deployed_files(pkg, pkg_state.deployed_files, target_dir, sudo, dry_run=dry_run)

        # 3. Restore backups
        restore_backups(workspace_config, pkg, target_dir, sudo, dry_run=dry_run)

        if dry_run:
            return True

        # 4. Trigger post_uninstall hook (only if drift_package.toml is available, CWD is install_pkg_dir)
        if pkg_config.hooks.post_uninstall:
            pkg_config.hooks.trigger_post_uninstall(
                flags=hook_flags
            )
        clean_up_package_directories(workspace_config, pkg)
        return True


# =====================================================================
# Layer 4: Public Primitive Entry Point
# =====================================================================

def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    force: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> UninstallResult:
    """Uninstalls or detaches one or more packages from the system (Primitive 7).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to uninstall, or empty/omitted to uninstall all orphans.
        force: If True, bypasses the active package safeguard, allowing uninstallation of packages
            that are still active/enabled in the workspace configuration (drift_workspace.toml).
        dry_run: If True, simulates uninstallation without removing files from disk.
        detach: If True, deregisters packages from Drift tracking while leaving deployed files on disk.
        flags: Optional HookExecFlags controlling hook execution options.

    Returns:
        UninstallResult containing details of uninstalled packages.

    Note:
        Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
        triggered if the package configuration file ('drift_package.toml') is available
        in the install/<pkg>/ directory.
    """
    hook_flags = HookExecFlags.resolve(flags)
    # 1. Load state registry (if exists, otherwise empty)
    state_file = workspace_config.install_path / "state.toml"
    registry = load_state_registry(state_file)

    # 2. Filter packages
    packages_to_uninstall, rejected_pkgs = filter_uninstallable_packages(
        workspace_config, registry, package_names, force=force
    )

    if rejected_pkgs:
        for pkg in rejected_pkgs:
            logger.error(f"🛡️  [SAFEGUARD] Package '{pkg}' is still active/enabled in workspace configuration.")
        logger.error("   To safely uninstall, first disable it in drift_workspace.toml or use --force.")
        raise RuntimeError(f"Safeguard abort: Package(s) {', '.join(rejected_pkgs)} are active.")

    if not packages_to_uninstall:
        if package_names is not None:
            logger.info("Nothing to uninstall.")
        return UninstallResult(status="SUCCESS", detach_mode=detach, packages=[])

    # 3. Gather package configuration for all target packages
    pkg_config_map = {
        pkg: load_package_config_for_uninstall(workspace_config, pkg)
        for pkg in packages_to_uninstall
    }

    # Pre-check sudo privileges and uninstall hook files
    if not dry_run:
        needs_sudo = any(pkg_cfg.sudo for pkg_cfg in pkg_config_map.values())
        if needs_sudo:
            check_sudo_privilege(True)

        if not detach and not hook_flags.no_hooks:
            for pkg, pkg_config in pkg_config_map.items():
                pkg_config.hooks.check_hook_files(
                    workspace_config.install_path / pkg, is_source=False, hook_names=UNINSTALL_HOOK_NAMES
                )

    package_results: List[PackageUninstallResult] = []
    successfully_uninstalled: List[str] = []

    for pkg, pkg_state in packages_to_uninstall.items():
        pkg_config = pkg_config_map[pkg]
        if detach:
            success = detach_one_package(
                workspace_config, pkg_state, pkg_config, dry_run=dry_run
            )
        else:
            success = uninstall_one_package(
                workspace_config, pkg_state, pkg_config, dry_run=dry_run, flags=hook_flags
            )

        if success:
            successfully_uninstalled.append(pkg)
            if not dry_run:
                registry.remove_package(pkg)
            target_dir = pkg_config.get_target_directory(workspace_config)
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

    # 4. Save state registry & commit changes in install repo
    if not dry_run:
        if successfully_uninstalled:
            registry.save()
            from .install_repo import run_primitive_6_commit_install_repo
            action_name = "Detach" if detach else "Uninstall"
            commit_msg = f"{action_name}: Removed package(s) {', '.join(successfully_uninstalled)}"
            run_primitive_6_commit_install_repo(workspace_config, commit_msg, successfully_uninstalled)
            logger.info(f"✨ Successfully {action_name.lower()}ed {len(successfully_uninstalled)} package(s)!")
        else:
            logger.info("Nothing was uninstalled.")

    return UninstallResult(
        status="SUCCESS",
        detach_mode=detach,
        packages=package_results
    )
