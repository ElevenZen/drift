"""Data models, plans, and result containers for workspace-level primitives (gc, new, add, status, diff, repair, health, clone, hook, check)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Union

from ..core.constants import InstallMethod
from ..core.file_action import (
    FileAction,
    FileActionType,
    filter_display_actions,
    format_action_line,
    format_action_summary,
)
from ..core.folder_diff import FolderDiff
from ..core.serialization import SerializableModel
from ..utils.git_utils import GitStatusChanges


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
    purged_wal_packages: List[str] = field(default_factory=list)
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
    install_method: InstallMethod = InstallMethod.DEFAULT
    error_message: Optional[str] = None


# =============================================================================
# Primitive 11: Add Resources
# =============================================================================

@dataclass
class AddResourcePlan(SerializableModel):
    """Declarative plan for importing host resources into a package source directory."""
    package: str = ""
    src_dir_to_render: Path = field(default_factory=Path)
    target_base: Path = field(default_factory=Path)
    actions: List[FileAction] = field(default_factory=list)
    dry_run: bool = False

    @property
    def has_changes(self) -> bool:
        """Returns True if there are any actions planned to import."""
        return bool(self.actions)

    @property
    def created(self) -> List[FileAction]:
        """Returns all CREATE_COPY planned actions."""
        return [a for a in self.actions if a.action_type == FileActionType.CREATE_COPY]

    def __len__(self) -> int:
        return len(self.actions)

    def __iter__(self):
        return iter(self.actions)

    def format_text(self, verbose: bool = False) -> str:
        """Formats human-readable summary of planned resource imports."""
        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            return f"No resources to import into '{self.package}'."
        lines = [f"📦 Package '{self.package}':"]
        lines.extend(format_action_line(action) for action in display_actions)
        lines.append(f"  Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass
class AddResourceResult(SerializableModel):
    command: str = "add"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    package: str = ""
    imported_files: List[str] = field(default_factory=list)
    dry_run: bool = False
    plan: AddResourcePlan = field(default_factory=AddResourcePlan)
    error_message: Optional[str] = None

    def format_text(self, verbose: bool = False) -> str:
        """Formats the import result for human-readable terminal output."""
        if self.status != "SUCCESS":
            return f"❌ Failed to import resources into package '{self.package}': {self.error_message}"
        if not self.plan.has_changes:
            if self.dry_run:
                return f"🔍 [DRY-RUN] No resources to import into '{self.package}'."
            return f"No resources to import into '{self.package}'."
        if self.dry_run:
            lines = [
                f"🔍 [DRY-RUN] Planned Resource Imports for package '{self.package}':",
                "=" * 60,
                self.plan.format_text(verbose=verbose),
                "=" * 60,
                f"✨ [DRY-RUN] Import simulation completed. {len(self.plan.actions)} file(s) would be imported into '{self.package}'.",
            ]
            return "\n".join(lines)
        lines = [f"✨ Successfully imported {len(self.imported_files)} file(s) into package '{self.package}'."]
        for f in self.imported_files:
            lines.append(f"  📥 {f}")
        return "\n".join(lines)


# =============================================================================
# High-Level Commands: Status, Diff, Repair, Health, Clone, Check
# =============================================================================

@dataclass
class PackageStatus(SerializableModel):
    """Unified status and deployment metadata model for a single package."""
    name: str
    enabled: bool = True
    state: str = "unknown"
    target_directory: Optional[str] = None
    install_method: Optional[str] = None
    last_deployed: Optional[str] = None
    deployed_files_count: Optional[int] = None
    template_status: str = "UNKNOWN"
    system_status: str = "UNKNOWN"
    pending_status: str = "UNKNOWN"
    template_changes: Optional[GitStatusChanges] = None
    system_changes: Optional[GitStatusChanges] = None
    pending_changes: Optional[FolderDiff] = None

    def format_list_text(self) -> str:
        """Formats slim deployment metadata block for this package."""
        lines = [f"Package: {self.name}"]
        lines.append(f"  State:    {self.state}")
        if self.target_directory:
            lines.append(f"  Target:   {self.target_directory}")
        if self.install_method:
            lines.append(f"  Method:   {self.install_method}")
        if self.last_deployed:
            lines.append(f"  Deployed: {self.last_deployed}")
        if self.deployed_files_count is not None:
            lines.append(f"  Files:    {self.deployed_files_count} deployed")
        return "\n".join(lines)

    def format_text(self) -> str:
        """Formats the full status block for this package."""
        lines = [f"Package: {self.name}"]
        if self.state != "unknown":
            lines.append(f"  State:    {self.state}")
        if self.target_directory:
            lines.append(f"  Target:   {self.target_directory}")
        if self.install_method:
            lines.append(f"  Method:   {self.install_method}")
        if self.last_deployed:
            lines.append(f"  Deployed: {self.last_deployed}")
        if self.deployed_files_count is not None:
            lines.append(f"  Files:    {self.deployed_files_count} deployed")
        lines.append(f"  [A] Template: {self.template_status}")
        if self.template_changes is not None:
            plus = len(self.template_changes.added)
            tilde = len(self.template_changes.modified)
            minus = len(self.template_changes.deleted)
            rn = len(self.template_changes.renamed)
            parts = [f"+{plus}", f"~{tilde}", f"-{minus}"]
            if rn > 0:
                parts.append(f"->{rn}")
            lines.append(f"      ({', '.join(parts)} files)")
        lines.append(f"  [B] System:   {self.system_status}")
        if self.system_changes is not None:
            plus = len(self.system_changes.added)
            tilde = len(self.system_changes.modified)
            minus = len(self.system_changes.deleted)
            rn = len(self.system_changes.renamed)
            parts = [f"+{plus}", f"~{tilde}", f"-{minus}"]
            if rn > 0:
                parts.append(f"->{rn}")
            lines.append(f"      ({', '.join(parts)} files)")
        lines.append(f"  [Δ] Pending:  {self.pending_status}")
        if self.pending_changes is not None and self.pending_status == "STAGED":
            plus = len(self.pending_changes.added)
            tilde = len(self.pending_changes.modified)
            minus = len(self.pending_changes.deleted)
            lines.append(f"      (+{plus}, ~{tilde}, -{minus} files)")
        return "\n".join(lines)


@dataclass
class StatusResult(SerializableModel):
    """Container representing the aggregated status of the drift workspace across packages."""
    command: str = "status"
    overall_status: str = "CLEAN"  # "CLEAN", "DRIFTED", "PENDING", "UNKNOWN", "BROKEN"
    list_only: bool = False
    packages: List[PackageStatus] = field(default_factory=list)

    def __iter__(self):
        return iter(self.packages)

    def __len__(self):
        return len(self.packages)

    def __getitem__(self, index):
        return self.packages[index]

    def format_text(self) -> str:
        """Formats the human-readable workspace status output."""
        if not self.packages:
            return ""
        pkg_names = [pkg.name for pkg in self.packages]
        header = f"Enabled Packages: {', '.join(pkg_names)}\n"
        if self.list_only:
            body = "\n\n".join(pkg.format_list_text() for pkg in self.packages)
        else:
            body = "\n\n".join(pkg.format_text() for pkg in self.packages)
        return header + "\n" + body


# =============================================================================
# Primitive 15: Workspace Diff
# =============================================================================

class WorkspaceDiffType(str, Enum):
    """Enumeration of workspace diff comparison modes between configuration layers."""
    TEMPLATE = "template"  # Diff A: src/ -> render/ (Template Evolution)
    SYSTEM = "system"      # Diff B: System -> install/ (Active System Drift)
    PENDING = "pending"    # Diff Δ: render/ -> install/ (Pending Delta)

    def __str__(self) -> str:
        return self.value


@dataclass
class FileDiffDetail(SerializableModel):
    path: str
    change_type: str  # "added", "modified", "deleted", "renamed"
    renamed_from: Optional[str] = None
    patch: Optional[str] = None


@dataclass
class PackageDiffDetail(SerializableModel):
    package: str
    has_changes: bool = False
    files: List[FileDiffDetail] = field(default_factory=list)


@dataclass
class DiffResult(SerializableModel):
    command: str = "diff"
    diff_type: WorkspaceDiffType = WorkspaceDiffType.PENDING
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


class ComponentStatus(str, Enum):
    GOOD = "good"
    BROKEN = "broken"
    NOT_FOUND = "not_found"

    def __str__(self) -> str:
        return self.value


@dataclass
class CheckResult:
    name: str
    status: ComponentStatus
    details: str
    fix_hint: Optional[str] = None


@dataclass
class WorkspaceHealthReport:
    overall_status: ComponentStatus
    checks: List[CheckResult] = field(default_factory=list)

    def is_healthy(self) -> bool:
        return self.overall_status == ComponentStatus.GOOD

    def is_uninitialized(self) -> bool:
        return self.overall_status == ComponentStatus.NOT_FOUND

    def is_broken(self) -> bool:
        return self.overall_status == ComponentStatus.BROKEN

    def __bool__(self) -> bool:
        """Returns True only if overall_status is GOOD."""
        return self.is_healthy()

    def get_check(self, name: str) -> Optional[CheckResult]:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def format_diagnostic_summary(self) -> str:
        lines = []
        for c in self.checks:
            if c.status == ComponentStatus.GOOD:
                icon = "✅"
            elif c.status == ComponentStatus.BROKEN:
                icon = "❌"
            else:
                icon = "⚪"
            hint_str = f" (Hint: {c.fix_hint})" if c.fix_hint and c.status != ComponentStatus.GOOD else ""
            lines.append(f"   {icon} {c.name}: [{c.status.value.upper()}] {c.details}{hint_str}")
        return "\n".join(lines)

    def to_repair_result(
        self,
        actions: Iterable[str] = (),
        dry_run: bool = False,
    ) -> RepairResult:
        """Converts this health report and any performed repair actions into a RepairResult object."""
        checks_list = [
            RepairCheckDetail(
                name=c.name,
                status=c.status.value,
                details=c.details,
                fix_hint=c.fix_hint,
            )
            for c in self.checks
        ]
        return RepairResult(
            overall_health=self.overall_status.value,
            dry_run=dry_run,
            actions_performed=list(actions),
            checks=checks_list,
        )


class PackageHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"            # Exit code 0
    UNHEALTHY = "UNHEALTHY"        # Non-zero exit code
    TIMEOUT = "TIMEOUT"            # Process timed out
    MISSING_HOOK = "MISSING_HOOK"  # Specified hook file does not exist
    NO_HOOK = "NO_HOOK"            # Package does not define a health hook (skipped)
    NOT_INSTALLED = "NOT_INSTALLED"# Package is not present in install/ registry
    ERROR = "ERROR"                # Unexpected exception

    def __str__(self) -> str:
        return self.value


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
    stdout: Optional[str] = None
    stderr: Optional[str] = None
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
    ) -> HookResult:
        """Constructs a HookResult with status SKIPPED."""
        return cls(
            command="hook",
            package=package,
            hook_name=hook_name,
            status="SKIPPED",
            exit_code=0,
            cwd=str(cwd) if cwd is not None else None,
            hook_base_dir=str(hook_base_dir) if hook_base_dir is not None else None,
            duration_ms=0.0,
        )

    def format_text(self) -> str:
        """Formats a human-readable summary of hook execution."""
        if self.status == "SUCCESS":
            return f"✨ Successfully executed hook '{self.hook_name}' for package '{self.package}' ({self.duration_ms:.1f}ms)!"
        elif self.status == "SKIPPED":
            return f"⏭️ Skipped hook '{self.hook_name}' for package '{self.package}'."
        return f"❌ Failed to execute hook '{self.hook_name}' for package '{self.package}': {self.error_message}"
