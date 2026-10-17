"""Data models, options, contexts, plans, and result containers for package installation (Primitive 5)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, TYPE_CHECKING

from ..core.constants import BackupSubfolder, InstallMethod
from ..core.file_action import (
    FileAction,
    FileActionExecutionContext,
    FileActionType,
    filter_display_actions,
    format_action_line,
    format_action_summary,
    is_mutating_action,
    rebase_file_action,
)
from ..core.folder_delivery import DeliveryInspectionContext
from ..core.mixins import AlwaysTruthy
from ..core.serialization import SerializableModel

if TYPE_CHECKING:
    from ..config.package_config import PackageConfig
    from ..config.package_hooks import PackageHooks
    from ..config.workspace_config import WorkspaceConfig
    from ..core.ignore import DriftIgnore
    from ..core.state_registry import StateRegistry
    from ..hooks.lifecycle_hooks import HookExecFlags


def _default_package_hooks() -> PackageHooks:
    """Lazily constructs an empty PackageHooks instance to avoid import-time cycles."""
    from ..config.package_hooks import PackageHooks
    return PackageHooks()


@dataclass
class InstallOptions(AlwaysTruthy):
    """Options controlling package installation behavior."""
    resolve_symlinks: bool = True
    force: bool = False
    reinstall: bool = False
    run_hooks_when_unchanged: bool = False
    dry_run: bool = False
    no_deps: bool = False
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings: Any = None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run from InstallOptions to HookExecFlags."""
        from ..hooks.lifecycle_hooks import HookExecFlags
        return HookExecFlags.resolve(
            self.flags,
            settings=settings,
            override=HookExecFlags(dry_run=self.dry_run),
        )


@dataclass(frozen=True)
class PackageInstallContext:
    """Encapsulates resolved package metadata and filesystem paths for installation operations."""
    pkg_name: str
    install_pkg_dir: Path
    backup_pkg_dir: Path
    target_dir: Path
    install_method: InstallMethod
    ignore_handler: DriftIgnore
    sudo: bool
    is_first_time: bool
    drift_root: Path
    source_dir_mask: Optional[Path] = None
    hooks: PackageHooks = field(default_factory=_default_package_hooks)
    reinstall: bool = False
    run_hooks_when_unchanged: bool = False

    def rebase_install_dir(self, install_base: Path) -> PackageInstallContext:
        """Returns a copy of PackageInstallContext bound to the canonical install_base directory."""
        return replace(
            self,
            install_pkg_dir=install_base / self.pkg_name,
            source_dir_mask=None,
        )

    @property
    def file_action_context(self) -> FileActionExecutionContext:
        return FileActionExecutionContext(
            sudo=self.sudo,
            wal_dir=self.backup_pkg_dir / BackupSubfolder.WAL,
        )

    @property
    def delivery_context(self) -> DeliveryInspectionContext:
        """Derives delivery inspection context from package install context."""
        return DeliveryInspectionContext(
            target_dir=self.target_dir,
            source_dir=self.install_pkg_dir,
            drift_root=self.drift_root,
            install_method=self.install_method,
            is_first_time=self.is_first_time,
            backup_pkg_dir=self.backup_pkg_dir,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            source_dir_mask=self.source_dir_mask,
            reinstall=self.reinstall,
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
    def from_package(
        cls,
        workspace_config: WorkspaceConfig,
        state_registry: StateRegistry,
        metadata: PackageConfig,
        reinstall: bool = False,
        run_hooks_when_unchanged: bool = False,
    ) -> PackageInstallContext:
        from ..core.ignore import DriftIgnore
        pkg = metadata.name
        target_dir = metadata.get_target_directory(workspace_config)
        install_pkg_dir = workspace_config.install_path / pkg
        source_dir_mask = (
            workspace_config.install_path_mask / pkg
            if workspace_config.install_path_mask is not None
            else None
        )
        backup_pkg_dir = workspace_config.backup_path / pkg
        pkg_state = state_registry.packages.get(pkg)
        is_first_time = pkg_state is None or (pkg_state.last_deployed is None and pkg_state.state != "installed")
        ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
        install_method = metadata.get_install_method(workspace_config)
        return cls(
            pkg_name=pkg,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=backup_pkg_dir,
            target_dir=target_dir,
            install_method=install_method,
            ignore_handler=ignore_handler,
            sudo=metadata.package.sudo,
            is_first_time=is_first_time,
            drift_root=workspace_config.drift_root,
            source_dir_mask=source_dir_mask,
            hooks=metadata.hooks,
            reinstall=reinstall,
            run_hooks_when_unchanged=run_hooks_when_unchanged,
        )


@dataclass
class PackageInstallPlan(SerializableModel):
    """Structured install plan detailing all planned filesystem operations and lifecycle hooks."""
    package: str = ""
    target_directory: str = ""
    install_method: InstallMethod = InstallMethod.DEFAULT
    actions: List[FileAction] = field(default_factory=list)
    hooks_to_trigger: List[str] = field(default_factory=list)
    can_skip: bool = False

    @property
    def has_mutations(self) -> bool:
        return any(is_mutating_action(a) for a in self.actions)

    @property
    def has_changes(self) -> bool:
        """Returns True if the package has mutations on the host and cannot be skipped."""
        return not self.can_skip and self.has_mutations

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

    def rebased(self, old_base: Path, new_base: Path) -> PackageInstallPlan:
        """Returns a copy of PackageInstallPlan with action paths rebased from old_base to new_base."""
        return replace(
            self,
            actions=[rebase_file_action(a, old_base, new_base) for a in self.actions],
        )

    def format_text(self, verbose: bool = False) -> str:
        """Formats the deployment plan for human-readable terminal output."""
        method_str = self.install_method.value if isinstance(self.install_method, Enum) else str(self.install_method)
        lines = [f"📦 Package '{self.package}':"]
        lines.append(f"  Target: {self.target_directory}")
        lines.append(f"  Method: {method_str}")

        display_actions = filter_display_actions(self.actions, verbose=verbose)
        if not display_actions:
            lines.append("  Planned Actions: (None)")
        else:
            lines.append("  Planned Actions:")
            lines.extend(format_action_line(action) for action in display_actions)

        if self.hooks_to_trigger:
            hooks_str = ", ".join(self.hooks_to_trigger)
            lines.append(f"  Lifecycle Hooks: {hooks_str}")

        lines.append(f"  Summary: {format_action_summary(self.actions)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class InstallPlan:
    """Pre-flight validated installation plan containing package configurations, options, and state registry."""
    pkg_metadata_map: Dict[str, PackageConfig]
    state_registry: StateRegistry
    options: InstallOptions
    packages_install_order: List[str]
    skipped_packages: List[str] = field(default_factory=list)
    contexts: Dict[str, PackageInstallContext] = field(default_factory=dict)
    package_plans: Dict[str, PackageInstallPlan] = field(default_factory=dict)

    def rebase_install_dir(
        self,
        install_base: Path,
        state_registry: Optional[StateRegistry] = None,
    ) -> InstallPlan:
        """Returns a copy of InstallPlan with all package contexts bound to canonical install_base."""
        return replace(
            self,
            state_registry=state_registry if state_registry is not None else self.state_registry,
            contexts={
                pkg: ctx.rebase_install_dir(install_base)
                for pkg, ctx in self.contexts.items()
            },
        )


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

    def format_text(self, verbose: bool = False) -> str:
        """Formats the install or simulation results for human-readable output."""
        if not self.packages:
            return "No packages targeted."

        lines = []
        if self.dry_run:
            lines.append("🔍 [DRY-RUN] Package Install Simulation Plan")
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
            lines.append("✨ Install Summary:")
            for pkg_res in self.packages:
                status_icon = "✨" if pkg_res.status == "SUCCESS" else "❌"
                lines.append(f"  {status_icon} Package '{pkg_res.package}': {pkg_res.status}")
                if pkg_res.error:
                    lines.append(f"     Error: {pkg_res.error}")
        return "\n".join(lines)
