"""Single-Package Drift Adoption Execution and Status Derivation (Layer 4).

Architecture & Call Chain Overview:
===================================
Executes a `PackageAdoptPlan` for a single package, staging converged and adopted
paths in the `install/` repository index and deriving the 5-state `AdoptStatus`:

1. Status Derivation:
   - `_derive_package_adopt_status`: Maps `(has_drifts, has_adopted_changes, has_unresolved_files)`
     to `AdoptStatus`.
   - `_derive_adopt_result_status`: Aggregates `PackageAdoptResult` statuses into a
     workspace-wide `AdoptStatus`.
2. Converged Staging & Dry-Run Simulation:
   - `stage_converged_package_drifts`: Stages `plan.converged_files` in `install/<pkg>/`.
   - `_dry_run_package_adopt`: Produces a simulated `PackageAdoptResult` without mutations.
3. Single-Package Execution:
   - `execute_package_adopt`: Triggers `pre_source` hook (if not already run during pre-render),
     dispatches additions, deletions, renames, and modifications through Layer 3 handlers,
     and stages resolved paths in `install/`.
   - `adopt_one_package_drifts`: Plans and executes (or dry-runs) adoption for a single package.
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess
from typing import List, Optional, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..hooks.lifecycle_hooks import trigger_pre_source_hook
from ..models.adopt_models import (
    AdoptOptions,
    AdoptStatus,
    ItemAdoptOutcome,
    PackageAdoptPlan,
    PackageAdoptResult,
    get_rename_patch_key,
)
from ..utils.func_utils import branch
from .adopt_handlers import (
    handle_single_addition,
    handle_single_deletion,
    handle_single_modification,
    handle_single_rename,
)
from .adopt_plan import plan_package_adopt

logger = logging.getLogger(__name__)


def _derive_package_adopt_status(
    has_drifts: bool,
    has_adopted_changes: bool,
    has_unresolved_files: bool,
) -> AdoptStatus:
    """Derives the 5-state AdoptStatus for a single package."""
    if not has_drifts:
        return AdoptStatus.UP_TO_DATE
    if has_adopted_changes and not has_unresolved_files:
        return AdoptStatus.SUCCESS
    if has_adopted_changes and has_unresolved_files:
        return AdoptStatus.PARTIAL
    return AdoptStatus.SKIPPED


def _derive_adopt_result_status(package_results: Sequence[PackageAdoptResult]) -> AdoptStatus:
    """Derives the aggregate 5-state AdoptStatus across all packages."""
    if any(p.status == AdoptStatus.FAILED for p in package_results):
        return AdoptStatus.FAILED
    if not package_results or all(p.status == AdoptStatus.UP_TO_DATE for p in package_results):
        return AdoptStatus.UP_TO_DATE
    any_adopted = any(p.has_adopted_changes for p in package_results)
    any_unresolved = any(p.has_unresolved_files for p in package_results)
    if any_adopted and not any_unresolved:
        return AdoptStatus.SUCCESS
    if any_adopted and any_unresolved:
        return AdoptStatus.PARTIAL
    return AdoptStatus.SKIPPED


def stage_converged_package_drifts(
    workspace_config: WorkspaceConfig,
    plan: PackageAdoptPlan,
) -> List[Path]:
    """Stages planned converged_files for a single package in install/<pkg>/."""
    if not plan.converged_files:
        return []

    stage_args = [(Path(plan.package) / p).as_posix() for p in plan.converged_files]
    res_add = subprocess.run(
        ["git", "-C", workspace_config.install_path.as_posix(), "add", "--", *stage_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_add.returncode != 0:
        logger.warning(
            f"Staging converged changes for '{plan.package}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
        )
        return []

    logger.info(
        f"✨ Auto-resolved {len(plan.converged_files)} converged file(s) in '{plan.package}': "
        f"{', '.join(p.as_posix() for p in plan.converged_files)}"
    )
    return list(plan.converged_files)


def _dry_run_package_adopt(pkg_plan: PackageAdoptPlan) -> PackageAdoptResult:
    """Builds a pure simulated PackageAdoptResult for a single package in dry-run mode."""
    if not pkg_plan.has_drifts:
        return PackageAdoptResult(
            package=pkg_plan.package,
            status=AdoptStatus.UP_TO_DATE,
            plan=pkg_plan,
        )
    return PackageAdoptResult(
        package=pkg_plan.package,
        adopted_additions=[p.as_posix() for p in pkg_plan.additions],
        adopted_deletions=[p.as_posix() for p in pkg_plan.deletions],
        adopted_modifications=[p.as_posix() for p in pkg_plan.modifications],
        adopted_renames=[f"{old.as_posix()} -> {new.as_posix()}" for old, new in pkg_plan.renames],
        adopted_converged=[p.as_posix() for p in pkg_plan.converged_files],
        status=AdoptStatus.SUCCESS,
        plan=pkg_plan,
    )


def execute_package_adopt(
    workspace_config: WorkspaceConfig,
    plan: PackageAdoptPlan,
    options: Optional[AdoptOptions] = None,
) -> PackageAdoptResult:
    """Executes adoption for a single PackageAdoptPlan, reconciling source files and staging adopted paths.

    Planned converged_files are staged directly without mutating src/, while divergent additions,
    deletions, renames, and modifications are reconciled into src/ and then staged.

    Note:
        This function stages resolved/adopted files ('git add -- <resolved_paths>') in the install/
        repository index, but does NOT commit them. Committing staged changes is performed at
        Layer 5 orchestration (e.g. execute_adopt) or via commit_staged_repo_changes.
    """
    opts = options or AdoptOptions()
    if not plan.has_drifts:
        logger.info(f"✨ Package '{plan.package}' has no drifts.")
        return PackageAdoptResult(
            package=plan.package,
            status=AdoptStatus.UP_TO_DATE,
            plan=plan,
        )

    if plan.has_divergent_drifts and not plan.pre_source_already_triggered:
        trigger_pre_source_hook(
            workspace_config,
            plan.package,
            flags=opts.get_hook_flags(settings=workspace_config.settings),
        )

    adopted_converged: List[str] = [p.as_posix() for p in plan.converged_files]
    adopted_additions: List[str] = []
    adopted_deletions: List[str] = []
    adopted_renames: List[str] = []
    adopted_modifications: List[str] = []
    skipped_files: List[str] = []
    conflicted_files: List[str] = []
    resolved_paths: List[Path] = list(plan.converged_files)

    if plan.converged_files:
        logger.info(
            f"✨ Auto-resolved {len(plan.converged_files)} converged file(s) in '{plan.package}': "
            f"{', '.join(p.as_posix() for p in plan.converged_files)}"
        )

    for rel_path in plan.additions:
        outcome = handle_single_addition(
            plan.render_engines,
            plan.src_pkg_dir,
            plan.src_dir_to_render,
            plan.install_pkg_dir,
            rel_path,
            opts.interactive,
            force=opts.force,
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_additions.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    for rel_path in plan.deletions:
        outcome = handle_single_deletion(
            plan.render_engines,
            plan.package,
            plan.src_dir_to_render,
            rel_path,
            opts.interactive,
            force=opts.force,
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_deletions.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    for old_rel_path, new_rel_path in plan.renames:
        patch_content = plan.rename_patches.get(get_rename_patch_key(old_rel_path, new_rel_path))
        outcome = handle_single_rename(
            plan.render_engines,
            plan.package,
            plan.src_pkg_dir,
            plan.src_dir_to_render,
            plan.install_pkg_dir,
            workspace_config.install_path,
            plan.render_pkg_dir,
            old_rel_path,
            new_rel_path,
            opts.interactive,
            opts.accept_conflicts,
            force=opts.force,
            precomputed_patch=patch_content,
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_renames.append(f"{old_rel_path.as_posix()} -> {new_rel_path.as_posix()}")
            resolved_paths.append(old_rel_path)
            resolved_paths.append(new_rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(old_rel_path.as_posix())
            conflicted_files.append(new_rel_path.as_posix())
        else:
            skipped_files.append(old_rel_path.as_posix())
            skipped_files.append(new_rel_path.as_posix())

    for rel_path in plan.modifications:
        key = rel_path.as_posix()
        patch_content = plan.mod_patches.get(key)
        outcome = handle_single_modification(
            plan.render_engines,
            plan.package,
            plan.src_pkg_dir,
            plan.src_dir_to_render,
            plan.install_pkg_dir,
            workspace_config.install_path,
            plan.render_pkg_dir,
            rel_path,
            opts.interactive,
            opts.accept_conflicts,
            force=opts.force,
            precomputed_patch=patch_content,
            src_match=plan.mod_targets.get(key),
            precomputed_conflict=plan.mod_conflicts.get(key),
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_modifications.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    if resolved_paths:
        stage_args = [(Path(plan.package) / p).as_posix() for p in resolved_paths]
        res_add = subprocess.run(
            ["git", "-C", workspace_config.install_path.as_posix(), "add", "--", *stage_args],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_add.returncode != 0:
            logger.warning(
                f"Staging adopted changes for '{plan.package}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
            )

    has_adopted = bool(resolved_paths)
    has_unresolved = bool(skipped_files or conflicted_files)
    status = _derive_package_adopt_status(
        has_drifts=plan.has_drifts,
        has_adopted_changes=has_adopted,
        has_unresolved_files=has_unresolved,
    )
    return PackageAdoptResult(
        package=plan.package,
        adopted_additions=adopted_additions,
        adopted_modifications=adopted_modifications,
        adopted_deletions=adopted_deletions,
        adopted_renames=adopted_renames,
        adopted_converged=adopted_converged,
        skipped_files=skipped_files,
        conflicted_files=conflicted_files,
        status=status,
        plan=plan,
    )


def adopt_one_package_drifts(
    workspace_config: WorkspaceConfig,
    pkg: str,
    options: Optional[AdoptOptions] = None,
) -> PackageAdoptResult:
    """Adopts drifts for a single package using decoupled plan and execution phases.

    Note:
        adopt_one_package_drifts does NOT commit the install/ repo; it only reconciles source files
        and stages adopted paths in the install/ repository index. Committing staged changes is
        the responsibility of Layer 5 orchestration (execute_adopt / run_primitive_adopt_drifts)
        or explicit commit helpers (commit_staged_repo_changes).
    """
    opts = options or AdoptOptions()
    plan = plan_package_adopt(workspace_config, pkg, skip_patches=opts.skip_patches)
    return branch(
        opts.dry_run,
        lambda: _dry_run_package_adopt(plan),
        lambda: execute_package_adopt(
            workspace_config=workspace_config,
            plan=plan,
            options=opts,
        ),
    )
