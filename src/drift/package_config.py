"""Package-specific configuration loading and metadata parsing using pathlib."""

import logging
from pathlib import Path
from typing import List, Optional
from .toml_parser import parse_toml

from .constants import PACKAGE_CONFIG_FILE_NAME, PACKAGE_CONFIG_FILE_NAME_LIST
from .workspace_config import RenderEngineConfig, WorkspaceConfig

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/package.toml."""
    name: str
    config_template_path: Optional[Path] = None
    config_rendered_path: Optional[Path] = None
    enable_render: bool = True
    enable_install: bool = True
    install_method: str = "stow"
    target_directory: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    on_install: Optional[str] = None
    on_update: Optional[str] = None
    hook_timeout: int = 120

    def __post_init__(self) -> None:
        """Handles path expansion on load/initialization."""
        if self.target_directory:
            self.target_directory = Path(self.target_directory).expanduser()

    def is_static(self) -> bool:
        return (self.config_template_path is not None
            and self.config_rendered_path is not None
            and self.config_template_path == self.config_rendered_path)

    def validate(self) -> None:
        """Validates configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Package config must have a non-empty 'name'.")
        if self.install_method not in ("stow", "copy"):
            raise ValueError(
                f"Invalid install_method '{self.install_method}' for package '{self.name}'. "
                "Must be 'stow' or 'copy'."
            )
        if not isinstance(self.enable_render, bool):
            raise TypeError(f"enable_render must be a boolean for package '{self.name}'.")
        if not isinstance(self.enable_install, bool):
            raise TypeError(f"enable_install must be a boolean for package '{self.name}'.")
        if not isinstance(self.sudo, bool):
            raise TypeError(f"sudo must be a boolean for package '{self.name}'.")
        if not isinstance(self.fully_controlled_dirs, list):
            raise TypeError(f"fully_controlled_dirs must be a list for package '{self.name}'.")
        for d in self.fully_controlled_dirs:
            if not isinstance(d, Path):
                raise TypeError(f"fully_controlled_dirs entries must be Path objects for package '{self.name}'.")
        if not isinstance(self.hook_timeout, int):
            raise TypeError(f"hook_timeout must be an integer for package '{self.name}'.")
        if self.hook_timeout <= 0:
            raise ValueError(f"hook_timeout must be a positive integer for package '{self.name}'.")

    def is_package_config_file(self, file_path: Path) -> bool:
        """Checks if the given file path is the package config file or its template."""
        is_template = (self.config_template_path is not None
                       and file_path.resolve() == self.config_template_path.resolve())
        is_rendered = (self.config_rendered_path is not None
                       and file_path.resolve() == self.config_rendered_path.resolve())
        return is_template or is_rendered

    @classmethod
    def from_dict(cls, data: dict, default_name: Optional[str] = None) -> "PackageConfig":
        """Builds a PackageConfig instance from a parsed TOML dictionary."""
        # Warning for unknown top-level sections
        known_top_sections = {"package"}
        for key in data:
            if key not in known_top_sections:
                logger.warning(f"Unknown top-level package config section: '{key}'")

        package_data = data.get("package", {})
        
        name = package_data.get("name", default_name)
        if not name:
            raise ValueError("Package configuration is missing the required 'name' field.")

        # Warning for unknown package options
        known_package_keys = {
            "name",
            "enable_render",
            "enable_install",
            "install_method",
            "target_directory",
            "sudo",
            "fully_controlled_dirs",
            "on_install",
            "on_update",
            "hook_timeout"
        }
        for key in package_data:
            if key not in known_package_keys:
                logger.warning(f"Unknown package option: '{key}'")
            
        fcd = package_data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd = [fcd]
        elif not isinstance(fcd, list):
            fcd = []

        # Expand home directory for target_directory on load
        target_dir = package_data.get("target_directory")
        if target_dir:
            target_dir = Path(target_dir).expanduser()
            
        raw_timeout = package_data.get("hook_timeout", 120)
        if isinstance(raw_timeout, str) and raw_timeout.isdigit():
            raw_timeout = int(raw_timeout)

        config = cls(
            name=str(name),
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=str(package_data.get("install_method", "stow")),
            target_directory=target_dir,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[Path(d) for d in fcd],
            on_install=package_data.get("on_install"),
            on_update=package_data.get("on_update"),
            hook_timeout=raw_timeout
        )
        config.validate()
        return config


def load_package_config_static(
    file_path: Path,
    default_name: Optional[str] = None
) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml or package.toml."""
    if not file_path.exists():
        raise FileNotFoundError(f"Package configuration file not found: {file_path}")
    content = file_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    config = PackageConfig.from_dict(data, default_name=default_name)
    config.config_template_path = file_path.resolve()
    config.config_rendered_path = file_path.resolve()
    return config


def locate_package_config_file_static(package_dir: Path) -> Optional[Path]:
    """Finds the drift_package.toml or package.toml in a given package directory."""
    for filename in PACKAGE_CONFIG_FILE_NAME_LIST:
        path = package_dir / filename
        if path.is_file():
            return path
    return None


@dataclass
class PackageConfigFileInfo:
    """Represents file info for a found package config file (or template).

    This class is only used in this file, and is not part of the public API.
    Public API users should use the PackageConfig class instead.
    """
    type: str  # 'static' or 'template'
    path: Path  # path to the file/template
    engine: Optional[RenderEngineConfig] = None  # RenderEngineConfig instance (if 'template', otherwise None)
    target_name: str = "package.toml"  # 'drift_package.toml' or 'package.toml'


def get_package_config_file_info(
    package_dir: Path,
    workspace_config: "WorkspaceConfig"
) -> Optional[PackageConfigFileInfo]:
    """Finds the package config file (or template) in the given package directory.

    Returns a PackageConfigFileInfo instance with:
    - type: 'static' or 'template'
    - path: path to the file/template
    - engine: RenderEngineConfig instance (if 'template', otherwise None)
    - target_name: 'drift_package.toml' or 'package.toml' (PACKAGE_CONFIG_FILE_NAMES)
    """
    # 1. drift_package.toml and package.toml
    for filename in PACKAGE_CONFIG_FILE_NAME_LIST:
        p = package_dir / filename
        if p.is_file():
            return PackageConfigFileInfo(type="static", path=p, engine=None,
                                         target_name=filename)

    # 2. Iterate over render engines in definition order
    for engine in workspace_config.render_engine_configs.values():
        suffix = engine.suffix
        if not suffix:
            continue
        for filename in PACKAGE_CONFIG_FILE_NAME_LIST:
            template_filename = filename.replace(".toml", f".{suffix}.toml")
            p = package_dir / template_filename
            if p.is_file():
                return PackageConfigFileInfo(type="template", path=p, engine=engine,
                                             target_name=filename)

    return None


def load_package_config_from_dir(
    package_dir: Path,
    package_name: str,
    workspace_config: Optional["WorkspaceConfig"] = None
) -> PackageConfig:
    """Loads package configuration from a package directory, optionally rendering it if it is a template."""
    if workspace_config is None:
        # Fallback for backward compatibility/static loading without workspace
        config_file = locate_package_config_file_static(package_dir)
        if not config_file:
            raise FileNotFoundError(f"No package configuration file found in directory: {package_dir}")
        return load_package_config_static(config_file, default_name=package_name)

    info = get_package_config_file_info(package_dir, workspace_config)
    if not info:
        raise FileNotFoundError(f"No package configuration file found in directory: {package_dir}")

    if info.type == "static":
        return load_package_config_static(info.path, default_name=package_name)
    else:
        # It is a template, we need to render it!
        engine = info.engine
        if engine is None:
            raise ValueError(f"Template configuration file found, but render engine is not specified: {info.path}")

        # Determine output path: render/<package_name>/drift_package.toml
        output_file_path = workspace_config.render_path / package_name / PACKAGE_CONFIG_FILE_NAME

        # Perform rendering using standard render function
        from .render_core import render_template_to_file
        render_template_to_file(
            engine_config=engine,
            drift_root=workspace_config.drift_root,
            template_file_path=info.path,
            output_file_path=output_file_path
        )

        # Load from the rendered path
        config = load_package_config_static(output_file_path, default_name=package_name)
        config.config_template_path = info.path.resolve()
        config.config_rendered_path = output_file_path.resolve()
        return config
