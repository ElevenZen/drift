"""Data models, plans, options, and result containers for drift adoption (Primitive 8).

Extracted into a dedicated module to keep all adopt-related domain structures
(AdoptOptions, PackageDriftFiles, PackageAdoptPlan, AdoptPlan, AdoptStatus,
ItemAdoptOutcome, PackageAdoptResult, AdoptResult) co-located without circular
imports across drift.core, drift.config, and drift.primitives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from .mixins import AlwaysTruthy
from .serialization import SerializableModel

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineRegistry
    from ..hooks.lifecycle_hooks import HookExecFlags


class AdoptStatus(str, Enum):
    """Outcome status for single-package and workspace-wide drift adoption."""
    UP_TO_DATE = "UP_TO_DATE"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class ItemAdoptOutcome(str, Enum):
    """Outcome of reconciling a single drifted file or directory."""
    ADOPTED = "ADOPTED"
    SKIPPED = "SKIPPED"
    CONFLICT = "CONFLICT"

    def __bool__(self) -> bool:
        return self == ItemAdoptOutcome.ADOPTED


def get_rename_patch_key(old_rel: Path, new_rel: Path) -> str:
    """Generates a collision-free dictionary key for a rename pair (old_rel, new_rel).

    Because normalized Path.as_posix() strings never contain '//', joining with '//->//'
    guarantees that no two distinct (old_rel, new_rel) pairs can ever produce the same key.
    """
    return f"{old_rel.as_posix()}//->//{new_rel.as_posix()}"


def _default_render_engines() -> RenderEngineRegistry:
    """Lazily constructs an empty RenderEngineRegistry to avoid import-time cycles."""
    from ..config.render_engine_config import RenderEngineRegistry
    return RenderEngineRegistry()


@dataclass
class AdoptOptions(SerializableModel, AlwaysTruthy):
    """Configuration options controlling drift adoption behavior."""
    interactive: bool = False
    accept_conflicts: bool = False
    force: bool = False
    dry_run: bool = False
    no_render: bool = False
    no_reverse_sync: bool = False
    skip_patches: bool = False
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Any = None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run from AdoptOptions to HookExecFlags."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            override=HookExecFlags(dry_run=self.dry_run),
        )


@dataclass
class PackageDriftFiles(SerializableModel):
    """Categorized drift file paths for a single package."""
    additions: List[Path] = field(default_factory=list)
    deletions: List[Path] = field(default_factory=list)
    modifications: List[Path] = field(default_factory=list)
    renames: List[Tuple[Path, Path]] = field(default_factory=list)
    converged_files: List[Path] = field(default_factory=list)

    @property
    def has_divergent_drifts(self) -> bool:
        return bool(self.additions or self.deletions or self.modifications or self.renames)

    @property
    def has_drifts(self) -> bool:
        return bool(self.has_divergent_drifts or self.converged_files)

    @property
    def total_drifts(self) -> int:
        return (
            len(self.additions)
            + len(self.deletions)
            + len(self.modifications)
            + len(self.renames)
            + len(self.converged_files)
        )


@dataclass
class PackageAdoptPlan(SerializableModel):
    """Structured plan representing detected system drifts for a single package."""
    package: str = ""
    src_pkg_dir: Path = field(default_factory=Path)
    src_dir_to_render: Path = field(default_factory=Path)
    install_pkg_dir: Path = field(default_factory=Path)
    render_engines: RenderEngineRegistry = field(default_factory=_default_render_engines)
    drifts: PackageDriftFiles = field(default_factory=PackageDriftFiles)
    mod_patches: Dict[str, str] = field(default_factory=dict)
    rename_patches: Dict[str, str] = field(default_factory=dict)
    pre_source_already_triggered: bool = False
    status: str = "PENDING"
    error: Optional[str] = None

    @property
    def additions(self) -> List[Path]:
        return self.drifts.additions

    @property
    def deletions(self) -> List[Path]:
        return self.drifts.deletions

    @property
    def modifications(self) -> List[Path]:
        return self.drifts.modifications

    @property
    def renames(self) -> List[Tuple[Path, Path]]:
        return self.drifts.renames

    @property
    def converged_files(self) -> List[Path]:
        return self.drifts.converged_files

    @property
    def has_divergent_drifts(self) -> bool:
        return self.drifts.has_divergent_drifts

    @property
    def has_drifts(self) -> bool:
        return self.drifts.has_drifts

    @property
    def total_drifts(self) -> int:
        return self.drifts.total_drifts

    def _format_modification_line(self, file: Path, install_base: Path) -> str:
        """Formats a single modification preview line with template/static and conflict status."""
        from ..primitives.adopt_repo import (
            check_patch_conflicts,
            resolve_source_file_path,
            test_file_conflict,
        )

        src_file = resolve_source_file_path(self.render_engines, self.src_dir_to_render, file)
        if not src_file:
            return f"       ~ {file} [Static Overwrite]"

        is_templated = self.render_engines.find_engine_for_file(src_file.name) is not None
        if is_templated:
            patch_content = self.mod_patches.get(file.as_posix())
            if patch_content is None:
                install_file = self.install_pkg_dir / file
                pkg_rel_path = Path(self.package) / file
                has_conflict = test_file_conflict(src_file, install_file, install_base, pkg_rel_path)
            else:
                has_conflict = check_patch_conflicts(src_file, patch_content)
            if has_conflict:
                return f"       ~ {file} [CONFLICTS with template: {src_file.name}]"
            return f"       ~ {file} [Patch applies cleanly to: {src_file.name}]"

        return f"       ~ {file} [Static Overwrite of: {src_file.name}]"

    def format_text(self, verbose: bool = False) -> str:
        """Formats a human-readable preview of all planned drift changes and potential conflicts."""
        lines = [f"📦 Package '{self.package}':"]
        if not self.has_drifts:
            lines.append("   No drifts detected.")
            return "\n".join(lines)

        install_base = self.install_pkg_dir.parent

        if self.converged_files:
            lines.append("   [=] Converged (already identical to render/, will be staged directly):")
            lines.extend(f"       = {item}" for item in self.converged_files)

        if self.additions:
            lines.append("   [+] Additions (will be copied to source):")
            lines.extend(
                f"       + {item}{' (directory)' if (self.install_pkg_dir / item).is_dir() else ''}"
                for item in self.additions
            )

        if self.deletions:
            lines.append("   [-] Deletions (will be removed from source):")
            lines.extend(
                f"       - {item}{' (directory)' if (self.src_dir_to_render / item).is_dir() else ''}"
                for item in self.deletions
            )

        if self.renames:
            lines.append("   [R] Renames (will rename the template in source):")
            for old_file, new_file in self.renames:
                lines.append(f"       R {old_file} -> {new_file}")
                if verbose:
                    patch_str = self.rename_patches.get(get_rename_patch_key(old_file, new_file), "").strip()
                    if patch_str:
                        lines.extend(f"         {line}" for line in patch_str.splitlines())

        if self.modifications:
            lines.append("   [~] Modifications:")
            for file in self.modifications:
                lines.append(self._format_modification_line(file, install_base))
                if verbose:
                    patch_str = self.mod_patches.get(file.as_posix(), "").strip()
                    if patch_str:
                        lines.extend(f"         {line}" for line in patch_str.splitlines())

        return "\n".join(lines)


@dataclass
class AdoptPlan(SerializableModel):
    """Structured multi-package drift adoption plan."""
    plans: List[PackageAdoptPlan] = field(default_factory=list)
    options: AdoptOptions = field(default_factory=AdoptOptions)

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run

    @property
    def has_drifts(self) -> bool:
        return any(p.has_drifts for p in self.plans)

    @property
    def drifted_packages(self) -> List[str]:
        return [p.package for p in self.plans if p.has_drifts]

    def get_package_plan(self, package: str) -> Optional[PackageAdoptPlan]:
        return next((p for p in self.plans if p.package == package), None)

    def format_text(self, verbose: bool = False) -> str:
        """Formats the multi-package adoption plan for human-readable terminal output."""
        if not self.plans or not self.has_drifts:
            return "🔍 [DRY-RUN] No drifted packages found in local state database."
        return "\n\n".join(plan.format_text(verbose=verbose) for plan in self.plans)


@dataclass
class PackageAdoptResult(SerializableModel):
    """Structured result for a single package's drift adoption execution or simulation."""
    package: str = ""
    adopted_additions: List[str] = field(default_factory=list)
    adopted_modifications: List[str] = field(default_factory=list)
    adopted_deletions: List[str] = field(default_factory=list)
    adopted_renames: List[str] = field(default_factory=list)
    adopted_converged: List[str] = field(default_factory=list)
    skipped_files: List[str] = field(default_factory=list)
    conflicted_files: List[str] = field(default_factory=list)
    status: AdoptStatus = AdoptStatus.SUCCESS
    error: Optional[str] = None
    plan: PackageAdoptPlan = field(default_factory=PackageAdoptPlan, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.package and self.plan.package:
            self.package = self.plan.package
        elif self.package and not self.plan.package:
            self.plan.package = self.package

    @property
    def has_adopted_changes(self) -> bool:
        return bool(
            self.adopted_additions
            or self.adopted_modifications
            or self.adopted_deletions
            or self.adopted_renames
            or self.adopted_converged
        )

    @property
    def unresolved_files(self) -> List[str]:
        return self.skipped_files + self.conflicted_files

    @property
    def has_unresolved_files(self) -> bool:
        return bool(self.skipped_files or self.conflicted_files)

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data.pop("plan", None)
        return data

    def format_text(self, dry_run: bool = False, verbose: bool = False) -> str:
        """Formats the package adoption result or dry-run plan for human-readable terminal output."""
        if dry_run:
            return self.plan.format_text(verbose=verbose)

        status_icons = {
            AdoptStatus.UP_TO_DATE: "✨",
            AdoptStatus.SUCCESS: "✨",
            AdoptStatus.PARTIAL: "⚠️",
            AdoptStatus.SKIPPED: "⚠️",
            AdoptStatus.FAILED: "❌",
        }
        icon = status_icons.get(self.status, "📦")
        lines = [f"📦 Package '{self.package}' [{icon} {self.status}]:"]

        if self.status == AdoptStatus.UP_TO_DATE and not self.has_adopted_changes and not self.has_unresolved_files:
            lines.append("   No drifts detected.")
            return "\n".join(lines)

        if self.adopted_converged:
            lines.append("   [=] Converged (staged directly):")
            lines.extend(f"       = {item}" for item in self.adopted_converged)

        if self.adopted_additions:
            lines.append("   [+] Adopted Additions:")
            lines.extend(f"       + {item}" for item in self.adopted_additions)

        if self.adopted_deletions:
            lines.append("   [-] Adopted Deletions:")
            lines.extend(f"       - {item}" for item in self.adopted_deletions)

        if self.adopted_renames:
            lines.append("   [R] Adopted Renames:")
            lines.extend(f"       R {item}" for item in self.adopted_renames)

        if self.adopted_modifications:
            lines.append("   [~] Adopted Modifications:")
            lines.extend(f"       ~ {item}" for item in self.adopted_modifications)

        if self.conflicted_files:
            lines.append("   [!] Conflicted (requires manual resolution or --accept-conflicts):")
            lines.extend(f"       ! {item}" for item in self.conflicted_files)

        if self.skipped_files:
            lines.append("   [S] Skipped:")
            lines.extend(f"       S {item}" for item in self.skipped_files)

        if self.error:
            lines.append(f"   Error: {self.error}")

        return "\n".join(lines)


@dataclass
class AdoptResult(SerializableModel):
    """Aggregate result for workspace-wide or multi-package drift adoption."""
    command: str = "adopt"
    status: str = AdoptStatus.SUCCESS  # "UP_TO_DATE", "SUCCESS", "PARTIAL", "SKIPPED", "FAILED"
    packages: List[PackageAdoptResult] = field(default_factory=list)
    error_message: Optional[str] = None
    dry_run: bool = False

    def __iter__(self):
        return iter([p.package for p in self.packages if p.status == AdoptStatus.SUCCESS])

    def __len__(self):
        return len([p.package for p in self.packages if p.status == AdoptStatus.SUCCESS])

    def format_text(self, verbose: bool = False) -> str:
        """Formats the adoption result or dry-run preview for human-readable terminal output."""
        if not self.packages or (not verbose and all(p.status == AdoptStatus.UP_TO_DATE for p in self.packages)):
            if self.dry_run:
                return "🔍 [DRY-RUN] No drifted packages found in local state database."
            return "✨ No drifted packages found in local state database."

        lines: List[str] = []
        if self.dry_run:
            lines.append("🔍 [DRY-RUN] Planned Drift Adoption Operations (install/ -> src/):")
            lines.append("=" * 60)
            for pkg_res in self.packages:
                lines.append(pkg_res.format_text(dry_run=True, verbose=verbose))
                lines.append("")
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Adoption preview completed for {len(self.packages)} package(s). "
                f"Overall status: {self.status} (zero source or repository mutations performed)."
            )
            return "\n".join(lines)

        lines.append(f"✨ Adoption Summary (status: {self.status}):")
        for pkg_res in self.packages:
            lines.append(pkg_res.format_text(dry_run=False, verbose=verbose))
        if self.error_message:
            lines.append(f"❌ Error: {self.error_message}")
        return "\n".join(lines)
