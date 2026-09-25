"""Workspace and global configuration data models.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Top-Level Workspace Model Container
    WorkspaceConfig (Dataclass)
        - validate(): Comprehensive workspace layout & engine validation
        - from_dict(): Factory constructor from parsed TOML table
        - from_workspace_dir(): Factory loader delegating to workspace_loader
        - Discovery & Filter: get_package_names_from_source_dir(), filter_*_packages_by_target()
        - Properties: source_path, render_path, install_path, backup_path, default_target_path

Layer 1: Workspace Specifications & Settings Models
    WorkspaceSectionConfig (Dataclass)
        - [workspace] section options (source_dir, render_dir, default_install_method, etc.)
        - from_dict(): Factory constructor & path validation
    SettingsConfig (Dataclass)
        - [settings] section options (hook_inject_non_interactive_envs)
        - from_dict(): Factory constructor
===============================================================================
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from ..core.constants import (
    CONFIG_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    FORBIDDEN_PACKAGE_NAMES,
    InstallMethod,
    PACKAGE_CONFIG_FILE_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.env_utils import (
    EnvConfig,
    EnvResolve,
    parse_env_dict,
    parse_secrets_env,
    resolve_env_configs,
)
from ..utils.path_utils import expand_path
from ..utils.toml_utils import (
    get_first_from,
    get_nested_from,
    parse_bool_value,
    validate_known_keys,
)
from .render_engine_config import (
    RenderEngineConfig,
    RenderEngineRegistry,
    RenderSourceMatch,
)

logger = logging.getLogger(__name__)


@dataclass
class SettingsConfig:
    """Workspace-level settings defined in [settings] in drift_workspace.toml."""
    HOOK_INJECT_NON_INTERACTIVE_ENVS_KEYS: ClassVar[Tuple[str, ...]] = (
        "hook_inject_non_interactive_envs",
        "hook_inject_non_interactive_env",
        "inject_hook_non_interactive_envs",
    )
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        *HOOK_INJECT_NON_INTERACTIVE_ENVS_KEYS,
    )

    hook_inject_non_interactive_envs: bool = True

    def validate(self) -> None:
        """Validates settings types."""
        if not isinstance(self.hook_inject_non_interactive_envs, bool):
            raise ConfigError(f"hook_inject_non_interactive_envs under [settings] must be a boolean, got {type(self.hook_inject_non_interactive_envs).__name__}.")

    @classmethod
    def from_dict(cls, data: Any) -> "SettingsConfig":
        """Builds a SettingsConfig instance from a parsed TOML dictionary."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("[settings] must be a TOML table.")

        validate_known_keys(data, cls.KNOWN_KEYS, context="[settings]")

        raw_hook_env = get_first_from(
            data,
            cls.HOOK_INJECT_NON_INTERACTIVE_ENVS_KEYS,
            default=True,
        )
        settings = cls(
            hook_inject_non_interactive_envs=parse_bool_value(
                raw_hook_env,
                default=True,
                strict=True,
                context="[settings] hook_inject_non_interactive_envs",
            ),
        )
        settings.validate()
        return settings


@dataclass
class WorkspaceSectionConfig:
    """Represents options defined under the [workspace] section in drift_workspace.toml."""
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        "source_directory",
        "render_directory",
        "install_directory",
        "backup_directory",
        "default_target_directory",
        "default_install_method",
        "hook_file",
    )

    source_directory: Path = Path("src")
    render_directory: Path = Path("render")
    install_directory: Path = Path("install")
    backup_directory: Path = Path("backup")
    default_target_directory: Path = expand_path(Path("~"))
    default_install_method: InstallMethod = InstallMethod.STOW
    hook_file: Optional[Path] = None

    def __init__(
        self,
        source_directory: Union[Path, str] = Path("src"),
        render_directory: Union[Path, str] = Path("render"),
        install_directory: Union[Path, str] = Path("install"),
        backup_directory: Union[Path, str] = Path("backup"),
        default_target_directory: Union[Path, str] = Path("~"),
        default_install_method: InstallMethod = InstallMethod.STOW,
        hook_file: Optional[Union[Path, str]] = None,
    ) -> None:
        if not isinstance(source_directory, (str, Path)):
            raise ConfigError(f"source_directory must be a Path or str, got {type(source_directory).__name__}")
        if not isinstance(render_directory, (str, Path)):
            raise ConfigError(f"render_directory must be a Path or str, got {type(render_directory).__name__}")
        if not isinstance(install_directory, (str, Path)):
            raise ConfigError(f"install_directory must be a Path or str, got {type(install_directory).__name__}")
        if not isinstance(backup_directory, (str, Path)):
            raise ConfigError(f"backup_directory must be a Path or str, got {type(backup_directory).__name__}")
        if not isinstance(default_target_directory, (str, Path)):
            raise ConfigError(f"default_target_directory must be a Path or str, got {type(default_target_directory).__name__}")
        if not isinstance(default_install_method, InstallMethod):
            raise ConfigError(f"default_install_method must be an InstallMethod instance, got {type(default_install_method).__name__}")
        if hook_file is not None and not isinstance(hook_file, (str, Path)):
            raise ConfigError(f"hook_file must be a Path or str, got {type(hook_file).__name__}")
        self.source_directory = Path(source_directory)
        self.render_directory = Path(render_directory)
        self.install_directory = Path(install_directory)
        self.backup_directory = Path(backup_directory)
        self.default_target_directory = expand_path(Path(default_target_directory))
        self.default_install_method = default_install_method
        self.hook_file = Path(hook_file) if hook_file is not None else None

    def validate(self) -> None:
        """Validates [workspace] section configuration values."""
        if not isinstance(self.source_directory, Path) or str(self.source_directory) == ".":
            raise ConfigError("source_directory must be a non-empty path.")
        if not isinstance(self.render_directory, Path) or str(self.render_directory) == ".":
            raise ConfigError("render_directory must be a non-empty path.")
        if not isinstance(self.install_directory, Path) or str(self.install_directory) == ".":
            raise ConfigError("install_directory must be a non-empty path.")
        if not isinstance(self.backup_directory, Path) or str(self.backup_directory) == ".":
            raise ConfigError("backup_directory must be a non-empty path.")
        if not isinstance(self.default_target_directory, Path) or str(self.default_target_directory) == ".":
            raise ConfigError("default_target_directory must be a non-empty path.")
        if not self.default_target_directory.is_absolute():
            raise ConfigError(f"default_target_directory must be an absolute path, got: '{self.default_target_directory}'")
        if not isinstance(self.default_install_method, InstallMethod):
            raise ConfigError(f"default_install_method must be 'stow' or 'copy', got '{self.default_install_method}'")

    @classmethod
    def from_dict(cls, data: Any) -> "WorkspaceSectionConfig":
        """Builds a WorkspaceSectionConfig instance from a parsed TOML dictionary."""
        if not isinstance(data, dict):
            raise ConfigError("[workspace] must be a TOML table.")
        validate_known_keys(
            data,
            cls.KNOWN_KEYS,
            message_prefix="Unknown workspace option",
        )

        raw_install_method = data.get("default_install_method", InstallMethod.STOW)
        try:
            default_install_method = InstallMethod.from_str(raw_install_method)
        except ValueError as e:
            raise ConfigError(f"default_install_method must be 'stow' or 'copy', got '{raw_install_method}'") from e

        return cls(
            source_directory=data.get("source_directory", "src"),
            render_directory=data.get("render_directory", "render"),
            install_directory=data.get("install_directory", "install"),
            backup_directory=data.get("backup_directory", "backup"),
            default_target_directory=data.get("default_target_directory", "~"),
            default_install_method=default_install_method,
            hook_file=data.get("hook_file"),
        )


@dataclass
class WorkspaceConfig:
    """Represents the global workspace configurations inside config/drift_workspace.toml."""
    PACKAGES_ENABLE_DEFAULT_KEY: ClassVar[str] = "DEFAULT"
    WORKSPACE_PACKAGES_DEFAULT_KEY: ClassVar[str] = PACKAGES_ENABLE_DEFAULT_KEY
    KNOWN_TOP_SECTIONS: ClassVar[Tuple[str, ...]] = (
        "workspace",
        "packages",
        "render",
        "env",
        "settings",
    )

    drift_root: Path
    workspace: WorkspaceSectionConfig = field(default_factory=WorkspaceSectionConfig)
    packages_enable: Dict[str, bool] = field(default_factory=dict)
    packages_enable_default: bool = False
    render_engine_configs: RenderEngineRegistry = field(default_factory=RenderEngineRegistry)
    env_resolve: EnvResolve = field(default_factory=EnvResolve)
    settings: SettingsConfig = field(default_factory=SettingsConfig)

    def __init__(
        self,
        drift_root: Union[Path, str],
        workspace: Optional[WorkspaceSectionConfig] = None,
        packages_enable: Mapping[str, bool] = {},
        packages_enable_default: bool = False,
        render_engine_configs: Optional[RenderEngineRegistry] = None,
        env_resolve: Optional[EnvResolve] = None,
        settings: Optional[SettingsConfig] = None,
    ) -> None:
        if not isinstance(drift_root, (str, Path)):
            raise ConfigError(f"drift_root must be a Path or str, got {type(drift_root).__name__}")
        if workspace is not None and not isinstance(workspace, WorkspaceSectionConfig):
            raise ConfigError(f"workspace must be a WorkspaceSectionConfig instance, got {type(workspace).__name__}")
        if settings is not None and not isinstance(settings, SettingsConfig):
            raise ConfigError(f"settings must be a SettingsConfig instance, got {type(settings).__name__}")
        if render_engine_configs is not None and not isinstance(render_engine_configs, RenderEngineRegistry):
            raise ConfigError(f"render_engine_configs must be a RenderEngineRegistry instance, got {type(render_engine_configs).__name__}")
        if not isinstance(packages_enable, (dict, Mapping)):
            raise ConfigError("packages_enable must be a dictionary.")
        if env_resolve is not None and not isinstance(env_resolve, EnvResolve):
            raise ConfigError(f"env_resolve must be an EnvResolve instance, got {type(env_resolve).__name__}")

        self.drift_root = Path(drift_root)
        self.workspace = workspace if workspace is not None else WorkspaceSectionConfig()
        self.packages_enable = dict(packages_enable)
        self.packages_enable_default = packages_enable_default
        self.render_engine_configs = render_engine_configs if render_engine_configs is not None else RenderEngineRegistry()
        self.env_resolve = env_resolve if env_resolve is not None else EnvResolve()
        self.settings = settings if settings is not None else SettingsConfig()

    def validate(self) -> None:
        """Validates workspace configuration values."""
        if not isinstance(self.drift_root, Path):
            raise ConfigError("drift_root must be a Path object.")
        if not isinstance(self.workspace, WorkspaceSectionConfig):
            raise ConfigError("workspace must be a WorkspaceSectionConfig instance.")
        self.workspace.validate()
        if not isinstance(self.packages_enable, dict):
            raise ConfigError("packages_enable must be a dictionary.")
        if not isinstance(self.packages_enable_default, bool):
            raise ConfigError("packages_enable_default must be a boolean.")
        if not isinstance(self.render_engine_configs, RenderEngineRegistry):
            raise ConfigError("render_engine_configs must be a RenderEngineRegistry instance.")
        self.render_engine_configs.validate()
        if not isinstance(self.env_resolve, EnvResolve):
            raise ConfigError("env_resolve must be an EnvResolve instance.")
        if not isinstance(self.settings, SettingsConfig):
            raise ConfigError("settings must be a SettingsConfig instance.")
        self.settings.validate()

    @property
    def source_path(self) -> Path:
        """Returns the absolute path to source directory."""
        return self.drift_root / self.workspace.source_directory

    @property
    def render_path(self) -> Path:
        """Returns the absolute path to render directory."""
        return self.drift_root / self.workspace.render_directory

    @property
    def install_path(self) -> Path:
        """Returns the absolute path to install directory."""
        return self.drift_root / self.workspace.install_directory

    @property
    def backup_path(self) -> Path:
        """Returns the absolute path to backup directory."""
        return self.drift_root / self.workspace.backup_directory

    @property
    def default_target_path(self) -> Path:
        """Returns the resolved path to default target directory."""
        return self.workspace.default_target_directory

    @classmethod
    def get_package_names_from_dir(cls, custom_dir: Path) -> List[str]:
        if not custom_dir.exists() or not custom_dir.is_dir():
            return []

        # Step 1: Find all subdirectories that are not hidden (.git, .cache, etc.)
        subdirs = {
            d.name for d in custom_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        }

        # Step 2: Check for forbidden directory names and warn
        forbidden_found = subdirs & FORBIDDEN_PACKAGE_NAMES
        if forbidden_found:
            for name in sorted(forbidden_found):
                logger.warning(f"⚠️  Ignoring directory with reserved package name '{name}' in '{custom_dir.name}/'.")

        valid_packages = subdirs - FORBIDDEN_PACKAGE_NAMES
        return sorted(list(valid_packages))

    @classmethod
    def get_package_names_with_config_file_from_dir(cls, custom_dir: Path) -> List[str]:
        packages = [
            pkg for pkg in cls.get_package_names_from_dir(custom_dir)
            if (custom_dir / pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).exists()
            or (custom_dir / pkg / PACKAGE_CONFIG_FILE_NAME).exists()
        ]
        return sorted(packages)

    def get_drift_package_facts(self, pkg_name: str) -> Dict[str, str]:
        return {
            'drift_package_name': pkg_name,
            'drift_package_source_dir': str(self.source_path / pkg_name),
            'drift_package_src_dir': str(self.source_path / pkg_name),
            'drift_package_render_dir': str(self.render_path / pkg_name),
            'drift_package_install_dir': str(self.install_path / pkg_name),
            'drift_package_install_method': str(self.workspace.default_install_method),
            'drift_package_target_dir': str(self.workspace.default_target_directory),
        }

    def get_package_names_from_source_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the source directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.source_path)

    def is_package_enabled(self, package_name: str) -> bool:
        """Checks if a package is enabled based on WorkspaceConfig packages list or packages_enable_default."""
        if package_name in self.packages_enable:
            return self.packages_enable[package_name]
        return self.packages_enable_default

    def filter_source_packages_by_target(
        self,
        target_packages: Optional[Sequence[str]] = None,
        missing_ok: bool = False,
    ) -> List[str]:
        """Discovers and validates packages in the source directory (src/).

        Finds all package subdirectories in src/ (regardless of whether drift_package.toml is static,
        templated, or default), and filters by target_packages and enablement.
        """
        candidates = self.get_package_names_from_source_dir()
        return self.filter_given_packages_by_target(
            available_packages=candidates,
            target_packages=target_packages,
            error_context_dir=self.source_path,
            missing_ok=missing_ok,
        )

    def filter_render_packages_by_target(
        self,
        target_packages: Optional[Sequence[str]] = None,
        missing_ok: bool = False,
    ) -> List[str]:
        """Discovers and validates compiled packages in the render directory (render/).

        Packages in render/ are compiled and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.render_path)
        return self.filter_given_packages_by_target(
            available_packages=discovered,
            target_packages=target_packages,
            error_context_dir=self.render_path,
            missing_ok=missing_ok,
        )

    def filter_install_packages_by_target(
        self,
        target_packages: Optional[Sequence[str]] = None,
        missing_ok: bool = False,
    ) -> List[str]:
        """Discovers and validates staged/installed packages in the install directory (install/).

        Packages in install/ are staged and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.install_path)
        return self.filter_given_packages_by_target(
            available_packages=discovered,
            target_packages=target_packages,
            error_context_dir=self.install_path,
            missing_ok=missing_ok,
        )

    def filter_custom_dir_packages_by_target(
        self,
        custom_dir: Path,
        target_packages: Optional[Sequence[str]] = None,
        missing_ok: bool = False,
    ) -> List[str]:
        """Discovers packages in the given directory containing a literal drift_package.toml,
        filtering by target packages if provided.
        """
        discovered = self.get_package_names_with_config_file_from_dir(custom_dir)
        return self.filter_given_packages_by_target(
            available_packages=discovered,
            target_packages=target_packages,
            error_context_dir=custom_dir,
            missing_ok=missing_ok,
        )

    def filter_given_packages_by_target(
        self,
        available_packages: Sequence[str],
        target_packages: Optional[Sequence[str]] = None,
        error_context_dir: Optional[Path] = None,
        missing_ok: bool = False,
    ) -> List[str]:
        """Filters available packages by target_packages and workspace enablement.

        If target_packages is None, returns all available packages enabled in the workspace config.
        If target_packages is an empty sequence (), returns an empty list [].
        If target_packages contains package names:
            - If missing_ok is False (default), validates that all target packages exist in available_packages,
              raising a ValueError if any are missing.
            - If missing_ok is True, logs an info message for any missing package and returns the matching ones.
        """
        if target_packages is None:
            return [pkg for pkg in available_packages if self.is_package_enabled(pkg)]

        if not target_packages:
            return []

        remaining_packages = [x for x in target_packages if x not in available_packages]
        if remaining_packages:
            if not missing_ok:
                if error_context_dir:
                    raise ValueError(
                        f"Given target packages not found in directory '{error_context_dir}': {remaining_packages}"
                    )
                raise ValueError(f"Given target packages not found: {remaining_packages}")
            for pkg in remaining_packages:
                if error_context_dir:
                    logger.info(f"Package '{pkg}' not found in directory '{error_context_dir}'. Skipping.")
                else:
                    logger.info(f"Package '{pkg}' not found. Skipping.")

        return [x for x in target_packages if x in available_packages]

    @classmethod
    def from_dict(
        cls,
        data: dict,
        drift_root: Path,
        env_resolve: Optional[EnvResolve] = None,
    ) -> "WorkspaceConfig":
        """Builds a WorkspaceConfig instance from a parsed TOML dictionary."""
        root = drift_root
        if not isinstance(data, dict):
            raise ConfigError("Workspace configuration data must be a dictionary.")

        # Error for unknown top-level sections
        validate_known_keys(
            data,
            cls.KNOWN_TOP_SECTIONS,
            message_prefix="Unknown top-level config section",
        )

        if "workspace" not in data:
            raise ConfigError("Missing '[workspace]' section in workspace configuration.")

        workspace_section = WorkspaceSectionConfig.from_dict(data.get("workspace", {}))

        packages_enable_data = get_nested_from(
            data,
            "packages.enable",
            required=True,
            is_table=True,
            context="workspace configuration",
        )

        packages = {
            pkg: parse_bool_value(val)
            for pkg, val in packages_enable_data.items()
            if pkg != cls.PACKAGES_ENABLE_DEFAULT_KEY
        }

        packages_enable_default = parse_bool_value(
            packages_enable_data.get(cls.PACKAGES_ENABLE_DEFAULT_KEY, False)
        )
        if not packages_enable_default and len(packages) == 0:
            logger.warning("No packages are enabled in the workspace configuration. "
                        + "Consider enabling packages or setting 'DEFAULT = true' under [packages.enable].")

        # Parse render engines configurations under [render.*]
        render_engine_configs = RenderEngineRegistry.from_dict(
            data.get("render", {}),
            base_dir=root.resolve() / CONFIG_DIR_NAME
        )

        # Parse [env]
        if env_resolve is not None:
            env_res = env_resolve
        else:
            parsed_env = parse_env_dict(data.get("env", {}), context_desc="workspace configuration")
            secrets_file = parse_secrets_env(root)
            env_res = resolve_env_configs(
                parsed_env,
                lower_layer=EnvConfig(secrets=dict(secrets_file)) if secrets_file else None,
            )

        # Parse [settings]
        settings_data = data.get("settings", {})
        settings = SettingsConfig.from_dict(settings_data)

        config = cls(
            drift_root=root.resolve(),
            workspace=workspace_section,
            packages_enable=packages,
            packages_enable_default=packages_enable_default,
            render_engine_configs=render_engine_configs,
            env_resolve=env_res,
            settings=settings,
        )
        config.validate()
        return config

    @classmethod
    def from_workspace_dir(
        cls,
        drift_root: Path,
        check_legacy: bool = True,
        config_files_override: Optional[Sequence[Path]] = None,
    ) -> "WorkspaceConfig":
        """Loads, transforms, and validates the workspace configuration from a drift workspace directory."""
        from .workspace_loader import load_workspace_config
        return load_workspace_config(
            drift_root=drift_root,
            check_legacy=check_legacy,
            config_files_override=config_files_override,
        )
