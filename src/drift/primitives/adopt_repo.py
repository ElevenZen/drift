"""Primitive 8: Multi-Package Drift Adoption Orchestrator (`drift adopt`, Layer 5).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-Flight Synchronization, Rendering & Adoption Planning:
        prepare_adopt(workspace_config, package_names, options) [Layer 5]
            - Optional Reverse Sync & Render Refresh (_sync_and_render_before_adopt):
                * run_primitive_1_reverse_sync (when not options.no_reverse_sync)
                * get_drifted_packages [Layer 1c]
                * run_primitive_2_render_packages (when options.with_render or not options.no_render)
            - Per-Package 3-Stage Planning (plan_package_adopt [Layer 1c]):
                * Stage 1 — Collect & Normalize Raw Git Status (_collect_package_git_status)
                * Stage 2 — Route Drift Categories (_route_package_drifts)
                * Stage 3 — Validate Source Cleanliness & Calculate Patches (_precompute_package_drift_patches)
            -> Returns AdoptPlan(packages, package_plans, options)

    2. Multi-Package Adoption Execution & Commit Boundary:
        execute_adopt(workspace_config, plan: AdoptPlan) [Layer 5]
            - If dry_run -> compiles inspectable PackageAdoptResult summaries (_dry_run_package_adopt [Layer 4])
            - Iterates over plan.package_plans:
                execute_package_adopt(workspace_config, pkg_plan, options) [Layer 4]
                    * trigger_pre_source_hook (when package has divergent drifts)
                    * handle_single_addition [Layer 3 -> Layer 2]
                    * handle_single_deletion [Layer 3 -> Layer 2]
                    * handle_single_rename [Layer 3 -> Layer 2]
                    * handle_single_modification [Layer 3 -> Layer 2]
                    * Stages resolved paths in install/ ('git add -- <resolved_paths>')
            - Commits staged changes in install/ (commit_staged_repo_changes)
            -> Returns Aggregated AdoptResult

    3. Public Composite Primitive Entry Points:
        run_primitive_adopt_drifts(workspace_config, package_names, options) [Layer 5]
            = prepare_adopt >> execute_adopt
        resolve_and_commit_converged_drifts(workspace_config, package_names, dry_run) [Layer 5]
            = prepare_adopt(no_render=True, no_reverse_sync=True, skip_patches=True)
              >> execute_converged_drifts (stage_converged_package_drifts + commit_staged_repo_changes)

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 0: Domain Models (`models/adopt_models.py`)
        AdoptStatus, ItemAdoptOutcome, TokenKind, TokenizedLine, SkeletonMatch,
        DiffRelation, DiffUnitType, DiffUnit, CleanPatchHunk, ConflictCluster,
        TwoStepDerenderPlan, AdoptPatch, AdoptOptions, PackageDriftFiles,
        PackageAdoptPlan, AdoptPlan, PackageAdoptResult, AdoptResult
    Layer 1a-1: Hunk Fission & Single-Line Token Calculus (`adopt/adopt_fission.py`)
        tokenize_line, compute_skeleton_match, find_monotonic_line_matches,
        fission_diff_units, try_derender_single_line, refine_single_line_units
    Layer 1a-2: Orthogonal 3-Way Classification & Two-Step Plan (`adopt/adopt_derender.py`)
        split_rh_deletes_at_tr_boundaries, build_two_step_derender_plan,
        build_display_unified_diff, generate_derendered_patch
    Layer 1b: Pure-Python Unified Patch & Conflict Engine (`adopt/adopt_patch.py`)
        generate_install_git_patch, generate_source_to_install_patch,
        parse_unified_patch_hunks, check_patch_conflicts, apply_source_patch
    Layer 1c: 3-Stage Drift Discovery, Category Routing & Planning (`adopt/adopt_plan.py`)
        get_drifted_packages, _collect_package_git_status, _route_package_drifts,
        assert_source_file_clean, _precompute_package_drift_patches, plan_package_adopt
    Layer 2: Atomic Single-File Reconciliation Actions (`adopt/adopt_actions.py`)
        adopt_addition, ignore_addition, adopt_deletion, patch_and_edit,
        adopt_rename, fallback_over_render, fallback_side_by_side
    Layer 3: Single-Item Interactive & Non-Interactive Dispatchers (`adopt/adopt_handlers.py`)
        handle_single_addition, handle_single_deletion,
        handle_single_rename, handle_single_modification
    Layer 4: Single-Package Adoption Execution (`adopt/adopt_package.py`)
        stage_converged_package_drifts, execute_package_adopt, adopt_one_package_drifts
    Layer 5: Multi-Package Primitive Orchestrator (`primitives/adopt_repo.py`)
        prepare_adopt, execute_converged_drifts,
        resolve_and_commit_converged_drifts, execute_adopt, run_primitive_adopt_drifts
===============================================================================
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..adopt import (
    _derive_adopt_result_status,
    _dry_run_package_adopt,
    execute_package_adopt,
    get_drifted_packages,
    plan_package_adopt,
    stage_converged_package_drifts,
)
from ..config.workspace_config import WorkspaceConfig
from ..models import (
    AdoptOptions,
    AdoptPlan,
    AdoptResult,
    AdoptStatus,
    PackageAdoptResult,
)
from ..utils.func_utils import choose
from ..utils.git_utils import commit_staged_repo_changes
from .render_repo import RenderOptions, run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 5: Public Primitive Entry Point & Multi-Package Orchestration
# =====================================================================


def _sync_and_render_before_adopt(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str],
    options: AdoptOptions,
) -> Tuple[List[str], List[str]]:
    """Synchronizes host changes into install/ and renders drifted source packages before adoption planning.

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Available source packages resolved via filter_source_packages_by_target.
        options: AdoptOptions controlling reverse-sync, render, and hook behavior.

    Returns:
        Tuple of (drifted_pkgs, rendered_pkgs), both sublists of package_names.
    """
    if not options.no_reverse_sync and package_names:
        run_primitive_1_reverse_sync(
            workspace_config,
            package_names=package_names,
            missing_ok=True,
        )

    drifted_set = set(get_drifted_packages(workspace_config))
    drifted_pkgs = list(filter(drifted_set.__contains__, package_names))
    if options.no_render or not drifted_pkgs:
        return drifted_pkgs, []

    hook_flags = options.get_hook_flags(settings=workspace_config.settings)
    render_res = run_primitive_2_render_packages(
        workspace_config,
        target_pkgs=drifted_pkgs,
        options=RenderOptions.resolve(hook_flags),
    )
    if hook_flags.no_hooks or hook_flags.dry_run:
        return drifted_pkgs, []

    rendered_set = {
        pkg_res.package
        for pkg_res in render_res.packages
        if pkg_res.status in ("SUCCESS", "UP_TO_DATE")
    }
    rendered_pkgs = list(filter(rendered_set.__contains__, drifted_pkgs))
    return drifted_pkgs, rendered_pkgs


def execute_converged_drifts(
    workspace_config: WorkspaceConfig,
    plan: AdoptPlan,
) -> Dict[str, List[Path]]:
    """Executes only the converged-files plan: stages converged files and commits them in install/."""
    converged_by_pkg: Dict[str, List[Path]] = {
        pkg_plan.package: staged_paths
        for pkg_plan in plan.plans
        if pkg_plan.converged_files
        and (staged_paths := stage_converged_package_drifts(workspace_config, pkg_plan))
    }

    if converged_by_pkg:
        converged_pkgs = sorted(converged_by_pkg.keys())
        pkg_word = choose(len(converged_pkgs) == 1, "package", "packages")
        commit_staged_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Auto-resolved converged drifts for {pkg_word} {', '.join(converged_pkgs)}",
            repo_name="install repo",
        )

    return converged_by_pkg


def resolve_and_commit_converged_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    dry_run: bool = False,
) -> Dict[str, List[Path]]:
    """Dedicated entry point that plans adoption without reverse-sync, render, or patch generation, and executes only converged files.

    Not called by run_primitive_adopt_drifts (since normal adoption planning and execution
    already handle converged_files directly). Used by deploy_repo after reverse-sync to
    auto-resolve and commit converged files before checking for remaining divergent drift.
    """
    plan = prepare_adopt(
        workspace_config,
        package_names=package_names,
        options=AdoptOptions(
            dry_run=dry_run,
            no_render=True,
            no_reverse_sync=True,
            skip_patches=True,
        ),
    )
    if dry_run:
        return {
            pkg_plan.package: list(pkg_plan.converged_files)
            for pkg_plan in plan.plans
            if pkg_plan.converged_files
        }
    return execute_converged_drifts(workspace_config, plan)


def prepare_adopt(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    options: Optional[AdoptOptions] = None,
) -> AdoptPlan:
    """Synchronizes host changes, renders drifted templates, and creates an AdoptPlan."""
    opts = options or AdoptOptions()
    if opts.no_render and opts.with_render:
        raise ValueError("Cannot specify both --no-render and --with-render.")
    available_packages = workspace_config.filter_source_packages_by_target(
        target_packages=package_names or None
    )
    drifted_pkgs, rendered_pkgs = _sync_and_render_before_adopt(
        workspace_config,
        package_names=available_packages,
        options=opts,
    )

    resolved_packages = choose(bool(package_names), available_packages, drifted_pkgs)
    rendered_pkg_set = set(rendered_pkgs)

    plans = [
        plan_package_adopt(
            workspace_config,
            pkg,
            skip_patches=opts.skip_patches,
            pre_source_already_triggered=(pkg in rendered_pkg_set),
            force=opts.force,
        )
        for pkg in resolved_packages
    ]
    return AdoptPlan(plans=plans, options=opts)


def execute_adopt(
    workspace_config: WorkspaceConfig,
    plan: AdoptPlan,
) -> AdoptResult:
    """Executes adoption for all plans in AdoptPlan, staging adopted/converged files and committing changes."""
    opts = plan.options

    # Dry-run Mode
    if opts.dry_run:
        dry_run_pkgs = [_dry_run_package_adopt(pkg_plan) for pkg_plan in plan.plans]
        return AdoptResult(
            command="adopt",
            status=_derive_adopt_result_status(dry_run_pkgs),
            packages=dry_run_pkgs,
            dry_run=True,
        )

    package_results: List[PackageAdoptResult] = [
        execute_package_adopt(
            workspace_config=workspace_config,
            plan=pkg_plan,
            options=opts,
        )
        for pkg_plan in plan.plans
    ]
    adopted_pkgs = [p.package for p in package_results if p.has_adopted_changes]

    if adopted_pkgs:
        pkg_word = choose(len(adopted_pkgs) == 1, "package", "packages")
        commit_staged_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Resolved and locked drifts for {pkg_word} {', '.join(adopted_pkgs)}",
            repo_name="install repo",
        )

    return AdoptResult(
        command="adopt",
        status=_derive_adopt_result_status(package_results),
        packages=package_results,
        dry_run=False,
    )


def run_primitive_adopt_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    options: Optional[AdoptOptions] = None,
) -> AdoptResult:
    """High-level orchestrator for adopting system drifts back to declarative templates (Primitive Adopt).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to adopt, or empty/omitted to discover all drifted packages.
        options: Optional AdoptOptions controlling interactive mode, conflict markers, force, dry_run,
            pre-adoption rendering, reverse-sync, and hook execution flags.

    Returns:
        AdoptResult containing detailed results for all adopted packages.
    """
    opts = options or AdoptOptions()

    # 1. Preparation (reverse-sync + scoped render + converged/divergent planning)
    plan = prepare_adopt(workspace_config, package_names=package_names, options=opts)
    if not plan.plans or not plan.has_drifts:
        if not opts.dry_run:
            if not package_names:
                logger.info("✨ No drifted packages found in local state database.")
            else:
                for p in plan.plans:
                    logger.info(f"✨ Package '{p.package}' has no drifts.")
        return AdoptResult(
            command="adopt",
            status=AdoptStatus.UP_TO_DATE,
            packages=[
                PackageAdoptResult(package=p.package, status=AdoptStatus.UP_TO_DATE, plan=p)
                for p in plan.plans
            ],
            dry_run=opts.dry_run,
        )

    # 2. Execution (stages converged_files directly and reconciles divergent drifts)
    return execute_adopt(
        workspace_config=workspace_config,
        plan=plan,
    )
