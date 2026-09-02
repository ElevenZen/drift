"""Workspace and global configuration definitions using pathlib."""

import os
import re
import tempfile
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterator, Union, Any

from .constants import (
        CONFIG_DIR_NAME,
        GLOBAL_CONFIG_FILE_NAME,
        PACKAGE_CONFIG_FILE_NAME,
        SECRETS_ENV_FILE_NAME,
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
)

logger = logging.getLogger(__name__)


@dataclass
class RenderEngineConfig:
    """Represents a render engine configuration inside workspace configuration."""
    name: str
    input_file: Path = Path("")
    suffix: str = ""
    render_command: str = ""

    def __post_init__(self) -> None:
        """Coerces any string path fields to pathlib.Path objects for absolute safety."""
        self.input_file = Path(self.input_file) if self.input_file is not None else Path("")

    @property
    def is_internal(self) -> bool:
        """Returns True if the engine uses Drift's built-in Python substitution renderer."""
        return self.render_command == INTERNAL_RENDER_COMMAND

    def validate(self) -> None:
        """Validates render engine configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Render engine must have a non-empty 'name'.")
        if not self.suffix or not isinstance(self.suffix, str):
            raise ValueError("suffix must be a non-empty string.")
        if "." in self.suffix:
            raise ValueError(f"Render engine suffix '{self.suffix}' cannot contain dots ('.').")
        if not self.render_command or not isinstance(self.render_command, str):
            raise ValueError("render_command must be a non-empty string.")
        if not self.is_internal:
            if not isinstance(self.input_file, Path) or str(self.input_file) in ("", "."):
                raise ValueError("input_file must be a non-empty Path.")

    @property
    def is_disabled(self) -> bool:
        """Returns True if the render engine is disabled due to missing or empty input file."""
        if self.is_internal:
            return False
        return not self.input_file or str(self.input_file) in ("", ".")

    def strip_suffix(self, filename: str) -> str:
        """Strips the engine suffix segment from the filename, replacing only the last occurrence."""
        suffix = self.suffix
        if filename.endswith(f".{suffix}"):
            return filename[:-len(f".{suffix}")]
        
        pattern = f".{suffix}."
        idx = filename.rfind(pattern)
        if idx != -1:
            # Replaces only the last occurrence of the pattern with "."
            return filename[:idx] + "." + filename[idx + len(pattern):]
        return filename


@dataclass
class RenderSourceMatch:
    """Encapsulates a match or blocking entry found in the source directory."""
    path: Path
    engine: Optional[RenderEngineConfig]
    target_name: str
    status: str = "match"  # "match" or "block"


@dataclass
class DriftSettings:
    """Workspace-level settings defined in [settings] in drift.toml."""
    probe_wan_ip: bool = False

    def validate(self) -> None:
        """Validates settings types."""
        if not isinstance(self.probe_wan_ip, bool):
            raise TypeError(f"probe_wan_ip under [settings] must be a boolean, got {type(self.probe_wan_ip).__name__}.")

    @classmethod
    def from_dict(cls, data: Any) -> "DriftSettings":
        """Builds a DriftSettings instance from a parsed TOML dictionary."""
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
            raise TypeError("probe_wan_ip under [settings] must be a boolean.")

        settings = cls(probe_wan_ip=bool(raw_val))
        settings.validate()
        return settings


@dataclass
class WorkspaceConfig:
    """Represents the global workspace configurations inside config/drift.toml."""
    drift_root_path: Path = Path(".")
    source_directory: Path = Path("src")
    render_directory: Path = Path("render")
    install_directory: Path = Path("install")
    backup_directory: Path = Path("backup")
    default_target_directory: Path = Path("~")
    default_install_method: str = "stow"
    packages_enable: Dict[str, bool] = field(default_factory=dict)
    packages_enable_default: bool = False
    render_engine_configs: Dict[str, RenderEngineConfig] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    settings: DriftSettings = field(default_factory=DriftSettings)

    def __init__(
        self,
        drift_root_path: Union[Path, str] = Path("."),
        source_directory: Union[Path, str] = Path("src"),
        render_directory: Union[Path, str] = Path("render"),
        install_directory: Union[Path, str] = Path("install"),
        backup_directory: Union[Path, str] = Path("backup"),
        default_target_directory: Union[Path, str] = Path("~"),
        default_install_method: str = "stow",
        packages_enable: Optional[Dict[str, bool]] = None,
        packages_enable_default: bool = False,
        render_engine_configs: Optional[Dict[str, RenderEngineConfig]] = None,
        env: Optional[Dict[str, str]] = None,
        settings: Optional[DriftSettings] = None,
        render_engine_config: Optional[Dict[str, RenderEngineConfig]] = None,
    ) -> None:
        self.drift_root_path = Path(drift_root_path)
        self.source_directory = Path(source_directory)
        self.render_directory = Path(render_directory)
        self.install_directory = Path(install_directory)
        self.backup_directory = Path(backup_directory)
        self.default_target_directory = expand_user_and_env(Path(default_target_directory))
        self.default_install_method = default_install_method
        self.packages_enable = packages_enable if packages_enable is not None else {}
        self.packages_enable_default = packages_enable_default
        if render_engine_configs is not None:
            self.render_engine_configs = render_engine_configs
        elif render_engine_config is not None:
            self.render_engine_configs = render_engine_config
        else:
            self.render_engine_configs = {}
        self.env = env if env is not None else {}
        self.settings = settings if settings is not None else DriftSettings()

    def validate(self) -> None:
        """Validates workspace configuration values."""
        if not isinstance(self.drift_root_path, Path):
            raise TypeError("drift_root_path must be a Path object.")
        if not isinstance(self.source_directory, Path) or str(self.source_directory) == ".":
            raise ValueError("source_directory must be a non-empty path.")
        if not isinstance(self.render_directory, Path) or str(self.render_directory) == ".":
            raise ValueError("render_directory must be a non-empty path.")
        if not isinstance(self.install_directory, Path) or str(self.install_directory) == ".":
            raise ValueError("install_directory must be a non-empty path.")
        if not isinstance(self.backup_directory, Path) or str(self.backup_directory) == ".":
            raise ValueError("backup_directory must be a non-empty path.")
        if not isinstance(self.default_target_directory, Path) or str(self.default_target_directory) == ".":
            raise ValueError("default_target_directory must be a non-empty path.")
        if not self.default_target_directory.is_absolute():
            raise ValueError(f"default_target_directory must be an absolute path, got: '{self.default_target_directory}'")
        if self.default_install_method not in ("stow", "copy"):
            raise ValueError(f"default_install_method must be 'stow' or 'copy', got '{self.default_install_method}'")
        if not isinstance(self.packages_enable, dict):
            raise TypeError("packages_enable must be a dictionary.")
        if not isinstance(self.packages_enable_default, bool):
            raise TypeError("packages_enable_default must be a boolean.")
        if not isinstance(self.render_engine_configs, dict):
            raise TypeError("render_engine_configs must be a dictionary.")
        for _, v in self.render_engine_configs.items():
            if not isinstance(v, RenderEngineConfig):
                raise TypeError("render_engine_configs values must be RenderEngineConfig instances.")
            v.validate()
        if not isinstance(self.env, dict):
            raise TypeError("env must be a dictionary.")
        if not isinstance(self.settings, DriftSettings):
            raise TypeError("settings must be a DriftSettings instance.")
        self.settings.validate()

    @property
    def drift_root(self) -> Path:
        """Returns the absolute path to drift workspace root."""
        return self.drift_root_path

    @property
    def source_path(self) -> Path:
        """Returns the absolute path to source directory."""
        return self.drift_root_path / self.source_directory

    @property
    def render_path(self) -> Path:
        """Returns the absolute path to render directory."""
        return self.drift_root_path / self.render_directory

    @property
    def install_path(self) -> Path:
        """Returns the absolute path to install directory."""
        return self.drift_root_path / self.install_directory

    @property
    def backup_path(self) -> Path:
        """Returns the absolute path to backup directory."""
        return self.drift_root_path / self.backup_directory

    @property
    def default_target_path(self) -> Path:
        """Returns the resolved path to default target directory."""
        return self.default_target_directory

    @property
    def packages(self) -> Dict[str, bool]:
        """Alias property for packages_enable to support backward compatibility."""
        return self.packages_enable

    @property
    def render_engine_config(self) -> Dict[str, RenderEngineConfig]:
        """Alias property for render_engine_configs to support backward compatibility."""
        return self.render_engine_configs

    @render_engine_config.setter
    def render_engine_config(self, value: Dict[str, RenderEngineConfig]) -> None:
        self.render_engine_configs = value

    @classmethod
    def get_package_names_from_dir(cls, custom_dir: Path) -> List[str]:
        if not custom_dir.exists() or not custom_dir.is_dir():
            return []

        packages = [d.name for d in custom_dir.iterdir()
                    if d.is_dir() and d.name != '.git']
        return sorted(packages)

    @classmethod
    def get_package_names_with_config_file_from_dir(cls, custom_dir: Path) -> List[str]:
        if not custom_dir.exists() or not custom_dir.is_dir():
            return []

        packages = [d.name for d in custom_dir.iterdir()
                    if d.is_dir()
                    and d.name != '.git'
                    and (d / PACKAGE_CONFIG_FILE_NAME).exists()]
        return sorted(packages)

    def make_new_template_name(self, old_template_name: str, new_rendered_name: str) -> str:
        """Calculates the new template filename based on the old template's engine suffix and the new target filename.
        
        Example: old_template_name = "dot-old.envst.sh", new_rendered_name = "dot-new.sh"
                 Returns: "dot-new.envst.sh"
                 old_template_name = "dot-old.envst", new_rendered_name = "dot-new"
                 Returns: "dot-new.envst"
        """
        old_parts = old_template_name.split(".")
        engine_suffix = None
        
        # Determine engine config suffixes dynamically from workspace configurations
        valid_suffixes = {engine.suffix for engine in self.render_engine_configs.values() if engine.suffix}
        
        # Search the old parts for any valid engine suffix
        engine_suffix = next(part for part in reversed(old_parts) if part in valid_suffixes)
        if not engine_suffix:
            return new_rendered_name  # No engine suffix found, return the new name as is

        dot_idx = new_rendered_name.rfind('.')
        if dot_idx == -1:
            return f"{new_rendered_name}.{engine_suffix}"
        else:
            return new_rendered_name[:dot_idx] + f".{engine_suffix}" + new_rendered_name[dot_idx:]

    def get_package_names_from_source_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the source directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.source_path)

    def is_package_enabled(self, package_name: str) -> bool:
        """Checks if a package is enabled based on WorkspaceConfig packages list or packages_enable_default."""
        if package_name in self.packages_enable:
            return self.packages_enable[package_name]
        return self.packages_enable_default

    def get_source_packages(self, target_pkgs: Optional[List[str]] = None) -> List[str]:
        """
        Discovers and validates packages in the source directory (src/).
        Finds all package subdirectories in src/ (regardless of whether drift_package.toml is static,
        templated, or default), and filters by target_pkgs and enablement.
        """
        candidates = self.get_package_names_from_source_dir()
        return self.get_packages(candidates, target_pkgs, custom_dir=self.source_path)

    def get_rendered_packages(self, target_pkgs: Optional[List[str]] = None) -> List[str]:
        """
        Discovers and validates compiled packages in the render directory (render/).
        Packages in render/ are compiled and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.render_path)
        return self.get_packages(discovered, target_pkgs, custom_dir=self.render_path)

    def get_installed_packages(self, target_pkgs: Optional[List[str]] = None) -> List[str]:
        """
        Discovers and validates staged/installed packages in the install directory (install/).
        Packages in install/ are staged and guaranteed to have a literal drift_package.toml.
        """
        discovered = self.get_package_names_with_config_file_from_dir(self.install_path)
        return self.get_packages(discovered, target_pkgs, custom_dir=self.install_path)

    def get_discovered_packages(self, custom_dir: Path, target_pkgs: Optional[List[str]] = None) -> List[str]:
        """
        Discovers packages in the given directory that contain a literal PACKAGE_CONFIG_FILE_NAME (drift_package.toml),
        filtering by target packages if provided.
        """
        discovered = self.get_package_names_with_config_file_from_dir(custom_dir)
        return self.get_packages(discovered, target_pkgs, custom_dir)

    def get_packages(self, discovered: List[str],
                     target_pkgs: Optional[List[str]] = None,
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
        Checks for static files/dirs first, then for templates using defined render engines.
        Returns a RenderSourceMatch or None if no match is found.

        The callers includes package_config_render, drift_new, reverse_sync.
        """
        # 1. Static check
        for name in target_names:
            p = directory / name
            if p.exists():
                return RenderSourceMatch(path=p, engine=None, target_name=name, status="match")

        # 2. Template check (using defined engines)
        # Only normal file templates are considered for rendering; directories are not rendered.
        # So directories with a template suffix won't conflict.
        for engine in self.render_engine_configs.values():
            suffix = engine.suffix
            if not suffix:
                continue
            for name in target_names:
                # Check for template form 1: name.suffix (e.g., config.envst)
                template_name_1 = f"{name}.{suffix}"
                p1 = directory / template_name_1
                if p1.is_file():
                    return RenderSourceMatch(path=p1, engine=engine, target_name=name, status="match")

                # Check for template form 2: name with suffix inserted before last dot (e.g., config.envst.toml)
                dot_idx = name.rfind('.')
                if dot_idx != -1:
                    template_name_2 = name[:dot_idx] + f".{suffix}" + name[dot_idx:]
                    p2 = directory / template_name_2
                    if p2.is_file():
                        return RenderSourceMatch(path=p2, engine=engine, target_name=name, status="match")
        return None

    def find_conflict_in_source_dir(
        self,
        src_pkg_dir: Path,
        rel_target_path: Path
    ) -> Optional[RenderSourceMatch]:
        """
        Finds a source file that renders to rel_target_path or a blocking path.
        Returns RenderSourceMatch with status="match" if it's an exact rendering match,
        or status="block" if an intermediate path segment is blocked by a file.
        """
        # We avoid circular import by importing here
        from .file_utils import translate_dot_prefixes_reverse
        
        translated_path = translate_dot_prefixes_reverse(rel_target_path)
        parts = translated_path.parts
        
        current_dir = src_pkg_dir
        for i, part in enumerate(parts):
            match = self.find_source_file_for_rendered_names(current_dir, [part])
            
            if match:
                if i == len(parts) - 1:
                    # Last segment reached: exact conflict (match).
                    match.status = "match"
                    return match
                else:
                    # Not last segment: if it's a file, it's a conflict (file blocking directory).
                    if match.path.is_file():
                        match.status = "block"
                        return match
                    # Directory found, descend for next segment.
                    current_dir = match.path
            else:
                # No match for this segment, no conflict possible for this path.
                return None
                
            if not current_dir.exists() or not current_dir.is_dir():
                return None
        return None

    @classmethod
    def from_dict(cls, data: dict, drift_root_path: Path = Path(".")) -> "WorkspaceConfig":
        """Builds a WorkspaceConfig instance from a parsed TOML dictionary."""
        # Error for unknown top-level sections
        known_top_sections = {"workspace", "packages", "render", "env", "settings"}
        for key in data:
            if key not in known_top_sections:
                raise ConfigError(f"Unknown top-level config section: '{key}'")

        if "workspace" not in data:
            raise ConfigError("Missing '[workspace]' section in workspace configuration.")
            
        workspace_data = data.get("workspace", {})
        # Error for unknown workspace options
        known_workspace_keys = {
            "source_directory",
            "render_directory",
            "install_directory",
            "backup_directory",
            "default_target_directory",
            "default_install_method"
        }
        for key in workspace_data:
            if key not in known_workspace_keys:
                raise ConfigError(f"Unknown workspace option: '{key}'")

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
        render_data = data.get("render", {})
        render_engine_configs = {}
        known_render_keys = {"input_file", "suffix", "render_command"}
        for name, config_dict in render_data.items():
            if isinstance(config_dict, dict):
                for key in config_dict:
                    if key not in known_render_keys:
                        raise ConfigError(f"Unknown option under render.{name}: '{key}'")
                render_engine_configs[name] = RenderEngineConfig(
                    name=name,
                    input_file=Path(config_dict.get("input_file", "")),
                    suffix=str(config_dict.get("suffix", "")),
                    render_command=str(config_dict.get("render_command", ""))
                )

        # Parse [env]
        env_data = data.get("env", {})
        env = {}
        if isinstance(env_data, dict):
            for k, v in env_data.items():
                env[str(k)] = str(v)

        # Parse [settings]
        settings_data = data.get("settings", {})
        settings = DriftSettings.from_dict(settings_data)

        # Expand home directory and env vars for default_target_directory on load
        default_target_dir = expand_user_and_env(workspace_data.get("default_target_directory", "~"))

        config = cls(
            drift_root_path=Path(drift_root_path).resolve(),
            source_directory=Path(workspace_data.get("source_directory", "src")),
            render_directory=Path(workspace_data.get("render_directory", "render")),
            install_directory=Path(workspace_data.get("install_directory", "install")),
            backup_directory=Path(workspace_data.get("backup_directory", "backup")),
            default_target_directory=default_target_dir,
            default_install_method=str(workspace_data.get("default_install_method", "stow")),
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
    Renders the drift.envst.toml template using python_envsubst.
    returning the rendered output.
    """
    from .render_core import python_envsubst
    content = envst_path.read_text(encoding="utf-8")
    rendered_content = python_envsubst(content, error_cls=ConfigError)
    logger.debug(f"Rendered workspace config from template '{envst_path}':\n{rendered_content}")
    return rendered_content
    

def add_envst(file: Path) -> Path:
    return file.with_name(file.stem + ".envst" + file.suffix)


def render_envst_load_toml(config_path: Path) -> Optional[dict]:
    """Loads and parses the TOML file at path.

    Checks the static file first, then falls back to rendering its .envst.toml counterpart.
    Propagates FileNotFoundError if neither exists.
    """
    envst_path = add_envst(config_path)

    if config_path.exists():
        logger.debug(f"Workspace config is loaded from: '{config_path}'")
        content = config_path.read_text(encoding="utf-8")
    elif envst_path.exists():
        content = render_workspace_config_toml(envst_path)
        logger.debug(f"Workspace config is rendered from template: '{envst_path}'")
    else:
        return None

    return parse_toml(content)


def load_workspace_config(drift_root_path: Path) -> WorkspaceConfig:
    """Loads and parses the workspace configuration, merging drift.toml and drift.local.toml if present."""
    drift_root_path = Path(drift_root_path).resolve()
    file_path = drift_root_path / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME

    # Ensure system facts are present before rendering workspace config (default: no WAN probe)
    inject_system_facts(probe_wan_ip=False)

    main_dict = render_envst_load_toml(file_path)
    if main_dict is None:
        raise ConfigError(
            f"Workspace main configuration file not found in '{file_path}' or its template '{add_envst(file_path)}'."
        )

    # Search for the *.local.toml override (e.g. drift.local.toml)
    local_path = file_path.with_name(file_path.stem + ".local" + file_path.suffix)
    local_dict = render_envst_load_toml(local_path)
    if local_dict is None:
        logger.debug(f"No local workspace config override at '{local_path}'")
        combined_dict = main_dict
    else:
        logger.debug(f"Loaded workspace config override from '{local_path}'")
        combined_dict = merge_toml(main_dict, local_dict)

    # If [settings] enables probe_wan_ip, re-inject system facts with WAN probe enabled
    settings_dict = combined_dict.get("settings", {})
    if isinstance(settings_dict, dict) and (
            settings_dict.get("probe_wan_ip") or settings_dict.get("probe_network_ip")):
        inject_system_facts(probe_wan_ip=True)

    # Load and apply [env] variables to os.environ immediately (preserving CLI envs and system facts)
    env_dict = combined_dict.get("env", {})
    if isinstance(env_dict, dict):
        protected_keys = set(INITIAL_ENV) | set(SYSTEM_FACT_KEYS)
        load_env_settings(env_dict, overwrite=False, env_keep=protected_keys)

    try:
        return WorkspaceConfig.from_dict(combined_dict, drift_root_path=drift_root_path)
    except ConfigError:
        raise
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid workspace configuration in '{file_path}': {e}") from e


