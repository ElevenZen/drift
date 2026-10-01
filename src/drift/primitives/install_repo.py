"""Primitive 5: Install Deployment (install/ -> active host system) & Primitive 6: Commit Install Repo.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-flight Preparation & Assertion (Read-Only):
        prepare_install_deployment(workspace_config, target_pkgs, config) [Layer 4]
            - Package Discovery & Selection (filter_install_packages_by_target)
            - Metadata Resolution (PackageConfig.from_install_dir)
            - Pre-flight Readiness (assert_packages_deployment_ready [Layer 4])
                * assert_can_escalate (if any target requires sudo)
                * assert_packages_install_dirs_exist
                * assert_packages_not_in_midway_state (if not force)
                * assert_packages_target_dirs_valid (absolute & outside drift_root)
                * assert_packages_target_dirs_writable
                * assert_packages_hooks_exist (lifecycle hooks)
                * assert_no_cross_package_conflicts [from package_assertions]
                * assert_no_cyclic_package_dependencies [from package_assertions]
            - Universe Construction & Topological Ordering (resolve_package_install_order)
            - Autonomous Context Construction (PackageInstallContext.from_package)
            - Declarative Plan Compilation (plan_package_deployment)
            -> Returns InstallPlan(pkg_metadata_map, state_registry, packages_to_install, config, contexts, package_plans)

    2. Single-Package Deployment Execution:
        execute_install_deployment(workspace_config, plan: InstallPlan) [Layer 4]
            - If dry_run -> compiles inspectable result summaries from plan.package_plans (zero host mutations)
            - Resolves hook flags once per batch (HookExecFlags.resolve)
            - Iterates over plan.packages_to_install:
                execute_package_install [Layer 2]
                    * Skip evaluation (if no mutations and not redeploy)
                    * with context.package_envs():
                        execute_package_install_impl [Layer 2]
                            - state_registry.set_package_state("installing") & save
                            - trigger pre_install / pre_update hook
                            - state_registry.sync_deployed_files & save
                            - execute_package_actions (applies PlannedFileAction items deterministically)
                            - trigger post_install / post_update hook
                            - update_state_registry_post_deployment ("installed") & save
            -> Returns Aggregated InstallDeploymentResult

    3. Public Composite Primitive Entry Points:
        run_primitive_5_install_deployment(workspace_config, target_pkgs, options) [Layer 3]
            = prepare_install_deployment >> execute_install_deployment
        run_primitive_6_commit_install_repo(workspace_config, commit_message, target_pkgs) [Layer 3]
            commit_repo_changes (commits state repository changes in install/)

    4. Declarative Per-Path Install Planner (plan_package_deployment):
        Pure read-only pre-deployment audit inspecting candidate paths shallowest to deepest:
        - Canonical Target Boundary: Verifies target_dir.resolve() does not point into drift_root.resolve().
        - Ancestor Directory Guard: Deduplicates intermediate target directory checks; detects files or internal
          symlinks blocking required directories, planning BACKUP_OVERWRITE and ENSURE_DIR.
        - Leaf File State Machine: Inspects each deployable file against host state:
            * CREATE_SYMLINK / CREATE_COPY: Target missing on host.
            * SKIP_IDENTICAL: Existing symlink/copy already matches expected source/content.
            * UPDATE_COPY: Existing managed copy has changed content.
            * BACKUP_OVERWRITE: Pre-existing physical file, foreign symlink, or internal symlink collision.
        - Orphan Reconciliation: Identifies historical files no longer in deployable set, planning
          BACKUP_PRUNE.

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Context & Action Compilation
        PackageInstallContext
        plan_package_deployment
        execute_package_actions
        update_state_registry_post_deployment
    Layer 2: Single-Package Pipeline & Pre-flight Validation
        execute_package_install_impl
        execute_package_install
        deploy_one_package
        assert_packages_deployment_ready
        prepare_install_deployment
        execute_install_deployment
    Layer 3: Public Primitive Entry Points
        run_primitive_5_install_deployment
        run_primitive_6_commit_install_repo
===============================================================================
"""

from __future__ import annotations

import os
import sys
import re
import logging
import subprocess
import datetime
import shlex
import collections
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Sequence, Mapping, Iterable, Union, Iterator
from contextlib import contextmanager

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import (
    PackageConfig,
    PackageDependencies,
    PackageSectionConfig,
)
from ..config.package_hooks import PackageHooks
from ..core.constants import (
    LineEnding,
    InstallMethod,
    BackupSubfolder,
)
from ..core.exceptions import (
    ConfigError,
    InstallCollisionError,
    CrossPackageCollisionError,
    HookMissingError,
    PackageInstallDirMissingError,
    TargetPermissionError,
    HookExecutionError,
    mark_logged,
)
from ..core.ignore import DriftIgnore
from ..hooks.lifecycle_hooks import HookExecFlags
from ..core.state_registry import load_state_registry, StateRegistry
from .package_assertions import (
    assert_packages_hooks_exist,
    assert_packages_not_in_midway_state,
    assert_packages_install_dirs_exist,
    assert_packages_target_dirs_valid,
    assert_packages_target_dirs_writable,
    assert_no_cross_package_conflicts,
    assert_no_cyclic_package_dependencies,
    resolve_package_install_order,
    resolve_target_package_order,
)
from ..utils.path_utils import (
    resolve_target_path,
    encode_dot_prefix,
    decode_dot_prefix,
    relative_path_between,
    compute_relative_symlink_target,
    is_relative_to,
)
from ..utils.file_ops import (
    copy_file,
    create_symlink,
    assert_writable,
    ensure_dir,
    remove,
)
from ..utils.process_utils import run_command
from ..core.sync_ops import backup_file_or_dir_external
from ..core.folder_deployment import (
    ActionExecutionContext,
    ActionType,
    PlannedFileAction,
    plan_folder_deployment,
    plan_file_removals,
    execute_deployment_actions,
    assert_target_dir_outside_drift_root,
)
from ..core.result_models import (
    PackageDeploymentPlan,
    PackageInstallResult,
    InstallDeploymentResult,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Deployment Data Structures
# =============================================================================

@dataclass
class InstallConfig:
    """Options controlling package deployment behavior."""
    resolve_symlinks: bool = True
    force: bool = False
    redeploy: bool = False
    dry_run: bool = False
    no_deps: bool = False
    flags: Optional[HookExecFlags] = None


@dataclass(frozen=True)
class InstallPlan:
    """Pre-flight validated installation plan containing package configurations, config, and state registry."""
    pkg_metadata_map: Dict[str, PackageConfig]
    state_registry: StateRegistry
    packages_to_install: List[str]
    config: InstallConfig
    contexts: Dict[str, PackageInstallContext] = field(default_factory=dict)
    package_plans: Dict[str, PackageDeploymentPlan] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageInstallContext:
    """Encapsulates resolved package metadata and filesystem paths for installation operations."""
    pkg_name: str
    install_pkg_dir: Path
    backup_pkg_dir: Path
    target_dir: Path
    install_method: InstallMethod
    ignore_handler: DriftIgnore
    sudo: bool
    is_first_time: bool
    drift_root: Path
    hooks: PackageHooks = field(default_factory=PackageHooks)

    @property
    def action_context(self) -> ActionExecutionContext:
        return ActionExecutionContext(
            target_dir=self.target_dir,
            install_pkg_dir=self.install_pkg_dir,
            backup_pkg_dir=self.backup_pkg_dir,
            sudo=self.sudo,
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
    def from_package(
        cls,
        workspace_config: WorkspaceConfig,
        state_registry: StateRegistry,
        metadata: PackageConfig,
    ) -> "PackageInstallContext":
        pkg = metadata.name
        target_dir = metadata.get_target_directory(workspace_config)
        install_pkg_dir = workspace_config.install_path / pkg
        backup_pkg_dir = workspace_config.backup_path / pkg
        pkg_state = state_registry.packages.get(pkg)
        is_first_time = (pkg_state is None or pkg_state.last_deployed is None)
        ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
        install_method = metadata.get_install_method(workspace_config)
        return cls(
            pkg_name=pkg,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=backup_pkg_dir,
            target_dir=target_dir,
            install_method=install_method,
            ignore_handler=ignore_handler,
            sudo=metadata.package.sudo,
            is_first_time=is_first_time,
            drift_root=workspace_config.drift_root,
            hooks=metadata.hooks,
        )


# =============================================================================
# Layer 1: Context & Action Compilation
# =============================================================================

def plan_package_deployment(
    context: PackageInstallContext,
    deployable_files: Sequence[Path],
    deployed_files: Sequence[Path] = (),
    target_migrated_from: Optional[Path] = None,
) -> PackageDeploymentPlan:
    """Pure, read-only planner that inspects package candidate paths and host state to produce a deterministic deployment plan.

    Does NOT modify the host filesystem, execute hooks, or touch state.toml.
    """
    hooks_to_trigger = (
        ["pre_install", "post_install"]
        if context.is_first_time
        else ["pre_update", "post_update"]
    )
    actions: List[PlannedFileAction] = []

    # 1. Undeploy from previous location if target directory migrated
    if target_migrated_from is not None:
        actions.append(
            PlannedFileAction(
                action_type=ActionType.INFO_MESSAGE,
                rel_path=Path("."),
                system_target=target_migrated_from,
                reason=(
                    f"🔄 [MIGRATE] Target directory for package '{context.pkg_name}' changed: "
                    f"'{target_migrated_from}' -> '{context.target_dir}'. Undeploying from previous location."
                ),
            )
        )
        if deployed_files:
            migration_removals = plan_file_removals(
                deployed_files=deployed_files,
                target_dir=target_migrated_from,
            )
            actions.extend(migration_removals)
        active_deployed_files: Iterable[Path] = ()
    else:
        active_deployed_files = deployed_files

    # 2. Informational banner indicating package deployment begins
    actions.append(
        PlannedFileAction(
            action_type=ActionType.INFO_MESSAGE,
            rel_path=Path("."),
            system_target=context.target_dir,
            reason=f"🚀 Deploying package: {context.pkg_name}",
        )
    )

    # 3. Plan folder deployment actions to current target directory
    folder_actions = plan_folder_deployment(
        target_dir=context.target_dir,
        source_dir=context.install_pkg_dir,
        install_method=context.install_method,
        drift_root=context.drift_root,
        active_files=deployable_files,
        deployed_files=active_deployed_files,
        is_first_time=context.is_first_time,
    )
    actions.extend(folder_actions)

    return PackageDeploymentPlan(
        package=context.pkg_name,
        target_directory=str(context.target_dir),
        install_method=context.install_method,
        actions=actions,
        hooks_to_trigger=hooks_to_trigger,
    )


def execute_package_actions(
    context: PackageInstallContext,
    plan: PackageDeploymentPlan,
    resolve_symlinks: bool = True,
) -> None:
    """Executes all planned actions in deterministic order on the host filesystem."""
    action_ctx = ActionExecutionContext(
        target_dir=context.target_dir,
        install_pkg_dir=context.install_pkg_dir,
        backup_pkg_dir=context.backup_pkg_dir,
        sudo=context.sudo,
        resolve_symlinks=resolve_symlinks,
    )
    execute_deployment_actions(action_ctx, plan.actions)


def update_state_registry_post_deployment(
    state_registry: StateRegistry,
    pkg: str,
    target_directory: Path,
    install_method: InstallMethod,
    deployable_files: Sequence[Path],
    sudo: bool = False,
) -> None:
    """Updates and saves state registry to reflect successful package deployment."""
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(
        pkg,
        "installed",
        last_deployed=now_str,
    )
    state_registry.sync_deployed_files(
        pkg=pkg,
        target_directory=target_directory,
        install_method=install_method,
        deployable_files=deployable_files,
        sudo=sudo,
    )
    state_registry.save()


# =============================================================================
# Layer 2: Single-Package Pipeline & Pre-flight Validation
# =============================================================================

def execute_package_install_impl(
    context: PackageInstallContext,
    plan: PackageDeploymentPlan,
    state_registry: StateRegistry,
    hook_flags: HookExecFlags,
    config: InstallConfig,
) -> PackageInstallResult:
    """Performs state transition, hooks, physical file actions, and registry updates under active package envs."""
    state_registry.set_package_state(context.pkg_name, "installing")
    state_registry.save()

    # 1. Pre-deployment hooks
    try:
        if context.is_first_time:
            context.hooks.trigger_pre_install(flags=hook_flags)
        else:
            context.hooks.trigger_pre_update(flags=hook_flags)
    except HookExecutionError as e:
        if not e.requires_rollback:
            if context.is_first_time:
                state_registry.packages.pop(context.pkg_name, None)
            else:
                state_registry.set_package_state(context.pkg_name, "installed")
            state_registry.save()
            logger.error(f"❌ Pre-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Deployment stopped (no rollback needed).")
        raise

    # 2. Persist the target file manifest to state.toml before physical delivery
    # so that midway file deployment crashes have an authoritative list of files to uninstall/rollback
    deployable_files = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)
    state_registry.sync_deployed_files(
        pkg=context.pkg_name,
        target_directory=context.target_dir,
        install_method=context.install_method,
        deployable_files=deployable_files,
        sudo=context.sudo,
    )
    state_registry.save()

    # 3. Physical Actions Execution
    execute_package_actions(
        context=context,
        plan=plan,
        resolve_symlinks=config.resolve_symlinks,
    )
    logger.debug(f"   File delivery completed via {context.install_method}")

    # 4. Post-deployment hooks
    success = False
    no_rollback_err = False
    try:
        if context.is_first_time:
            context.hooks.trigger_post_install(flags=hook_flags)
        else:
            context.hooks.trigger_post_update(flags=hook_flags)
        success = True
    except HookExecutionError as e:
        if not e.requires_rollback:
            no_rollback_err = True
            logger.error(f"❌ Post-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Files remain installed (no rollback needed).")
        raise
    finally:
        if success or no_rollback_err:
            update_state_registry_post_deployment(
                state_registry=state_registry,
                pkg=context.pkg_name,
                target_directory=context.target_dir,
                install_method=context.install_method,
                deployable_files=deployable_files,
                sudo=context.sudo,
            )

    logger.info(f"✨ Package '{context.pkg_name}' deployed successfully.")

    return PackageInstallResult(
        plan=plan,
        is_first_time=context.is_first_time,
        status="SUCCESS",
    )


def execute_package_install(
    context: PackageInstallContext,
    plan: PackageDeploymentPlan,
    state_registry: StateRegistry,
    hook_flags: HookExecFlags,
    config: InstallConfig,
) -> PackageInstallResult:
    """Applies a pre-compiled PackageDeploymentPlan to the host system and updates state registry."""
    target_migrated_from = state_registry.get_target_migrated_from(context.pkg_name, context.target_dir)

    has_mutations = any(
        a.action_type not in (ActionType.SKIP_IDENTICAL, ActionType.INFO_MESSAGE)
        for a in plan.actions
    )
    if not config.redeploy and not context.is_first_time and not has_mutations and target_migrated_from is None:
        logger.info(f"Skipping package '{context.pkg_name}' deployment (no changes detected and redeploy is False).")
        return PackageInstallResult(
            plan=plan,
            is_first_time=False,
            status="SKIPPED",
            error="No changes detected and redeploy is False",
        )

    with context.package_envs():
        return execute_package_install_impl(
            context=context,
            plan=plan,
            state_registry=state_registry,
            hook_flags=hook_flags,
            config=config,
        )


def deploy_one_package(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    config: Optional[InstallConfig] = None,
) -> PackageInstallResult:
    """Executes deployment planning, lifecycle hooks, file deliveries, and state registry updates for a single package."""
    cfg = config if config is not None else InstallConfig()
    context = PackageInstallContext.from_package(
        workspace_config=workspace_config,
        state_registry=state_registry,
        metadata=metadata,
    )
    assert_packages_install_dirs_exist(workspace_config.install_path, [context.pkg_name])

    target_dir = context.target_dir
    target_migrated_from = state_registry.get_target_migrated_from(context.pkg_name, target_dir)

    deployable_files = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)
    deployed_files = state_registry.get_package_deployed_files(context.pkg_name)

    # 1. Pure Planning (Inspect and compile planned actions without mutating state)
    plan = plan_package_deployment(
        context=context,
        deployable_files=deployable_files,
        deployed_files=deployed_files,
        target_migrated_from=target_migrated_from,
    )

    if cfg.dry_run:
        logger.info(f"🔍 [DRY-RUN] Planned {len(plan.actions)} actions for package '{context.pkg_name}'.")
        return PackageInstallResult(
            plan=plan,
            is_first_time=context.is_first_time,
            status="SUCCESS",
        )

    hook_flags = HookExecFlags.resolve(cfg.flags, settings=workspace_config.settings)
    return execute_package_install(
        context=context,
        plan=plan,
        state_registry=state_registry,
        hook_flags=hook_flags,
        config=cfg,
    )


def assert_packages_deployment_ready(
    workspace_config: WorkspaceConfig,
    discovered_packages: Iterable[str],
    pkg_metadata_map: Mapping[str, PackageConfig],
    hook_flags: HookExecFlags,
    state_registry: StateRegistry,
    force: bool = False,
    full_universe_deps: Optional[Mapping[str, PackageDependencies]] = None,
) -> None:
    """Pre-flight checks for permissions, lifecycle hook scripts, and cross-package file conflicts before deployment.

    Raises:
        HookMissingError: If any configured lifecycle hook files are missing or invalid.
        CrossPackageCollisionError: If two or more packages claim colliding destination host paths.
        MidwayTransactionError: If one or more packages are in a midway transaction state and force is False.
        PackageInstallDirMissingError: If a package install directory does not exist on disk.
        ConfigError: If a target directory is not an absolute path or dependency cycle detected.
        InstallCollisionError: If a target directory resolves inside or equal to drift_root.
        TargetPermissionError: If any target directory is not writable.
    """
    discovered_list = list(discovered_packages)
    active_packages = {
        pkg: metadata
        for pkg, metadata in pkg_metadata_map.items()
        if metadata.package.enable_install
    }

    if any(metadata.package.sudo for metadata in active_packages.values()):
        from ..utils.process_utils import assert_can_escalate
        assert_can_escalate()

    # 1. Staged install directories must exist
    assert_packages_install_dirs_exist(workspace_config.install_path, discovered_list)

    # 2. Midway transaction state lock
    if not force:
        assert_packages_not_in_midway_state(discovered_list, state_registry)

    # 3. Target directories validity (absolute and outside drift_root)
    assert_packages_target_dirs_valid(active_packages, workspace_config)

    # 4. Target directories permissions
    assert_packages_target_dirs_writable(active_packages, workspace_config)

    # 5. Lifecycle hook scripts
    if not hook_flags.no_hooks:
        assert_packages_hooks_exist(
            active_packages,
            workspace_config.install_path,
            is_source=False,
        )

    # 6. Audit cross-package destination collisions
    assert_no_cross_package_conflicts(
        workspace_config=workspace_config,
        discovered_packages=discovered_list,
        pkg_metadata_map=pkg_metadata_map,
        state_registry=state_registry,
    )

    # 7. Package dependency DAG validation (skipped if None)
    if full_universe_deps is not None:
        assert_no_cyclic_package_dependencies(full_universe_deps)


def prepare_install_deployment(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    config: Optional[InstallConfig] = None,
) -> InstallPlan:
    """Pre-flight checks for permissions, lifecycle hook scripts, and cross-package conflicts before deployment.

    Runs pre-flight assertion guards (sudo escalation, install dirs exist, midway transaction state,
    target directory validity, target directory permissions, hook script existence, cross-package collisions).
    Does NOT modify the host filesystem or mutate state registry.

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to deploy, or empty sequence for all installed packages.
        config: Optional InstallConfig controlling installation behavior.

    Returns:
        InstallPlan containing validated package metadata mapping, state registry, discovered packages in
        topological order, and config.
    """
    cfg = config if config is not None else InstallConfig()
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    hook_flags = HookExecFlags.resolve(cfg.flags, settings=workspace_config.settings)

    state_registry = load_state_registry(state_file)

    # Targeted packages for this deployment
    discovered_packages = workspace_config.filter_install_packages_by_target(
        target_packages=target_pkgs or None,
    )

    # 1. Collect: Load metadata for targeted packages from INSTALL directory
    all_metadata: Dict[str, PackageConfig] = {
        pkg: PackageConfig.from_install_dir(install_base / pkg, workspace_config)
        for pkg in discovered_packages
    }

    # 2. Filter: Retain only packages enabled for installation/deployment
    pkg_metadata_map = {
        pkg: meta for pkg, meta in all_metadata.items()
        if meta.package.enable_install
    }
    if not pkg_metadata_map:
        logger.info("No active packages are enabled for installation/deployment. Skipping.")
        return InstallPlan(
            pkg_metadata_map={},
            state_registry=state_registry,
            packages_to_install=[],
            config=cfg,
        )

    # Pre-flight assertions on targeted packages (full_universe_deps=None skips redundant DAG sort)
    assert_packages_deployment_ready(
        workspace_config=workspace_config,
        discovered_packages=list(pkg_metadata_map.keys()),
        pkg_metadata_map=pkg_metadata_map,
        hook_flags=hook_flags,
        state_registry=state_registry,
        force=cfg.force,
        full_universe_deps=None,
    )

    # Construct full installable universe and resolve topological action order
    action_order = resolve_target_package_order(
        target_metadata=pkg_metadata_map,
        state_registry=state_registry,
        workspace_config=workspace_config,
        no_deps=(cfg.force or cfg.no_deps),
    )

    contexts = {
        pkg: PackageInstallContext.from_package(
            workspace_config=workspace_config,
            state_registry=state_registry,
            metadata=pkg_metadata_map[pkg],
        )
        for pkg in action_order
        if pkg in pkg_metadata_map
    }

    package_plans = {
        pkg: plan_package_deployment(
            context=contexts[pkg],
            deployable_files=contexts[pkg].ignore_handler.filter_deployable_files(contexts[pkg].install_pkg_dir),
            deployed_files=state_registry.get_package_deployed_files(pkg),
            target_migrated_from=state_registry.get_target_migrated_from(pkg, contexts[pkg].target_dir),
        )
        for pkg in action_order
        if pkg in pkg_metadata_map
    }

    return InstallPlan(
        pkg_metadata_map=pkg_metadata_map,
        state_registry=state_registry,
        packages_to_install=action_order,
        config=cfg,
        contexts=contexts,
        package_plans=package_plans,
    )


def execute_install_deployment(
    workspace_config: WorkspaceConfig,
    plan: InstallPlan,
) -> InstallDeploymentResult:
    """Applies validated configuration changes to host system and updates state registry.

    Args:
        workspace_config: The workspace configuration instance.
        plan: Pre-flight validated InstallPlan containing package metadata, state registry,
              packages_to_install, contexts, package_plans, and install config.

    Returns:
        InstallDeploymentResult with detailed per-package deployment results.
    """
    cfg = plan.config

    if cfg.dry_run:
        logger.info(f"🔍 [DRY-RUN] Simulating deployment for {len(plan.packages_to_install)} package(s).")
        package_results = [
            PackageInstallResult(
                plan=plan.package_plans[pkg],
                is_first_time=plan.contexts[pkg].is_first_time,
                status="SUCCESS",
            )
            for pkg in plan.packages_to_install
        ]
        return InstallDeploymentResult(
            status="SUCCESS",
            packages=package_results,
        )

    hook_flags = HookExecFlags.resolve(cfg.flags, settings=workspace_config.settings)
    results: List[PackageInstallResult] = []

    for pkg in plan.packages_to_install:
        context = plan.contexts[pkg]
        pkg_plan = plan.package_plans[pkg]
        try:
            pkg_res = execute_package_install(
                context=context,
                plan=pkg_plan,
                state_registry=plan.state_registry,
                hook_flags=hook_flags,
                config=cfg,
            )
            results.append(pkg_res)
        except subprocess.CalledProcessError as e:
            stderr_str = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr or "")
            stdout_str = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout or "")
            err_msg = (
                f"Subcommand failed during package '{pkg}' deployment.\n"
                f"Command: {shlex.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)}\n"
                f"Exit Code: {e.returncode}"
            )
            if stderr_str.strip():
                err_msg += f"\nStderr:\n{stderr_str.strip()}"
            if stdout_str.strip():
                err_msg += f"\nStdout:\n{stdout_str.strip()}"
            logger.error(err_msg)
            raise mark_logged(RuntimeError(err_msg)) from e

    return InstallDeploymentResult(
        status="SUCCESS",
        packages=results,
    )


# =============================================================================
# Layer 5: Public Primitive Entry Points
# =============================================================================

def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    config: Optional[InstallConfig] = None,
) -> InstallDeploymentResult:
    """Applies changes from the install/ state database to the active host system (Primitive 5).

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to deploy, or empty/omitted for all installed packages.
        config: Optional InstallConfig controlling deployment behavior (resolve_symlinks, force, redeploy, flags).

    Returns:
        InstallDeploymentResult with detailed per-package deployment results.
    """
    plan = prepare_install_deployment(
        workspace_config=workspace_config,
        target_pkgs=target_pkgs,
        config=config,
    )
    return execute_install_deployment(workspace_config, plan=plan)


def run_primitive_6_commit_install_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    target_pkgs: Sequence[str] = ()
) -> None:
    """Stages and commits changes inside the install/ state Git repository (Primitive 6).

    If target_pkgs is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    from ..utils.git_utils import commit_repo_changes
    
    install_dir = workspace_config.install_path
    
    committed = commit_repo_changes(
        repo_path=install_dir,
        commit_message=commit_message,
        target_pkgs=target_pkgs,
        repo_name="install repo"
    )
    
    if committed:
        logger.info(f"💾 Committed install repo changes: {commit_message}")
    else:
        logger.info("Nothing to commit, install repository is clean.")
