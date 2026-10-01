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
            - Autonomous Context Construction (PackageUninstallContext.from_package_state)
            - Declarative Plan Compilation (plan_package_uninstall)
            -> Returns UninstallPlan(pkg_config_map, packages_to_uninstall, state_registry, ordered_packages, config, contexts, package_plans)

    2. Single-Package Execution & Coordination:
        execute_uninstall_packages(workspace_config, plan: UninstallPlan) [Layer 4]
            - If dry_run -> compiles inspectable result summaries (zero host mutations)
            - Iterates over plan.ordered_packages:
                execute_package_uninstall [Layer 3]
                    * Missing Install Directory -> clean_up_package_directories
                    * Detach Mode -> execute_deployment_actions (removes symlinks, copies concrete files)
                    * Standard Uninstall -> pre_uninstall hook -> execute_deployment_actions -> post_uninstall hook
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
    Layer 2: Plan Generation & Single-Package Execution Handlers
        plan_package_uninstall
        uninstall_missing_package
        detach_one_package
        uninstall_one_package
        execute_package_uninstall
    Layer 3: Batch Uninstall Pipelines & Preparation
        UninstallConfig
        UninstallPlan
        PackageUninstallContext
        assert_packages_uninstall_ready
        prepare_uninstall_packages
        execute_uninstall_packages
    Layer 4: Public Primitive Entry Point
        run_primitive_7_uninstall_packages
===============================================================================
"""

from __future__ import annotations

import logging
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ..config.package_config import PackageConfig, PackageSectionConfig
from ..config.package_hooks import PackageHooks
from ..config.workspace_config import WorkspaceConfig
from ..core.constants import (
    DEFAULT_INSTALL_METHOD,
    UNINSTALL_HOOK_NAMES,
    BackupSubfolder,
    InstallMethod,
)
from ..core.folder_deployment import (
    ActionExecutionContext,
    ActionType,
    PlannedFileAction,
    execute_deployment_actions,
    plan_backup_restoration,
    plan_file_removals,
    plan_symlink_conversions,
)
from ..core.result_models import PackageUninstallPlan, PackageUninstallResult, RestoredBackup, UninstallResult
from ..core.state_registry import PackageState, StateRegistry, load_state_registry
from ..hooks.lifecycle_hooks import HookExecFlags
from ..utils.file_ops import prune_empty_parents, remove
from ..utils.process_utils import assert_can_escalate
from .package_assertions import (
    assert_no_broken_dependencies_on_uninstall,
    assert_packages_hooks_exist,
    resolve_package_uninstall_order,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Uninstall Data Structures & Contexts
# =====================================================================

@dataclass
class UninstallConfig:
    """Configuration options controlling package uninstallation behavior."""
    force: bool = False
    dry_run: bool = False
    detach: bool = False
    no_deps: bool = False
    flags: Optional[HookExecFlags] = None


@dataclass(frozen=True)
class PackageUninstallContext:
    """Encapsulates resolved package metadata and filesystem paths for uninstallation operations."""
    pkg_name: str
    target_dir: Path
    install_method: InstallMethod
    deployed_files: List[Path]
    sudo: bool
    install_pkg_dir: Path
    backup_pkg_dir: Path
    drift_root: Path
    hooks: Optional[PackageHooks] = None
    detach: bool = False
    is_missing_install_dir: bool = False

    @property
    def action_context(self) -> ActionExecutionContext:
        return ActionExecutionContext(
            target_dir=self.target_dir,
            install_pkg_dir=self.install_pkg_dir,
            backup_pkg_dir=self.backup_pkg_dir,
            sudo=self.sudo,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
        )

    @contextmanager
    def package_envs(self) -> Iterator[None]:
        """Context manager to activate package-specific environment variables for hooks."""
        if self.hooks and getattr(self.hooks, "_package_config", None):
            with self.hooks._package_config.package_envs():
                yield
        else:
            from ..utils.env_utils import env_scope
            with env_scope({"drift_package_name": self.pkg_name}):
                yield

    @classmethod
    def from_package_state(
        cls,
        workspace_config: WorkspaceConfig,
        pkg: str,
        pkg_state: PackageState,
        pkg_config: Optional[PackageConfig] = None,
        detach: bool = False,
    ) -> "PackageUninstallContext":
        install_pkg_dir = workspace_config.install_path / pkg
        is_missing = not install_pkg_dir.is_dir()
        target_dir = (
            pkg_state.target_directory
            if pkg_state.target_directory is not None
            else (pkg_config.get_target_directory(workspace_config) if pkg_config else Path("/"))
        )
        # Sudo resolution: prefer state registry (authoritative historical deployment), fallback to pkg_config
        sudo = pkg_state.sudo if pkg_state.sudo else (pkg_config.package.sudo if pkg_config else False)
        install_method = pkg_state.install_method or DEFAULT_INSTALL_METHOD
        hooks = pkg_config.hooks if pkg_config and not is_missing else None

        return cls(
            pkg_name=pkg,
            target_dir=target_dir,
            install_method=install_method,
            deployed_files=list(pkg_state.deployed_files),
            sudo=sudo,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=workspace_config.backup_path / pkg,
            drift_root=workspace_config.drift_root,
            hooks=hooks,
            detach=detach,
            is_missing_install_dir=is_missing,
        )


@dataclass(frozen=True)
class UninstallPlan:
    """Pre-flight validated uninstallation plan containing package configurations and state registry."""
    pkg_config_map: Dict[str, PackageConfig]
    packages_to_uninstall: Dict[str, PackageState]
    state_registry: StateRegistry
    ordered_packages: List[str]
    config: UninstallConfig
    contexts: Dict[str, PackageUninstallContext] = field(default_factory=dict)
    package_plans: Dict[str, PackageUninstallPlan] = field(default_factory=dict)


# =====================================================================
# Layer 1: Pre-flight & Metadata / Cleanup Helpers
# =====================================================================

def filter_uninstallable_packages(
    workspace_config: WorkspaceConfig,
    registry: StateRegistry,
    package_names: Sequence[str] = (),
    force: bool = False,
) -> Tuple[Dict[str, PackageState], List[str]]:
    """Filters which packages are safe to uninstall according to the workspace configuration and registry.

    Returns (packages_to_uninstall, active_but_rejected_names).
    Force will allow uninstalling even if the package is still enabled in workspace config.
    """
    installed_packages = registry.packages

    if package_names:
        target_names = list(package_names)
    else:
        # If no packages specified, target all installed packages that are NOT enabled in config (orphans)
        target_names = [pkg for pkg in installed_packages if not workspace_config.is_package_enabled(pkg)]

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
    pkg: str,
) -> PackageConfig:
    """Loads package configuration from install base, or constructs a default configuration if missing or invalid."""
    try:
        return PackageConfig.from_install_dir(workspace_config.install_path / pkg, workspace_config)
    except Exception as e:
        logger.warning(f"   Failed to load package config for '{pkg}': {e}. Using defaults.")
        return PackageConfig(PackageSectionConfig(name=pkg))


def clean_up_package_directories(context: PackageUninstallContext) -> None:
    """Cleans up the package directory under install_path and empty package directory under backup_path."""
    if context.install_pkg_dir.exists():
        try:
            shutil.rmtree(context.install_pkg_dir)
        except Exception as e:
            logger.warning(f"   Failed to clean up install directory {context.install_pkg_dir}: {e}")

    prune_empty_parents(context.backup_pkg_dir, context.backup_pkg_dir.parent)


# =====================================================================
# Layer 2: Plan Generation & Single-Package Execution Handlers
# =====================================================================

def plan_package_uninstall(
    context: PackageUninstallContext,
) -> PackageUninstallPlan:
    """Compiles a deterministic PackageUninstallPlan without modifying host files or registry."""
    actions: List[PlannedFileAction] = []
    hooks_to_trigger: List[str] = []

    if context.is_missing_install_dir:
        return PackageUninstallPlan(
            package=context.pkg_name,
            target_directory=str(context.target_dir),
            install_method=context.install_method,
            detach_mode=context.detach,
            actions=[],
            hooks_to_trigger=[],
        )

    if context.detach:
        actions.extend(
            plan_symlink_conversions(
                deployed_files=context.deployed_files,
                target_dir=context.target_dir,
                install_pkg_dir=context.install_pkg_dir,
                drift_root=context.drift_root,
            )
        )
    else:
        # Standard uninstall: hooks + file removals + backup restoration
        if context.hooks and (context.hooks.pre_uninstall or context.hooks.post_uninstall):
            if context.hooks.pre_uninstall:
                hooks_to_trigger.append("pre_uninstall")
            if context.hooks.post_uninstall:
                hooks_to_trigger.append("post_uninstall")

        actions.extend(
            plan_file_removals(
                deployed_files=context.deployed_files,
                target_dir=context.target_dir,
            )
        )
        backup_overwritten = context.backup_pkg_dir / BackupSubfolder.OVERWRITTEN.value
        actions.extend(
            plan_backup_restoration(
                backup_overwritten_dir=backup_overwritten,
                target_dir=context.target_dir,
                drift_root=context.drift_root,
            )
        )

    return PackageUninstallPlan(
        package=context.pkg_name,
        target_directory=str(context.target_dir),
        install_method=context.install_method,
        detach_mode=context.detach,
        actions=actions,
        hooks_to_trigger=hooks_to_trigger,
    )


def uninstall_missing_package(
    context: PackageUninstallContext,
    plan: PackageUninstallPlan,
    dry_run: bool = False,
) -> PackageUninstallResult:
    """Handles uninstallation and cleanup for a package whose directory is missing in the install repository."""
    if not dry_run:
        logger.warning(
            f"⚠️  Package directory not found in install repository for '{context.pkg_name}'. "
            f"Removing entry from state registry."
        )
        clean_up_package_directories(context)
    return PackageUninstallResult(
        plan=plan,
        removed_files=[],
        converted_symlinks=[],
        restored_backups=[],
        status="SUCCESS",
    )


def detach_one_package(
    context: PackageUninstallContext,
    plan: PackageUninstallPlan,
    dry_run: bool = False,
) -> PackageUninstallResult:
    """Decouples/detaches a single package from Drift, replacing symlinks with physical copies."""
    converted = [str(a.rel_path) for a in plan.converted]
    if not dry_run:
        logger.info(f"🔌 Detaching package: {context.pkg_name} (converting to independent system config)")
        execute_deployment_actions(context.action_context, plan.actions)
        logger.info(f"🔌 Successfully detached and converted {context.pkg_name} files to independent configurations on the host.")
        clean_up_package_directories(context)
    return PackageUninstallResult(
        plan=plan,
        removed_files=[],
        converted_symlinks=converted,
        restored_backups=[],
        status="SUCCESS",
    )


def uninstall_one_package(
    context: PackageUninstallContext,
    plan: PackageUninstallPlan,
    hook_flags: HookExecFlags,
    dry_run: bool = False,
) -> PackageUninstallResult:
    """Orchestrates standard uninstallation of a single package."""
    removed = [str(a.rel_path) for a in plan.removed]
    restored = [
        RestoredBackup(
            source_backup=str(a.source_path or (context.backup_pkg_dir / BackupSubfolder.OVERWRITTEN.value / a.rel_path)),
            restored_to=str(a.system_target),
        )
        for a in plan.restored
    ]
    if not dry_run:
        logger.info(f"🗑️  Uninstalling package: {context.pkg_name}")
        with context.package_envs():
            # 1. Pre-uninstall hook
            if context.hooks and context.hooks.pre_uninstall:
                context.hooks.trigger_pre_uninstall(flags=hook_flags)

            # 2. Execute plan actions (removes deployed files + recreates directories + copies restored backups)
            execute_deployment_actions(context.action_context, plan.actions)

            # 3. Clean up restored backup files from backup store
            backup_overwritten = context.backup_pkg_dir / BackupSubfolder.OVERWRITTEN.value
            if backup_overwritten.is_dir():
                for a in plan.restored:
                    backup_file = a.source_path or (backup_overwritten / a.rel_path)
                    if backup_file.exists():
                        remove(backup_file, context.sudo)
                prune_empty_parents(backup_overwritten, context.backup_pkg_dir)

            # 4. Post-uninstall hook
            if context.hooks and context.hooks.post_uninstall:
                context.hooks.trigger_post_uninstall(flags=hook_flags)

        clean_up_package_directories(context)

    return PackageUninstallResult(
        plan=plan,
        removed_files=removed,
        converted_symlinks=[],
        restored_backups=restored,
        status="SUCCESS",
    )


def execute_package_uninstall(
    context: PackageUninstallContext,
    plan: PackageUninstallPlan,
    hook_flags: HookExecFlags,
    dry_run: bool = False,
) -> PackageUninstallResult:
    """Applies planned actions deterministically to host and returns PackageUninstallResult."""
    if context.is_missing_install_dir:
        return uninstall_missing_package(context, plan, dry_run=dry_run)

    if context.detach:
        return detach_one_package(context, plan, dry_run=dry_run)

    return uninstall_one_package(context, plan, hook_flags=hook_flags, dry_run=dry_run)


# =====================================================================
# Layer 3: Batch Uninstall Pipelines & Preparation
# =====================================================================

def assert_packages_uninstall_ready(
    workspace_config: WorkspaceConfig,
    packages_to_uninstall: Mapping[str, PackageState],
    pkg_config_map: Mapping[str, PackageConfig],
    state_registry: StateRegistry,
    config: UninstallConfig,
) -> None:
    """Pre-flight checks for dependencies, sudo permissions, and uninstall hooks before uninstallation.

    Raises:
        ConfigError: If removing targeted packages breaks dependencies of remaining installed packages.
        SubprocessError: If sudo escalation is required but unavailable.
        HookMissingError: If any configured uninstall hook files are missing.
    """
    hook_flags = HookExecFlags.resolve(config.flags, settings=workspace_config.settings)

    # 1. Dependency integrity check on remaining installed packages
    if not (config.force or config.no_deps):
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
        needs_sudo = any(
            (state.sudo or (pkg_config_map[pkg].package.sudo if pkg in pkg_config_map else False))
            for pkg, state in packages_to_uninstall.items()
        )
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

    Runs pre-flight assertion guards (safeguards, dependency integrity, sudo escalation, hook existence),
    computes topologically sorted uninstallation order (reverse of installation order), compiles autonomous
    PackageUninstallContext objects, and generates declarative PackageUninstallPlan structures.
    Does NOT modify the filesystem, remove deployed files, or mutate the state registry.
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

    # 6. Build autonomous contexts and compile uninstallation plans
    contexts = {
        pkg: PackageUninstallContext.from_package_state(
            workspace_config=workspace_config,
            pkg=pkg,
            pkg_state=packages_to_uninstall[pkg],
            pkg_config=pkg_config_map.get(pkg),
            detach=cfg.detach,
        )
        for pkg in ordered_packages
    }

    package_plans = {
        pkg: plan_package_uninstall(contexts[pkg])
        for pkg in ordered_packages
    }

    return UninstallPlan(
        pkg_config_map=pkg_config_map,
        packages_to_uninstall=packages_to_uninstall,
        state_registry=state_registry,
        ordered_packages=ordered_packages,
        config=cfg,
        contexts=contexts,
        package_plans=package_plans,
    )


def execute_uninstall_packages(
    workspace_config: WorkspaceConfig,
    plan: UninstallPlan,
) -> UninstallResult:
    """Executes uninstallation or detachment of packages according to the validated UninstallPlan.

    In dry_run mode, compiles inspectable result models with zero host or registry mutations.
    """
    cfg = plan.config
    package_results: List[PackageUninstallResult] = []
    successfully_uninstalled: List[str] = []

    if cfg.dry_run:
        logger.info(f"🔍 [DRY-RUN] Simulating uninstallation for {len(plan.ordered_packages)} package(s).")

    hook_flags = HookExecFlags.resolve(cfg.flags, settings=workspace_config.settings)

    for pkg in plan.ordered_packages:
        ctx = plan.contexts[pkg]
        pkg_plan = plan.package_plans[pkg]

        pkg_res = execute_package_uninstall(
            context=ctx,
            plan=pkg_plan,
            hook_flags=hook_flags,
            dry_run=cfg.dry_run,
        )
        package_results.append(pkg_res)

        if not cfg.dry_run and pkg_res.status == "SUCCESS":
            successfully_uninstalled.append(pkg)
            plan.state_registry.remove_package(pkg)

    if cfg.dry_run:
        return UninstallResult(
            status="SUCCESS",
            detach_mode=cfg.detach,
            packages=package_results,
        )

    # Save state registry & commit changes in install repo
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
# Layer 4: Public Primitive Entry Point
# =====================================================================

def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    config: Optional[UninstallConfig] = None,
) -> UninstallResult:
    """Uninstalls or detaches one or more packages from the system (Primitive 7)."""
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
