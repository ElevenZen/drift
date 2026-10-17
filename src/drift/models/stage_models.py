"""Data models, plans, and result containers for staging render/ to install/ (Primitive 4)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ..core.file_action import (
    DELETE_ACTION_TYPES,
    FileAction,
    FileActionType,
    filter_display_actions,
    format_action_line,
    format_action_summary,
    is_mutating_action,
    rebase_file_action,
)
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel

if TYPE_CHECKING:
    from ..config.package_config import PackageConfig
    from ..core.state_registry import StateRegistry


@dataclass
class StageOptions(AlwaysTruthy):
    """Options controlling sandbox staging behavior."""
    force: bool = False
    no_deps: bool = False
    dry_run: bool = False


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
        return [a for a in self.actions if a.action_type in DELETE_ACTION_TYPES]

    @property
    def ensured_dirs(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.ENSURE_DIR]

    @property
    def package_name(self) -> str:
        return self.package

    @property
    def skipped(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.SKIP_IDENTICAL]

    @property
    def has_mutations(self) -> bool:
        return any(is_mutating_action(a) for a in self.actions)

    @property
    def has_changes(self) -> bool:
        """Returns True if the package staging plan contains physical mutations."""
        return self.has_mutations

    def rebased(self, old_base: Path, new_base: Path) -> PackageStagePlan:
        """Returns a copy of PackageStagePlan with action paths rebased from old_base to new_base."""
        return replace(
            self,
            actions=[rebase_file_action(a, old_base, new_base) for a in self.actions],
        )

    def format_text(self, verbose: bool = False) -> str:
        """Formats the staging plan for human-readable terminal output."""
        lines = [f"📦 Package '{self.package}':"]
        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            lines.append("  Stage Actions: (None)")
        else:
            lines.append("  Stage Actions:")
            lines.extend(format_action_line(action) for action in display_actions)
        lines.append(f" Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class StagePlan:
    """Pre-flight validated staging plan containing package configurations, plans, and state registry."""
    pkg_metadata: Dict[str, PackageConfig]
    state_registry: StateRegistry
    packages_stage_order: List[str] = field(default_factory=list)
    package_plans: Dict[str, PackageStagePlan] = field(default_factory=dict)
    unchanged_packages: List[str] = field(default_factory=list)
    options: StageOptions = field(default_factory=StageOptions)

    @property
    def has_changes(self) -> bool:
        """Returns True if any package stage plan contains actions."""
        return bool(self.packages_stage_order)


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

    def format_text(self, verbose: bool = False) -> str:
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
                lines.append(plan.format_text(verbose=verbose))
                lines.append("")
            total_actions = sum(len(p.actions) for p in self.plans)
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Staging simulation completed for {len(self.plans)} package(s). "
                f"Total planned actions: {total_actions}."
            )
            return "\n".join(lines)
        return "\n".join(plan.format_text(verbose=verbose) for plan in self.plans)
