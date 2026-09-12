"""Workspace and global configuration definitions using pathlib."""

import os
import re
import tempfile
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterator, Union, Any, Sequence, Mapping

from .constants import (
        add_envst_path,
        CONFIG_DIR_NAME,
        WORKSPACE_CONFIG_FILE_NAME,
        WORKSPACE_CONFIG_LOCAL_FILE_NAME,
        LEGACY_WORKSPACE_CONFIG_FILE_NAMES,
        PACKAGE_CONFIG_FILE_NAME,
        SECRETS_ENV_FILE_NAME,
        FORBIDDEN_PACKAGE_NAMES,
        INITIAL_ENV,
        SYSTEM_FACT_KEYS,
        inject_system_facts,
        INTERNAL_RENDER_COMMAND,
)
from .toml_utils import parse_toml, merge_toml
from .exceptions import ConfigError
from .file_utils import expand_user_and_env
from .env_utils import (
    parse_env_text,
    parse_env_file,
    parse_secrets_env,
    load_env_settings,
    unload_env_settings,
    env_scope,
    secrets_env_scope,
    resolve_env_references,
    interpolate_config_dict,
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
    probe_wan_ip: bool = False

    def validate(self) -> None:
        """Validates settings types."""
        if not isinstance(self.probe_wan_ip, bool):
            raise ConfigError(f"probe_wan_ip under [settings] must be a boolean, got {type(self.probe_wan_ip).__name__}.")

    @classmethod
    def from_dict(cls, data: Any) -> "SettingsConfig":
        """Builds a SettingsConfig instance from a parsed TOML dictionary."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("[settings] must be a TOML table.")
        known_keys = {"probe_wan_ip", "probe_network_ip", "probe_internet_ip"}
        for k in data:
            if k not in known_keys:
                raise ConfigError(f"Unknown option under [settings]: '{k}'")

        raw_val = data.get("probe_wan_ip")
        if raw_val is None:
            raw_val = data.get("probe_network_ip")
        if raw_val is None:
            raw_val = data.get("probe_internet_ip", False)

        if not isinstance(raw_val, bool):
            raise ConfigError("probe_wan_ip under [settings] must be a boolean.")

        settings = cls(probe_wan_ip=bool(raw_val))
        settings.validate()
        return settings




@dataclass
class WorkspaceSectionConfig:
    """Represents options defined under the [workspace] section in drift_workspace.toml."""
    source_directory: Path = Path("src")
    render_directory: Path = Path("render")
    install_directory: Path = Path("install")
    backup_directory: Path = Path("backup")
    default_target_directory: Path = Path("~")
    default_install_method: str = "stow"
    hook_file: Optional[Path] = None

    def __init__(
        self,
        source_directory: Union[Path, str] = Path("src"),
        render_directory: Union[Path, str] = Path("render"),
        install_directory: Union[Path, str] = Path("install"),
        backup_directory: Union[Path, str] = Path("backup"),
        default_target_directory: Union[Path, str] = Path("~"),
        default_install_method: str = "stow",
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
        if not isinstance(default_install_method, str):
            raise ConfigError(f"default_install_method must be a string, got {type(default_install_method).__name__}")
        if hook_file is not None and not isinstance(hook_file, (str, Path)):
            raise ConfigError(f"hook_file must be a Path or str, got {type(hook_file).__name__}")
        self.source_directory = Path(source_directory)
        self.render_directory = Path(render_directory)
        self.install_directory = Path(install_directory)
        self.backup_directory = Path(backup_directory)
        self.default_target_directory = expand_user_and_env(Path(default_target_directory))
        self.default_install_method = str(default_install_method)
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
        if self.default_install_method not in ("stow", "copy"):
            raise ConfigError(f"default_install_method must be 'stow' or 'copy', got '{self.default_install_method}'")

    @classmethod
    def from_dict(cls, data: Any) -> "WorkspaceSectionConfig":
        """Builds a WorkspaceSectionConfig instance from a parsed TOML dictionary."""
        if not isinstance(data, dict):
            raise ConfigError("[workspace] must be a TOML table.")
        known_workspace_keys = {
            "source_directory",
            "render_directory",
            "install_directory",
            "backup_directory",
            "default_target_directory",
            "default_install_method",
            "hook_file",
        }
        for key in data:
            if key not in known_workspace_keys:
                raise ConfigError(f"Unknown workspace option: '{key}'")

        return cls(
            source_directory=data.get("source_directory", "src"),
            render_directory=data.get("render_directory", "render"),
            install_directory=data.get("install_directory", "install"),
            backup_directory=data.get("backup_directory", "backup"),
            default_target_directory=data.get("default_target_directory", "~"),
            default_install_method=data.get("default_install_method", "stow"),
            hook_file=data.get("hook_file"),
        )


@dataclass
class WorkspaceConfig:
    """Represents the global workspace configurations inside config/drift_workspace.toml."""
    drift_root: Path
    workspace: WorkspaceSectionConfig = field(default_factory=WorkspaceSectionConfig)
    packages_enable: Dict[str, bool] = field(default_factory=dict)
    packages_enable_default: bool = False
    render_engine_configs: RenderEngineRegistry = field(default_factory=RenderEngineRegistry)
    env: Dict[str, str] = field(default_factory=dict)
    settings: SettingsConfig = field(default_factory=SettingsConfig)

    def __init__(
        self,
        drift_root: Union[Path, str],
        workspace: Optional[WorkspaceSectionConfig] = None,
        packages_enable: Mapping[str, bool] = {},
        packages_enable_default: bool = False,
        render_engine_configs: Optional[RenderEngineRegistry] = None,
        env: Mapping[str, str] = {},
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
        if not isinstance(env, (dict, Mapping)):
            raise ConfigError("env must be a dictionary.")

        self.drift_root = Path(drift_root)
        self.workspace = workspace if workspace is not None else WorkspaceSectionConfig()
        self.packages_enable = dict(packages_enable)
        self.packages_enable_default = packages_enable_default
        self.render_engine_configs = render_engine_configs if render_engine_configs is not None else RenderEngineRegistry()
        self.env = dict(env)
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
        if not isinstance(self.env, dict):
            raise ConfigError("env must be a dictionary.")
        if not isinstance(self.settings, SettingsConfig):
            raise ConfigError("settings must be a SettingsConfig instance.")
        self.settings.validate()

    @property
    def drift_root_path(self) -> Path:
        """Alias property for drift_root to support backward compatibility."""
        return self.drift_root

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

    @property
    def packages(self) -> Dict[str, bool]:
        """Alias property for packages_enable to support backward compatibility."""
        return self.packages_enable

    @property
    def render_engine_config(self) -> RenderEngineRegistry:
        """Alias property for render_engine_configs to support backward compatibility."""
        return self.render_engine_configs

    @render_engine_config.setter
    def render_engine_config(self, value: RenderEngineRegistry) -> None:
        self.render_engine_configs = value

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
        forbidden_set = set(FORBIDDEN_PACKAGE_NAMES)
        forbidden_found = subdirs & forbidden_set
        if forbidden_found:
            for name in sorted(forbidden_found):
                logger.warning(f"⚠️  Ignoring directory with reserved package name '{name}' in '{custom_dir.name}/'.")

        valid_packages = subdirs - forbidden_set
        return sorted(list(valid_packages))

    @classmethod
    def get_package_names_with_config_file_from_dir(cls, custom_dir: Path) -> List[str]:
        packages = [pkg for pkg in cls.get_package_names_from_dir(custom_dir)
                    if (custom_dir / pkg / PACKAGE_CONFIG_FILE_NAME).exists()]
        return sorted(packages)

    def make_new_template_name(self, old_template_name: str, new_rendered_name: str) -> str:
        """Calculates the new template filename based on the old template's engine suffix and the new target filename."""
        return self.render_engine_configs.make_new_template_name(old_template_name, new_rendered_name)

    def get_package_names_from_source_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the source directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.source_path)

    def is_package_enabled(self, package_name: str) -> bool:
        """Checks if a package is enabled based on WorkspaceConfig packages list or packages_enable_default."""
        if package_name in self.packages_enable:
            return self.packages_enable[package_name]
        return self.packages_enable_default

    def get_source_packages(self, target_pkgs: Sequence[str] = ()) -> List[str]:
        """
        Discovers and validates packages in the source directory (src/).
        Finds all package subdirectories in src/ (regardless of whether drift_package.toml is static,
        templated, or default), and filters by target_pkgs and enablement.
        """
        candidates = self.get_package_names_from_source_dir()
        return self.get_packages(candidates, target_pkgs, custom_dir=self.source_path)

    def get_rendered_packages(self, target_pkgs: Sequence[str] = ()) -> List[str]:
        """
        Discovers and validates compiled packages in the render directory (render/).
        Packages in render/ are compiled and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.render_path)
        return self.get_packages(discovered, target_pkgs, custom_dir=self.render_path)

    def get_installed_packages(self, target_pkgs: Sequence[str] = ()) -> List[str]:
        """
        Discovers and validates staged/installed packages in the install directory (install/).
        Packages in install/ are staged and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.install_path)
        return self.get_packages(discovered, target_pkgs, custom_dir=self.install_path)

    def get_discovered_packages(self, custom_dir: Path, target_pkgs: Sequence[str] = ()) -> List[str]:
        """
        Discovers packages in the given directory that contain a literal PACKAGE_CONFIG_FILE_NAME (drift_package.toml),
        filtering by target packages if provided.
        """
        discovered = self.get_package_names_with_config_file_from_dir(custom_dir)
        return self.get_packages(discovered, target_pkgs, custom_dir)

    def get_packages(self, discovered: Sequence[str],
                     target_pkgs: Sequence[str] = (),
                     custom_dir: Optional[Path] = None) -> List[str]:
        """
        Get the list of packages to operate on based on discovered packages and target packages.
        If target_pkgs is None or empty, returns all discovered packages that are enabled in the
        workspace config.
        custom_dir is used for error messages when target packages are not found in the directory.
        """

        if not target_pkgs:
            # Fallback: redeploy all discovered packages currently inside install/ that are enabled in workspace config
            return [pkg for pkg in discovered if self.is_package_enabled(pkg)]

        remaining_packages = [x for x in target_pkgs if x not in discovered]
        if remaining_packages:
            if custom_dir:
                raise ValueError(f"Given target packages not found in directory '{custom_dir}': {remaining_packages}")
            else:
                raise ValueError(f"Given target packages not found: {remaining_packages}")

        # filter input target packages to only those that are discovered
        # otherwise raise an error for missing packages
        return [x for x in target_pkgs if x in discovered]

    def find_source_file_for_rendered_names(
        self, 
        directory: Path, 
        target_names: List[str]
    ) -> Optional[RenderSourceMatch]:
        """
        Locates a file or directory in the given directory that will render to one of the rendered names.
        Delegates to self.render_engine_configs.
        """
        return self.render_engine_configs.find_source_file_for_rendered_names(directory, target_names)

    def find_conflict_in_source_dir(
        self,
        src_pkg_dir: Path,
        rel_target_path: Path
    ) -> Optional[RenderSourceMatch]:
        """
        Finds a source file that renders to rel_target_path or a blocking path.
        Delegates to self.render_engine_configs.
        """
        return self.render_engine_configs.find_conflict_in_source_dir(src_pkg_dir, rel_target_path)

    @classmethod
    def from_dict(
        cls,
        data: dict,
        drift_root: Path,
    ) -> "WorkspaceConfig":
        """Builds a WorkspaceConfig instance from a parsed TOML dictionary."""
        root = drift_root
        if not isinstance(data, dict):
            raise ConfigError("Workspace configuration data must be a dictionary.")

        # Error for unknown top-level sections
        known_top_sections = {"workspace", "packages", "render", "env", "settings"}
        for key in data:
            if key not in known_top_sections:
                raise ConfigError(f"Unknown top-level config section: '{key}'")

        if "workspace" not in data:
            raise ConfigError("Missing '[workspace]' section in workspace configuration.")

        workspace_section = WorkspaceSectionConfig.from_dict(data.get("workspace", {}))

        if "packages" not in data or not isinstance(data.get("packages"), dict) or "enable" not in data["packages"]:
            raise ConfigError("Missing '[packages.enable]' section in workspace configuration.")

        packages_enable_data = data["packages"]["enable"]
        if not isinstance(packages_enable_data, dict):
            raise ConfigError("'[packages.enable]' must be a TOML table.")
        
        packages = {}
        for pkg, val in packages_enable_data.items():
            if pkg == "DEFAULT":
                continue
            if isinstance(val, bool):
                packages[pkg] = val
            elif str(val).lower() in ("true", "1", "yes"):
                packages[pkg] = True
            else:
                packages[pkg] = False

        packages_enable_default = bool(packages_enable_data.get("DEFAULT", False))
        if not packages_enable_default and len(packages) == 0:
            logger.warning("No packages are enabled in the workspace configuration. "
                        + "Consider enabling packages or setting 'DEFAULT = true' under [packages.enable].")

        # Parse render engines configurations under [render.*]
        render_engine_configs = RenderEngineRegistry.from_dict(
            data.get("render", {}),
            base_dir=root.resolve() / CONFIG_DIR_NAME
        )

        # Parse [env]
        env_data = data.get("env", {})
        env = {}
        if isinstance(env_data, dict):
            for k, v in env_data.items():
                env[str(k)] = str(v)

        # Parse [settings]
        settings_data = data.get("settings", {})
        settings = SettingsConfig.from_dict(settings_data)

        config = cls(
            drift_root=root.resolve(),
            workspace=workspace_section,
            packages_enable=packages,
            packages_enable_default=packages_enable_default,
            render_engine_configs=render_engine_configs,
            env=env,
            settings=settings,
        )
        config.validate()
        return config


def render_workspace_config_toml(envst_path: Path) -> str:
    """
    Renders the drift_workspace.envst.toml template using python_envsubst.
    returning the rendered output.
    """
    from .render_core import python_envsubst
    content = envst_path.read_text(encoding="utf-8")
    rendered_content = python_envsubst(content, error_cls=ConfigError)
    logger.debug(f"Rendered workspace config from template '{envst_path}':\n{rendered_content}")
    return rendered_content
    

def render_envst_load_toml(config_path: Path) -> Optional[dict]:
    """Loads and parses the TOML file at path.

    Checks the static file first, then falls back to rendering its .envst.toml counterpart.
    Propagates FileNotFoundError if neither exists.
    """
    envst_path = add_envst_path(config_path)
    if config_path.exists():
        logger.debug(f"Workspace config is being loaded from: '{config_path}'")
        content = config_path.read_text(encoding="utf-8")
    elif envst_path.exists():
        logger.debug(f"Workspace config is being rendered from template: '{envst_path}'")
        content = render_workspace_config_toml(envst_path)
    else:
        return None
    return parse_toml(content)


def check_for_legacy_workspace_config(drift_root: Path) -> None:
    """Checks for deprecated legacy workspace config files and aborts with a prominent error if found."""
    import sys
    config_dir = Path(drift_root) / CONFIG_DIR_NAME
    legacy_candidates = [
        config_dir / "drift.toml",
        config_dir / "drift.local.toml",
        config_dir / "drift.envst.toml",
        config_dir / "drift.local.envst.toml",
    ]
    legacy_found = [x for x in legacy_candidates if x.is_file()]
    if not legacy_found:
        return
    logger.error("❌ DEPRECATION ERROR: Legacy workspace configuration file detected!")
    err_box = (
        "\n" + "=" * 80 + "\n"
        "❌ DEPRECATION ERROR: Legacy workspace configuration file detected!\n\n"
        f"Found legacy file: {', '.join(str(x) for x in legacy_found)}\n\n"
        "The workspace configuration file has been renamed:\n"
        "  • 'drift.toml'             -> 'drift_workspace.toml'\n"
        "  • 'drift.local.toml'       -> 'drift_workspace.local.toml'\n"
        "  • 'drift.envst.toml'       -> 'drift_workspace.envst.toml'\n"
        "  • 'drift.local.envst.toml' -> 'drift_workspace.local.envst.toml'\n\n"
        "Backward compatibility for 'drift.toml' has been completely removed.\n"
        "Please rename your configuration file to 'drift_workspace.toml' (or appropriate suffix) to proceed.\n"
        "You can also run 'drift repair' to automatically migrate legacy configuration files.\n"
        + "=" * 80 + "\n"
    )
    print(err_box, file=sys.stderr)
    raise ConfigError(
        f"Legacy workspace configuration file [{', '.join(x.name for x in legacy_found)}] is no longer supported. "
        f"Please rename [{', '.join(x.name for x in legacy_found)}] to "
        f"[{', '.join(x.name.replace('drift', 'drift_workspace') for x in legacy_found)}] "
        f"or run 'drift repair' to automatically migrate it."
    )


def load_workspace_config_dict(config_files: Sequence[Path]) -> Dict[str, Any]:
    """Sequentially loads, renders (if templated), and deep-merges an arbitrary list of workspace configuration files.

    Accepts an arbitrary sequence of workspace config paths (e.g. base drift_workspace.toml,
    machine-local drift_workspace.local.toml, or custom override layers), rendering any .envst.toml
    templates as needed. Base missing with override existing is supported.

    Args:
        config_files: Ordered list of candidate workspace configuration file paths.

    Returns:
        Merged configuration dictionary across all loaded file layers.

    Raises:
        ConfigError: If none of the specified configuration files or their templates exist.
    """
    result: Dict[str, Any] = {}
    for idx, file in enumerate(config_files):
        f_dict = render_envst_load_toml(file)
        if not f_dict:
            continue
        logger.debug(f"Loaded workspace config {'base' if idx == 0 else 'override'} from '{file}'")
        result = merge_toml(result, f_dict)
    # Base config file missing with override files existence is accepted.
    # The whole result cannot be an empty dict.
    if not result:
        raise ConfigError(
            f"Workspace configuration file not found in [{', '.join(str(x) for x in config_files)}] "
            "or their templates."
        )
    return result


def load_workspace_config(
    drift_root: Path,
    check_legacy: bool = True,
    config_files_override: Optional[Sequence[Path]] = None,
) -> WorkspaceConfig:
    """Loads, transforms, and validates the workspace configuration.

    Configuration Pipeline Execution Order:
    1. Multi-File Discovery & Merging: Loads base and override TOML files (or .envst.toml templates)
       via load_workspace_config_dict.
    2. Dynamic Python Workspace Hook: Executes configure_workspace(context) from config/drift_workspace.py
       (or custom hook_file). The hook operates as a preprocessor on the raw dictionary with access to
       resolved context facts and environment.
    3. Variable Stitching & Topological Sort: Resolves inter-variable references in [env] using Kahn's
       topological sort algorithm with cycle detection.
    4. Cross-Section Interpolation: Interpolates ${VAR} references across all non-env sections.
    5. Schema Validation & Model Construction: Instantiates strongly-typed WorkspaceConfig.

    Args:
        drift_root: Path to the drift workspace repository root.
        check_legacy: Whether to detect and reject legacy drift.toml files.
        config_files_override: Optional custom sequence of configuration paths to load instead of defaults.

    Returns:
        Fully resolved and validated WorkspaceConfig instance.
    """
    root = Path(drift_root).resolve()
    if check_legacy:
        check_for_legacy_workspace_config(root)

    load_configs_from = list(config_files_override) if config_files_override else [
            root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME,
            root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    ]

    # Ensure system facts are present before rendering workspace config (default: no WAN probe)
    inject_system_facts(probe_wan_ip=False)

    combined_dict = load_workspace_config_dict(load_configs_from)

    # If [settings] enables probe_wan_ip, re-inject system facts with WAN probe enabled
    settings_dict = combined_dict.get("settings", {})
    if isinstance(settings_dict, dict) and (
            settings_dict.get("probe_wan_ip") or settings_dict.get("probe_network_ip")):
        inject_system_facts(probe_wan_ip=True)

    # Apply dynamic workspace hook (config/drift_workspace.py or custom hook_file)
    from .workspace_hook import apply_workspace_hook
    combined_dict = apply_workspace_hook(root, combined_dict)

    # 1. Resolve inter-variable dependencies within [env] using topological sorting
    env_dict = combined_dict.get("env", {})
    if isinstance(env_dict, dict) and env_dict:
        resolved_env = resolve_env_references(env_dict, base_env=os.environ, error_cls=ConfigError)
        combined_dict["env"] = resolved_env
        protected_keys = set(INITIAL_ENV) | set(SYSTEM_FACT_KEYS)
        load_env_settings(resolved_env, overwrite=False, env_keep=protected_keys)

    # 2. Interpolate ${VAR} across all other sections of combined_dict using resolved env + os.environ
    active_env = dict(os.environ)
    if isinstance(combined_dict.get("env"), dict):
        active_env.update(combined_dict["env"])
    combined_dict = interpolate_config_dict(
        combined_dict,
        env=active_env,
        exclude_keys={"env"},
        error_cls=ConfigError
    )

    try:
        return WorkspaceConfig.from_dict(combined_dict, drift_root=root)
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid workspace configuration in '{load_configs_from[0]}': {e}") from e


