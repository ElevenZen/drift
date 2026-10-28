"""Primitive 8: Multi-Package Drift Adoption Orchestrator (`drift adopt`, Layer 5).

Architecture & Call Chain Overview:
===================================
Orchestrates workspace-wide drift adoption (`install/` -> `src/`) on top of the modular
`drift.adopt` engine (`adopt_derender`, `adopt_patch`, `adopt_plan`, `adopt_actions`,
`adopt_handlers`, `adopt_package`):

Call Chain:
-----------
run_primitive_adopt_drifts (Layer 5)
 ├── prepare_adopt (Layer 5)
 │    ├── _sync_and_render_before_adopt
 │    │    ├── run_primitive_1_reverse_sync
 │    │    ├── get_drifted_packages (Layer 1c)
 │    │    └── run_primitive_2_render_packages
 │    └── plan_package_adopt (Layer 1c, per package)
 │         ├── _temporary_stage_package_index
 │         ├── _normalize_keep_file_drifts
 │         ├── _partition_converged_package_drifts
 │         └── _precompute_package_drift_patches
 │              ├── generate_derendered_patch (Layer 1a, templated modifications & renames)
 │              └── generate_install_git_patch / generate_retargeted_patch (Layer 1b, static files)
 ├── _dry_run_package_adopt (Layer 4, when options.dry_run=True)
 └── execute_adopt (Layer 5)
      ├── execute_package_adopt (Layer 4, per package)
      │    ├── trigger_pre_source_hook
      │    ├── handle_single_addition (Layer 3 -> Layer 2)
      │    ├── handle_single_deletion (Layer 3 -> Layer 2)
      │    ├── handle_single_rename (Layer 3 -> Layer 2)
      │    ├── handle_single_modification (Layer 3 -> Layer 2)
      │    └── git -C <install_path> add -- <resolved_paths>
      └── commit_staged_repo_changes (commits staged changes in install/)

Dedicated Converged-Only Entry Point (called by deploy_repo):
-------------------------------------------------------------
resolve_and_commit_converged_drifts (Layer 5)
 ├── prepare_adopt(..., no_render=True, no_reverse_sync=True, skip_patches=True)
 └── execute_converged_drifts (Layer 5)
      ├── stage_converged_package_drifts (Layer 4)
      └── commit_staged_repo_changes

Layers (ordered bottom-up by dependency):
-----------------------------------------
- Layer 0 (`models/adopt_models.py`): `AdoptStatus`, `ItemAdoptOutcome`, `TemplateHunk`,
  `DerenderedPair`, `AdoptOptions`, `PackageDriftFiles`, `PackageAdoptPlan`, `AdoptPlan`,
  `PackageAdoptResult`, `AdoptResult`.
- Layer 1a (`adopt/adopt_derender.py`): 3-way hunk-map template de-rendering (`T <-> R <-> H`).
- Layer 1b (`adopt/adopt_patch.py`): Unified diff generation, conflict probing, and
  cross-platform patch application (`patch` + `git merge-file`).
- Layer 1c (`adopt/adopt_plan.py`): Drift discovery, convergence partitioning, and planning.
- Layer 2 (`adopt/adopt_actions.py`): Atomic single-file reconciliation actions.
- Layer 3 (`adopt/adopt_handlers.py`): Single-item interactive & non-interactive dispatchers.
- Layer 4 (`adopt/adopt_package.py`): Single-package adoption execution & status derivation.
- Layer 5 (`primitives/adopt_repo.py`): Multi-package primitive orchestrator & commit boundary.
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

    # 2. Dry-run Mode
    if opts.dry_run:
        dry_run_pkgs = [_dry_run_package_adopt(pkg_plan) for pkg_plan in plan.plans]
        return AdoptResult(
            command="adopt",
            status=_derive_adopt_result_status(dry_run_pkgs),
            packages=dry_run_pkgs,
            dry_run=True,
        )

    # 3. Execution (stages converged_files directly and reconciles divergent drifts)
    return execute_adopt(
        workspace_config=workspace_config,
        plan=plan,
    )
