"""Data models, plans, and result containers for reverse-sync (Primitive 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.file_action import (
    DELETE_ACTION_TYPES,
    FileAction,
    FileActionType,
    filter_display_actions,
    format_action_line,
    format_action_summary,
    is_mutating_action,
)
from ..core.serialization import SerializableModel


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
        return [
            a for a in self.actions
            if a.action_type in (FileActionType.CREATE_COPY, FileActionType.CREATE_KEEP_FILE)
        ]

    @property
    def updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_COPY]

    @property
    def permissions_updated(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.UPDATE_PERMISSION]

    @property
    def deleted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type in DELETE_ACTION_TYPES]

    @property
    def skipped(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.SKIP_IDENTICAL]

    @property
    def has_mutations(self) -> bool:
        return any(is_mutating_action(a) for a in self.actions)

    @property
    def has_changes(self) -> bool:
        return self.has_mutations

    def format_text(self, verbose: bool = False) -> str:
        lines = [f"=== Reverse Sync Plan: {self.package} ==="]
        if self.status != "PENDING":
            lines.append(f"Status: {self.status}")
        if self.error:
            lines.append(f"Error: {self.error}")
        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            lines.append("  Reverse Sync Actions: (None)")
        else:
            lines.append("  Reverse Sync Actions:")
            for action in display_actions:
                lines.append(format_action_line(action))
        lines.append(f"  Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass
class ReverseSyncPlan(SerializableModel):
    """Structured reverse-sync plan for multiple packages."""
    plans: List[PackageReverseSyncPlan] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any(p.has_changes for p in self.plans)

    def format_text(self, verbose: bool = False) -> str:
        return "\n".join(plan.format_text(verbose=verbose) for plan in self.plans)


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
