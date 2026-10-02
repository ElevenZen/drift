"""Primitive 1 Reverse Sync operation (Systee -> install/ state database).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

This primitive synchronizes host configuration changes back into the install/
state database (local Git repository). It inspects tracked package files and
fully-controlled directories (FCDs), computes necessary filesystem actions
via plan_folder_delivery with reverse_mode=True, executes them deterministically,
and records drifted and synced path manifests.

Layer 1: Path Inspection & File Gathering
    - is_under_any_fcd(rel, fully_controlled_dirs)
    - _check_has_synced_ancestor(rel, synced_ancestors)
    - _collect_leaf_paths(root)
    - record_sync_result(drifted_str, synced_str, drifted_files, synced_files)
    - _record_actions_in_results(actions, install_pkg_dir, drifted_files, synced_files)
    - _gather_single_tracked_candidate(rel, target_dir_path, ignore_handler)
    - gather_tracked_reverse_sync_files(install_pkg_dir, target_dir_path, ignore_handler, ...)
    - gather_single_fcd_reverse_sync_files(fcd, install_pkg_dir, target_dir_path, ignore_handler)
    - gather_fcd_reverse_sync_files(install_pkg_dir, target_dir_path, fully_controlled_dirs, ...)

Layer 2: Single-Package Planning & Execution Pipelines
    - plan_package_reverse_sync(pkg, install_base, workspace_config) -> PackageReverseSyncPlan
    - execute_package_reverse_sync(plan, install_base) -> PackageReverseSyncResult
    - reverse_sync_package(pkg, install_base, workspace_config) -> PackageReverseSyncResult

Layer 3: Multi-Package Workspace Orchestration
    - prepare_reverse_sync(workspace_config, package_names) -> ReverseSyncPlan
    - execute_reverse_sync_plan(workspace_config, plan) -> ReverseSyncResult
    - run_primitive_1_reverse_sync(workspace_config, package_names) -> ReverseSyncResult

===============================================================================
"""

import logging
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..core.constants import MANAGED_CONFIG_FILES, STATE_REGISTRY_FILE_NAME, DirMode
from ..core.folder_diff import list_folder_paths
from ..core.ignore import DriftIgnore
from ..core.state_registry import StateRegistry, load_state_registry
from ..core.folder_delivery import (
    DeliveryInspectionContext,
    FileAction,
    FileActionExecutionContext,
    FileActionType,
    InstallMethod,
    BackupSubfolder,
    plan_folder_delivery,
    execute_delivery_actions,
)
from ..core.result_models import (
    PackageReverseSyncPlan,
    ReverseSyncPlan,
    PackageReverseSyncResult,
    ReverseSyncResult,
)
from ..utils.path_utils import (
    is_relative_to,
    encode_dot_prefix,
    decode_dot_prefix,
)
from ..utils.file_ops import prune_empty_parents

logger = logging.getLogger(__name__)


# =============================================================================
# Layer 1: Path Inspection & File Gathering
# =============================================================================

def is_under_any_fcd(rel: Path, fully_controlled_dirs: Sequence[Path]) -> bool:
    """Read-only check returning True if rel is inside any fully-controlled directory."""
    decoded_rel = decode_dot_prefix(rel)
    decoded_fcds = [decode_dot_prefix(f) for f in fully_controlled_dirs]
    return any(is_relative_to(decoded_rel, fcd) for fcd in decoded_fcds)


def _check_has_synced_ancestor(rel: Path, synced_ancestors: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of rel has already been synced."""
    return any(parent in synced_ancestors for parent in rel.parents if parent != Path("") and parent != Path("."))


def _collect_leaf_paths(root: Path) -> List[Path]:
    """Returns all leaf files and symlinks under root, or [root] if root is not a directory or empty."""
    if not root.is_dir() or root.is_symlink():
        return [root]
    leaves = [p for p in root.rglob("*") if not p.is_dir() or p.is_symlink()]
    return sorted(leaves) if leaves else [root]


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


def _record_actions_in_results(
    actions: Sequence[FileAction],
    install_pkg_dir: Path,
    drifted_files: List[str],
    synced_files: List[str],
) -> None:
    """Derives and records drifted and synced file paths from planned execution actions."""
    for action in actions:
        if action.action_type in (
            FileActionType.CREATE_COPY,
            FileActionType.UPDATE_COPY,
            FileActionType.UPDATE_PERMISSION,
            FileActionType.DELETE_FILE,
            FileActionType.BACKUP_PRUNE,
        ):
            if action.dst_path:
                try:
                    repo_rel = action.dst_path.relative_to(install_pkg_dir)
                except ValueError:
                    continue
                synced_str = str(repo_rel)
                drifted_str = str(encode_dot_prefix(repo_rel))
                record_sync_result(drifted_str, synced_str, drifted_files, synced_files)


def _gather_single_tracked_candidate(
    rel: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    install_pkg_dir: Optional[Path] = None,
) -> Tuple[List[Path], List[Path]]:
    """Gathers deployable and deployed paths for a single tracked non-FCD candidate."""
    host_target = target_dir_path / encode_dot_prefix(rel)
    if host_target.is_dir() and not host_target.is_symlink():
        paths = list_folder_paths(
            src_dir=host_target,
            base_rel=encode_dot_prefix(rel),
            ignore_handler=ignore_handler,
            resolve_symlinks=True,
            translate_mode="reverse",
            dir_mode=DirMode.NO_DIR,
            dest_dir=install_pkg_dir,
        )
        deployable = [
            decode_dot_prefix(p)
            for p in paths
            if decode_dot_prefix(p).name not in MANAGED_CONFIG_FILES
        ]
        return deployable, [rel]
    return [rel], [rel]


def gather_tracked_reverse_sync_files(
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
    fully_controlled_dirs: Sequence[Path] = (),
    state_registry: Optional[StateRegistry] = None,
    pkg: Optional[str] = None,
) -> Tuple[List[Path], List[Path]]:
    """Gathers tracked non-FCD files to inspect for reverse sync.

    Returns (deployable_files, deployed_files).
    Deployable and deployed files are matched for non-FCD files to ensure orphan reconciliation
    does not delete undeployed new files in install_pkg_dir.
    """
    candidates: List[Path] = []
    if state_registry is not None and pkg is not None:
        if state_registry.get_package_state(pkg) is not None and state_registry.get_package_deployed_files(pkg):
            candidates = list(state_registry.get_package_deployed_files(pkg))

    if not candidates:
        candidates = ignore_handler.filter_deployable_files(install_pkg_dir)

    filtered = [
        decode_dot_prefix(p)
        for p in candidates
        if p.name not in MANAGED_CONFIG_FILES
        and not is_under_any_fcd(p, fully_controlled_dirs)
        and not ignore_handler.match_path(decode_dot_prefix(p), is_dir=False)
    ]

    candidate_results = [
        _gather_single_tracked_candidate(
            rel=rel,
            target_dir_path=target_dir_path,
            ignore_handler=ignore_handler,
            install_pkg_dir=install_pkg_dir,
        )
        for rel in filtered
    ]
    deployable = [p for dep, _ in candidate_results for p in dep]
    deployed = [p for _, depl in candidate_results for p in depl]
    return deployable, deployed


def gather_single_fcd_reverse_sync_files(
    fcd: Path,
    install_pkg_dir: Path,
    target_dir_path: Path,
    ignore_handler: DriftIgnore,
) -> Tuple[List[Path], List[Path]]:
    """Gathers deployable (host) and deployed (install/) files for a single fully-controlled directory."""
    host_fcd_path = target_dir_path / encode_dot_prefix(fcd)
    repo_fcd_path = install_pkg_dir / decode_dot_prefix(fcd)

    if not host_fcd_path.exists() and not host_fcd_path.is_symlink():
        return [], []

    # 1. Host deployable files (dest_dir=install_pkg_dir guards against self-referential loops)
    host_paths = list_folder_paths(
        src_dir=host_fcd_path,
        base_rel=encode_dot_prefix(fcd),
        ignore_handler=ignore_handler,
        resolve_symlinks=True,
        translate_mode="reverse",
        dir_mode=DirMode.NO_DIR,
        dest_dir=install_pkg_dir,
    )
    deployable = [
        decode_dot_prefix(p)
        for p in host_paths
        if decode_dot_prefix(p).name not in MANAGED_CONFIG_FILES
    ]

    # 2. Repo deployed files (no symlinks inside install/ package state)
    deployed: List[Path] = []
    if repo_fcd_path.exists() or repo_fcd_path.is_symlink():
        repo_paths = list_folder_paths(
            src_dir=repo_fcd_path,
            base_rel=decode_dot_prefix(fcd),
            ignore_handler=ignore_handler,
            resolve_symlinks=False,
            translate_mode=None,
            dir_mode=DirMode.NO_DIR,
        )
        deployed = [
            p
            for p in repo_paths
            if p.name not in MANAGED_CONFIG_FILES
        ]

    return deployable, deployed


def gather_fcd_reverse_sync_files(
    install_pkg_dir: Path,
    target_dir_path: Path,
    fully_controlled_dirs: Sequence[Path],
    ignore_handler: DriftIgnore,
) -> Tuple[List[Path], List[Path]]:
    """Gathers deployable (host) and deployed (install/) files across all FCDs.

    Returns (fcd_deployable, fcd_deployed).
    """
    fcd_results = [
        gather_single_fcd_reverse_sync_files(
            fcd=fcd,
            install_pkg_dir=install_pkg_dir,
            target_dir_path=target_dir_path,
            ignore_handler=ignore_handler,
        )
        for fcd in fully_controlled_dirs
    ]
    fcd_deployable = [p for dep, _ in fcd_results for p in dep]
    fcd_deployed = [p for _, depl in fcd_results for p in depl]
    return fcd_deployable, fcd_deployed


# =============================================================================
# Layer 2: Single-Package Planning & Execution Pipelines
# =============================================================================

def plan_package_reverse_sync(
    pkg: str,
    install_base: Path,
    workspace_config: WorkspaceConfig,
) -> PackageReverseSyncPlan:
    """Pure planning function that inspects tracked package files and FCDs to compile a PackageReverseSyncPlan.

    Does NOT modify the repository filesystem.
    """
    install_pkg_dir = install_base / pkg
    try:
        metadata = PackageConfig.from_install_dir(install_pkg_dir, workspace_config)
    except Exception as e:
        logger.warning(f"Skipping package '{pkg}' during reverse sync: {e}")
        return PackageReverseSyncPlan(
            package=pkg,
            target_directory="",
            status="FAILED",
            error=str(e),
        )

    target_dir_path = metadata.get_target_directory(workspace_config)
    if not metadata.package.enable_install:
        logger.info(f"Reverse sync is disabled for package '{pkg}' (enable_install = false). Skipping.")
        return PackageReverseSyncPlan(
            package=pkg,
            target_directory=str(target_dir_path),
            status="SKIPPED",
        )

    assert target_dir_path.is_absolute(), f"Target directory '{target_dir_path}' for package '{pkg}' must be an absolute path."
    if not target_dir_path.exists():
        logger.warning(f"Target directory '{target_dir_path}' for package '{pkg}' does not exist. Skipping reverse sync.")
        return PackageReverseSyncPlan(
            package=pkg,
            target_directory=str(target_dir_path),
            status="SKIPPED",
        )

    # Load ignore patterns and state registry
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
    state_file = workspace_config.install_path / STATE_REGISTRY_FILE_NAME
    state_registry = load_state_registry(state_file)

    # 1. Gather tracked non-FCD files
    tracked_deployable, tracked_deployed = gather_tracked_reverse_sync_files(
        install_pkg_dir=install_pkg_dir,
        target_dir_path=target_dir_path,
        ignore_handler=ignore_handler,
        fully_controlled_dirs=metadata.package.fully_controlled_dirs,
        state_registry=state_registry,
        pkg=pkg,
    )

    # 2. Gather Fully-Controlled Directories files
    fcd_deployable, fcd_deployed = gather_fcd_reverse_sync_files(
        install_pkg_dir=install_pkg_dir,
        target_dir_path=target_dir_path,
        fully_controlled_dirs=metadata.package.fully_controlled_dirs,
        ignore_handler=ignore_handler,
    )

    total_deployable = tracked_deployable + fcd_deployable
    total_deployed = tracked_deployed + fcd_deployed

    delivery_ctx = DeliveryInspectionContext(
        target_dir=install_pkg_dir,
        source_dir=target_dir_path,
        drift_root=workspace_config.drift_root,
        install_method=InstallMethod.COPY,
        is_first_time=False,
        backup_pkg_dir=None,
        backup_subfolder=BackupSubfolder.DELETED_FILES,
        reverse_mode=True,
    )

    actions = plan_folder_delivery(
        context=delivery_ctx,
        deployable_files=total_deployable,
        deployed_files=total_deployed,
    )

    return PackageReverseSyncPlan(
        package=pkg,
        target_directory=str(target_dir_path),
        actions=actions,
        status="PENDING",
    )


def execute_package_reverse_sync(
    plan: PackageReverseSyncPlan,
    install_base: Path,
) -> PackageReverseSyncResult:
    """Executes a planned PackageReverseSyncPlan on the install/ package repository."""
    if plan.status == "FAILED":
        return PackageReverseSyncResult(
            package=plan.package,
            target_directory=plan.target_directory,
            status="FAILED",
            error=plan.error,
        )

    if plan.status == "SKIPPED":
        return PackageReverseSyncResult(
            package=plan.package,
            target_directory=plan.target_directory,
            status="SKIPPED",
        )

    install_pkg_dir = install_base / plan.package
    exec_ctx = FileActionExecutionContext(sudo=False, resolve_symlinks=True)
    execute_delivery_actions(exec_ctx, plan.actions)

    for action in plan.actions:
        if action.action_type in (FileActionType.DELETE_FILE, FileActionType.BACKUP_PRUNE):
            if action.dst_path and action.dst_path.parent != install_pkg_dir:
                prune_empty_parents(action.dst_path.parent, limit_dir=install_pkg_dir)

    drifted_files: List[str] = []
    synced_files: List[str] = []
    _record_actions_in_results(plan.actions, install_pkg_dir, drifted_files, synced_files)

    return PackageReverseSyncResult(
        package=plan.package,
        target_directory=plan.target_directory,
        drifted_files=drifted_files,
        synced_files=synced_files,
        status="SUCCESS",
    )


def reverse_sync_package(
    pkg: str,
    install_base: Path,
    workspace_config: WorkspaceConfig,
) -> PackageReverseSyncResult:
    """Performs the reverse sync process for a single package by compiling a plan and executing it."""
    plan = plan_package_reverse_sync(pkg, install_base, workspace_config)
    return execute_package_reverse_sync(plan, install_base)


# =============================================================================
# Layer 3: Multi-Package Workspace Orchestration
# =============================================================================

def prepare_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
) -> ReverseSyncPlan:
    """Discovers and compiles reverse-sync plans for all targeted packages without executing changes."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist.")
        return ReverseSyncPlan(plans=[])

    discovered_packages = workspace_config.filter_install_packages_by_target(
        target_packages=package_names or None,
    )

    plans = [
        plan_package_reverse_sync(pkg, install_base, workspace_config)
        for pkg in discovered_packages
    ]
    return ReverseSyncPlan(plans=plans)


def execute_reverse_sync_plan(
    workspace_config: WorkspaceConfig,
    plan: ReverseSyncPlan,
) -> ReverseSyncResult:
    """Executes a compiled ReverseSyncPlan across all targeted packages."""
    install_base = workspace_config.install_path
    results: List[PackageReverseSyncResult] = [
        execute_package_reverse_sync(pkg_plan, install_base)
        for pkg_plan in plan.plans
    ]
    return ReverseSyncResult(
        status="SUCCESS",
        packages=results,
    )


def run_primitive_1_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
) -> ReverseSyncResult:
    """Unconditionally pulls configuration state from host system back to the install/ repository (Primitive 1)."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist. Skipping reverse sync.")
        return ReverseSyncResult(
            status="FAILED",
            error_message=f"Install state database directory '{install_base}' does not exist.",
        )

    plan = prepare_reverse_sync(workspace_config, package_names)
    return execute_reverse_sync_plan(workspace_config, plan)
