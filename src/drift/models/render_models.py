"""Data models, options, and result containers for template rendering (Primitive 2)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING, Union

from ..core.file_action import (
    FileAction,
    filter_display_actions,
    format_action_line,
    format_action_summary,
    is_mutating_action,
    rebase_file_action,
)
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel

if TYPE_CHECKING:
    from ..config.workspace_config import SettingsConfig
    from ..hooks.lifecycle_hooks import HookExecFlags


@dataclass
class RenderOptions(AlwaysTruthy):
    """Options controlling package rendering behavior.

    Attributes:
        no_cache: When True, bypasses cache and forces clean re-rendering.
        dry_run: When True, simulates render planning and digestion without modifying
            the filesystem or executing lifecycle hooks.
        flags: Optional HookExecFlags controlling hook execution options.
    """
    no_cache: bool = False
    dry_run: bool = False
    silent: bool = False
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Optional[SettingsConfig] = None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run and no_cache from RenderOptions to HookExecFlags.

        When flags is not explicitly specified, defaults no_hooks to True in dry_run mode
        (zero mutation simulation), and False in normal mode.
        """
        from ..hooks.lifecycle_hooks import HookExecFlags
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            fallback=HookExecFlags(no_hooks=self.dry_run),
            override=HookExecFlags(no_cache=self.no_cache, dry_run=self.dry_run),
        )

    @classmethod
    def resolve(
        cls,
        options: Optional[Union[RenderOptions, HookExecFlags]] = None,
    ) -> RenderOptions:
        """Resolves RenderOptions from either RenderOptions or HookExecFlags."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        if isinstance(options, RenderOptions):
            return options
        if isinstance(options, HookExecFlags):
            return cls(
                no_cache=options.no_cache,
                dry_run=options.dry_run,
                flags=options,
            )
        return cls()


# NOTE [Action Path Representation Invariant]:
# PackageRenderResult.actions uses absolute paths because template compilation involves complex,
# non-trivial file mappings (template extensions, custom engines, partials, and .drift/ control plane).
# Conversely, PackageStagePlan, PackageInstallPlan, and PackageUninstallPlan operate on relative paths
# representing strict 1:1 structural mirroring between repository tiers and host targets.

@dataclass
class PackageRenderResult(SerializableModel):
    package: str
    status: str = "SUCCESS"  # "SUCCESS", "SKIPPED", "FAILED", "UP_TO_DATE"
    actions: List[FileAction] = field(default_factory=list)
    rendered_files: List[str] = field(default_factory=list)
    copied_static_files: List[str] = field(default_factory=list)
    skip_reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """Returns True if the package rendered successfully or was already up-to-date."""
        return self.status in ("SUCCESS", "UP_TO_DATE")

    @property
    def has_changes(self) -> bool:
        """Returns True if any file was rendered, copied, or pruned (excluding pure cache skips and info messages)."""
        return any(is_mutating_action(a) for a in self.actions)

    def rebased(self, old_base: Path, new_base: Path) -> PackageRenderResult:
        """Returns a copy of PackageRenderResult with all action paths rebased from old_base to new_base."""
        return replace(
            self,
            actions=[rebase_file_action(a, old_base, new_base) for a in self.actions],
        )

    def format_text(self, drift_root: Optional[Path] = None, verbose: bool = False) -> str:
        """Formats the render plan for human-readable terminal output."""
        lines = [f"📦 Package '{self.package}':"]
        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            lines.append("  Render Actions: (None)")
        else:
            lines.append("  Render Actions:")
            lines.extend(format_action_line(action, drift_root=drift_root) for action in display_actions)
        lines.append(f" Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass
class RenderResult(SerializableModel):
    command: str = "render"
    status: str = "SUCCESS"  # "SUCCESS", "FAILED"
    packages: List[PackageRenderResult] = field(default_factory=list)
    error_package: Optional[str] = None
    error_message: Optional[str] = None
    dry_run: bool = False

    def format_text(self, drift_root: Optional[Path] = None, verbose: bool = False) -> str:
        """Formats the render result for human-readable terminal output."""
        if not self.packages:
            if self.dry_run:
                return "🔍 [DRY-RUN] No packages to render."
            return "No packages rendered."
        if self.dry_run:
            lines = [
                "🔍 [DRY-RUN] Planned Render Operations (src/ -> render/):",
                "=" * 60,
            ]
            for pkg_res in self.packages:
                lines.append(pkg_res.format_text(drift_root, verbose=verbose))
                lines.append("")
            total_actions = sum(len(p.actions) for p in self.packages)
            lines.append("=" * 60)
            lines.append(
                f"✨ [DRY-RUN] Render simulation completed for {len(self.packages)} package(s). "
                f"Total planned actions: {total_actions}."
            )
            return "\n".join(lines)
        return "\n".join(
            f"📦 Package '{p.package}': {format_action_summary(p.actions)}" for p in self.packages
        )
