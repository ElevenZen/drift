"""Data models, options, contexts, plans, and result containers for package uninstallation (Primitive 7)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

from ..core.constants import InstallMethod
from ..core.file_action import (
    DELETE_ACTION_TYPES,
    FileAction,
    FileActionExecutionContext,
    FileActionType,
    filter_display_actions,
    format_action_line,
    is_mutating_action,
)
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel

if TYPE_CHECKING:
    from ..config.package_config import PackageConfig
    from ..config.package_hooks import PackageHooks
    from ..config.workspace_config import SettingsConfig, WorkspaceConfig
    from ..core.state_registry import PackageState, StateRegistry
    from ..hooks.lifecycle_hooks import HookExecFlags


@dataclass
class UninstallOptions(AlwaysTruthy):
    """Configuration options controlling package uninstallation behavior."""
    force: bool = False
    adhoc: bool = False
    dry_run: bool = False
    detach: bool = False
    no_deps: bool = False
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Optional[SettingsConfig] = None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run from UninstallOptions to HookExecFlags."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            override=HookExecFlags(dry_run=self.dry_run),
        )


@dataclass(frozen=True)
class PackageUninstallContext:
    """Encapsulates resolved package metadata and filesystem paths for uninstallation operations."""
    pkg_name: str
    target_dir: Path
    install_method: InstallMethod
    deployed_files: List[Path]
    sudo: bool
    install_pkg_dir: Path
    backup_pkg_dir: Path
    drift_root: Path
    hooks: Optional[PackageHooks] = None
    detach: bool = False
    is_missing_install_dir: bool = False

    @property
    def file_action_context(self) -> FileActionExecutionContext:
        return FileActionExecutionContext(
            sudo=self.sudo,
        )

    @contextmanager
    def package_envs(self) -> Iterator[None]:
        """Context manager to activate package-specific environment variables for hooks."""
        if self.hooks and getattr(self.hooks, "_package_config", None):
            assert self.hooks._package_config is not None, "PackageHooks missing _package_config for envs"
            with self.hooks._package_config.package_envs():
                yield
        else:
            from ..utils.env_utils import env_scope
            with env_scope({"drift_package_name": self.pkg_name}):
                yield

    @classmethod
    def from_package_state(
        cls,
        workspace_config: WorkspaceConfig,
        pkg: str,
        pkg_state: PackageState,
        pkg_config: Optional[PackageConfig] = None,
        detach: bool = False,
    ) -> PackageUninstallContext:
        install_pkg_dir = workspace_config.install_path / pkg
        is_missing = not install_pkg_dir.is_dir()
        target_dir = (
            pkg_state.target_directory
            if pkg_state.target_directory is not None
            else (pkg_config.get_target_directory(workspace_config) if pkg_config else Path("/"))
        )
        sudo = pkg_state.sudo if pkg_state.sudo else (pkg_config.package.sudo if pkg_config else False)
        install_method = pkg_state.install_method or InstallMethod.DEFAULT
        hooks = pkg_config.hooks if pkg_config and not is_missing else None

        return cls(
            pkg_name=pkg,
            target_dir=target_dir,
            install_method=install_method,
            deployed_files=list(pkg_state.deployed_files),
            sudo=sudo,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=workspace_config.backup_path / pkg,
            drift_root=workspace_config.drift_root,
            hooks=hooks,
            detach=detach,
            is_missing_install_dir=is_missing,
        )


@dataclass
class PackageUninstallPlan(SerializableModel):
    """Structured uninstallation plan detailing all planned filesystem operations and lifecycle hooks."""
    package: str = ""
    target_directory: str = ""
    install_method: InstallMethod = InstallMethod.DEFAULT
    detach_mode: bool = False
    actions: List[FileAction] = field(default_factory=list)
    hooks_to_trigger: List[str] = field(default_factory=list)

    @property
    def deleted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type in DELETE_ACTION_TYPES]

    @property
    def removed(self) -> List[FileAction]:
        return self.deleted

    @property
    def restored(self) -> List[FileAction]:
        return [
            a for a in self.actions
            if a.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY) and not self.detach_mode
        ]

    @property
    def converted(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.CREATE_COPY and self.detach_mode]

    @property
    def ensured_dirs(self) -> List[FileAction]:
        return [a for a in self.actions if a.action_type == FileActionType.ENSURE_DIR]

    @property
    def has_mutations(self) -> bool:
        return any(is_mutating_action(a) for a in self.actions)

    @property
    def has_changes(self) -> bool:
        return self.has_mutations

    def format_text(self, verbose: bool = False) -> str:
        """Formats the uninstallation plan for human-readable terminal output."""
        lines = [f"📦 Package '{self.package}':"]
        lines.append(f"  Target: {self.target_directory}")
        mode_label = "detach (convert to host copies)" if self.detach_mode else "uninstall"
        lines.append(f"  Mode: {mode_label}")

        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            lines.append("  Planned Actions: (None - directory missing or no deployed files)")
        else:
            lines.append("  Planned Actions:")
            lines.extend(format_action_line(action) for action in display_actions)

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


@dataclass(frozen=True)
class UninstallPlan:
    """Pre-flight validated uninstallation plan containing package configurations and state registry."""
    pkg_config_map: Dict[str, PackageConfig]
    packages_to_uninstall: Dict[str, PackageState]
    state_registry: StateRegistry
    ordered_packages: List[str]
    options: UninstallOptions
    contexts: Dict[str, PackageUninstallContext] = field(default_factory=dict)
    package_plans: Dict[str, PackageUninstallPlan] = field(default_factory=dict)


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

    def format_text(self, verbose: bool = False) -> str:
        """Formats the uninstallation/detachment results or simulation plan for terminal output."""
        if not self.packages:
            return "No packages targeted."

        lines = []
        if self.dry_run:
            mode_header = "Detachment" if self.detach_mode else "Uninstallation"
            lines.append(f"🔍 [DRY-RUN] Package {mode_header} Simulation Plan")
            lines.append("=" * 60)
            for pkg_res in self.packages:
                lines.append(pkg_res.plan.format_text(verbose=verbose))
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
