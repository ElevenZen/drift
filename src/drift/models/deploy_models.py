"""Data models, options, previews, and result containers for deploy, plan, and rollback pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

from ..core.constants import ExitCode, RollbackPolicy
from ..core.file_action import (
    FileAction,
    filter_display_actions,
    format_action_line,
    format_action_summary,
)
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel
from ..utils.func_utils import partition
from .install_models import PackageInstallPlan, PackageInstallResult
from .render_models import PackageRenderResult
from .reverse_sync_models import PackageReverseSyncPlan
from .stage_models import PackageStagePlan
from .workspace_models import GcResult

if TYPE_CHECKING:
    from ..config.workspace_config import SettingsConfig
    from ..hooks.lifecycle_hooks import HookExecFlags


class NextActionType(str, Enum):
    """Deterministic next-action recommendation for automation tools and users."""
    NONE = "none"                        # Action succeeded completely
    FIX_TEMPLATE = "fix_template"        # Safe template compile failure (no rollback needed)
    ADOPT_OR_FORCE = "adopt_or_force"    # Sentinel detected host drift (run adopt or deploy --force)
    ROLLBACK = "rollback"                # Midway crash during install/staging (rollback required)
    INSTALL_COMMIT = "install_commit"    # Deployed to host, but install/ git commit failed
    RESOLVE_GIT = "resolve_git"          # Git repo corrupt/bare or merge conflict
    MANUAL_INSPECTION = "manual_check"   # Unclassified failure requiring manual intervention

    def __str__(self) -> str:
        return self.value


@dataclass
class DeployOptions(AlwaysTruthy):
    """Configuration options controlling the full-cycle deployment and planning pipelines."""
    force: bool = False
    adhoc: bool = False
    gc: bool = False
    reinstall: bool = False
    no_deps: bool = False
    no_hooks: bool = False
    with_hooks: bool = False
    no_cache: bool = False
    dry_run: bool = False
    show_all: bool = False
    verbose: bool = False
    rollback: Optional[RollbackPolicy] = None
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Optional[SettingsConfig] = None) -> HookExecFlags:
        """Derives HookExecFlags, merging DeployOptions flags with workspace settings."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        effective_no_hooks = self.no_hooks if not self.dry_run else (not self.with_hooks)
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            fallback=HookExecFlags(no_hooks=effective_no_hooks, no_cache=self.no_cache),
            override=HookExecFlags(dry_run=self.dry_run),
        )


@dataclass
class CompletedStep(SerializableModel):
    step_index: int
    name: str
    status: str = "SUCCESS"


@dataclass
class DeployFailure(SerializableModel):
    step_index: int
    step_name: str
    package: Optional[str]
    error_message: str
    error_type: str
    requires_rollback: bool
    next_action_type: NextActionType
    recommended_command: Optional[str] = None
    alternative_command: Optional[str] = None
    remedy_instructions: Optional[str] = None
    drifted_files: List[str] = field(default_factory=list)
    exit_code: int = ExitCode.GENERAL_ERROR  # Process exit code of the underlying exception (DriftError.exit_code)


@dataclass
class DeployResult(SerializableModel):
    command: str = "deploy"
    status: str = "SUCCESS"  # "SUCCESS", "ABORTED_DRIFT", "FAILED"
    is_global_deploy: bool = False
    target_packages: List[str] = field(default_factory=list)
    deployed_packages: List[PackageInstallResult] = field(default_factory=list)
    disabled_installed_packages: List[str] = field(default_factory=list)
    gc: Optional[GcResult] = None
    failure: Optional[DeployFailure] = None
    completed_steps: List[CompletedStep] = field(default_factory=list)


@dataclass
class RollbackResult(SerializableModel):
    command: str = "rollback"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    target_packages: List[str] = field(default_factory=list)
    restored_packages: List[str] = field(default_factory=list)
    error_message: Optional[str] = None


@dataclass
class PackageDeployPreview(SerializableModel):
    """Planned actions across all deployment stages for a single package."""
    package_name: str = ""
    reverse_sync_plan: Optional[PackageReverseSyncPlan] = None
    render_plan: Optional[PackageRenderResult] = None
    stage_plan: Optional[PackageStagePlan] = None
    install_plan: Optional[PackageInstallPlan] = None
    drift_warning: Optional[str] = None
    error: Optional[Exception] = None

    @property
    def has_changes(self) -> bool:
        render_changed = self.render_plan is not None and self.render_plan.has_changes
        stage_changed = self.stage_plan is not None and self.stage_plan.has_changes
        install_changed = self.install_plan is not None and self.install_plan.has_changes
        return render_changed or stage_changed or install_changed

    @property
    def has_drift(self) -> bool:
        return self.drift_warning is not None or (self.reverse_sync_plan is not None and self.reverse_sync_plan.has_changes)

    @property
    def has_any_activity(self) -> bool:
        return self.has_changes or self.has_drift or self.error is not None

    @property
    def error_message(self) -> Optional[str]:
        return str(self.error) if self.error is not None else None

    @property
    def error_type(self) -> Optional[str]:
        return type(self.error).__name__ if self.error is not None else None

    def format_text(self, verbose: bool = False, show_all: bool = False) -> str:
        """Formats the package deploy preview for terminal output."""
        status_parts = []
        if self.error:
            status_parts.append(f"ERROR: {self.error}")
        elif self.has_changes:
            status_parts.append("CHANGES")
        else:
            status_parts.append("UNCHANGED")
        if self.has_drift:
            status_parts.append("DRIFT")

        status_str = f" [{', '.join(status_parts)}]"
        lines = [f"📦 Package: {self.package_name}{status_str}"]

        if self.error:
            lines.append(f"  ❌ Error: {self.error}")

        if self.drift_warning:
            lines.append(f"  ⚠️  Drift: {self.drift_warning}")

        def len_actions(actions: Optional[List[FileAction]]) -> str:
            return (
                f"{len(actions)} {'action' if len(actions) == 1 else 'actions'}"
                if actions is not None
                else "empty"
            )

        if self.reverse_sync_plan and (verbose or self.reverse_sync_plan.has_changes):
            display_rev_actions = filter_display_actions(self.reverse_sync_plan.actions, verbose=verbose)
            if display_rev_actions:
                lines.append(f"  🔄 Reverse Sync Plan: ({len_actions(display_rev_actions)})")
                lines.extend(f"  {format_action_line(a)}" for a in display_rev_actions)
                lines.append(f"     Summary: {format_action_summary(self.reverse_sync_plan.actions)}")

        if self.render_plan:
            display_render_actions = filter_display_actions(self.render_plan.actions, verbose=verbose)
            if verbose or self.render_plan.has_changes:
                lines.append(f"  🎨 Render Actions: ({len_actions(display_render_actions)})")
                if display_render_actions:
                    lines.extend(f"  {format_action_line(a)}" for a in display_render_actions)
                lines.append(f"     Summary: {format_action_summary(self.render_plan.actions)}")

        if self.stage_plan:
            display_stage_actions = filter_display_actions(self.stage_plan.actions, verbose=verbose)
            if verbose or self.stage_plan.has_changes:
                lines.append(f"  📁 Stage Actions: ({len_actions(display_stage_actions)})")
                if display_stage_actions:
                    lines.extend(f"  {format_action_line(a)}" for a in display_stage_actions)
                lines.append(f"     Summary: {format_action_summary(self.stage_plan.actions)}")

        if self.install_plan:
            display_install_actions = filter_display_actions(self.install_plan.actions, verbose=verbose)
            will_deploy = self.stage_plan is not None and self.stage_plan.has_changes
            has_hooks = bool(self.install_plan.hooks_to_trigger)
            if verbose or self.install_plan.has_changes or (will_deploy and has_hooks):
                lines.append(f"  🚀 Install Actions: ({len_actions(display_install_actions)})")
                if display_install_actions:
                    lines.extend(f"  {format_action_line(a)}" for a in display_install_actions)
                if self.install_plan.hooks_to_trigger:
                    lines.append(f"     Hooks: {', '.join(self.install_plan.hooks_to_trigger)}")
                lines.append(f"     Summary: {format_action_summary(self.install_plan.actions)}")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["has_changes"] = self.has_changes
        data["has_drift"] = self.has_drift
        data["error_message"] = self.error_message
        return data


@dataclass
class WorkspaceDeployPreview(SerializableModel):
    """Full-cycle deployment preview and execution plan across the workspace."""
    command: str = "plan"
    status: str = "SUCCESS"  # "SUCCESS", "DRIFT_DETECTED", "FAILED"
    packages_install_order: List[str] = field(default_factory=list)
    package_previews: Dict[str, PackageDeployPreview] = field(default_factory=dict)
    global_errors: List[Exception] = field(default_factory=list)
    drift_warnings: Dict[str, str] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return any(p.has_changes for p in self.package_previews.values())

    @property
    def has_drift(self) -> bool:
        return bool(self.drift_warnings) or any(p.has_drift for p in self.package_previews.values())

    def _order_package_names(self, package_names: Iterable[str]) -> List[str]:
        """Orders package names according to packages_install_order, placing unlisted packages at the end."""
        order_set = set(self.packages_install_order)
        in_order, remaining = partition(order_set.__contains__, package_names)
        ordered_set = set(in_order)
        ordered = list(filter(ordered_set.__contains__, self.packages_install_order))
        return ordered + remaining

    @property
    def packages_with_changes(self) -> List[str]:
        changed = list(filter(lambda pkg: self.package_previews[pkg].has_changes, self.package_previews))
        return self._order_package_names(changed)

    @property
    def packages_unchanged(self) -> List[str]:
        unchanged = list(filter(lambda pkg: not self.package_previews[pkg].has_any_activity, self.package_previews))
        return self._order_package_names(unchanged)

    @property
    def packages_with_drift(self) -> List[str]:
        drifted = list(filter(lambda pkg: self.package_previews[pkg].has_drift, self.package_previews))
        return self._order_package_names(drifted)

    @property
    def packages_with_errors(self) -> List[str]:
        errored = list(filter(lambda pkg: self.package_previews[pkg].error is not None, self.package_previews))
        return self._order_package_names(errored)

    @property
    def drifted_packages(self) -> List[str]:
        return list(self.drift_warnings.keys())

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["has_changes"] = self.has_changes
        data["has_drift"] = self.has_drift
        data["global_errors"] = [str(e) for e in self.global_errors]
        data["packages_with_changes"] = self.packages_with_changes
        data["packages_unchanged"] = self.packages_unchanged
        data["packages_with_drift"] = self.packages_with_drift
        data["packages_with_errors"] = self.packages_with_errors
        return data

    def format_text(self, verbose: bool = False, show_all: bool = False, use_rich: bool = True) -> str:
        """Formats the deployment preview for human-readable terminal output."""
        lines = [f"=== Workspace Deployment Preview: {self.command} (Status: {self.status}) ==="]
        total_count = len(self.package_previews)
        changed_count = len(self.packages_with_changes)
        unchanged_count = len(self.packages_unchanged)
        drift_count = len(self.packages_with_drift)
        lines.append(
            f"📊 Summary: {total_count} package(s) planned | {changed_count} with changes | "
            f"{unchanged_count} unchanged | {drift_count} drifted"
        )

        if self.global_errors:
            lines.append("🚨 Global Pre-Flight Errors (Real deploy pipeline will fail):")
            for err in self.global_errors:
                lines.append(f"  ❌ {err}")
        if self.drift_warnings:
            lines.append("⚠️  Drift Warnings:")
            for pkg, warn in self.drift_warnings.items():
                lines.append(f"  • {pkg}: {warn}")
            lines.append("     💡 Host changes detected. Run 'drift adopt' to absorb changes into src/ or pass '--force' to overwrite.")
        for pkg in self.packages_install_order:
            if pkg in self.package_previews:
                p = self.package_previews[pkg]
                if show_all or p.has_changes or p.error or p.has_drift or verbose:
                    lines.append(p.format_text(verbose=verbose, show_all=show_all))
        unchanged = self.packages_unchanged
        if unchanged and not show_all and not verbose:
            lines.append(f"✨ {len(unchanged)} package(s) unchanged: {', '.join(unchanged)}")
        if self.global_errors or self.packages_with_errors:
            lines.append("💥 Real deployment pipeline will fail due to detected errors.")
        return "\n".join(lines)


DeployPreview = WorkspaceDeployPreview
