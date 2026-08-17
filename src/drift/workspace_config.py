"""Workspace and global configuration definitions using pathlib."""

import os
import re
import tempfile
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .constants import in_test_mode
from .toml_parser import parse_toml

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
class WorkspaceConfig:
    """Represents the global workspace configurations inside config/drift.toml."""
    drift_root_path: Path = Path(".")
    source_directory: Path = Path("src")
    render_directory: Path = Path("render")
    install_directory: Path = Path("install")
    backup_directory: Path = Path("backup")
    default_target_directory: Path = Path("~")
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
        self.default_target_directory = Path(self.default_target_directory)

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

        packages = []
        for entry in custom_dir.iterdir():
            if entry.is_dir() and entry.name != '.git':
                packages.append(entry.name)
        return sorted(packages)

    def get_package_names_from_source_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the source directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.source_path)

    def get_package_names_from_render_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the render directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.render_path)

    def get_package_names_from_install_dir(self) -> List[str]:
        """Finds all potential package subdirectory names within the install directory."""
        return WorkspaceConfig.get_package_names_from_dir(self.install_path)

    def is_package_enabled(self, package_name: str) -> bool:
        """Checks if a package is enabled based on WorkspaceConfig packages list or packages_enable_default."""
        if package_name in self.packages_enable:
            return self.packages_enable[package_name]
        return self.packages_enable_default

    @classmethod
    def from_dict(cls, data: dict, drift_root_path: Path = Path(".")) -> "WorkspaceConfig":
        """Builds a WorkspaceConfig instance from a parsed TOML dictionary."""
        if "workspace" not in data:
            raise ValueError("Missing '[workspace]' section in workspace configuration.")
            
        workspace_data = data.get("workspace", {})
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
        for name, config_dict in render_data.items():
            if isinstance(config_dict, dict):
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
    p_envst = Path(envst_path)
    content = p_envst.read_text(encoding="utf-8")
    
    # Perform envsubst rendering
    rendered_content = render_envsubst_string(content)
    
    # Create temporary file
    fd, temp_path = tempfile.mkstemp(suffix=".toml", prefix="drift_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(rendered_content)
    return temp_path


def load_workspace_config(file_path: Path) -> WorkspaceConfig:
    """Loads and parses the workspace configuration from drift.toml."""
    p_file = Path(file_path)
    actual_path = p_file
    if not p_file.exists():
        # Look for .envst.toml alternative
        envst_path = p_file.with_name(p_file.stem + ".envst" + p_file.suffix)
        if envst_path.exists():
            actual_path = Path(render_workspace_config_toml(envst_path))
        else:
            raise FileNotFoundError(
                f"Workspace configuration file not found: {file_path} or {envst_path}"
            )
            
    logger.info(f"Workspace config is loaded from: {actual_path}")
    content = actual_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    
    # Compute drift_root_path (parent of 'config' directory containing drift.toml)
    drift_root_path = p_file.resolve().parent.parent
    return WorkspaceConfig.from_dict(data, drift_root_path=drift_root_path)
