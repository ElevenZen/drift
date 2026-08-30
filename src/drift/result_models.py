"""Structured Result Dataclasses and JSON Serialization for Drift Primitives and Pipelines."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, is_dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from datetime import datetime


class NextActionType(str, Enum):
    """Deterministic next-action recommendation for automation tools and users."""
    NONE = "none"                        # Action succeeded completely
    FIX_TEMPLATE = "fix_template"        # Safe template compile failure (no rollback needed)
    ADOPT_OR_FORCE = "adopt_or_force"    # Sentinel detected host drift (run adopt or deploy --force)
    ROLLBACK = "rollback"                # Midway crash during install/staging (rollback required)
    INSTALL_COMMIT = "install_commit"    # Deployed to host, but install/ git commit failed
    RESOLVE_GIT = "resolve_git"          # Git repo corrupt/bare or merge conflict
    MANUAL_INSPECTION = "manual_check"   # Unclassified failure requiring manual intervention


class DiffType(str, Enum):
    """Enumeration of diff comparison modes between configuration layers."""
    TEMPLATE = "template"  # Diff A: src/ -> render/ (Template Evolution)
    SYSTEM = "system"      # Diff B: System -> install/ (Active System Drift)
    PENDING = "pending"    # Diff Δ: render/ -> install/ (Pending Delta)


def serialize_for_json(obj: Any) -> Any:
    """Recursively converts Dataclasses, Paths, Enums, and Sets into standard JSON primitives."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple, set)):
        return [serialize_for_json(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): serialize_for_json(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        fields_dict = asdict(obj)
        return serialize_for_json(fields_dict)
    return str(obj)


@dataclass
class SerializableModel:
    """Base dataclass providing automatic dictionary and JSON serialization."""
    
    def to_dict(self) -> Dict[str, Any]:
        """Converts model to a JSON-serializable dictionary."""
        return serialize_for_json(self)  # type: ignore[return-value]

    def to_json(self, indent: int = 2) -> str:
        """Serializes model to a formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


# =============================================================================
# Primitive 1: Reverse-Sync
# =============================================================================

@dataclass
class PackageReverseSyncResult(SerializableModel):
    package: str
    target_directory: str
    drifted_files: List[str] = field(default_factory=list)
    synced_files: List[str] = field(default_factory=list)
    status: str = "SUCCESS"
    error: Optional[str] = None


@dataclass
class ReverseSyncResult(SerializableModel):
    command: str = "reverse-sync"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageReverseSyncResult] = field(default_factory=list)
    error_message: Optional[str] = None


# =============================================================================
# Primitive 2: Template Render
# =============================================================================

@dataclass
class PackageRenderResult(SerializableModel):
    package: str
    status: str = "SUCCESS"  # "SUCCESS", "SKIPPED", "FAILED"
    rendered_files: List[str] = field(default_factory=list)
    copied_static_files: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RenderResult(SerializableModel):
    command: str = "render"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageRenderResult] = field(default_factory=list)
    error_package: Optional[str] = None
    error_message: Optional[str] = None


# =============================================================================
# Primitive 4: Stage Render to Install
# =============================================================================

@dataclass
class StageResult(SerializableModel):
    command: str = "stage"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages_changed: List[str] = field(default_factory=list)
    error_message: Optional[str] = None


# =============================================================================
# Primitive 5: Install Deployment & File Operations
# =============================================================================

@dataclass
class FileOperations(SerializableModel):
    added: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    backed_up: List[str] = field(default_factory=list)
    overwritten_backups: List[str] = field(default_factory=list)


@dataclass
class PackageInstallResult(SerializableModel):
    package: str
    install_method: str  # "stow" or "copy"
    target_directory: str
    operations: FileOperations = field(default_factory=FileOperations)
    is_first_time: bool = False
    status: str = "SUCCESS"
    error: Optional[str] = None


@dataclass
class InstallDeploymentResult(SerializableModel):
    command: str = "apply"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageInstallResult] = field(default_factory=list)
    error_package: Optional[str] = None
    error_message: Optional[str] = None


# =============================================================================
# Primitive 7: Uninstall
# =============================================================================

@dataclass
class RestoredBackup(SerializableModel):
    source_backup: str
    restored_to: str


@dataclass
class PackageUninstallResult(SerializableModel):
    package: str
    install_method: str
    target_directory: str
    detach_mode: bool = False
    removed_files: List[str] = field(default_factory=list)
    converted_symlinks: List[str] = field(default_factory=list)
    restored_backups: List[RestoredBackup] = field(default_factory=list)
    status: str = "SUCCESS"
    error: Optional[str] = None


@dataclass
class UninstallResult(SerializableModel):
    command: str = "uninstall"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    detach_mode: bool = False
    packages: List[PackageUninstallResult] = field(default_factory=list)
    error_message: Optional[str] = None

    def __iter__(self):
        """Allows iterating over uninstalled package names."""
        return iter([p.package for p in self.packages if p.status == "SUCCESS"])

    def __len__(self):
        return len([p.package for p in self.packages if p.status == "SUCCESS"])

    @property
    def uninstalled_packages(self) -> List[str]:
        return [p.package for p in self.packages if p.status == "SUCCESS"]


# =============================================================================
# Primitive 8: Adopt Drifts
# =============================================================================

@dataclass
class PackageAdoptResult(SerializableModel):
    package: str
    adopted_additions: List[str] = field(default_factory=list)
    adopted_modifications: List[str] = field(default_factory=list)
    adopted_deletions: List[str] = field(default_factory=list)
    adopted_renames: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)
    status: str = "SUCCESS"
    error: Optional[str] = None


@dataclass
class AdoptResult(SerializableModel):
    command: str = "adopt"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageAdoptResult] = field(default_factory=list)
    error_message: Optional[str] = None

    def __iter__(self):
        return iter([p.package for p in self.packages if p.status == "SUCCESS"])

    def __len__(self):
        return len([p.package for p in self.packages if p.status == "SUCCESS"])


# =============================================================================
# Primitive 9: Workspace Garbage Collection
# =============================================================================

@dataclass
class GcResult(SerializableModel):
    command: str = "gc"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    dry_run: bool = False
    uninstalled_orphans: List[str] = field(default_factory=list)
    purged_render_zombies: List[str] = field(default_factory=list)
    purged_install_zombies: List[str] = field(default_factory=list)
    render_commit_message: Optional[str] = None
    install_commit_message: Optional[str] = None
    error_message: Optional[str] = None


# =============================================================================
# Primitive 10: New Package
# =============================================================================

@dataclass
class NewPackageResult(SerializableModel):
    command: str = "new"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    package: str = ""
    package_dir: str = ""
    config_file: str = ""
    target_directory: str = ""
    install_method: str = ""
    error_message: Optional[str] = None


# =============================================================================
# Primitive 11: Add Resources
# =============================================================================

@dataclass
class AddResourceResult(SerializableModel):
    command: str = "add"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    package: str = ""
    imported_files: List[str] = field(default_factory=list)
    dry_run: bool = False
    error_message: Optional[str] = None


# =============================================================================
# High-Level Pipeline: Deploy & Rollback
# =============================================================================

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


@dataclass
class DeployResult(SerializableModel):
    command: str = "deploy"
    status: str = "SUCCESS"  # "SUCCESS", "ABORTED_DRIFT", "FAILED"
    is_global_deploy: bool = False
    target_packages: List[str] = field(default_factory=list)
    deployed_packages: List[PackageInstallResult] = field(default_factory=list)
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


# =============================================================================
# High-Level Commands: Status, Diff, Repair
# =============================================================================

@dataclass
class PackageStatusSummary(SerializableModel):
    name: str
    enabled: bool = True
    template_status: str = "CLEAN"
    system_drift_status: str = "CLEAN"
    staging_status: str = "CLEAN"
    drifted_files: List[str] = field(default_factory=list)
    pending_files: List[str] = field(default_factory=list)


@dataclass
class StatusResult(SerializableModel):
    command: str = "status"
    overall_status: str = "CLEAN"  # "CLEAN", "DRIFTED", "PENDING", "BROKEN"
    packages: List[PackageStatusSummary] = field(default_factory=list)


@dataclass
class FileDiffDetail(SerializableModel):
    path: str
    change_type: str  # "added", "modified", "deleted"
    patch: Optional[str] = None


@dataclass
class PackageDiffDetail(SerializableModel):
    package: str
    has_changes: bool = False
    files: List[FileDiffDetail] = field(default_factory=list)


@dataclass
class DiffResult(SerializableModel):
    command: str = "diff"
    diff_type: DiffType = DiffType.PENDING
    packages: List[PackageDiffDetail] = field(default_factory=list)


@dataclass
class RepairCheckDetail(SerializableModel):
    name: str
    status: str
    details: str
    fix_hint: Optional[str] = None


@dataclass
class RepairResult(SerializableModel):
    command: str = "repair"
    status: str = "SUCCESS"
    overall_health: str = "good"
    dry_run: bool = False
    actions_performed: List[str] = field(default_factory=list)
    checks: List[RepairCheckDetail] = field(default_factory=list)


class PackageHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"            # Exit code 0
    UNHEALTHY = "UNHEALTHY"        # Non-zero exit code
    TIMEOUT = "TIMEOUT"            # Process timed out
    MISSING_HOOK = "MISSING_HOOK"  # Specified hook file does not exist
    NO_HOOK = "NO_HOOK"            # Package does not define a health hook (skipped)
    NOT_INSTALLED = "NOT_INSTALLED"# Package is not present in install/ registry
    ERROR = "ERROR"                # Unexpected exception


@dataclass
class PackageHealthResult(SerializableModel):
    """Health check outcome for an individual package."""
    package: str
    status: PackageHealthStatus = PackageHealthStatus.HEALTHY
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    hook_path: Optional[str] = None
    target_directory: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class HealthResult(SerializableModel):
    """Aggregated health check result across all evaluated packages."""
    command: str = "health"
    status: str = "SUCCESS"        # "SUCCESS" if all evaluated are healthy/skipped, "FAILED" if any unhealthy/timeout
    packages: List[PackageHealthResult] = field(default_factory=list)
    healthy_count: int = 0
    unhealthy_count: int = 0
    skipped_count: int = 0
    total_duration_ms: float = 0.0

    def format_text(self, verbose: bool = False) -> str:
        """Formats a human-readable summary of package health check probes."""
        if not self.packages:
            return "No packages found to check health."

        lines = ["🩺 Running package health probes...\n"]
        for p in self.packages:
            status_val = p.status.value if isinstance(p.status, PackageHealthStatus) else str(p.status)
            status_tag = f"[{status_val}]"
            if status_val == "HEALTHY":
                lines.append(f"  \033[32m{status_tag:<14}\033[0m {p.package:<20} (exit {p.exit_code}, {p.duration_ms:.1f}ms)")
            elif status_val == "UNHEALTHY":
                lines.append(f"  \033[31m{status_tag:<14}\033[0m {p.package:<20} (exit {p.exit_code}, {p.duration_ms:.1f}ms)")
                if p.stderr:
                    err_preview = p.stderr.strip()
                    if not verbose and len(err_preview.splitlines()) > 3:
                        err_preview = "\n".join(err_preview.splitlines()[:3]) + "\n         ..."
                    for line in err_preview.splitlines():
                        lines.append(f"         └─ {line}")
                elif p.stdout:
                    out_preview = p.stdout.strip()
                    if not verbose and len(out_preview.splitlines()) > 3:
                        out_preview = "\n".join(out_preview.splitlines()[:3]) + "\n         ..."
                    for line in out_preview.splitlines():
                        lines.append(f"         └─ {line}")
            elif status_val == "TIMEOUT":
                lines.append(f"  \033[31m{status_tag:<14}\033[0m {p.package:<20} (timed out after {p.duration_ms:.1f}ms)")
            elif status_val == "MISSING_HOOK":
                lines.append(f"  \033[33m{status_tag:<14}\033[0m {p.package:<20} (hook file missing: {p.hook_path})")
            elif status_val == "NO_HOOK":
                lines.append(f"  \033[90m{status_tag:<14}\033[0m {p.package:<20} (no health hook defined)")
            elif status_val == "NOT_INSTALLED":
                lines.append(f"  \033[33m{status_tag:<14}\033[0m {p.package:<20} (not installed)")
            else:
                lines.append(f"  \033[31m{status_tag:<14}\033[0m {p.package:<20} (error: {p.error_message})")

            if verbose:
                if p.stdout and status_val != "UNHEALTHY":
                    for line in p.stdout.strip().splitlines():
                        lines.append(f"         [stdout] {line}")
                if p.stderr and status_val != "UNHEALTHY":
                    for line in p.stderr.strip().splitlines():
                        lines.append(f"         [stderr] {line}")

        lines.append("")
        lines.append("=" * 70)
        lines.append(
            f"📊 Health Summary: {self.healthy_count} Healthy, {self.unhealthy_count} Unhealthy, "
            f"{self.skipped_count} Skipped ({self.total_duration_ms:.1f}ms total)"
        )
        lines.append("=" * 70)
        return "\n".join(lines)


@dataclass
class CloneResult(SerializableModel):
    """Structured result for drift clone execution."""
    command: str = "clone"
    status: str = "SUCCESS"
    git_url: str = ""
    target_directory: str = ""
    is_drift_workspace: bool = True
    converted_legacy_package: Optional[str] = None
    repaired_actions: List[str] = field(default_factory=list)
    recommended_next_steps: List[str] = field(default_factory=list)
    recommended_next_command: str = ""
    error_message: Optional[str] = None

    def format_text(self) -> str:
        """Formats clone execution results for human-readable terminal output."""
        lines = []
        if self.status != "SUCCESS":
            lines.append(f"❌ [ERROR] Failed to clone workspace: {self.error_message}")
            return "\n".join(lines)

        if self.is_drift_workspace:
            lines.append(f"🔍 Detected Drift workspace at '{self.target_directory}'.")
            if self.repaired_actions:
                lines.append("🔧 Reconstructing workspace databases and sandbox repositories...")
                for action in self.repaired_actions:
                    lines.append(f"  ✨ {action}")
            lines.append("✨ Workspace successfully cloned and prepared!")
        else:
            pkg = self.converted_legacy_package or "dotfiles"
            lines.append(f"🔍 Detected plain dotfiles repository. Converting to Drift package '{pkg}'...")
            if self.repaired_actions:
                for action in self.repaired_actions:
                    lines.append(f"  ✨ {action}")
            lines.append("✨ Converted repository into a Drift workspace!")

        if self.recommended_next_steps:
            lines.append("")
            lines.append("👉 Next steps:")
            for i, step in enumerate(self.recommended_next_steps, 1):
                lines.append(f"   {i}. {step}")

        return "\n".join(lines)


# =============================================================================
# Primitive: Direct Lifecycle Hook Execution
# =============================================================================

@dataclass
class HookResult(SerializableModel):
    """Structured result for drift hook execution."""
    command: str = "hook"
    package: str = ""
    hook_name: str = ""
    status: str = "SUCCESS"  # "SUCCESS", "SKIPPED", "FAILED"
    exit_code: int = 0
    hook_path: Optional[str] = None
    cwd: Optional[str] = None
    hook_base_dir: Optional[str] = None
    sudo: bool = False
    duration_ms: float = 0.0
    error_message: Optional[str] = None

    def __bool__(self) -> bool:
        """Returns True if the hook executed successfully, False if skipped or failed."""
        return self.status == "SUCCESS"

    @classmethod
    def skipped(
        cls,
        package: str = "",
        hook_name: str = "",
        cwd: Optional[Union[str, Path]] = None,
        hook_base_dir: Optional[Union[str, Path]] = None,
    ) -> "HookResult":
        """Constructs a HookResult with status SKIPPED."""
        return cls(
            command="hook",
            package=package,
            hook_name=hook_name,
            status="SKIPPED",
            exit_code=0,
            cwd=str(cwd) if cwd is not None else None,
            hook_base_dir=str(hook_base_dir) if hook_base_dir is not None else None,
            duration_ms=0.0
        )

    def format_text(self) -> str:
        """Formats a human-readable summary of hook execution."""
        if self.status == "SUCCESS":
            return f"✨ Successfully executed hook '{self.hook_name}' for package '{self.package}' ({self.duration_ms:.1f}ms)!"
        elif self.status == "SKIPPED":
            return f"⏭️ Skipped hook '{self.hook_name}' for package '{self.package}'."
        return f"❌ Failed to execute hook '{self.hook_name}' for package '{self.package}': {self.error_message}"




