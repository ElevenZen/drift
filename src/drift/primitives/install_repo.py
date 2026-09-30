"""Primitive 5: Install Deployment (install/ -> active host system) & Primitive 6: Commit Install Repo.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-flight Preparation & Assertion (Read-Only):
        prepare_install_deployment(workspace_config, packages_to_redeploy, config) [Layer 4]
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
            -> Returns InstallPlan(pkg_metadata_map, state_registry, discovered_packages, config)

    2. Single-Package Deployment Execution:
        execute_install_deployment(workspace_config, plan: InstallPlan) [Layer 4]
            - Iterates over plan.discovered_packages:
                deploy_one_package_with_error_wrapping [Layer 4]
                    deploy_one_package [Layer 4]
                        check_package_deployment_skip [Layer 4]
                        state_registry.set_package_state("installing") & save (if not dry_run)
                        pkg_config.package_envs context
                        deploy_one_package_impl [Layer 4]
                            Target Directory Migration Detection -> cleanup old target & force redeploy
                            plan_package_deployment [Layer 3] (Pure, inspectable per-path plan)
                            If dry_run -> return PackageInstallResult(plan=plan) immediately (zero disk mutations)
                            trigger pre_install / pre_update hook
                            state_registry.sync_deployed_files & save
                            execute_package_deployment [Layer 3] (Applies PlannedFileAction items deterministically)
                            trigger post_install / post_update hook
                            update_state_registry_post_deployment [Layer 3]
                                state_registry.set_package_state("installed") & save
            -> Returns Aggregated InstallDeploymentResult

    3. Public Composite Primitive Entry Points:
        run_primitive_5_install_deployment(workspace_config, packages_to_redeploy, options) [Layer 5]
            = prepare_install_deployment >> execute_install_deployment
        run_primitive_6_commit_install_repo(workspace_config, commit_message, target_pkgs) [Layer 5]
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
          BACKUP_PRUNE and DELETE_ORPHAN.

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Atomic Host Actions
        backup_host_item
        execute_single_action
    Layer 2: Single-Path Inspection & Planning Helpers
        assert_target_dir_outside_drift_root
        _plan_file_creation
        _check_symlink_points_to_source
        _check_has_backed_up_ancestor
        _inspect_single_ancestor
        _inspect_ancestor_directories
        _inspect_symlink_leaf
        _inspect_physical_file_leaf
        _inspect_leaf_file
        _plan_orphan_prune
        _inspect_orphans
    Layer 3: Plan Generation & Batch Execution
        plan_package_deployment
        execute_package_deployment
        update_state_registry_post_deployment
    Layer 4: Single-Package Pipeline & Pre-flight Validation
        check_package_deployment_skip
        deploy_one_package_impl
        deploy_one_package
        deploy_one_package_with_error_wrapping
        assert_packages_deployment_ready
        prepare_install_deployment
        execute_install_deployment
    Layer 5: Public Primitive Entry Points
        run_primitive_5_install_deployment
        run_primitive_6_commit_install_repo
===============================================================================
"""

import os
import sys
import re
import logging
import subprocess
import datetime
import shlex
import collections
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Sequence, Mapping, Iterable

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import (
    PackageConfig,
    PackageDependencies,
    PackageSectionConfig,
)
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
from .stage_repo import PackageStageChanges
from .package_assertions import (
    assert_packages_hooks_exist,
    assert_packages_not_in_midway_state,
    assert_packages_install_dirs_exist,
    assert_packages_target_dirs_valid,
    assert_packages_target_dirs_writable,
    assert_no_cross_package_conflicts,
    assert_no_cyclic_package_dependencies,
    resolve_package_install_order,
    resolve_ordered_packages,
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
from ..core.result_models import (
    ActionType,
    PlannedFileAction,
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
    redeploy: bool = True
    dry_run: bool = False
    no_deps: bool = False
    package_changes: Optional[Mapping[str, PackageStageChanges]] = None
    flags: Optional[HookExecFlags] = None

    def get_package_changes(self, pkg: str) -> Optional[PackageStageChanges]:
        """Retrieves stage changes for a specific package name from mapping."""
        if self.package_changes is not None:
            return self.package_changes.get(pkg)
        return None


@dataclass(frozen=True)
class InstallPlan:
    """Pre-flight validated installation plan containing package configurations, config, and state registry."""
    pkg_metadata_map: Dict[str, PackageConfig]
    state_registry: StateRegistry
    discovered_packages: List[str]
    config: InstallConfig


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
        )


# =============================================================================
# Layer 1: Atomic Host Actions
# =============================================================================

def backup_host_item(
    context: PackageInstallContext,
    system_target: Path,
    subfolder: BackupSubfolder,
    rel_path: Path,
    reason: Optional[str] = None,
    resolve_symlinks: bool = True,
) -> None:
    """Safely backs up a host file, symlink, or directory to the package backup directory."""
    subfolder_str = subfolder.value if isinstance(subfolder, BackupSubfolder) else str(subfolder)
    backup_rel_path = decode_dot_prefix(rel_path)
    backup_path = context.backup_pkg_dir / subfolder_str / backup_rel_path
    if reason:
        logger.warning(f"🛡️  [BACKUP] {reason} at '{system_target}'")
    logger.debug(f"   Backing up to: {backup_path}")
    backup_file_or_dir_external(system_target, backup_path, context.sudo, resolve_symlinks=resolve_symlinks)


def execute_single_action(
    context: PackageInstallContext,
    action: PlannedFileAction,
    resolve_symlinks: bool = True,
) -> None:
    """Executes a single planned file/directory deployment action on the host system."""
    if action.action_type == ActionType.BACKUP_OVERWRITE:
        backup_host_item(
            context=context,
            system_target=action.system_target,
            subfolder=BackupSubfolder.OVERWRITTEN,
            rel_path=action.rel_path,
            reason=action.reason,
            resolve_symlinks=resolve_symlinks,
        )
        remove(action.system_target, context.sudo)

    elif action.action_type == ActionType.BACKUP_PRUNE:
        backup_host_item(
            context=context,
            system_target=action.system_target,
            subfolder=BackupSubfolder.DELETED_FILES,
            rel_path=action.rel_path,
            reason=action.reason,
            resolve_symlinks=resolve_symlinks,
        )

    elif action.action_type == ActionType.DELETE_ORPHAN:
        if action.system_target.exists() or action.system_target.is_symlink():
            remove(action.system_target, context.sudo)

    elif action.action_type == ActionType.ENSURE_DIR:
        ensure_dir(action.system_target, context.sudo)

    elif action.action_type == ActionType.CREATE_SYMLINK:
        source_file = context.install_pkg_dir / action.rel_path
        relative_target = compute_relative_symlink_target(source_file, action.system_target.parent)
        create_symlink(relative_target, action.system_target, context.sudo)

    elif action.action_type in (ActionType.CREATE_COPY, ActionType.UPDATE_COPY):
        source_file = context.install_pkg_dir / action.rel_path
        copy_file(source_file, action.system_target, context.sudo)

    elif action.action_type == ActionType.SKIP_IDENTICAL:
        logger.debug(f"   Skipping '{action.system_target}': already up-to-date")


# =============================================================================
# Layer 2: Single-File Delivery & Conflict Resolution Helpers
# =============================================================================

def assert_target_dir_outside_drift_root(target_dir: Path, drift_root: Path) -> None:
    """Guards against deployment into drift_root via direct path or symlink resolution."""
    abs_drift_root = drift_root.resolve()
    resolved_target = target_dir.resolve()
    if is_relative_to(resolved_target, abs_drift_root):
        raise InstallCollisionError(
            f"Safety Abort: Target directory '{target_dir}' (resolved to '{resolved_target}') "
            f"points inside drift workspace root '{drift_root}'. "
            f"Resolving this automatically is unsafe. Please resolve manually."
        )


def _plan_file_creation(
    install_method: InstallMethod,
    rel_file: Path,
    system_target: Path,
) -> PlannedFileAction:
    """Returns CREATE_SYMLINK or CREATE_COPY depending on the package install method."""
    action_type = ActionType.CREATE_SYMLINK if install_method == InstallMethod.SYMLINK else ActionType.CREATE_COPY
    return PlannedFileAction(action_type=action_type, rel_path=rel_file, system_target=system_target)


def _check_symlink_points_to_source(system_target: Path, source_file: Path) -> bool:
    """Read-only check returning True if system_target symlink resolves or points to source_file."""
    try:
        link_raw = Path(os.readlink(system_target))
        expected_rel = compute_relative_symlink_target(source_file, system_target.parent)
        return link_raw == expected_rel or (system_target.parent / link_raw).resolve() == source_file.resolve()
    except Exception:
        return False


def _inspect_single_ancestor(
    ancestor_target: Path,
    rel_path: Path,
    abs_drift_root: Path,
) -> List[PlannedFileAction]:
    """Inspects a single ancestor directory path and returns necessary directory creation or backup actions."""
    if not (ancestor_target.exists() or ancestor_target.is_symlink()):
        return [PlannedFileAction(action_type=ActionType.ENSURE_DIR, rel_path=rel_path, system_target=ancestor_target)]

    if ancestor_target.is_dir() and not ancestor_target.is_symlink():
        return []

    # Blocked by an internal symlink, foreign symlink, or physical file
    if ancestor_target.is_symlink():
        is_internal = False
        try:
            is_internal = is_relative_to(ancestor_target.resolve(), abs_drift_root)
        except Exception:
            is_internal = False
        reason = "Internal ancestor symlink conflict" if is_internal else "Symlink blocking directory"
    else:
        reason = "File blocking directory"

    return [
        PlannedFileAction(action_type=ActionType.BACKUP_OVERWRITE, rel_path=rel_path, system_target=ancestor_target, reason=reason),
        PlannedFileAction(action_type=ActionType.ENSURE_DIR, rel_path=rel_path, system_target=ancestor_target),
    ]


def _check_has_backed_up_ancestor(target: Path, backed_up_targets: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of target has been backed up."""
    return any(parent in backed_up_targets for parent in target.parents)


def _inspect_ancestor_directories(
    context: PackageInstallContext,
    active_files: Iterable[Path],
    handled_targets: Set[Path],
    actions: List[PlannedFileAction],
) -> Set[Path]:
    """Inspects parent directories of active files from shallowest to deepest, planning necessary directory recreation or backups."""
    def _expand_path_ancestors(rel: Path) -> Set[Path]:
        target_rel = encode_dot_prefix(rel)
        return {p for p in target_rel.parents if p != Path(".")}

    all_ancestors = {p for rel in active_files for p in _expand_path_ancestors(rel)}
    sorted_ancestors = sorted(all_ancestors, key=lambda p: len(p.parts))
    abs_drift_root = context.drift_root.resolve()
    backed_up_ancestor_targets: Set[Path] = set()

    for p in sorted_ancestors:
        ancestor_target = context.target_dir / p
        if ancestor_target in handled_targets:
            continue
        handled_targets.add(ancestor_target)
        rel_path = decode_dot_prefix(p)

        if _check_has_backed_up_ancestor(ancestor_target, backed_up_ancestor_targets):
            actions.append(PlannedFileAction(
                action_type=ActionType.ENSURE_DIR,
                rel_path=rel_path,
                system_target=ancestor_target,
            ))
            continue

        res = _inspect_single_ancestor(ancestor_target, rel_path, abs_drift_root)
        if any(a.action_type == ActionType.BACKUP_OVERWRITE for a in res):
            backed_up_ancestor_targets.add(ancestor_target)
        actions.extend(res)

    return backed_up_ancestor_targets


def _inspect_symlink_leaf(
    context: PackageInstallContext,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
    abs_drift_root: Path,
) -> List[PlannedFileAction]:
    """Inspects a host symlink at destination and plans overwrite, skip, or re-link actions."""
    if not system_target.exists():
        # Broken symlink
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Broken symlink collision",
            ),
            _plan_file_creation(context.install_method, rel_file, system_target),
        ]

    # Check if existing symlink already points to source_file
    if _check_symlink_points_to_source(system_target, source_file):
        if context.install_method == InstallMethod.SYMLINK:
            return [PlannedFileAction(
                action_type=ActionType.SKIP_IDENTICAL,
                rel_path=rel_file,
                system_target=system_target,
                reason="Symlink already points to source",
            )]
        # Switching from symlink to copy: backup the existing symlink and create physical copy
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Replacing symlink with copy",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_COPY,
                rel_path=rel_file,
                system_target=system_target,
            ),
        ]

    # Symlink points elsewhere (internal conflict vs external collision)
    points_into_drift = False
    try:
        points_into_drift = is_relative_to(system_target.resolve(), abs_drift_root)
    except Exception:
        points_into_drift = False

    reason = "Conflicting internal symlink" if points_into_drift else "Colliding external symlink"
    return [
        PlannedFileAction(
            action_type=ActionType.BACKUP_OVERWRITE,
            rel_path=rel_file,
            system_target=system_target,
            reason=reason,
        ),
        _plan_file_creation(context.install_method, rel_file, system_target),
    ]


def _inspect_physical_file_leaf(
    context: PackageInstallContext,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
) -> List[PlannedFileAction]:
    """Inspects a host regular physical file and plans overwrite, update, or skip actions."""
    if context.install_method == InstallMethod.SYMLINK:
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Physical file collides with symlink",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_SYMLINK,
                rel_path=rel_file,
                system_target=system_target,
            ),
        ]

    # COPY method
    if context.is_first_time:
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Pre-existing file collision",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_COPY,
                rel_path=rel_file,
                system_target=system_target,
            ),
        ]

    from ..utils.file_inspect import contents_differ
    try:
        differs = contents_differ(source_file, system_target)
    except Exception:
        differs = True

    if differs:
        return [PlannedFileAction(
            action_type=ActionType.UPDATE_COPY,
            rel_path=rel_file,
            system_target=system_target,
            reason="File content updated",
        )]

    return [PlannedFileAction(
        action_type=ActionType.SKIP_IDENTICAL,
        rel_path=rel_file,
        system_target=system_target,
        reason="File content matches",
    )]


def _inspect_leaf_file(
    context: PackageInstallContext,
    rel_file: Path,
    handled_targets: Set[Path],
    actions: List[PlannedFileAction],
    backed_up_ancestor_targets: Set[Path],
) -> None:
    """Inspects a single package file against host filesystem state and plans appropriate actions."""
    rel_file = decode_dot_prefix(rel_file)
    system_target = resolve_target_path(rel_file, context.target_dir)
    source_file = context.install_pkg_dir / rel_file
    handled_targets.add(system_target)
    abs_drift_root = context.drift_root.resolve()

    if _check_has_backed_up_ancestor(system_target, backed_up_ancestor_targets):
        actions.append(_plan_file_creation(context.install_method, rel_file, system_target))
        return

    target_exists_or_symlink = system_target.exists() or system_target.is_symlink()

    if not target_exists_or_symlink:
        actions.append(_plan_file_creation(context.install_method, rel_file, system_target))
        return

    if system_target.is_dir() and not system_target.is_symlink():
        actions.append(PlannedFileAction(
            action_type=ActionType.BACKUP_OVERWRITE,
            rel_path=rel_file,
            system_target=system_target,
            reason="Directory blocking file",
        ))
        actions.append(_plan_file_creation(context.install_method, rel_file, system_target))
        return

    if system_target.is_symlink():
        actions.extend(_inspect_symlink_leaf(context, rel_file, system_target, source_file, abs_drift_root))
        return

    actions.extend(_inspect_physical_file_leaf(context, rel_file, system_target, source_file))


def _plan_orphan_prune(
    context: PackageInstallContext,
    orphan_rel: Path,
    reason: str,
) -> List[PlannedFileAction]:
    """Inspects an orphaned path on host and plans BACKUP_PRUNE and DELETE_ORPHAN if present."""
    system_target = resolve_target_path(orphan_rel, context.target_dir)
    if not (system_target.exists() or system_target.is_symlink()):
        return []

    decoded_rel = decode_dot_prefix(orphan_rel)
    return [
        PlannedFileAction(
            action_type=ActionType.BACKUP_PRUNE,
            rel_path=decoded_rel,
            system_target=system_target,
            reason=reason,
        ),
        PlannedFileAction(
            action_type=ActionType.DELETE_ORPHAN,
            rel_path=decoded_rel,
            system_target=system_target,
            reason=f"Orphaned file '{orphan_rel}' deletion",
        ),
    ]


def _inspect_orphans(
    context: PackageInstallContext,
    deployable_files: Sequence[Path],
    deployed_files: Sequence[Path],
    redeploy: bool,
    package_changes: Optional[PackageStageChanges],
    actions: List[PlannedFileAction],
) -> None:
    """Plans backup and deletion of historical or stage-detected orphaned files."""
    if redeploy:
        orphaned_files = sorted(set(deployed_files) - set(deployable_files))
        for orphaned in orphaned_files:
            actions.extend(_plan_orphan_prune(context, orphaned, f"Orphaned file '{orphaned}' prune"))
    elif package_changes is not None and package_changes.deployable_changes.deleted:
        for deleted_rel in sorted(package_changes.deployable_changes.deleted):
            actions.extend(_plan_orphan_prune(context, deleted_rel, f"Deleted file '{deleted_rel}' prune"))


# =============================================================================
# Layer 3: Batch Delivery & Collision Audit
# =============================================================================

def plan_package_deployment(
    context: PackageInstallContext,
    deployable_files: Iterable[Path],
    deployed_files: Iterable[Path] = (),
    redeploy: bool = True,
    package_changes: Optional[PackageStageChanges] = None,
) -> PackageDeploymentPlan:
    """Pure, read-only planner that inspects package candidate paths and host state to produce a deterministic deployment plan.

    Does NOT modify the host filesystem, execute hooks, or touch state.toml.
    """
    assert_target_dir_outside_drift_root(context.target_dir, context.drift_root)

    deployable_list = list(deployable_files)
    deployed_list = list(deployed_files)

    actions: List[PlannedFileAction] = []
    handled_targets: Set[Path] = set()

    # Determine active files to install or update
    if redeploy or package_changes is None:
        active_files = deployable_list
    else:
        # Incremental redeploy: consider added and modified
        active_files = [
            p for p in deployable_list
            if p in package_changes.deployable_changes.added or p in package_changes.deployable_changes.modified
        ]

    # 1. Prune historical or stage orphans first
    _inspect_orphans(
        context=context,
        deployable_files=deployable_list,
        deployed_files=deployed_list,
        redeploy=redeploy,
        package_changes=package_changes,
        actions=actions,
    )

    # 2. Inspect ancestor directories shallowest to deepest
    backed_up_ancestors = _inspect_ancestor_directories(
        context=context,
        active_files=active_files,
        handled_targets=handled_targets,
        actions=actions,
    )

    # 3. Inspect leaf files
    for rel_file in sorted(active_files):
        _inspect_leaf_file(
            context=context,
            rel_file=rel_file,
            handled_targets=handled_targets,
            actions=actions,
            backed_up_ancestor_targets=backed_up_ancestors,
        )

    hooks_to_trigger = (
        ["pre_install", "post_install"]
        if context.is_first_time
        else ["pre_update", "post_update"]
    )

    return PackageDeploymentPlan(
        package=context.pkg_name,
        target_directory=str(context.target_dir),
        install_method=context.install_method,
        actions=actions,
        hooks_to_trigger=hooks_to_trigger,
    )


def execute_package_deployment(
    context: PackageInstallContext,
    plan: PackageDeploymentPlan,
    resolve_symlinks: bool = True,
) -> None:
    """Executes all planned actions in deterministic order on the host filesystem."""
    for action in plan.actions:
        execute_single_action(context, action, resolve_symlinks=resolve_symlinks)


def update_state_registry_post_deployment(
    state_registry: StateRegistry,
    pkg: str,
    target_directory: Path,
    install_method: InstallMethod,
    redeploy: bool,
    deployable_files: Sequence[Path],
    package_changes: Optional[PackageStageChanges] = None,
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
        redeploy=redeploy,
        deployable_files=deployable_files,
        package_changes=package_changes,
    )
    state_registry.save()


# =============================================================================
# Layer 4: Single-Package Pipeline & Pre-flight Validation
# =============================================================================

def check_package_deployment_skip(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    config: InstallConfig,
) -> Optional[PackageInstallResult]:
    """Inspects package deployment conditions to determine if physical deployment should be skipped.

    Returns:
        A PackageInstallResult with status="SKIPPED" if the package should be skipped,
        or None if physical deployment should proceed.

    Raises:
        PackageInstallDirMissingError: If the package directory does not exist in install/.
    """
    pkg = metadata.name
    install_pkg_dir = workspace_config.install_path / pkg
    if not install_pkg_dir.is_dir():
        raise PackageInstallDirMissingError(
            f"Package installation directory '{install_pkg_dir}' does not exist on disk. "
            f"Please ensure the package is staged before deploying.",
            packages=[pkg],
        )

    if not metadata.package.enable_install:
        logger.info(f"Skipping package '{pkg}' during deployment (enable_install is False).")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(metadata.get_target_directory(workspace_config)),
            status="SKIPPED",
            error="enable_install is False",
        )

    target_dir = metadata.get_target_directory(workspace_config)
    pkg_change = config.get_package_changes(pkg)
    if (
        config.redeploy is False
        and (pkg_change is None or not pkg_change.has_changes)
        and (state_registry.get_target_migrated_from(pkg, target_dir) is None)
    ):
        logger.info(f"Skipping package '{pkg}' deployment (no changes detected and redeploy is False).")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(target_dir),
            status="SKIPPED",
            error="No changes detected and redeploy is False",
        )

    if not config.force and state_registry.is_package_in_midway_state(pkg):
        assert_packages_not_in_midway_state([pkg], state_registry)

    return None


def deploy_one_package_impl(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    config: InstallConfig,
) -> PackageInstallResult:
    """Executes deployment planning, lifecycle hooks, file deliveries, and state registry updates."""
    context = PackageInstallContext.from_package(
        workspace_config=workspace_config,
        state_registry=state_registry,
        metadata=metadata,
    )

    target_dir = context.target_dir
    target_migrated_from = state_registry.get_target_migrated_from(context.pkg_name, target_dir)
    redeploy = config.redeploy

    if target_migrated_from is not None:
        logger.info(
            f"🔄 [MIGRATE] Target directory for package '{context.pkg_name}' changed: "
            f"'{target_migrated_from}' -> '{target_dir}'. Undeploying from previous location."
        )
        old_deployed_files = state_registry.get_package_deployed_files(context.pkg_name)
        if old_deployed_files and not config.dry_run:
            from .uninstall_repo import remove_deployed_files
            remove_deployed_files(
                pkg=context.pkg_name,
                deployed_files=old_deployed_files,
                target_dir=target_migrated_from,
                sudo=context.sudo,
            )
        # Force redeploy to populate new target_dir completely
        redeploy = True

    package_changes = config.get_package_changes(context.pkg_name)
    deployable_files = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)
    deployed_files = state_registry.get_package_deployed_files(context.pkg_name)

    # 1. Pure Planning (Inspect and compile planned actions without mutating state)
    plan = plan_package_deployment(
        context=context,
        deployable_files=deployable_files,
        deployed_files=deployed_files,
        redeploy=redeploy,
        package_changes=package_changes,
    )

    if config.dry_run:
        logger.info(f"🔍 [DRY-RUN] Planned {len(plan.actions)} actions for package '{context.pkg_name}'.")
        return PackageInstallResult(
            package=context.pkg_name,
            install_method=context.install_method,
            target_directory=str(context.target_dir),
            plan=plan,
            is_first_time=context.is_first_time,
            status="SUCCESS",
        )

    # 2. Lifecycle Hooks & State registry update
    hook_flags = HookExecFlags.resolve(config.flags, settings=workspace_config.settings)
    try:
        if context.is_first_time:
            metadata.hooks.trigger_pre_install(flags=hook_flags)
        else:
            metadata.hooks.trigger_pre_update(flags=hook_flags)
    except HookExecutionError as e:
        if not e.requires_rollback:
            if context.is_first_time:
                state_registry.packages.pop(context.pkg_name, None)
            else:
                state_registry.set_package_state(context.pkg_name, "installed")
            state_registry.save()
            logger.error(f"❌ Pre-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Deployment stopped (no rollback needed).")
        raise

    # Persist the target file manifest to state.toml before physical delivery
    # so that midway file deployment crashes have an authoritative list of files to uninstall/rollback
    state_registry.sync_deployed_files(
        pkg=context.pkg_name,
        target_directory=context.target_dir,
        install_method=context.install_method,
        redeploy=redeploy,
        deployable_files=deployable_files,
        package_changes=package_changes,
    )
    state_registry.save()

    # 3. Physical Deployment Execution
    execute_package_deployment(
        context=context,
        plan=plan,
        resolve_symlinks=config.resolve_symlinks,
    )
    logger.debug(f"   File delivery completed via {context.install_method}")

    # 4. Post Hooks
    success = False
    no_rollback_err = False
    try:
        if context.is_first_time:
            metadata.hooks.trigger_post_install(flags=hook_flags)
        else:
            metadata.hooks.trigger_post_update(flags=hook_flags)
        success = True
    except HookExecutionError as e:
        if not e.requires_rollback:
            no_rollback_err = True
            logger.error(f"❌ Post-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Files remain installed (no rollback needed).")
        # The exception is raised here.
        raise
    finally:
        if success or no_rollback_err:
            update_state_registry_post_deployment(
                state_registry=state_registry,
                pkg=context.pkg_name,
                target_directory=context.target_dir,
                install_method=context.install_method,
                redeploy=redeploy,
                deployable_files=deployable_files,
                package_changes=package_changes,
            )

    logger.info(f"✨ Package '{context.pkg_name}' deployed successfully.")

    return PackageInstallResult(
        package=context.pkg_name,
        install_method=context.install_method,
        target_directory=str(context.target_dir),
        plan=plan,
        is_first_time=context.is_first_time,
        status="SUCCESS",
    )


def deploy_one_package(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    config: Optional[InstallConfig] = None,
) -> PackageInstallResult:
    """Core function to deploy a single package configuration."""
    cfg = config if config is not None else InstallConfig()
    pkg = metadata.name

    skip_res = check_package_deployment_skip(
        workspace_config=workspace_config,
        state_registry=state_registry,
        metadata=metadata,
        config=cfg,
    )
    if skip_res is not None:
        return skip_res

    logger.info(f"🚀 Deploying package: {pkg}")

    if not cfg.dry_run:
        state_registry.set_package_state(pkg, "installing")
        state_registry.save()

    with metadata.package_envs():
        return deploy_one_package_impl(
            workspace_config=workspace_config,
            state_registry=state_registry,
            metadata=metadata,
            config=cfg,
        )


def deploy_one_package_with_error_wrapping(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    config: Optional[InstallConfig] = None,
) -> PackageInstallResult:
    """Core function to deploy a single package configuration with subcommand error output reporting."""
    pkg = metadata.name
    try:
        return deploy_one_package(
            workspace_config=workspace_config,
            state_registry=state_registry,
            metadata=metadata,
            config=config,
        )
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
    packages_to_redeploy: Sequence[str] = (),
    config: Optional[InstallConfig] = None,
) -> InstallPlan:
    """Pre-flight checks for permissions, lifecycle hook scripts, and cross-package conflicts before deployment.

    Runs pre-flight assertion guards (sudo escalation, install dirs exist, midway transaction state,
    target directory validity, target directory permissions, hook script existence, cross-package collisions).
    Does NOT modify the host filesystem or mutate state registry.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_redeploy: Specific package name(s) to deploy, or empty sequence for all installed packages.
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
        target_packages=packages_to_redeploy or None,
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
            discovered_packages=[],
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
    action_order = resolve_ordered_packages(
        target_metadata=pkg_metadata_map,
        state_registry=state_registry,
        workspace_config=workspace_config,
        no_deps=(cfg.force or cfg.no_deps),
    )

    return InstallPlan(
        pkg_metadata_map=pkg_metadata_map,
        state_registry=state_registry,
        discovered_packages=action_order,
        config=cfg,
    )


def execute_install_deployment(
    workspace_config: WorkspaceConfig,
    plan: InstallPlan,
) -> InstallDeploymentResult:
    """Applies validated configuration changes to host system and updates state registry.

    Args:
        workspace_config: The workspace configuration instance.
        plan: Pre-flight validated InstallPlan containing package metadata, state registry,
              discovered packages, and install config.

    Returns:
        InstallDeploymentResult with detailed per-package deployment results.
    """
    results = [
        deploy_one_package_with_error_wrapping(
            workspace_config=workspace_config,
            state_registry=plan.state_registry,
            metadata=plan.pkg_metadata_map[pkg],
            config=plan.config,
        )
        for pkg in plan.discovered_packages
    ]

    return InstallDeploymentResult(
        status="SUCCESS",
        packages=results,
    )


# =============================================================================
# Layer 5: Public Primitive Entry Points
# =============================================================================

def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    packages_to_redeploy: Sequence[str] = (),
    config: Optional[InstallConfig] = None,
) -> InstallDeploymentResult:
    """Applies changes from the install/ state database to the active host system (Primitive 5).

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_redeploy: Specific package name(s) to deploy, or empty/omitted for all installed packages.
        config: Optional InstallConfig controlling deployment behavior (resolve_symlinks, force, redeploy, package_changes, flags).

    Returns:
        InstallDeploymentResult with detailed per-package deployment results.
    """
    plan = prepare_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=packages_to_redeploy,
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
