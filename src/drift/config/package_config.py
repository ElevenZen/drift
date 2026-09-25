"""Package-specific configuration data models and execution context.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Top-Level Package Model Container
    PackageConfig (Dataclass)
        - validate(): Strict validation of sub-sections, files, and engines
        - from_dict(): Factory constructor from parsed TOML table
        - from_source_dir(), from_render_dir(), ...: Stage-aware factory methods
        - evaluate_requirements(): Declarative & probe validation evaluator
        - load_package_envs(), package_envs(): Environment activation context managers
        - get_drift_package_facts(): Injected package context variables

Layer 1: Package Metadata & Section Specifications
    PackageSectionConfig (Dataclass)
        - [package] section options (name, target_dir, install_method, etc.)
        - from_dict(): Factory constructor and platform target resolver
===============================================================================
"""

import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    TYPE_CHECKING,
)

from ..core.constants import (
    INITIAL_ENV,
    InstallMethod,
    WINDOWS_PLATFORM_ALIASES,
)
from ..core.exceptions import ConfigError
from ..utils.env_utils import (
    EnvResolve,
    parse_env_dict,
    resolve_env_configs,
)
from ..utils.path_utils import expand_path
from ..utils.toml_utils import get_first_from, validate_known_keys
from .package_hooks import PackageHooks
from .package_requirements import PackageRequirements
from .render_engine_config import RenderEngineRegistry
from .workspace_config import WorkspaceConfig

if TYPE_CHECKING:
    from ..hooks.lifecycle_hooks import HookExecFlags

logger = logging.getLogger(__name__)


@dataclass
class PackageSectionConfig:
    """Represents package-level metadata and behaviors configured inside the [package] section of drift_package.toml."""
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        "name",
        "source_directory",
        "enable_render",
        "enable_install",
        "install_method",
        "target_directory",
        "sudo",
        "fully_controlled_dirs",
        "requirements",
        "hook_file",
        *(f"target_directory_{alias}" for alias in WINDOWS_PLATFORM_ALIASES),
    )

    name: str
    source_directory: Path = Path(".")
    enable_render: bool = True
    enable_install: bool = True
    install_method: Optional[InstallMethod] = None
    target_directory: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    hook_file: Optional[Path] = None

    def __init__(
        self,
        name: str,
        source_directory: Optional[Union[str, Path]] = None,
        enable_render: bool = True,
        enable_install: bool = True,
        install_method: Optional[InstallMethod] = None,
        target_directory: Optional[Union[str, Path]] = None,
        sudo: bool = False,
        fully_controlled_dirs: Iterable[Path] = (),
        hook_file: Optional[Union[Path, str]] = None,
    ) -> None:
        if not isinstance(name, str):
            raise ConfigError(f"Package name must be a string, got {type(name).__name__}")
        if source_directory is not None and not isinstance(source_directory, (str, Path)):
            raise ConfigError(f"source_directory must be a Path or str, got {type(source_directory).__name__}")
        if target_directory is not None and not isinstance(target_directory, (str, Path)):
            raise ConfigError(f"target_directory must be a Path or str, got {type(target_directory).__name__}")
        if install_method is not None and not isinstance(install_method, InstallMethod):
            raise ConfigError(f"install_method must be an InstallMethod instance, got {type(install_method).__name__}")
        if not isinstance(enable_render, bool):
            raise ConfigError(f"enable_render must be a boolean, got {type(enable_render).__name__}")
        if not isinstance(enable_install, bool):
            raise ConfigError(f"enable_install must be a boolean, got {type(enable_install).__name__}")
        if not isinstance(sudo, bool):
            raise ConfigError(f"sudo must be a boolean, got {type(sudo).__name__}")
        if hook_file is not None and not isinstance(hook_file, (str, Path)):
            raise ConfigError(f"hook_file must be a Path or str, got {type(hook_file).__name__}")

        self.name = name
        self.source_directory = Path(source_directory) if source_directory else Path(".")
        self.enable_render = enable_render
        self.enable_install = enable_install
        self.install_method = install_method
        self.target_directory = expand_path(target_directory) if target_directory else None
        self.sudo = sudo
        self.fully_controlled_dirs = list(fully_controlled_dirs) if fully_controlled_dirs else []
        self.hook_file = Path(hook_file) if hook_file is not None else None

    def validate(self) -> None:
        """Validates [package] section configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ConfigError("Package config must have a non-empty 'name'.")
        if not isinstance(self.source_directory, Path):
            raise ConfigError(f"source_directory must be a Path for package '{self.name}'.")
        if self.source_directory.is_absolute():
            raise ConfigError(f"Package '{self.name}' source_directory '{self.source_directory}' must be a relative path.")
        norm_src = os.path.normpath(str(self.source_directory))
        if norm_src == ".." or norm_src.startswith(".." + os.sep) or norm_src.startswith("../"):
            raise ConfigError(f"Package '{self.name}' source_directory '{self.source_directory}' escapes package root.")
        if self.install_method is not None and not isinstance(self.install_method, InstallMethod):
            raise ConfigError(
                f"Invalid install_method '{self.install_method}' for package '{self.name}'. "
                "Must be an InstallMethod instance."
            )
        if not isinstance(self.enable_render, bool):
            raise ConfigError(f"enable_render must be a boolean for package '{self.name}'.")
        if not isinstance(self.enable_install, bool):
            raise ConfigError(f"enable_install must be a boolean for package '{self.name}'.")
        if not isinstance(self.sudo, bool):
            raise ConfigError(f"sudo must be a boolean for package '{self.name}'.")
        if not isinstance(self.fully_controlled_dirs, list):
            raise ConfigError(f"fully_controlled_dirs must be a list for package '{self.name}'.")
        for d in self.fully_controlled_dirs:
            if not isinstance(d, Path):
                raise ConfigError(f"fully_controlled_dirs entries must be Path objects for package '{self.name}'.")
        if self.hook_file is not None:
            if not isinstance(self.hook_file, Path):
                raise ConfigError(f"hook_file must be a Path for package '{self.name}'.")
            if not self.hook_file.is_absolute():
                raise ConfigError(f"hook_file must be an absolute Path for package '{self.name}'.")

    @classmethod
    def from_dict(
        cls,
        data: Any,
        package_name: str,
        base_dir: Optional[Path] = None,
    ) -> "PackageSectionConfig":
        """Builds a PackageSectionConfig instance from a parsed TOML [package] dictionary."""
        if not isinstance(data, dict):
            raise ConfigError(f"[package] must be a TOML table for package '{package_name}'.")

        name_str = f" for package '{package_name}'" if package_name else ""
        validate_known_keys(
            data,
            cls.KNOWN_KEYS,
            message_prefix="Unknown package option",
            suffix=name_str,
        )

        name = str(data.get("name") or package_name)

        fcd = data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd_list = [Path(fcd)]
        elif isinstance(fcd, list):
            fcd_list = [Path(d) for d in fcd]
        else:
            raise ConfigError(f"fully_controlled_dirs must be a list of strings for package '{name}'.")

        src_dir_val = data.get("source_directory")
        if src_dir_val is not None:
            if not isinstance(src_dir_val, (str, Path)):
                raise ConfigError(f"source_directory must be a string for package '{name}'.")
            raw_str = str(src_dir_val).strip()
            source_dir = Path(raw_str) if len(raw_str) > 0 else Path(".")
        else:
            source_dir = Path(".")

        target_dir_keys = (
            *((f"target_directory_{alias}" for alias in WINDOWS_PLATFORM_ALIASES) if sys.platform == "win32" else ()),
            "target_directory",
        )
        target_dir: Optional[Path] = (val := get_first_from(data, target_dir_keys)) and expand_path(val)

        raw_hook_file = data.get("hook_file")
        if raw_hook_file is not None:
            resolved_hook_file = Path(raw_hook_file)
            if not resolved_hook_file.is_absolute() and base_dir is not None:
                resolved_hook_file = (base_dir / resolved_hook_file).resolve()
        else:
            resolved_hook_file = None

        raw_install_method = data.get("install_method")
        parsed_install_method: Optional[InstallMethod] = None
        if raw_install_method is not None:
            try:
                parsed_install_method = InstallMethod.from_str(raw_install_method)
            except ValueError as e:
                raise ConfigError(f"Invalid install_method '{raw_install_method}' for package '{name}'. Must be 'stow' or 'copy'.") from e

        sec = cls(
            name=name,
            source_directory=source_dir,
            enable_render=bool(data.get("enable_render", True)),
            enable_install=bool(data.get("enable_install", True)),
            install_method=parsed_install_method,
            target_directory=target_dir,
            sudo=bool(data.get("sudo", False)),
            fully_controlled_dirs=fcd_list,
            hook_file=resolved_hook_file,
        )
        sec.validate()
        return sec


@dataclass
class PackageConfig:
    """Represents the complete package configuration container inside src/<pkg>/drift_package.toml."""
    KNOWN_TOP_SECTIONS: ClassVar[Tuple[str, ...]] = (
        "package",
        "hooks",
        "env",
        "requirements",
        "render",
    )
    KNOWN_PACKAGE_KEYS: ClassVar[Tuple[str, ...]] = PackageSectionConfig.KNOWN_KEYS

    package: PackageSectionConfig
    source_files: List[Path] = field(default_factory=list)
    hooks: PackageHooks = field(default_factory=PackageHooks)
    requirements: PackageRequirements = field(default_factory=PackageRequirements)
    env_resolve: EnvResolve = field(default_factory=EnvResolve)
    render_engine_configs: RenderEngineRegistry = field(default_factory=RenderEngineRegistry)

    @property
    def name(self) -> str:
        """Canonical name of the package."""
        return self.package.name

    def assert_hooks_exist(
        self,
        base_dir: Path,
        is_source: bool,
        hook_names: Sequence[str] = ()
    ) -> None:
        """Validates that configured lifecycle hook files exist in base_dir and are regular files."""
        self.hooks.assert_hooks_exist(base_dir, is_source=is_source, hook_names=hook_names)

    def __init__(
        self,
        package: PackageSectionConfig,
        source_files: Iterable[Path] = (),
        hooks: Optional[PackageHooks] = None,
        requirements: Optional[PackageRequirements] = None,
        env_resolve: Optional[EnvResolve] = None,
        render_engine_configs: Optional[RenderEngineRegistry] = None,
    ) -> None:
        if not isinstance(package, PackageSectionConfig):
            raise ConfigError(f"package must be a PackageSectionConfig instance, got {type(package).__name__}")
        if hooks is not None and not isinstance(hooks, PackageHooks):
            raise ConfigError(f"hooks must be a PackageHooks instance, got {type(hooks).__name__}")
        if requirements is not None and not isinstance(requirements, PackageRequirements):
            raise ConfigError(f"requirements must be a PackageRequirements instance, got {type(requirements).__name__}")
        if env_resolve is not None and not isinstance(env_resolve, EnvResolve):
            raise ConfigError(f"env_resolve must be an EnvResolve instance, got {type(env_resolve).__name__}")
        if render_engine_configs is not None and not isinstance(render_engine_configs, RenderEngineRegistry):
            raise ConfigError(f"render_engine_configs must be a RenderEngineRegistry instance, got {type(render_engine_configs).__name__}")

        self.package = package
        self.source_files = list(source_files) if source_files else []
        self.hooks = hooks if hooks is not None else PackageHooks()
        self.hooks.package_config = self
        self.requirements = requirements if requirements is not None else PackageRequirements()
        self.env_resolve = env_resolve if env_resolve is not None else EnvResolve()
        self.render_engine_configs = render_engine_configs if render_engine_configs is not None else RenderEngineRegistry()

    def validate(self) -> None:
        """Validates configuration values."""
        if not isinstance(self.package, PackageSectionConfig):
            raise ConfigError("package must be a PackageSectionConfig instance.")
        self.package.validate()
        if not isinstance(self.source_files, list):
            raise ConfigError(f"source_files must be a list for package '{self.name}'.")
        for file in self.source_files:
            if not isinstance(file, Path):
                raise ConfigError(f"source_files entries must be Path objects for package '{self.name}'.")
        if not isinstance(self.hooks, PackageHooks):
            raise ConfigError(f"hooks must be a PackageHooks instance for package '{self.name}'.")
        self.hooks.validate(self.name)
        if not isinstance(self.requirements, PackageRequirements):
            raise ConfigError(f"requirements must be a PackageRequirements instance for package '{self.name}'.")
        if not isinstance(self.env_resolve, EnvResolve):
            raise ConfigError(f"env_resolve must be an EnvResolve instance for package '{self.name}'.")
        if not isinstance(self.render_engine_configs, RenderEngineRegistry):
            raise ConfigError(f"render_engine_configs must be a RenderEngineRegistry for package '{self.name}'.")

    def get_drift_package_facts(self, workspace_config: Optional[WorkspaceConfig]) -> Dict[str, str]:
        ws_pkg_facts = (workspace_config.get_drift_package_facts(self.name)
                        if workspace_config is not None else {})
        pkg_facts = { k: str(v) for k, v in {
            'drift_package_name': self.name,
            'drift_package_install_method': self.package.install_method,
            'drift_package_target_dir': self.package.target_directory,
        }.items() if v is not None }
        return { **ws_pkg_facts, **pkg_facts }

    def evaluate_requirements(
        self,
        workspace_config: WorkspaceConfig,
        flags: Optional["HookExecFlags"] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluates declarative requirements and dynamic probe hooks for this package.

        Returns:
            Tuple of (is_satisfied: bool, failure_reason: Optional[str]).
        """
        # 1. Declarative requirements check
        is_satisfied, reason = self.requirements.check_requirements()
        if not is_satisfied:
            return False, reason

        from ..hooks.lifecycle_hooks import HookExecFlags
        exec_flags = HookExecFlags.resolve(flags)

        # 2. Dynamic probe hook check (if configured and hooks enabled)
        if self.hooks.probe is not None and not exec_flags.no_hooks:
            res = self.hooks.trigger_probe(
                workspace_config=workspace_config,
                flags=exec_flags,
            )
            if res.status != "SUCCESS" or res.exit_code != 0:
                err_detail = (res.stderr or "").strip() or (res.stdout or "").strip() or f"exit code {res.exit_code}"
                return False, f"Probe hook failed ({err_detail})"

        return True, None

    def is_package_config_file(self, file_path: Path) -> bool:
        """Checks if the given file path is a package config file, its local override, or their template versions."""
        return file_path in self.source_files

    def get_source_directory_to_render(self, package_dir: Path) -> Path:
        """
        Returns the path of the subfolder to render within package_dir.
        The result is always a subdirectory of package_dir, and cannot escape it.

        Note: We avoid Path.resolve() here to prevent macOS APFS firmlink mutation
        (e.g., /home -> /System/Volumes/Data/home).
        """
        if not self.package.source_directory or self.package.source_directory == Path(".") or str(self.package.source_directory) in (".", ""):
            return package_dir
        return package_dir / self.package.source_directory

    def get_target_directory(self, workspace_config: WorkspaceConfig) -> Path:
        return self.package.target_directory or workspace_config.default_target_path

    def get_install_method(self, workspace_config: WorkspaceConfig) -> InstallMethod:
        if sys.platform == "win32":
            return InstallMethod.COPY
        return self.package.install_method or workspace_config.workspace.default_install_method

    def get_render_engines(self, workspace_config: WorkspaceConfig) -> RenderEngineRegistry:
        """Computes effective render engines by overlaying package engines onto workspace engines."""
        return workspace_config.render_engine_configs.overlay(self.render_engine_configs)

    def package_render_engines(self, workspace_config: WorkspaceConfig) -> RenderEngineRegistry:
        """Alias for get_render_engines."""
        return self.get_render_engines(workspace_config)

    def load_package_envs(
        self,
        overwrite: bool = True
    ) -> Dict[str, Optional[str]]:
        """Loads pre-resolved effective package environment into os.environ.

        Returns:
            A snapshot dictionary mapping modified keys to their original values.
        """
        from ..utils.env_utils import update_env_dict
        _, saved_eff = update_env_dict(os.environ, self.env_resolve.effective_dict, overwrite=overwrite, env_keep=INITIAL_ENV)
        return saved_eff

    def unload_package_envs(
        self,
        original_envs: Mapping[str, Optional[str]] = {}
    ) -> None:
        """Restores original environment variables using the snapshot returned by load_package_envs."""
        from ..utils.env_utils import unload_env_settings
        unload_env_settings(original_envs)

    @contextmanager
    def package_envs(
        self,
        overwrite: bool = True
    ) -> Iterator[None]:
        """Context manager to activate package-specific environment variables and secrets in os.environ."""
        saved_envs = self.load_package_envs(overwrite=overwrite)
        try:
            yield
        finally:
            self.unload_package_envs(saved_envs)

    @classmethod
    def from_dict(
        cls,
        data: dict,
        package_name: str,
        base_dir: Path,
        source_files: Sequence[Optional[Path]] = (),
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Builds a strongly-typed PackageConfig instance from a parsed TOML dictionary.

        Relationship and Meaning of base_dir vs. workspace_config:
            - workspace_config (Global Workspace Context): Supplies workspace-wide configuration,
              multi-stage directory layouts (source_path, render_path, install_path), workspace defaults
              (default_target_path, default_install_method), and workspace render engine registries.
              workspace_config is required for lifecycle hooks to bind and execute properly across stage sandboxes
              (probe, pre_source, post_render in render/, pre_install, post_install, pre_update, post_update,
              pre_uninstall, post_uninstall, health in install/).
            - base_dir (Local Package Directory): Required concrete directory of the package (e.g., <source_path>/<package_name>
              or <render_path>/<package_name>). It is forwarded to sub-parsers such as RenderEngineRegistry.from_dict
              to resolve relative template input_file paths, and serves as the local package directory for hook_file resolution
              and fallback hook resolution when workspace_config is absent.
            - Precedence: When workspace_config is provided (with package_name), stage-specific directories
              and the package source directory (<workspace.source_path>/<package_name>) are derived automatically
              and take precedence for multi-stage hook binding and hook_file resolution. base_dir is always required
              as the local package directory context.

        Args:
            data: Parsed configuration dictionary from drift_package.toml.
            package_name: Unique name of the package.
            base_dir: Required local directory of the package for relative asset/engine resolution.
            source_files: Candidate or loaded source configuration files.
            workspace_config: Optional WorkspaceConfig providing workspace layout, defaults, stage paths,
                and lower-layer workspace environment tables for computing effective package environments and facts.

        Returns:
            A validated and initialized PackageConfig instance.
        """
        if not package_name or not isinstance(package_name, str):
            raise ConfigError("Package name must be provided when constructing PackageConfig.")
        if not isinstance(data, dict):
            raise ConfigError(f"Package configuration data must be a dictionary for package '{package_name}'.")
        if base_dir is None or not isinstance(base_dir, (str, Path)):
            raise ConfigError(f"base_dir must be provided as a Path or string when constructing PackageConfig for package '{package_name}'.")

        base_dir_path = Path(base_dir).resolve()

        # Error for unknown top-level sections
        name_str = f" for package '{package_name}'" if package_name else ""
        validate_known_keys(
            data,
            cls.KNOWN_TOP_SECTIONS,
            message_prefix="Unknown top-level package config section",
            suffix=name_str,
        )

        package_data = data.get("package", {})
        hooks_data = data.get("hooks", {})
        env_data = data.get("env", {})
        render_data = data.get("render", {})

        name = str(package_name)

        # Resolve common package base directory for hooks and render engines
        # workspace_config takes priority over base_dir, matching PackageHooks.from_dict() precedence
        if workspace_config is not None and name:
            pkg_base = (workspace_config.source_path / name).resolve()
        else:
            pkg_base = base_dir_path

        # Parse [package] section
        package_section = PackageSectionConfig.from_dict(
            package_data,
            package_name=name,
            base_dir=pkg_base,
        )

        # Parse, validate, and resolve lifecycle hooks via PackageHooks.from_dict
        hooks = PackageHooks.from_dict(
            hooks_data,
            package_name=name,
            base_dir=pkg_base,
            workspace_config=workspace_config,
        )

        # Parse declarative requirements ([package.requirements] or top-level [requirements])
        req_data = package_data.get("requirements") or data.get("requirements") or {}
        requirements = PackageRequirements.from_dict(req_data, package_name=name)

        # Parse render engines configurations under [render.*]
        render_engine_configs = RenderEngineRegistry.from_dict(
            render_data,
            base_dir=pkg_base
        )

        config = cls(
            package=package_section,
            source_files=[x for x in source_files if isinstance(x, Path)], # filter out None items.
            hooks=hooks,
            requirements=requirements,
            render_engine_configs=render_engine_configs,
        )
        parsed_env = parse_env_dict(env_data, context_desc=f"package '{name}'")
        config.env_resolve = EnvResolve(current=parsed_env)
        config.compute_effective_envs(workspace_config)
        config.validate()
        return config

    def compute_effective_envs(self, workspace_config: Optional[WorkspaceConfig] = None) -> 'PackageConfig':
        self.env_resolve = resolve_env_configs(
            current_layer=self.env_resolve.current,
            lower_layer=(workspace_config and workspace_config.env_resolve.effective),
            extra_facts=self.get_drift_package_facts(workspace_config),
        )
        return self

    @classmethod
    def from_source_dir(
        cls,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Loads and resolves package configuration from a package source directory."""
        from .package_loader import load_package_config_from_source_dir
        return load_package_config_from_source_dir(
            package_dir=package_dir,
            workspace_config=workspace_config,
        )

    @classmethod
    def from_render_dir(
        cls,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Loads package configuration strictly from the render/ sandbox package directory.
        The name of package_dir is treated as the package name.
        """
        from .package_loader import load_package_config_from_render_dir
        return load_package_config_from_render_dir(
            package_dir=package_dir,
            workspace_config=workspace_config,
        )

    @classmethod
    def from_install_dir(
        cls,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Loads package configuration strictly from the install/ state database package directory.
        The name of package_dir is treated as the package name.
        """
        from .package_loader import load_package_config_for_install
        return load_package_config_for_install(
            package_dir=package_dir,
            workspace_config=workspace_config,
        )

    @classmethod
    def from_rendered_file(
        cls,
        package_toml_path: Path,
        package_name: str,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Loads package configuration directly from a rendered drift_package.toml file."""
        from .package_loader import load_package_config_rendered
        return load_package_config_rendered(
            package_toml_path=package_toml_path,
            package_name=package_name,
            package_dir=package_dir,
            workspace_config=workspace_config,
        )
