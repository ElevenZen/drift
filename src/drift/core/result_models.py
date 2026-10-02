"""Structured Result Dataclasses and JSON Serialization for Drift Primitives and Pipelines."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, is_dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Tuple
from datetime import datetime
from .constants import DEFAULT_INSTALL_METHOD, InstallMethod
from .folder_diff import FolderDiff
from ..utils.git_utils import GitStatusDiff
from ..utils.path_utils import is_relative_to


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


from .serialization import serialize_for_json, SerializableModel


from .folder_delivery import (
    FileActionType,
    FileAction,
    format_action_line,
    format_action_summary,
)


# =============================================================================
# Primitive 1: Reverse-Sync
# =============================================================================

@dataclass
class PackageReverseSyncPlan(SerializableModel):
    """Structured reverse-sync plan detailing planned filesystem operations to sync host back to install/."""
    package: str = ""
    target_directory: str = ""
    actions: List[FileAction] = field(default_factory=list)
    status: str = "PENDING"  # "PENDING", "SKIPPED", "FAILED"
    error: Optional[str] = None

    @property
    def created(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.CREATE_COPY]

    @property
    def updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_COPY]

    @property
    def permissions_updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_PERMISSION]

    @property
    def deleted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type in (FileActionType.DELETE_FILE, FileActionType.BACKUP_PRUNE)]

    @property
    def skipped(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.SKIP_IDENTICAL]

    def format_text(self) -> str:
        lines = [f"=== Reverse Sync Plan: {self.package} ==="]
        if self.status != "PENDING":
            lines.append(f"Status: {self.status}")
        if self.error:
            lines.append(f"Error: {self.error}")
        for action in self.actions:
            lines.append(format_action_line(action))
        return "\n".join(lines)


@dataclass
class ReverseSyncPlan(SerializableModel):
    """Structured reverse-sync plan for multiple packages."""
    plans: List[PackageReverseSyncPlan] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(bool(p.created or p.updated or p.permissions_updated or p.deleted) for p in self.plans)

    def format_text(self) -> str:
        return "\n".join(plan.format_text() for plan in self.plans)


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
    skip_reason: Optional[str] = None
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
class PackageStagePlan(SerializableModel):
    """Structured staging plan detailing operations to sync render/ to install/ for a package."""
    package: str = ""
    actions: List[FileAction] = field(default_factory=list)

    @property
    def created(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.CREATE_COPY]

    @property
    def updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_COPY]

    @property
    def permissions_updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_PERMISSION]

    @property
    def deleted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.DELETE_FILE]

    @property
    def ensured_dirs(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.ENSURE_DIR]

    @property
    def package_name(self) -> str:
        return self.package

    @property
    def has_changes(self) -> bool:
        return bool(self.actions)

    def format_text(self) -> str:
        """Formats the staging plan for human-readable terminal output."""
        lines = [f"📦 Package '{self.package}':"]
        if self.actions:
            lines.extend(format_action_line(action) for action in self.actions)
        lines.append(f"  Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass
class StageResult(SerializableModel):
    command: str = "stage"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages_changed: List[str] = field(default_factory=list)
    plans: List[PackageStagePlan] = field(default_factory=list)
    error_message: Optional[str] = None
    dry_run: bool = False

    @property
    def has_changes(self) -> bool:
        """Returns True if any package stage plan contains actions."""
        return any(plan.has_changes for plan in self.plans)

    @property
    def plan_map(self) -> Dict[str, PackageStagePlan]:
        """Provides a mapping from package name to PackageStagePlan."""
        return {p.package: p for p in self.plans}

    def __getitem__(self, package: str) -> PackageStagePlan:
        """Allows direct dictionary-like indexing: result[pkg]."""
        for p in self.plans:
            if p.package == package:
                return p
        raise KeyError(f"Package '{package}' not found in stage plans.")

    def __contains__(self, package: str) -> bool:
        """Allows 'pkg in result' checks."""
        return any(p.package == package for p in self.plans)

    def __iter__(self):
        """Iterates over plans."""
        return iter(self.plans)

    def __len__(self) -> int:
        """Returns count of changed package plans."""
        return len(self.plans)

    def __bool__(self) -> bool:
        """Returns True if any packages have changes."""
        return bool(self.plans)

    def get(self, package: str, default: Any = None) -> Optional[PackageStagePlan]:
        """Dictionary-like get accessor."""
        for p in self.plans:
            if p.package == package:
                return p
        return default

    def keys(self) -> List[str]:
        """Returns package names with changes."""
        return [p.package for p in self.plans]

    def values(self) -> List[PackageStagePlan]:
        """Returns package stage plans."""
        return list(self.plans)

    def items(self) -> List[Tuple[str, PackageStagePlan]]:
        """Returns (package, plan) tuples."""
        return [(p.package, p) for p in self.plans]

    def format_text(self) -> str:
        """Formats the stage result for human-readable terminal output."""
        if not self.plans:
            if self.dry_run:
                return "🔍 [DRY-RUN] No changes to stage. All files are up-to-date."
            return "No changes staged. All files are up-to-date."
        if self.dry_run:
            lines = [
                "🔍 [DRY-RUN] Planned Staging Operations (render/ -> install/):",
                "=" * 60,
            ]
            for plan in self.plans:
                lines.append(plan.format_text())
                lines.append("")
            total_actions = sum(len(p.actions) for p in self.plans)
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Staging simulation completed for {len(self.plans)} package(s). "
                f"Total planned actions: {total_actions} (zero install/ mutations performed)."
            )
            return "\n".join(lines)
        return "\n".join(plan.format_text() for plan in self.plans)


@dataclass
class PackageInstallPlan(SerializableModel):
    """Structured install plan detailing all planned filesystem operations and lifecycle hooks."""
    package: str = ""
    target_directory: str = ""
    install_method: InstallMethod = DEFAULT_INSTALL_METHOD
    actions: List[FileAction] = field(default_factory=list)
    hooks_to_trigger: List[str] = field(default_factory=list)

    @property
    def created(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type in (FileActionType.CREATE_SYMLINK, FileActionType.CREATE_COPY)]

    @property
    def updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_COPY]

    @property
    def permissions_updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_PERMISSION]

    @property
    def skipped(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.SKIP_IDENTICAL]

    @property
    def pruned(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.BACKUP_PRUNE]

    @property
    def overwritten_backups(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.BACKUP_OVERWRITE]

    @property
    def prune_backups(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.BACKUP_PRUNE]

    def format_text(self) -> str:
        """Formats the deployment plan for human-readable terminal output."""
        method_str = self.install_method.value if isinstance(self.install_method, Enum) else str(self.install_method)
        lines = [f"📦 Package '{self.package}':"]
        lines.append(f"  Target: {self.target_directory}")
        lines.append(f"  Method: {method_str}")

        if not self.actions:
            lines.append("  Planned Actions: (None)")
        else:
            lines.append("  Planned Actions:")
            lines.extend(format_action_line(action) for action in self.actions)

        if self.hooks_to_trigger:
            hooks_str = ", ".join(self.hooks_to_trigger)
            lines.append(f"  Lifecycle Hooks: {hooks_str}")

        lines.append(f"  Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass
class PackageUninstallPlan(SerializableModel):
    """Structured uninstallation plan detailing all planned filesystem operations and lifecycle hooks."""
    package: str = ""
    target_directory: str = ""
    install_method: InstallMethod = DEFAULT_INSTALL_METHOD
    detach_mode: bool = False
    actions: List[FileAction] = field(default_factory=list)
    hooks_to_trigger: List[str] = field(default_factory=list)

    @property
    def deleted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.DELETE_FILE]

    @property
    def removed(self) -> List[FileAction]:
        return self.deleted

    @property
    def restored(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY) and not self.detach_mode]

    @property
    def converted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.CREATE_COPY and self.detach_mode]

    @property
    def ensured_dirs(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.ENSURE_DIR]

    def format_text(self) -> str:
        """Formats the uninstallation plan for human-readable terminal output."""
        lines = [f"📦 Package '{self.package}':"]
        lines.append(f"  Target: {self.target_directory}")
        mode_label = "detach (convert to host copies)" if self.detach_mode else "uninstall"
        lines.append(f"  Mode: {mode_label}")

        if not self.actions:
            lines.append("  Planned Actions: (None - directory missing or no deployed files)")
        else:
            lines.append("  Planned Actions:")
            lines.extend(format_action_line(action) for action in self.actions)

        if self.hooks_to_trigger:
            hooks_str = ", ".join(self.hooks_to_trigger)
            lines.append(f"  Lifecycle Hooks: {hooks_str}")

        summary_parts = []
        if self.ensured_dirs:
            summary_parts.append(f"{len(self.ensured_dirs)} directories")
        if self.removed:
            summary_parts.append(f"{len(self.removed)} to remove")
        if self.restored:
            summary_parts.append(f"{len(self.restored)} to restore")
        if self.converted:
            summary_parts.append(f"{len(self.converted)} to convert to host copies")
        lines.append(f"  Summary: {', '.join(summary_parts) if summary_parts else '0 actions'}")
        return "\n".join(lines)


@dataclass
class PackageInstallResult(SerializableModel):
    plan: PackageInstallPlan = field(default_factory=PackageInstallPlan)
    is_first_time: bool = False
    status: str = "SUCCESS"
    error: Optional[str] = None

    @property
    def package(self) -> str:
        return self.plan.package

    @property
    def install_method(self) -> InstallMethod:
        return self.plan.install_method

    @property
    def target_directory(self) -> str:
        return self.plan.target_directory

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["package"] = self.package
        data["install_method"] = self.install_method.value if isinstance(self.install_method, Enum) else self.install_method
        data["target_directory"] = self.target_directory
        return data


@dataclass
class InstallResult(SerializableModel):
    command: str = "apply"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageInstallResult] = field(default_factory=list)
    error_package: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False

    def format_text(self) -> str:
        """Formats the install or simulation results for human-readable output."""
        if not self.packages:
            return "No packages targeted."

        lines = []
        if self.dry_run:
            lines.append("🔍 [DRY-RUN] Package Install Simulation Plan")
            lines.append("=" * 60)
            for pkg_res in self.packages:
                lines.append(pkg_res.plan.format_text())
                lines.append("")
            total_actions = sum(len(p.plan.actions) for p in self.packages)
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Simulation completed for {len(self.packages)} package(s). "
                f"Total planned actions: {total_actions} (zero host mutations performed)."
            )
        else:
            lines.append("✨ Install Summary:")
            for pkg_res in self.packages:
                status_icon = "✨" if pkg_res.status == "SUCCESS" else "❌"
                lines.append(f"  {status_icon} Package '{pkg_res.package}': {pkg_res.status}")
                if pkg_res.error:
                    lines.append(f"     Error: {pkg_res.error}")
        return "\n".join(lines)


# =============================================================================
# Primitive 7: Uninstall
# =============================================================================

@dataclass
class RestoredBackup(SerializableModel):
    source_backup: str
    restored_to: str


@dataclass
class PackageUninstallResult(SerializableModel):
    plan: PackageUninstallPlan = field(default_factory=PackageUninstallPlan)
    removed_files: List[str] = field(default_factory=list)
    converted_symlinks: List[str] = field(default_factory=list)
    restored_backups: List[RestoredBackup] = field(default_factory=list)
    status: str = "SUCCESS"
    error: Optional[str] = None

    @property
    def package(self) -> str:
        return self.plan.package

    @property
    def install_method(self) -> InstallMethod:
        return self.plan.install_method

    @property
    def target_directory(self) -> str:
        return self.plan.target_directory

    @property
    def detach_mode(self) -> bool:
        return self.plan.detach_mode

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["package"] = self.package
        data["install_method"] = self.install_method.value if isinstance(self.install_method, Enum) else self.install_method
        data["target_directory"] = self.target_directory
        data["detach_mode"] = self.detach_mode
        return data


@dataclass
class UninstallResult(SerializableModel):
    command: str = "uninstall"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    detach_mode: bool = False
    packages: List[PackageUninstallResult] = field(default_factory=list)
    error_message: Optional[str] = None
    dry_run: bool = False

    def __iter__(self):
        """Allows iterating over uninstalled package names."""
        return iter([p.package for p in self.packages if p.status == "SUCCESS"])

    def __len__(self):
        return len([p.package for p in self.packages if p.status == "SUCCESS"])

    @property
    def uninstalled_packages(self) -> List[str]:
        return [p.package for p in self.packages if p.status == "SUCCESS"]

    def format_text(self) -> str:
        """Formats the uninstallation/detachment results or simulation plan for terminal output."""
        if not self.packages:
            return "No packages targeted."

        lines = []
        if self.dry_run:
            mode_header = "Detachment" if self.detach_mode else "Uninstallation"
            lines.append(f"🔍 [DRY-RUN] Package {mode_header} Simulation Plan")
            lines.append("=" * 60)
            for pkg_res in self.packages:
                lines.append(pkg_res.plan.format_text())
                lines.append("")
            total_actions = sum(len(p.plan.actions) for p in self.packages)
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Simulation completed for {len(self.packages)} package(s). "
                f"Total planned actions: {total_actions} (zero host mutations performed)."
            )
        else:
            action_desc = "Detached" if self.detach_mode else "Uninstalled"
            lines.append(f"✨ {action_desc} Summary:")
            for pkg_res in self.packages:
                status_icon = "✨" if pkg_res.status == "SUCCESS" else "❌"
                lines.append(f"  {status_icon} Package '{pkg_res.package}': {pkg_res.status}")
                if pkg_res.error:
                    lines.append(f"     Error: {pkg_res.error}")
        return "\n".join(lines)


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
    dry_run: bool = False

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
    install_method: InstallMethod = DEFAULT_INSTALL_METHOD
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

    def format_text(self) -> str:
        """Formats human-readable summary of planned resource imports."""
        if not self.actions:
            return f"No resources to import into '{self.package}'."
        lines = [f"📦 Package '{self.package}':"]
        lines.extend(format_action_line(action) for action in self.actions)
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

    def format_text(self) -> str:
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
                self.plan.format_text(),
                "=" * 60,
                f"✨ [DRY-RUN] Import simulation completed. {len(self.plan.actions)} file(s) would be imported into '{self.package}'.",
            ]
            return "\n".join(lines)
        lines = [f"✨ Successfully imported {len(self.imported_files)} file(s) into package '{self.package}'."]
        for f in self.imported_files:
            lines.append(f"  📥 {f}")
        return "\n".join(lines)


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
    template_changes: Optional[GitStatusDiff] = None
    system_changes: Optional[GitStatusDiff] = None
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




