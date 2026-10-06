"""Primitive: Deployment Planning and Preview (drift plan / drift deploy --dry-run)

Architecture & Call Chain Overview:
-----------------------------------
1. Dependency Ordering & Pre-Flight Validation [Layer 1]:
   - check_midway_states: Detects midway transaction states (e.g. incomplete previous operations).
   - load_source_package_metadata: Loads source PackageConfig metadata with dry_run=True.
   - audit_host_drift: Non-destructive host drift inspection (pure read-only).
   - resolve_target_package_order: Resolves topological installation order.

2. Ephemeral Sandbox Topology & Multi-Phase Planning [Layer 2]:
   - enter_plan_sandbox: Context manager setting up ephemeral temporary sandbox directory.
   - execute_sandbox_render_phase: Compiles source templates in sandbox (Phase 1), isolates errors,
     blocks dependent packages, and rebases action paths back to canonical render/.
   - execute_sandbox_stage_phase: Plans staging diffs against canonical install/ (Phase 2) and rebases paths.
   - execute_sandbox_install_phase: Plans host file deliveries (Phase 3) against sandbox with canonical source rebasing.

3. Single Package Preview Assembly [Layer 3]:
   - assemble_package_deploy_preview: Assembles PackageDeployPreview from audit, render, stage, and install plans.

4. Workspace Deploy Preview Orchestration [Layer 4]:
   - preview_deploy: Public coordinator calling sub-stages and compiling WorkspaceDeployPreview.
   - run_primitive_plan: Primitive entry point delegating to preview_deploy.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Dict, Generator, List, Mapping, Optional, Sequence, Set, Tuple

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..core.constants import STATE_REGISTRY_FILE_NAME
from ..core.file_action import NO_CHANGE_ACTION_TYPES
from ..core.state_registry import load_state_registry, StateRegistry
from ..core.result_models import (
    PackageDeployPreview,
    WorkspaceDeployPreview,
    PackageReverseSyncPlan,
    PackageRenderResult,
    PackageStagePlan,
    PackageInstallPlan,
)
from ..render.render_cache import RenderCache
from ..render.render_package import render_package, RenderOptions
from ..hooks.lifecycle_hooks import HookExecFlags
from .package_assertions import resolve_target_package_order, assert_no_cross_package_conflicts
from .reverse_sync import prepare_reverse_sync
from .stage_repo import plan_package_stage
from .install_repo import (
    assert_packages_install_ready,
    PackageInstallContext,
    plan_package_install,
)
from .deploy_repo import DeployOptions

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Inspection, Metadata Loading & Host Drift Audit
# =====================================================================

def check_midway_states(
    discovered_pkgs: Sequence[str],
    state_registry: StateRegistry,
    force: bool,
) -> Dict[str, str]:
    """Inspects state registry and returns mapping of package name to error message for midway states."""
    if force:
        return {}
    midway_pkgs = state_registry.get_rollback_eligible_packages(discovered_pkgs)
    return {
        pkg: (
            f"Package '{pkg}' is in midway transaction state '{state}'. "
            f"Run 'drift rollback {pkg}' or pass '--force'."
        )
        for pkg, state in midway_pkgs
    }


def load_source_package_metadata(
    workspace_config: WorkspaceConfig,
    discovered_pkgs: Sequence[str],
) -> Dict[str, PackageConfig]:
    """Loads PackageConfig metadata from source directory with dry_run=True to isolate compilation from render/."""
    return {
        pkg: PackageConfig.from_source_dir(
            workspace_config.source_path / pkg,
            workspace_config=workspace_config,
            dry_run=True,
            silent=True,
        )
        for pkg in discovered_pkgs
    }


def audit_host_drift(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str],
) -> Dict[str, PackageReverseSyncPlan]:
    """Executes non-destructive host drift inspection (pure read-only) against install/ database."""
    rev_sync_plan = prepare_reverse_sync(
        workspace_config=workspace_config,
        package_names=package_names,
        missing_ok=True,
    )
    return {p.package: p for p in rev_sync_plan.plans}


# =====================================================================
# Layer 2: Ephemeral Sandbox Topology & Isolated Render
# =====================================================================

@contextmanager
def enter_plan_sandbox(
    workspace_config: WorkspaceConfig,
    target_packages: Sequence[str] = (),
) -> Generator[Tuple[Path, WorkspaceConfig], None, None]:
    """Creates an ephemeral sandbox directory and builds a sandbox-redirected WorkspaceConfig.

    Pre-copies existing package render trees and install/state.toml to preserve baseline state
    for cache hits and stage diffing. Upon exit, the sandbox is completely removed.
    """
    with tempfile.TemporaryDirectory(prefix="drift_plan_sandbox_") as tmp_dir:
        sandbox_root = Path(tmp_dir).resolve()
        sandbox_render = sandbox_root / "render"
        sandbox_render.mkdir(parents=True, exist_ok=True)

        # Pre-copy existing render/<pkg> to sandbox_render/<pkg>
        real_render = workspace_config.render_path
        for pkg in target_packages:
            src_pkg_render = real_render / pkg
            if src_pkg_render.is_dir():
                dst_pkg_render = sandbox_render / pkg
                shutil.copytree(src_pkg_render, dst_pkg_render, symlinks=True)

        sandbox_workspace_config = replace(
            workspace_config,
            workspace=replace(
                workspace_config.workspace,
                render_directory=sandbox_render,
            ),
            render_cache=RenderCache(),
            render_path_mask=workspace_config.render_path,
            install_path_mask=workspace_config.install_path,
        )

        yield sandbox_root, sandbox_workspace_config


def execute_sandbox_render_phase(
    sandbox_workspace_config: WorkspaceConfig,
    real_workspace_config: WorkspaceConfig,
    packages_install_order: Sequence[str],
    pkg_metadata: Dict[str, PackageConfig],
    options: DeployOptions,
    midway_errors: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, PackageRenderResult], Dict[str, str]]:
    """Compiles source packages in an isolated sandbox and rebases action paths to canonical render/.

    Returns:
        A tuple of (render_plans, render_errors) mapping package name to results.
    """
    render_plans: Dict[str, PackageRenderResult] = {}
    render_errors: Dict[str, str] = {}
    blocked_pkgs: Set[str] = set()

    if midway_errors:
        for pkg, err in midway_errors.items():
            render_errors[pkg] = err

    effective_no_hooks = options.no_hooks or (not options.with_hooks)
    render_opts = RenderOptions(
        no_cache=options.no_cache,
        dry_run=False,
        silent=True,
        flags=HookExecFlags(
            no_hooks=effective_no_hooks,
            no_cache=options.no_cache,
            dry_run=False,
        ),
    )

    for pkg in packages_install_order:
        if midway_errors and pkg in midway_errors:
            continue

        pkg_config = pkg_metadata.get(pkg)
        if pkg_config is not None:
            failed_dep = next(
                (dep for dep in pkg_config.package.dependencies.required_names if dep in render_errors),
                None,
            )
            if failed_dep is not None:
                err_msg = f"Prerequisite package '{failed_dep}' failed to render."
                render_errors[pkg] = err_msg
                blocked_pkgs.add(pkg)
                continue

        pkg_dir = sandbox_workspace_config.source_path / pkg
        scoped_render_opts = RenderOptions(
            no_cache=options.no_cache,
            dry_run=False,
            silent=True,
            render_dir_mask=real_workspace_config.render_path / pkg,
            flags=HookExecFlags(
                no_hooks=effective_no_hooks,
                no_cache=options.no_cache,
                dry_run=False,
            ),
        )
        try:
            render_res = render_package(
                sandbox_workspace_config,
                pkg_dir,
                options=scoped_render_opts,
            )
            rebased_res = render_res.rebased(
                sandbox_workspace_config.render_path,
                real_workspace_config.render_path,
            )
            render_plans[pkg] = rebased_res
            if not render_res.is_success:
                render_errors[pkg] = render_res.error or f"Package '{pkg}' rendering failed."
        except Exception as e:
            logger.debug(f"Render exception during plan for package '{pkg}':", exc_info=True)
            err_msg = str(e)
            render_errors[pkg] = err_msg
            render_plans[pkg] = PackageRenderResult(
                package=pkg,
                status="FAILED",
                error=err_msg,
            )

    return render_plans, render_errors


def execute_sandbox_stage_phase(
    sandbox_workspace_config: WorkspaceConfig,
    real_workspace_config: WorkspaceConfig,
    packages_order: Sequence[str],
    pkg_metadata: Dict[str, PackageConfig],
    render_errors: Mapping[str, str],
) -> Tuple[Dict[str, PackageStagePlan], Dict[str, str]]:
    """Diffs sandbox rendered packages against canonical install/ and rebases action paths."""
    stage_plans: Dict[str, PackageStagePlan] = {}
    stage_errors: Dict[str, str] = {}
    for pkg in packages_order:
        if pkg in render_errors:
            continue
        cfg = pkg_metadata.get(pkg)
        if cfg is not None and not cfg.package.enable_install:
            continue
        try:
            stage_plan = plan_package_stage(
                pkg=pkg,
                install_base=real_workspace_config.install_path,
                render_base=sandbox_workspace_config.render_path,
            )
            stage_plans[pkg] = stage_plan.rebased(
                sandbox_workspace_config.render_path,
                real_workspace_config.render_path,
            )
        except Exception as e:
            logger.debug(f"Stage planning exception for package '{pkg}':", exc_info=True)
            err_msg = str(e) or f"Stage planning failed for package '{pkg}'."
            stage_errors[pkg] = err_msg
            stage_plans[pkg] = PackageStagePlan(
                package=pkg,
                actions=[],
            )
    return stage_plans, stage_errors


def execute_sandbox_install_phase(
    sandbox_workspace_config: WorkspaceConfig,
    real_workspace_config: WorkspaceConfig,
    packages_order: Sequence[str],
    pkg_metadata: Dict[str, PackageConfig],
    options: DeployOptions,
    render_errors: Mapping[str, str],
    stage_errors: Mapping[str, str],
    state_registry: StateRegistry,
) -> Tuple[Dict[str, PackageInstallPlan], Dict[str, str], List[Exception]]:
    """Executes host install planning package-by-package with batch cross-package conflict validation."""
    install_plans: Dict[str, PackageInstallPlan] = {}
    install_errors: Dict[str, str] = {}
    global_errors: List[Exception] = []

    eligible_pkgs = [
        pkg for pkg in packages_order
        if pkg not in render_errors and pkg not in stage_errors
        and (pkg in pkg_metadata and pkg_metadata[pkg].package.enable_install)
    ]
    if not eligible_pkgs:
        return install_plans, install_errors, global_errors

    effective_no_hooks = options.no_hooks or (not options.with_hooks)
    hook_flags = HookExecFlags(
        no_hooks=effective_no_hooks,
        no_cache=options.no_cache,
        dry_run=True,
    )

    # Prepare install workspace config whose install_directory points to sandbox render
    install_workspace_config = replace(
        sandbox_workspace_config,
        workspace=replace(
            sandbox_workspace_config.workspace,
            install_directory=sandbox_workspace_config.render_path,
            render_directory=sandbox_workspace_config.render_path,
        ),
    )

    # 1. Batch Pre-flight Cross-Package Conflict Guard
    try:
        assert_no_cross_package_conflicts(
            workspace_config=install_workspace_config,
            discovered_packages=eligible_pkgs,
            pkg_metadata_map=pkg_metadata,
            state_registry=state_registry,
        )
    except Exception as e:
        logger.debug("Cross-package collision detected during install planning:", exc_info=True)
        global_errors.append(e)

    # 2. Package-by-Package Installation Planning with dependency checks turned OFF
    for pkg in eligible_pkgs:
        meta = pkg_metadata.get(pkg)
        if meta is None:
            continue
        try:
            assert_packages_install_ready(
                workspace_config=install_workspace_config,
                discovered_packages=[pkg],
                pkg_metadata_map={pkg: meta},
                hook_flags=hook_flags,
                state_registry=state_registry,
                force=options.force,
                full_universe_deps=None,
            )
            context = PackageInstallContext.from_package(
                workspace_config=install_workspace_config,
                state_registry=state_registry,
                metadata=meta,
                reinstall=options.reinstall,
            )
            deployable_files = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)
            deployed_files = state_registry.get_package_deployed_files(pkg)
            target_migrated_from = state_registry.get_target_migrated_from(pkg, context.target_dir)

            plan = plan_package_install(
                context=context,
                deployable_files=deployable_files,
                deployed_files=deployed_files,
                target_migrated_from=target_migrated_from,
            )
            install_plans[pkg] = plan
        except Exception as e:
            logger.debug(f"Install planning exception for package '{pkg}':", exc_info=True)
            install_errors[pkg] = str(e) or f"Install planning failed for package '{pkg}'."

    return install_plans, install_errors, global_errors


# =====================================================================
# Layer 3: Single Package Preview Assembly
# =====================================================================

def assemble_package_deploy_preview(
    pkg: str,
    reverse_sync_plan: Optional[PackageReverseSyncPlan] = None,
    render_plan: Optional[PackageRenderResult] = None,
    stage_plan: Optional[PackageStagePlan] = None,
    install_plan: Optional[PackageInstallPlan] = None,
    midway_error: Optional[str] = None,
    render_error: Optional[str] = None,
    stage_error: Optional[str] = None,
    install_error: Optional[str] = None,
) -> PackageDeployPreview:
    """Assembles a PackageDeployPreview for a single package from Phase 0 audit and Phase 1-3 results."""
    error_msg = midway_error or render_error or stage_error or install_error
    error_obj: Optional[Exception] = RuntimeError(error_msg) if error_msg else None

    drift_warning: Optional[str] = None
    if reverse_sync_plan is not None and reverse_sync_plan.has_changes:
        mutating_actions = [
            a for a in reverse_sync_plan.actions
            if a.action_type not in NO_CHANGE_ACTION_TYPES
        ]
        mutations_count = len(mutating_actions)
        drift_warning = (
            f"Host drift detected in '{pkg}' ({mutations_count} files modified on host). "
            "Live 'drift deploy' will halt unless '--force' or 'drift adopt' is used."
        )

    return PackageDeployPreview(
        package_name=pkg,
        reverse_sync_plan=reverse_sync_plan,
        render_plan=render_plan,
        stage_plan=stage_plan,
        install_plan=install_plan,
        drift_warning=drift_warning,
        error=error_obj,
    )


# =====================================================================
# Layer 4: Workspace Deploy Preview Orchestration
# =====================================================================

def preview_deploy(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    options: Optional[DeployOptions] = None,
    command_name: str = "plan",
) -> WorkspaceDeployPreview:
    """Simulates deployment planning and compiles a full-cycle WorkspaceDeployPreview.

    Runs pre-flight topological dependency ordering, executes Phase 0 host drift inspection,
    compiles source templates in an isolated ephemeral sandbox (Phase 1), and builds declarative
    PackageDeployPreviews without mutating the host filesystem or committing to state repositories.

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to plan, or empty sequence for all active packages.
        options: Execution flags and options controlling the plan.
        command_name: Label identifying the invoking command ("plan" or "deploy").

    Returns:
        WorkspaceDeployPreview containing ordered package previews and status.
    """
    opts = options or DeployOptions()

    # 1. Discover target active packages from source directory
    discovered_pkgs = workspace_config.filter_source_packages_by_target(
        target_packages=target_pkgs or None
    )
    if not discovered_pkgs:
        logger.info("No active packages selected or enabled for deployment planning.")
        return WorkspaceDeployPreview(
            command=command_name,
            status="SUCCESS",
            packages_install_order=[],
            package_previews={},
        )

    install_base = workspace_config.install_path
    state_file = install_base / STATE_REGISTRY_FILE_NAME
    state_registry: StateRegistry = (
        load_state_registry(state_file)
        if state_file.exists()
        else StateRegistry()
    )

    # 2. Check for midway transaction states if not force
    midway_errors = check_midway_states(discovered_pkgs, state_registry, force=opts.force)

    # 3. Load package configurations from source (dry_run=True isolates compilation from render/)
    pkg_metadata = load_source_package_metadata(workspace_config, discovered_pkgs)

    # 4. Resolve topological order over universe, filtered to targeted packages
    try:
        packages_install_order = resolve_target_package_order(
            target_metadata=pkg_metadata,
            state_registry=state_registry,
            workspace_config=workspace_config,
            no_deps=(opts.force or opts.no_deps),
        )
    except Exception as e:
        logger.debug("Dependency ordering error:", exc_info=True)
        return WorkspaceDeployPreview(
            command=command_name,
            status="FAILED",
            packages_install_order=list(pkg_metadata.keys()),
            package_previews={},
            global_errors=[e],
        )

    # 5. Phase 0 Audit: Non-destructive host drift inspection (pure read-only)
    rev_sync_map = audit_host_drift(workspace_config, discovered_pkgs)

    # 6. Multi-Phase Ephemeral Sandbox Planning (Phase 1 Render -> Phase 2 Stage -> Phase 3 Install)
    with enter_plan_sandbox(workspace_config, target_packages=discovered_pkgs) as (sandbox_root, sandbox_workspace):
        # Phase 1: Isolated Ephemeral Sandbox Render
        render_plans, render_errors = execute_sandbox_render_phase(
            sandbox_workspace_config=sandbox_workspace,
            real_workspace_config=workspace_config,
            packages_install_order=packages_install_order,
            pkg_metadata=pkg_metadata,
            options=opts,
            midway_errors=midway_errors,
        )

        # Phase 2: Sandbox Staging Planning
        stage_plans, stage_errors = execute_sandbox_stage_phase(
            sandbox_workspace_config=sandbox_workspace,
            real_workspace_config=workspace_config,
            packages_order=packages_install_order,
            pkg_metadata=pkg_metadata,
            render_errors=render_errors,
        )

        # Phase 3: Physical Install Planning
        install_plans, install_errors, install_global_errors = execute_sandbox_install_phase(
            sandbox_workspace_config=sandbox_workspace,
            real_workspace_config=workspace_config,
            packages_order=packages_install_order,
            pkg_metadata=pkg_metadata,
            options=opts,
            render_errors=render_errors,
            stage_errors=stage_errors,
            state_registry=state_registry,
        )

    # 7. Assemble per-package previews
    package_previews: Dict[str, PackageDeployPreview] = {
        pkg: assemble_package_deploy_preview(
            pkg=pkg,
            reverse_sync_plan=rev_sync_map.get(pkg),
            render_plan=render_plans.get(pkg),
            stage_plan=stage_plans.get(pkg),
            install_plan=install_plans.get(pkg),
            midway_error=midway_errors.get(pkg),
            render_error=render_errors.get(pkg),
            stage_error=stage_errors.get(pkg),
            install_error=install_errors.get(pkg),
        )
        for pkg in packages_install_order
    }

    drift_warnings: Dict[str, str] = {
        pkg: preview.drift_warning
        for pkg, preview in package_previews.items()
        if preview.drift_warning is not None
    }

    global_errors = list(install_global_errors)

    # 8. Determine overall preview status
    status = "SUCCESS"
    if any(p.error is not None for p in package_previews.values()) or global_errors:
        status = "FAILED"
    elif drift_warnings and not opts.force:
        status = "DRIFT_DETECTED"

    return WorkspaceDeployPreview(
        command=command_name,
        status=status,
        packages_install_order=packages_install_order,
        package_previews=package_previews,
        global_errors=global_errors,
        drift_warnings=drift_warnings,
    )


def run_primitive_plan(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    options: Optional[DeployOptions] = None,
) -> WorkspaceDeployPreview:
    """Executes Primitive: Full-Cycle Deployment Planning and Preview."""
    return preview_deploy(
        workspace_config=workspace_config,
        target_pkgs=target_pkgs,
        options=options,
        command_name="plan",
    )
