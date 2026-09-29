"""Primitive 7: Uninstall package from system and restore backups.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-flight Preparation & Assertion (Read-Only):
        prepare_uninstall_packages(workspace_config, package_names, config) [Layer 4]
            - Discovery & Safeguard Filter (filter_uninstallable_packages) [Layer 1]
            - Metadata Gathering (load_package_config_for_uninstall) [Layer 1]
            - Pre-flight Readiness (assert_packages_uninstall_ready [Layer 4])
                * Dependency Integrity (assert_no_broken_dependencies_on_uninstall)
                * Escalation Privilege (assert_can_escalate)
                * Lifecycle Hook Existence (assert_packages_hooks_exist)
            - Reverse Topological Order Resolution (resolve_package_uninstall_order)
            -> Returns UninstallPlan(pkg_config_map, packages_to_uninstall, state_registry, ordered_packages, config)

    2. Single-Package Execution & Coordination (State-Mutating):
        execute_uninstall_packages(workspace_config, plan: UninstallPlan) [Layer 4]
            - Iterates over plan.ordered_packages:
                * Missing Install Directory -> uninstall_missing_package [Layer 3]
                * Detach Mode -> detach_one_package [Layer 3]
                * Standard Uninstall -> uninstall_one_package [Layer 3]
            - State Registry & Install Repo Synchronization:
                * state_registry.remove_package & state_registry.save
                * run_primitive_6_commit_install_repo
            -> Returns Aggregated UninstallResult

    3. Public Primitive Entry Point:
        run_primitive_7_uninstall_packages(workspace_config, package_names=(), config=None) [Layer 5]
            = prepare_uninstall_packages >> execute_uninstall_packages

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
        uninstall_missing_package
    Layer 4: Batch Uninstall Pipelines & Preparation
        UninstallConfig
        UninstallPlan
        assert_packages_uninstall_ready
        prepare_uninstall_packages
        execute_uninstall_packages
    Layer 5: Public Primitive Entry Point
        run_primitive_7_uninstall_packages
===============================================================================
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Sequence, Mapping

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig, PackageSectionConfig
from ..core.state_registry import load_state_registry, PackageState, StateRegistry
from .package_assertions import (
    assert_packages_hooks_exist,
    assert_no_broken_dependencies_on_uninstall,
    resolve_package_uninstall_order,
)
from ..utils.process_utils import assert_can_escalate
from ..utils.file_ops import (
    remove,
    prune_empty_parents,
    copy_tree,
    move_tree,
)
from ..utils.file_inspect import tree_files
from ..utils.path_utils import resolve_target_path
from ..core.constants import UNINSTALL_HOOK_NAMES, BackupSubfolder, InstallMethod
from ..hooks.lifecycle_hooks import HookExecFlags
from ..core.result_models import PackageUninstallResult, UninstallResult, RestoredBackup

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
        return PackageConfig.from_install_dir(workspace_config.install_path / pkg, workspace_config)
    except Exception as e:
        logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        return PackageConfig(PackageSectionConfig(name=pkg))


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
    prune_empty_parents(backup_pkg_dir, workspace_config.backup_path)


# =====================================================================
# Layer 2: File Operation Helpers
# =====================================================================

def remove_deployed_files(
    pkg: str,
    deployed_files: List[Path],
    target_dir: Path,
    sudo: bool,
    dry_run: bool = False
) -> List[Tuple[Path, Path]]:
    """Removes deployed files from the system. Returns list of (rel_file, system_target) tuples for removed files."""
    removed: List[Tuple[Path, Path]] = []
    # Sort in reverse to handle nested files/dirs (files before their parent dirs)
    for rel_file in sorted(deployed_files, reverse=True):
        system_target = resolve_target_path(rel_file, target_dir)
        
        if system_target.exists() or system_target.is_symlink():
            if dry_run:
                logger.info(f"🔍 [DRY RUN] Would remove: {system_target}")
            else:
                logger.debug(f"   Removing: {system_target}")
                remove(system_target, sudo)
                # Cleanup empty parent dirs up to target_dir
                prune_empty_parents(system_target.parent, target_dir)
            removed.append((rel_file, system_target))
    
    if not dry_run and removed:
        logger.info(f"🧹 Cleaned up {len(removed)} deployed file(s) for {pkg}")
    return removed


def restore_backups(
    workspace_config: WorkspaceConfig,
    pkg: str,
    target_dir: Path,
    sudo: bool,
    dry_run: bool = False
) -> List[RestoredBackup]:
    """Restores backups for a package. Returns list of RestoredBackup records."""
    restored: List[RestoredBackup] = []
    backup_pkg_overwritten = workspace_config.backup_path / pkg / BackupSubfolder.OVERWRITTEN.value
    if not backup_pkg_overwritten.exists():
        return restored  # No backups to restore

    # We assume we can just move symlinks in the backup without resolving,
    # so we can safely use tree_files
    backup_files = tree_files(backup_pkg_overwritten)
    if not backup_files:
        return restored  # No backups to restore

    if not dry_run:
        logger.info(f"🔄 Restoring backups for {pkg}...")
    
    for rel_backup in backup_files:
        src = backup_pkg_overwritten / rel_backup
        system_target = resolve_target_path(rel_backup, target_dir)
        
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would restore: {system_target}")
        else:
            logger.debug(f"   Restoring: {system_target}")
            # Use move=True to clean up backup as we restore it
            move_tree(src, system_target, sudo)
        restored.append(RestoredBackup(source_backup=str(src), restored_to=str(system_target)))
    
    if not dry_run:
        logger.info(f"✨ Restored {len(restored)} file(s) for {pkg}")
        # Clean up the 'overwritten' directory if it's now empty
        prune_empty_parents(backup_pkg_overwritten, workspace_config.backup_path)

    return restored


# =====================================================================
# Layer 3: Single-Package Execution Handlers
# =====================================================================

def detach_one_package(
    workspace_config: WorkspaceConfig,
    pkg_state: PackageState,
    pkg_config: PackageConfig,
    dry_run: bool = False,
) -> PackageUninstallResult:
    """Decouples/detaches a single package from Drift, replacing symlinks with physical copies."""
    pkg = pkg_config.name
    if dry_run:
        logger.info(f"🔍 [DRY RUN] Would detach package: {pkg} (replacing symlinks with copies)")
    else:
        logger.info(f"🔌 Detaching package: {pkg} (converting to independent system config)")

    target_dir = (
        pkg_state.target_directory
        if pkg_state.target_directory is not None
        else pkg_config.get_target_directory(workspace_config)
    )
    sudo = pkg_config.package.sudo
    converted_symlinks = []

    for rel_file in pkg_state.deployed_files:
        system_target = resolve_target_path(rel_file, target_dir)
        if not system_target.is_symlink():
            continue
        # Log the file that will be replaced in both dry-run and live modes
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would replace symlink with actual copy: {system_target}")
            converted_symlinks.append(str(rel_file))
            continue
        src_file = workspace_config.install_path / pkg / rel_file
        if not src_file.is_file():
            logger.error(f"❌ Source file not found in install/ directory for package '{pkg}': {src_file}")
            continue
        logger.info(f"   Replacing symlink with actual copy: {system_target}")
        remove(system_target, sudo)
        system_target.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(src_file, system_target, sudo)
        converted_symlinks.append(str(rel_file))

    if not dry_run:
        logger.info(f"🔌 Successfully detached and converted {pkg} files to independent configurations on the host.")
        clean_up_package_directories(workspace_config, pkg)

    return PackageUninstallResult(
        package=pkg,
        install_method=pkg_state.install_method or InstallMethod.STOW,
        target_directory=str(target_dir),
        detach_mode=True,
        removed_files=[],
        converted_symlinks=converted_symlinks,
        restored_backups=[],
        status="SUCCESS",
    )


def uninstall_one_package(
    workspace_config: WorkspaceConfig,
    pkg_state: PackageState,
    pkg_config: PackageConfig,
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> PackageUninstallResult:
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
    target_dir = (
        pkg_state.target_directory
        if pkg_state.target_directory is not None
        else pkg_config.get_target_directory(workspace_config)
    )
    sudo = pkg_config.package.sudo
    hook_flags = HookExecFlags.resolve(flags, settings=workspace_config.settings)

    # Check uninstall hook files exist before attempting uninstallation
    if not dry_run and not hook_flags.no_hooks:
        pkg_config.hooks.assert_hooks_exist(install_pkg_dir, is_source=False, hook_names=UNINSTALL_HOOK_NAMES)

    with pkg_config.package_envs():
        # 1. Trigger pre_uninstall hook (only if drift_package.toml is available)
        if not dry_run and pkg_config.hooks.pre_uninstall:
            pkg_config.hooks.trigger_pre_uninstall(
                flags=hook_flags
            )

        # 2. Remove deployed files
        removed = remove_deployed_files(pkg, pkg_state.deployed_files, target_dir, sudo, dry_run=dry_run)

        # 3. Restore backups
        restored = restore_backups(workspace_config, pkg, target_dir, sudo, dry_run=dry_run)

        if not dry_run:
            # 4. Trigger post_uninstall hook (only if drift_package.toml is available, CWD is install_pkg_dir)
            if pkg_config.hooks.post_uninstall:
                pkg_config.hooks.trigger_post_uninstall(
                    flags=hook_flags
                )
            clean_up_package_directories(workspace_config, pkg)

        return PackageUninstallResult(
            package=pkg,
            install_method=pkg_state.install_method or InstallMethod.STOW,
            target_directory=str(target_dir),
            detach_mode=False,
            removed_files=[str(rel) for rel, _ in removed],
            converted_symlinks=[],
            restored_backups=restored,
            status="SUCCESS",
        )


def uninstall_missing_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_state: PackageState,
    dry_run: bool = False,
    detach: bool = False,
) -> PackageUninstallResult:
    """Handles uninstallation and cleanup for a package whose directory is missing in the install repository.

    Args:
        workspace_config: The workspace configuration instance.
        pkg: Name of the missing package.
        pkg_state: Recorded PackageState from state registry.
        dry_run: If True, simulates actions without modifying the filesystem.
        detach: Whether uninstallation was requested in detach mode.

    Returns:
        PackageUninstallResult indicating successful cleanup of missing package record.
    """
    logger.warning(
        f"⚠️  Package directory not found in install repository for '{pkg}'. "
        f"Removing entry from state registry."
    )
    if not dry_run:
        clean_up_package_directories(workspace_config, pkg)

    return PackageUninstallResult(
        package=pkg,
        install_method=pkg_state.install_method or InstallMethod.STOW,
        target_directory=str(pkg_state.target_directory or ""),
        detach_mode=detach,
        removed_files=[],
        converted_symlinks=[],
        restored_backups=[],
        status="SUCCESS",
    )


# =====================================================================
# Layer 4: Batch Uninstall Pipelines & Preparation
# =====================================================================

@dataclass
class UninstallConfig:
    """Configuration options controlling package uninstallation behavior."""
    force: bool = False
    dry_run: bool = False
    detach: bool = False
    ignore_missing_dependencies: bool = False
    flags: Optional[HookExecFlags] = None


@dataclass(frozen=True)
class UninstallPlan:
    """Pre-flight validated uninstallation plan containing package configurations and state registry."""
    pkg_config_map: Dict[str, PackageConfig]
    packages_to_uninstall: Dict[str, PackageState]
    state_registry: StateRegistry
    ordered_packages: List[str]
    config: UninstallConfig


def assert_packages_uninstall_ready(
    workspace_config: WorkspaceConfig,
    packages_to_uninstall: Mapping[str, PackageState],
    pkg_config_map: Mapping[str, PackageConfig],
    state_registry: StateRegistry,
    config: UninstallConfig,
) -> None:
    """Pre-flight checks for dependencies, sudo permissions, and uninstall hooks before uninstallation.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_uninstall: Mapping of package names to recorded PackageState for uninstallation targets.
        pkg_config_map: Mapping of package names to loaded PackageConfig.
        state_registry: Active StateRegistry instance.
        config: UninstallConfig containing flags and execution modes.

    Raises:
        ConfigError: If removing targeted packages breaks dependencies of remaining installed packages.
        SubprocessError: If sudo escalation is required but unavailable.
        HookMissingError: If any configured uninstall hook files are missing.
    """
    hook_flags = HookExecFlags.resolve(config.flags, settings=workspace_config.settings)

    # 1. Dependency integrity check on remaining installed packages
    if not (config.force or config.ignore_missing_dependencies):
        all_installed = [pkg for pkg, _ in state_registry.filter_by_states(["installed"])]
        remaining_pkgs = set(all_installed) - set(packages_to_uninstall.keys())
        remaining_metadata = {
            pkg: (
                PackageConfig.from_install_dir(workspace_config.install_path / pkg, workspace_config)
                if (workspace_config.install_path / pkg).is_dir()
                else PackageConfig(PackageSectionConfig(name=pkg))
            )
            for pkg in remaining_pkgs
        }
        assert_no_broken_dependencies_on_uninstall(
            packages_to_uninstall=packages_to_uninstall.keys(),
            remaining_metadata=remaining_metadata,
        )

    # 2. Host and hook pre-checks (skipped in dry-run)
    if not config.dry_run:
        needs_sudo = any(pkg_cfg.package.sudo for pkg_cfg in pkg_config_map.values())
        if needs_sudo:
            assert_can_escalate()

        if not config.detach and not hook_flags.no_hooks:
            assert_packages_hooks_exist(
                pkg_config_map,
                workspace_config.install_path,
                is_source=False,
                hook_names=UNINSTALL_HOOK_NAMES,
            )


def prepare_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    config: Optional[UninstallConfig] = None,
) -> UninstallPlan:
    """Discovers, validates, and prepares packages for uninstallation or detachment.

    Runs pre-flight assertion guards (safeguards, dependency integrity, sudo escalation, hook existence)
    and computes topologically sorted uninstallation order (reverse of installation order).
    Does NOT modify the filesystem, remove deployed files, or mutate the state registry.

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to uninstall, or empty sequence for all orphans.
        config: Optional UninstallConfig controlling uninstallation behavior.

    Returns:
        UninstallPlan containing validated package configs, packages to uninstall, state registry,
        ordered packages, and resolved config.

    Raises:
        RuntimeError: If targeted packages are active/enabled in workspace config and force is False.
        ConfigError: If removing targeted packages breaks dependencies of remaining installed packages.
        SubprocessError: If sudo escalation is required but unavailable.
        HookMissingError: If any configured uninstall hook files are missing.
    """
    cfg = config if config is not None else UninstallConfig()

    # 1. Load state registry (if exists, otherwise empty)
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    # 2. Filter target packages and validate safeguards
    packages_to_uninstall, rejected_pkgs = filter_uninstallable_packages(
        workspace_config, state_registry, package_names, force=cfg.force
    )

    if rejected_pkgs:
        for pkg in rejected_pkgs:
            logger.error(f"🛡️  [SAFEGUARD] Package '{pkg}' is still active/enabled in workspace configuration.")
        logger.error("   To safely uninstall, first disable it in drift_workspace.toml or use --force.")
        raise RuntimeError(f"Safeguard abort: Package(s) {', '.join(rejected_pkgs)} are active.")

    if not packages_to_uninstall:
        return UninstallPlan(
            pkg_config_map={},
            packages_to_uninstall={},
            state_registry=state_registry,
            ordered_packages=[],
            config=cfg,
        )

    # 3. Gather package configuration for all target packages
    pkg_config_map = {
        pkg: load_package_config_for_uninstall(workspace_config, pkg)
        for pkg in packages_to_uninstall
    }

    # 4. Pre-flight checks on dependencies, sudo escalation, and lifecycle hooks
    assert_packages_uninstall_ready(
        workspace_config=workspace_config,
        packages_to_uninstall=packages_to_uninstall,
        pkg_config_map=pkg_config_map,
        state_registry=state_registry,
        config=cfg,
    )

    # 5. Resolve reverse topological order for uninstallation
    uninstall_deps = {pkg: meta.package.dependencies for pkg, meta in pkg_config_map.items()}
    ordered_packages = resolve_package_uninstall_order(uninstall_deps)

    return UninstallPlan(
        pkg_config_map=pkg_config_map,
        packages_to_uninstall=packages_to_uninstall,
        state_registry=state_registry,
        ordered_packages=ordered_packages,
        config=cfg,
    )


def execute_uninstall_packages(
    workspace_config: WorkspaceConfig,
    plan: UninstallPlan,
) -> UninstallResult:
    """Executes uninstallation or detachment of packages according to the validated UninstallPlan.

    Handles missing package directory cleanup, physical file removal, backup restoration,
    state registry updates, and Git state commits.

    Args:
        workspace_config: The workspace configuration instance.
        plan: The validated UninstallPlan containing packages, registry, configs, and order.

    Returns:
        UninstallResult containing details of uninstalled packages.
    """
    cfg = plan.config
    hook_flags = HookExecFlags.resolve(cfg.flags, settings=workspace_config.settings)
    package_results: List[PackageUninstallResult] = []
    successfully_uninstalled: List[str] = []

    for pkg in plan.ordered_packages:
        pkg_state = plan.packages_to_uninstall[pkg]
        pkg_config = plan.pkg_config_map[pkg]

        # Graceful handling for targeted packages whose directory is missing in install repo
        if not (workspace_config.install_path / pkg).is_dir():
            pkg_res = uninstall_missing_package(
                workspace_config=workspace_config,
                pkg=pkg,
                pkg_state=pkg_state,
                dry_run=cfg.dry_run,
                detach=cfg.detach,
            )
        elif cfg.detach:
            pkg_res = detach_one_package(
                workspace_config, pkg_state, pkg_config, dry_run=cfg.dry_run
            )
        else:
            pkg_res = uninstall_one_package(
                workspace_config, pkg_state, pkg_config, dry_run=cfg.dry_run, flags=hook_flags
            )

        if pkg_res.status == "SUCCESS":
            successfully_uninstalled.append(pkg)
            if not cfg.dry_run:
                plan.state_registry.remove_package(pkg)
            package_results.append(pkg_res)

    # Save state registry & commit changes in install repo
    if not cfg.dry_run:
        if successfully_uninstalled:
            plan.state_registry.save()
            from .install_repo import run_primitive_6_commit_install_repo
            action_name = "Detach" if cfg.detach else "Uninstall"
            pkg_word = "package" if len(successfully_uninstalled) == 1 else "packages"
            commit_msg = f"{action_name}: Removed {pkg_word} {', '.join(successfully_uninstalled)}"
            run_primitive_6_commit_install_repo(workspace_config, commit_msg, successfully_uninstalled)
            logger.info(f"✨ Successfully {action_name.lower()}ed {len(successfully_uninstalled)} {pkg_word}!")
        else:
            logger.info("Nothing was uninstalled.")

    return UninstallResult(
        status="SUCCESS",
        detach_mode=cfg.detach,
        packages=package_results,
    )


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    config: Optional[UninstallConfig] = None,
) -> UninstallResult:
    """Uninstalls or detaches one or more packages from the system (Primitive 7).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to uninstall, or empty/omitted to uninstall all orphans.
        config: Optional UninstallConfig controlling uninstallation behavior.

    Returns:
        UninstallResult containing details of uninstalled packages.

    Note:
        Package uninstall lifecycle hooks (pre_uninstall and post_uninstall) are only
        triggered if the package configuration file ('drift_package.toml') is available
        in the install/<pkg>/ directory.
    """
    plan = prepare_uninstall_packages(
        workspace_config=workspace_config,
        package_names=package_names,
        config=config,
    )
    if not plan.packages_to_uninstall:
        if package_names:
            logger.info("Nothing to uninstall.")
        return UninstallResult(status="SUCCESS", detach_mode=plan.config.detach, packages=[])
    return execute_uninstall_packages(workspace_config, plan)
