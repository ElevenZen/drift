"""Primitive 4: Sandbox Staging (render/ -> install/ state database).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-flight Preparation & Assertion (Read-Only):
        prepare_stage_packages(workspace_config, target_pkgs, force, no_deps) [Layer 4]
            - Package Discovery & Selection (filter_render_packages_by_target)
            - Metadata Resolution (PackageConfig.from_render_dir)
            - Filtering (enable_install predicate)
            - Pre-flight Readiness (assert_packages_stage_ready [Layer 1])
                * assert_packages_hooks_exist [from package_assertions]
                * state_registry.get_midway_packages
                * assert_install_pkg_dirs_clean [from package_assertions]
            - Staging Ordering (resolve_target_package_order)
            -> Returns StagePlan(pkg_metadata, state_registry, ordered_packages)

    2. Diff Computation & Planning (Read-Only):
        plan_package_stage(pkg, install_base, render_base) [Layer 2]
            - Runs single-pass compare_folders without ignore_handler (1:1 structural fidelity)
            - Compiles FolderDiff to actions via plan_actions_from_folder_diff
            -> Returns PackageStagePlan(package, actions)

    3. Single Package Physical Staging (State-Mutating):
        execute_package_stage(context, plan) [Layer 3]
            - Applies planned actions via execute_delivery_actions

    4. Staging Transaction & Orchestration:
        stage_modified_packages(packages_to_stage, pkg_metadata, ...) [Layer 4]
            * assert_can_escalate (if sudo required)
            * state_registry.set_package_state("staging") & save
            * execute_package_stage for each modified package
            * state_registry.set_package_state("staged") & save
        execute_stage_packages(workspace_config, pkg_metadata, state_registry, ordered_packages) [Layer 4]
            -> Returns Dict[str, PackageStagePlan]

    5. Public Composite Primitive Entry Point:
        run_primitive_4_stage_render_to_install(workspace_config, target_pkgs, force, no_deps) [Layer 5]
            = prepare_stage_packages >> execute_stage_packages

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Pre-flight Verification & File Operations
        assert_packages_stage_ready
    Layer 2: Diff Computation & Planning
        plan_package_stage
    Layer 3: Single Package Physical Staging
        execute_package_stage
    Layer 4: Staging Transaction & Sub-stages
        stage_modified_packages
        prepare_stage_packages
        execute_stage_packages
    Layer 5: Public Primitive Entry Point
        run_primitive_4_stage_render_to_install
===============================================================================
"""

import logging
from pathlib import Path
from typing import List, Union, Optional, Sequence, Tuple, Dict, Mapping, Iterable
from dataclasses import dataclass, field

from ..core.constants import DRIFT_GENERATED_FILES
from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import (
    PackageConfig,
    PackageDependencies,
    PackageSectionConfig,
)
from ..core.folder_delivery import (
    ActionExecutionContext,
    execute_delivery_actions,
    plan_actions_from_folder_diff,
    format_action_summary,
)
from ..core.result_models import PackageStagePlan, StageResult
from ..utils.process_utils import assert_can_escalate
from ..core.folder_diff import compare_folders, FolderDiff
from ..core.state_registry import load_state_registry, StateRegistry
from ..core.exceptions import MidwayTransactionError
from .package_assertions import (
    assert_packages_hooks_exist,
    assert_install_pkg_dirs_clean,
    assert_packages_not_in_midway_state,
    assert_no_cyclic_package_dependencies,
    resolve_package_install_order,
    resolve_target_package_order,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StagePlan:
    """Pre-flight validated staging plan containing package configurations and state registry."""
    pkg_metadata: Dict[str, PackageConfig]
    state_registry: StateRegistry
    ordered_packages: List[str] = field(default_factory=list)



# =====================================================================
# Layer 1: Pre-flight Verification & File Operations
# =====================================================================

def assert_packages_stage_ready(
    pkg_metadata: Mapping[str, PackageConfig],
    render_base: Path,
    install_base: Path,
    state_registry: StateRegistry,
    force: bool = False,
    full_universe_deps: Optional[Mapping[str, PackageDependencies]] = None,
) -> None:
    """Validates that packages in render/ are ready for staging into install/.

    Pre-flight safety assertions:
    1. Hook file existence in render/ sandbox for each enabled package.
    2. Package dependency DAG validation on full universe (if full_universe_deps is provided).
    3. When force is False, validates that no package is in a midway transaction state
       ('staging' or 'installing') in the state registry.
    4. When force is False, validates that no package directory in install/ has uncommitted
       local git modifications.

    Raises:
        HookMissingError: If configured hook files do not exist or are invalid.
        ConfigError: If a dependency cycle is detected.
        MidwayTransactionError: If any package is in a midway transaction state.
        DriftDetectedError: If install package directories have uncommitted modifications.
    """
    # 1. Collect all package hook assertion errors before raising
    assert_packages_hooks_exist(pkg_metadata, render_base, is_source=False)

    # 2. Package dependency DAG validation on full universe (skipped if None)
    if full_universe_deps is not None:
        assert_no_cyclic_package_dependencies(full_universe_deps)

    if force:
        return

    # 3. Collect all midway transaction state packages before raising
    assert_packages_not_in_midway_state(pkg_metadata.keys(), state_registry)

    # 4. Collect all unclean package directories before raising
    assert_install_pkg_dirs_clean(install_base, pkg_metadata.keys())



# =====================================================================
# Layer 2: Diff Computation & Planning
# =====================================================================

def plan_package_stage(
    pkg: str,
    install_base: Path,
    render_base: Path,
) -> PackageStagePlan:
    """Computes physical file diff between render/ and install/ and compiles a PackageStagePlan.

    Evaluates 1:1 structural fidelity differences without ignore filtering, compiling
    discrete CREATE_COPY, UPDATE_COPY, UPDATE_PERMISSION, ENSURE_DIR, and DELETE_FILE actions.
    """
    install_pkg_dir = install_base / pkg
    render_pkg_dir = render_base / pkg

    if not render_pkg_dir.exists():
        raise RuntimeError(f"Render sandbox directory for package '{pkg}' does not exist. Please render first.")

    diff = compare_folders(
        src_dir=render_pkg_dir,
        dst_dir=install_pkg_dir,
        ignore_handler=None,
        resolve_symlinks=False,
    )

    # Exclude DRIFT_GENERATED_FILES from deleted list as they are generated directly in install/
    diff.deleted = [p for p in diff.deleted if p.name not in DRIFT_GENERATED_FILES]

    actions = plan_actions_from_folder_diff(
        diff=diff,
        source_dir=render_pkg_dir,
        target_dir=install_pkg_dir,
    )

    return PackageStagePlan(
        package=pkg,
        actions=actions,
    )


# =====================================================================
# Layer 3: Single Package Physical Staging
# =====================================================================

def execute_package_stage(
    context: ActionExecutionContext,
    plan: PackageStagePlan,
) -> None:
    """Executes all planned staging actions on the install/ package directory."""
    execute_delivery_actions(context, plan.actions)


# =====================================================================
# Layer 4: Staging Transaction & State Management
# =====================================================================

def stage_modified_packages(
    packages_to_stage: Mapping[str, PackageStagePlan],
    pkg_metadata: Mapping[str, PackageConfig],
    install_base: Path,
    render_base: Path,
    state_registry: StateRegistry,
) -> None:
    """Applies physical changes and manages staging state transitions for packages with changes."""
    if not packages_to_stage:
        return

    # 1. Check sudo privilege ONLY if any package with actual changes requires sudo
    needs_sudo = any(pkg_metadata[pkg].package.sudo for pkg in packages_to_stage)
    if needs_sudo:
        assert_can_escalate()

    # 2. Set state of packages with changes to "staging" before staging to prevent partial staging issues
    for pkg in packages_to_stage:
        state_registry.set_package_state(pkg, "staging")
    state_registry.save()

    # 3. Apply stage actions to install/ directory for each package with changes
    for pkg, plan in packages_to_stage.items():
        install_pkg_dir = install_base / pkg
        context = ActionExecutionContext(
            target_dir=install_pkg_dir,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=install_pkg_dir,
            sudo=False,
            resolve_symlinks=False,
        )
        execute_package_stage(context, plan)

    # 4. Set state of packages with changes to "staged" after successful staging
    for pkg in packages_to_stage:
        state_registry.set_package_state(pkg, "staged")
    state_registry.save()


def prepare_stage_packages(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    force: bool = False,
    no_deps: bool = False,
) -> StagePlan:
    """Discovers, validates, and prepares packages for staging from render/ to install/.

    Runs pre-flight assertion guards (hook existence, midway transaction state, install repo cleanliness)
    and computes topologically sorted staging order.
    Does NOT modify the filesystem or mutate state registry.

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to stage, or empty sequence for all active packages.
        force: If True, bypasses checks for midway failed package states and uncommitted install modifications.
        no_deps: If True, bypasses missing required package dependency checks.

    Returns:
        StagePlan containing validated package metadata map, state registry, and ordered packages.
    """
    if isinstance(target_pkgs, str):
        target_pkgs = [target_pkgs]

    render_base = workspace_config.render_path
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    state_registry = load_state_registry(state_file)

    # Load active packages from render directory
    active_packages = workspace_config.filter_render_packages_by_target(target_packages=target_pkgs or None)
    if not active_packages:
        logger.info("No active packages selected for staging. Skipping.")
        return StagePlan(pkg_metadata={}, state_registry=state_registry, ordered_packages=[])

    # 1. Collect: Load metadata for active packages from RENDER directory
    all_metadata: Dict[str, PackageConfig] = {
        pkg: PackageConfig.from_render_dir(render_base / pkg, workspace_config)
        for pkg in active_packages
    }

    # 2. Filter: Retain only packages enabled for installation/deployment
    pkg_metadata = {
        pkg: meta for pkg, meta in all_metadata.items()
        if meta.package.enable_install
    }
    if not pkg_metadata:
        logger.info("No active packages are enabled for installation/deployment. Skipping.")
        return StagePlan(pkg_metadata={}, state_registry=state_registry, ordered_packages=[])

    # 3. Assert: Verify hook files, transaction state, and install directory cleanliness
    # (full_universe_deps=None skips redundant DAG sort here as resolve_package_install_order validates it below)
    assert_packages_stage_ready(
        pkg_metadata=pkg_metadata,
        render_base=render_base,
        install_base=install_base,
        state_registry=state_registry,
        force=force,
        full_universe_deps=None,
    )

    # 4. Resolve topological order over universe, filtered to targeted packages
    ordered_packages = resolve_target_package_order(
        target_metadata=pkg_metadata,
        state_registry=state_registry,
        workspace_config=workspace_config,
        no_deps=(force or no_deps),
    )

    return StagePlan(
        pkg_metadata=pkg_metadata,
        state_registry=state_registry,
        ordered_packages=ordered_packages,
    )


def execute_stage_packages(
    workspace_config: WorkspaceConfig,
    pkg_metadata: Mapping[str, PackageConfig],
    state_registry: StateRegistry,
    ordered_packages: Optional[Sequence[str]] = None,
) -> StageResult:
    """Computes stage plans and applies physical file changes and state transitions from render/ to install/.

    Args:
        workspace_config: The workspace configuration instance.
        pkg_metadata: Pre-flight validated package metadata mapping.
        state_registry: Active state registry for tracking staging state transitions.
        ordered_packages: Optional topologically sorted package order. If omitted, uses pkg_metadata keys.

    Returns:
        StageResult containing all changed package plans and execution status.
    """
    if not pkg_metadata:
        return StageResult(command="stage", status="SUCCESS", packages_changed=[], plans=[])

    render_base = workspace_config.render_path
    install_base = workspace_config.install_path

    package_order = list(ordered_packages) if ordered_packages is not None else list(pkg_metadata.keys())

    logger.info(f"🔍 Staging {len(pkg_metadata)} packages: {', '.join(package_order)}")

    # 1. Compute stage plans for all packages in order
    plans = {
        pkg: plan_package_stage(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
        )
        for pkg in package_order
    }

    # Identify packages that have physical stage changes
    packages_to_stage = {
        pkg: plan
        for pkg, plan in plans.items()
        if plan.has_changes
    }

    # 2. Apply physical changes and state transitions for modified packages in order
    if packages_to_stage:
        stage_modified_packages(
            packages_to_stage=packages_to_stage,
            pkg_metadata=pkg_metadata,
            install_base=install_base,
            render_base=render_base,
            state_registry=state_registry,
        )

    # 3. Prepare summary of changes for logging
    if packages_to_stage:
        logger.info("✨ Staging completed. Summary of changes:")
        for plan in packages_to_stage.values():
            logger.info(f"   Package '{plan.package}': {format_action_summary(plan.actions)}")
    else:
        logger.info("✨ Staging completed. No changes detected.")

    return StageResult(
        command="stage",
        status="SUCCESS",
        packages_changed=list(packages_to_stage.keys()),
        plans=list(packages_to_stage.values()),
    )


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def run_primitive_4_stage_render_to_install(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    force: bool = False,
    no_deps: bool = False,
) -> StageResult:
    """Reconciles the sandbox render/ folder into the install/ database (Primitive 4).

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to stage, or empty sequence for all active packages.
        force: If True, bypasses checks for midway failed package states ('staging' or 'installing')
            and ignores uncommitted local modifications in the install/ directory.
            Note: Does NOT bypass 'enable_install = false' package configurations.
        no_deps: If True, bypasses missing required package dependency checks.

    Returns:
        StageResult containing all changed package plans and execution status.
    """
    plan = prepare_stage_packages(
        workspace_config,
        target_pkgs=target_pkgs,
        force=force,
        no_deps=no_deps,
    )
    if not plan.pkg_metadata:
        return StageResult(command="stage", status="SUCCESS", packages_changed=[], plans=[])
    return execute_stage_packages(
        workspace_config,
        pkg_metadata=plan.pkg_metadata,
        state_registry=plan.state_registry,
        ordered_packages=plan.ordered_packages,
    )

