"""Primitive: Deployment Planning and Preview (drift plan / drift deploy --dry-run)

Architecture & Call Chain Overview:
-----------------------------------
1. Dependency Ordering & Pre-Flight Validation [Layer 1]:
   - Filters target packages via WorkspaceConfig.
   - Loads source PackageConfig metadata and StateRegistry.
   - Resolves topological package installation order via resolve_target_package_order.
   - Detects midway transaction states (e.g. incomplete previous operations).

2. Phase 0 Audit: Non-Destructive Host Drift Sentinel [Layer 2]:
   - Invokes pure read-only prepare_reverse_sync to diff host targets against install/ database.
   - Computes planned reverse-sync mutations without modifying install/ or dirtying Git index.
   - Emits actionable drift warning cards and records PackageDeployPreview with reverse_sync_plan.

3. Workspace Deploy Preview Assembly [Layer 3]:
   - Orchestrates per-package previews in topological order.
   - Assigns workspace status ("DRIFT_DETECTED" when host divergence is detected without --force).
   - Returns structured WorkspaceDeployPreview.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..core.constants import STATE_REGISTRY_FILE_NAME
from ..core.file_action import NO_CHANGE_ACTION_TYPES
from ..core.state_registry import load_state_registry, StateRegistry
from ..core.result_models import (
    PackageDeployPreview,
    WorkspaceDeployPreview,
    PackageReverseSyncPlan,
    ReverseSyncPlan,
)
from .package_assertions import resolve_target_package_order
from .reverse_sync import prepare_reverse_sync
from .deploy_repo import DeployOptions

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Single Package Preview Assembly
# =====================================================================

def assemble_package_deploy_preview(
    pkg: str,
    reverse_sync_plan_map: Dict[str, PackageReverseSyncPlan],
    midway_error: Optional[str] = None,
) -> PackageDeployPreview:
    """Assembles a PackageDeployPreview for a single package from Phase 0 audit results."""
    pkg_rev_plan = reverse_sync_plan_map.get(pkg)
    error_obj: Optional[Exception] = RuntimeError(midway_error) if midway_error else None

    drift_warning: Optional[str] = None
    if pkg_rev_plan is not None and pkg_rev_plan.has_changes:
        mutating_actions = [
            a for a in pkg_rev_plan.actions
            if a.action_type not in NO_CHANGE_ACTION_TYPES
        ]
        mutations_count = len(mutating_actions)
        drift_warning = (
            f"Host drift detected in '{pkg}' ({mutations_count} files modified on host). "
            "Live 'drift deploy' will halt unless '--force' or 'drift adopt' is used."
        )

    return PackageDeployPreview(
        package_name=pkg,
        reverse_sync_plan=pkg_rev_plan,
        drift_warning=drift_warning,
        error=error_obj,
    )


# =====================================================================
# Layer 2: Workspace Deploy Preview Orchestration
# =====================================================================

def prepare_deploy_preview(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    options: Optional[DeployOptions] = None,
    command_name: str = "plan",
) -> WorkspaceDeployPreview:
    """Simulates deployment planning and compiles a full-cycle WorkspaceDeployPreview.

    Runs pre-flight topological dependency ordering, executes Phase 0 host drift inspection
    via read-only prepare_reverse_sync, and builds declarative PackageDeployPreviews
    without modifying the host filesystem or committing to state repositories.

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
    midway_errors: Dict[str, str] = {}
    if state_file.exists() and not opts.force:
        midway_pkgs = state_registry.get_rollback_eligible_packages(discovered_pkgs)
        for pkg, state in midway_pkgs:
            midway_errors[pkg] = (
                f"Package '{pkg}' is in midway transaction state '{state}'. "
                f"Run 'drift rollback {pkg}' or pass '--force'."
            )

    # 3. Load package configurations from source (dry_run=True isolates compilation from render/)
    pkg_metadata = {
        pkg: PackageConfig.from_source_dir(
            workspace_config.source_path / pkg,
            workspace_config=workspace_config,
            dry_run=True,
        )
        for pkg in discovered_pkgs
    }

    # 4. Resolve topological order over universe, filtered to targeted packages
    packages_install_order = resolve_target_package_order(
        target_metadata=pkg_metadata,
        state_registry=state_registry,
        workspace_config=workspace_config,
        no_deps=(opts.force or opts.no_deps),
    )

    # 5. Phase 0 Audit: Non-destructive host drift inspection (pure read-only)
    rev_sync_plan = prepare_reverse_sync(
        workspace_config=workspace_config,
        package_names=discovered_pkgs,
        missing_ok=True,
    )
    rev_sync_map = {p.package: p for p in rev_sync_plan.plans}

    # 6. Assemble per-package previews
    package_previews: Dict[str, PackageDeployPreview] = {
        pkg: assemble_package_deploy_preview(
            pkg=pkg,
            reverse_sync_plan_map=rev_sync_map,
            midway_error=midway_errors.get(pkg),
        )
        for pkg in packages_install_order
    }

    drift_warnings: Dict[str, str] = {
        pkg: preview.drift_warning
        for pkg, preview in package_previews.items()
        if preview.drift_warning is not None
    }

    # 7. Determine overall preview status
    status = "SUCCESS"
    if any(p.error is not None for p in package_previews.values()):
        status = "FAILED"
    elif drift_warnings and not opts.force:
        status = "DRIFT_DETECTED"

    return WorkspaceDeployPreview(
        command=command_name,
        status=status,
        packages_install_order=packages_install_order,
        package_previews=package_previews,
        drift_warnings=drift_warnings,
    )


def run_primitive_plan(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    options: Optional[DeployOptions] = None,
) -> WorkspaceDeployPreview:
    """Executes Primitive: Full-Cycle Deployment Planning and Preview."""
    return prepare_deploy_preview(
        workspace_config=workspace_config,
        target_pkgs=target_pkgs,
        options=options,
        command_name="plan",
    )
