"""Data models, plans, options, and result containers for drift adoption (Primitive 8).

Co-locates all adopt-related domain structures (TokenKind, TokenizedLine, SkeletonMatch,
DiffRelation, DiffUnitType, DiffUnit, CleanPatchHunk, ConflictCluster,
TwoStepDerenderPlan, AdoptPatch, AdoptStatus, ItemAdoptOutcome, get_rename_patch_key,
AdoptOptions, PackageDriftFiles, PackageAdoptPlan, AdoptPlan, PackageAdoptResult,
AdoptResult) without circular imports across drift.core, drift.config, and drift.primitives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, TYPE_CHECKING

from ..core.constants import DRIFT_KEEP_FILE_NAME
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel
from ..utils.func_utils import choose, when_not_none

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineRegistry, RenderSourceMatch
    from ..config.workspace_config import SettingsConfig
    from ..hooks.lifecycle_hooks import HookExecFlags


class TokenKind(str, Enum):
    """Neovim-style three-way character class partition for lossless line tokenization."""
    WORD = "word"
    PUNCT = "punct"
    SPACE = "space"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class TokenizedLine:
    """Losslessly tokenized line with pre-extracted word set for O(1) disjointness pruning."""
    raw_line: str
    stripped_line: str
    line_ending: str
    tokens: Tuple[str, ...]
    token_kinds: Tuple[TokenKind, ...]
    word_set: FrozenSet[str]


@dataclass(frozen=True)
class SkeletonMatch:
    """Result of comparing two TokenizedLines for shared static skeleton."""
    is_valid: bool
    score: float
    signature: Tuple[str, ...]


class DiffRelation(str, Enum):
    """Identifies whether an alignment unit maps Template -> Rendered (TR) or Rendered -> Host (RH)."""
    TR = "tr"
    RH = "rh"

    def __str__(self) -> str:
        return self.value


class DiffUnitType(str, Enum):
    """Six mutually exclusive atomic alignment/edit unit types shared across TR and RH."""
    EQUAL = "equal"
    SINGLE_EDIT = "single_edit"
    SINGLE_SKEL_EDIT = "single_skel_edit"
    BLOCK_EDIT = "block_edit"
    INSERT = "insert"
    DELETE = "delete"

    def __str__(self) -> str:
        return self.value


@dataclass
class DiffUnit(SerializableModel):
    """Unified atomic alignment unit for both T -> R (TR) and R -> H (RH)."""
    relation: DiffRelation
    unit_type: DiffUnitType
    src_start: int
    src_end: int
    dst_start: int
    dst_end: int
    is_conflict: bool = False
    derendered_lines: Tuple[str, ...] = ()

    @property
    def r_start(self) -> int:
        """Start coordinate on the shared Rendered (R) axis."""
        return choose(self.relation == DiffRelation.TR, self.dst_start, self.src_start)

    @property
    def r_end(self) -> int:
        """End coordinate on the shared Rendered (R) axis."""
        return choose(self.relation == DiffRelation.TR, self.dst_end, self.src_end)

    @property
    def src_len(self) -> int:
        return self.src_end - self.src_start

    @property
    def dst_len(self) -> int:
        return self.dst_end - self.dst_start

    @property
    def is_positive_length(self) -> bool:
        """True when both source and destination spans have at least 1 line."""
        return self.src_len >= 1 and self.dst_len >= 1


@dataclass
class CleanPatchHunk(SerializableModel):
    """A single 0-based Step 1 slice replacement on source file `T[t_start:t_end]`.

    `old_lines` and `new_lines` store newline-stripped strings (`keepends=False`).
    `old_eof_newline` and `new_eof_newline` record whether the old or new side ends
    with a trailing newline when the hunk touches the end of the file.
    """
    t_start: int
    t_end: int
    old_lines: Tuple[str, ...] = ()
    new_lines: Tuple[str, ...] = ()
    old_eof_newline: bool = True
    new_eof_newline: bool = True

    @property
    def is_old_eof_nl_valid(self) -> bool:
        """True when `old_eof_newline` is meaningful (the hunk spans at least one old line)."""
        return bool(self.old_lines)

    @property
    def is_new_eof_nl_valid(self) -> bool:
        """True when `new_eof_newline` is meaningful (the hunk produces at least one new line)."""
        return bool(self.new_lines)


@dataclass
class ConflictCluster(SerializableModel):
    """A converged Step 2 conflict cluster over (T, R, H) with shifted coordinates in T_clean."""
    t_start: int
    t_end: int
    t_clean_start: int
    t_clean_end: int
    t_lines: Tuple[str, ...]
    r_lines: Tuple[str, ...]
    h_lines: Tuple[str, ...]


@dataclass
class TwoStepDerenderPlan(SerializableModel):
    """Complete pure-computation output of the 3-way template de-rendering engine."""
    t_lines: Tuple[str, ...] = ()
    t_clean_lines: Tuple[str, ...] = ()
    clean_hunks: Tuple[CleanPatchHunk, ...] = ()
    conflict_clusters: Tuple[ConflictCluster, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflict_clusters)


@dataclass
class AdoptPatch(SerializableModel):
    """Unified patch & target container for both modifications and renames (static or templated).

    Stored in `PackageAdoptPlan.mod_patches` keyed by `rel_mod.as_posix()` for modifications
    and `get_rename_patch_key(old_rel, new_rel)` for renames.
    """
    src_match: Optional[RenderSourceMatch] = None
    patch_from: Path = field(default_factory=Path)
    has_conflict: bool = False
    clean_hunks: Tuple[CleanPatchHunk, ...] = ()
    conflict_clusters: Tuple[ConflictCluster, ...] = ()
    display_diff: str = ""

    @property
    def is_templated(self) -> bool:
        """True if the target source file is a template."""
        return bool(self.src_match is not None and self.src_match.is_templated)

    @property
    def has_changes(self) -> bool:
        """True if there are Step 1 clean hunks or Step 2 conflict clusters."""
        return bool(self.clean_hunks) or bool(self.conflict_clusters)


class AdoptStatus(str, Enum):
    """Outcome status for single-package and workspace-wide drift adoption."""
    UP_TO_DATE = "UP_TO_DATE"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"

    def __str__(self) -> str:
        return self.value


class ItemAdoptOutcome(str, Enum):
    """Outcome of reconciling a single drifted file or directory."""
    ADOPTED = "ADOPTED"
    SKIPPED = "SKIPPED"
    CONFLICT = "CONFLICT"

    def __str__(self) -> str:
        return self.value

    def __bool__(self) -> bool:
        return self == ItemAdoptOutcome.ADOPTED


def get_rename_patch_key(old_rel: Path, new_rel: Path) -> str:
    """Generates a collision-free dictionary key for a rename pair (old_rel, new_rel).

    Because normalized Path.as_posix() strings never contain '//', joining with '//->//'
    guarantees that no two distinct (old_rel, new_rel) pairs or modification paths can
    ever produce the same key.
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
    with_render: bool = False
    no_reverse_sync: bool = False
    skip_patches: bool = False
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Optional[SettingsConfig] = None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run from AdoptOptions unless with_render is True."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            override=HookExecFlags(dry_run=self.dry_run and not self.with_render),
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
    render_pkg_dir: Path = field(default_factory=Path)
    render_engines: RenderEngineRegistry = field(default_factory=_default_render_engines)
    drifts: PackageDriftFiles = field(default_factory=PackageDriftFiles)
    mod_patches: Dict[str, AdoptPatch] = field(default_factory=dict)
    pre_source_already_triggered: bool = False
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

    def _format_modification_line(self, file: Path) -> str:
        """Formats a single modification preview line from pre-computed plan metadata."""
        if file.name == DRIFT_KEEP_FILE_NAME:
            return f"       ~ {file} [Discard corrupt keep file modification]"

        patch_plan = self.mod_patches.get(file.as_posix())
        src_match = when_not_none(patch_plan, lambda p: p.src_match)
        if src_match is None:
            return f"       ~ {file} [Static Overwrite]"

        src_name = src_match.path.name
        has_conflict = bool(patch_plan is not None and patch_plan.has_conflict)
        if src_match.is_templated:
            return choose(
                has_conflict,
                f"       ~ {file} [CONFLICTS with template: {src_name}]",
                f"       ~ {file} [Patch applies cleanly to: {src_name}]",
            )

        return choose(
            has_conflict,
            f"       ~ {file} [CONFLICTS with source: {src_name}]",
            f"       ~ {file} [Static Overwrite of: {src_name}]",
        )

    def format_text(self, verbose: bool = False) -> str:
        """Formats a human-readable preview of all planned drift changes and potential conflicts."""
        lines = [f"📦 Package '{self.package}':"]
        if not self.has_drifts:
            lines.append("   No drifts detected.")
            return "\n".join(lines)

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
                    rename_patch = self.mod_patches.get(get_rename_patch_key(old_file, new_file))
                    patch_str = when_not_none(rename_patch, lambda p: p.display_diff.strip()) or ""
                    if patch_str:
                        lines.extend(f"         {line}" for line in patch_str.splitlines())

        if self.modifications:
            lines.append("   [~] Modifications:")
            for file in self.modifications:
                lines.append(self._format_modification_line(file))
                if verbose:
                    mod_patch = self.mod_patches.get(file.as_posix())
                    patch_str = when_not_none(mod_patch, lambda p: p.display_diff.strip()) or ""
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
    status: AdoptStatus = AdoptStatus.SUCCESS
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
