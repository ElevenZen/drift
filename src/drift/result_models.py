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
