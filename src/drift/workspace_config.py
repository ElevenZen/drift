"""Workspace and global configuration definitions using pathlib."""

import os
import re
import tempfile
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import in_test_mode, PACKAGE_CONFIG_FILE_NAME
from .toml_utils import parse_toml, merge_toml

logger = logging.getLogger(__name__)


@dataclass
class RenderEngineConfig:
    """Represents a render engine configuration inside workspace configuration."""
    name: str
    input_file: Path
    suffix: str
    render_command: str

    def __post_init__(self) -> None:
        """Coerces any string path fields to pathlib.Path objects for absolute safety."""
        self.input_file = Path(self.input_file)

    def validate(self) -> None:
        """Validates render engine configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Render engine must have a non-empty 'name'.")
        if not isinstance(self.input_file, Path) or str(self.input_file) == ".":
            raise ValueError("input_file must be a non-empty Path.")
        if not self.suffix or not isinstance(self.suffix, str):
            raise ValueError("suffix must be a non-empty string.")
        if "." in self.suffix:
            raise ValueError(f"Render engine suffix '{self.suffix}' cannot contain dots ('.').")
        if not self.render_command or not isinstance(self.render_command, str):
            raise ValueError("render_command must be a non-empty string.")

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
    render_engine_config: Dict[str, RenderEngineConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerces any string path fields to pathlib.Path objects for absolute safety."""
        self.drift_root_path = Path(self.drift_root_path)
        self.source_directory = Path(self.source_directory)
        self.render_directory = Path(self.render_directory)
        self.install_directory = Path(self.install_directory)
        self.backup_directory = Path(self.backup_directory)
        self.default_target_directory = Path(self.default_target_directory).expanduser()

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
        if not isinstance(self.render_engine_config, dict):
            raise TypeError("render_engine_config must be a dictionary.")
        for _, v in self.render_engine_config.items():
            if not isinstance(v, RenderEngineConfig):
                raise TypeError("render_engine_config values must be RenderEngineConfig instances.")
            v.validate()

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
    def render_engine_configs(self) -> Dict[str, RenderEngineConfig]:
        """Alias property for render_engine_config to support backward compatibility."""
        return self.render_engine_config

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

    def get_discovered_packages(self, custom_dir: Path, target_pkgs: Optional[List[str]]):
        """
        Discovers packages in the given directory, filtering by target packages if provided.
        Discovered packages are those that have a package config file with name PACKAGE_CONFIG_FILE_NAME. 
        If target_pkgs is None or empty, all discovered packages that are enabled in the workspace config are returned.
        Otherwise, only the target packages that are discovered are returned, regardless of whether they are enabled or not.
        Raises ValueError if any target package is not found in the directory.
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
        # Warning for unknown top-level sections
        known_top_sections = {"workspace", "packages", "render"}
        for key in data:
            if key not in known_top_sections:
                logger.warning(f"Unknown top-level config section: '{key}'")

        if "workspace" not in data:
            raise ValueError("Missing '[workspace]' section in workspace configuration.")
            
        workspace_data = data.get("workspace", {})
        # Warning for unknown workspace options
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
                logger.warning(f"Unknown workspace option: '{key}'")

        packages_data = data.get("packages", {})
        
        # Symmetrically support both flat [packages] and nested [packages.enable] schemas
        if "enable" in packages_data and isinstance(packages_data["enable"], dict):
            packages_enable_data = packages_data["enable"]
        else:
            packages_enable_data = packages_data
        
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
        if not in_test_mode() and not packages_enable_default and len(packages) == 0:
            logger.warning("No packages are enabled in the workspace configuration. "
                        + "Consider enabling packages or setting 'DEFAULT = true' under [packages].")

        # Parse render engines configurations under [render.*]
        render_data = data.get("render", {})
        render_engine_config = {}
        known_render_keys = {"input_file", "suffix", "render_command"}
        for name, config_dict in render_data.items():
            if isinstance(config_dict, dict):
                for key in config_dict:
                    if key not in known_render_keys:
                        logger.warning(f"Unknown option under render.{name}: '{key}'")
                render_engine_config[name] = RenderEngineConfig(
                    name=name,
                    input_file=Path(config_dict.get("input_file", "")),
                    suffix=str(config_dict.get("suffix", "")),
                    render_command=str(config_dict.get("render_command", ""))
                )

        # Expand home directory for default_target_directory on load
        default_target_dir = Path(workspace_data.get("default_target_directory", "~")).expanduser()

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
            render_engine_config=render_engine_config,
        )
        config.validate()
        return config


def render_envsubst_string(template_content: str) -> str:
    """Renders envsubst template content using environment variables."""
    def replace_var(match: re.Match) -> str:
        var_name = match.group(1) or match.group(2)
        return os.environ.get(var_name, "")

    pattern = r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)'
    return re.sub(pattern, replace_var, template_content)


def render_workspace_config_toml(envst_path: Path) -> str:
    """Renders the drift.envst.toml template using env variables and writes to a temporary file,

    returning the path to the temporary file.
    """
    content = envst_path.read_text(encoding="utf-8")
    
    # Perform envsubst rendering
    rendered_content = render_envsubst_string(content)
    
    # Create temporary file
    fd, temp_path = tempfile.mkstemp(suffix=".toml", prefix="drift_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(rendered_content)
    return temp_path


def render_envst_load_toml(path: Path) -> dict:
    """Loads and parses the TOML file at path.

    Checks the static file first, then falls back to rendering its .envst.toml counterpart.
    Propagates FileNotFoundError if neither exists.
    """
    envst_path = path.with_name(path.stem + ".envst" + path.suffix)
    actual_path = None

    if path.exists():
        actual_path = path
    elif envst_path.exists():
        actual_path = Path(render_workspace_config_toml(envst_path))
        logger.info(f"Workspace config is rendered from template: '{envst_path}' to temporary file: '{actual_path}'")
    else:
        raise FileNotFoundError(
            f"Workspace configuration file not found: {path} or {envst_path}"
        )

    logger.info(f"Workspace config is loaded from: '{actual_path}'")
    content = actual_path.read_text(encoding="utf-8")
    return parse_toml(content)


def load_workspace_config(file_path: Path) -> WorkspaceConfig:
    """Loads and parses the workspace configuration, merging drift.toml and drift.local.toml if present."""
    main_dict = render_envst_load_toml(file_path)

    # Search for the *.local.toml override (e.g. drift.local.toml)
    local_path = file_path.with_name(file_path.stem + ".local" + file_path.suffix)
    local_dict = {}
    local_envst_path = local_path.with_name(local_path.stem + ".envst" + local_path.suffix)
    if local_path.exists() or local_envst_path.exists():
        try:
            local_dict = render_envst_load_toml(local_path)
            logger.info(f"Loaded workspace config override from '{local_path}'")
        except Exception as e:
            logger.warning(f"Failed to load workspace local override config from '{local_path}': {e}")

    combined_dict = merge_toml(main_dict, local_dict)

    # Compute drift_root_path (parent of 'config' directory containing drift.toml)
    drift_root_path = file_path.resolve().parent.parent
    return WorkspaceConfig.from_dict(combined_dict, drift_root_path=drift_root_path)

