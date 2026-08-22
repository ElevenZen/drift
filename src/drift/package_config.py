"""Package-specific configuration loading and metadata parsing using pathlib."""

import logging
import os
import tempfile
from pathlib import Path
from typing import List, Sequence, Optional, Tuple, Dict
from .toml_utils import parse_toml, merge_toml, dump_toml

from .constants import PACKAGE_CONFIG_FILE_NAME, PACKAGE_CONFIG_FILE_NAME_LIST, PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST
from .workspace_config import RenderEngineConfig, WorkspaceConfig

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/drift_package.toml."""
    name: str
    source_files: List[Path] = field(default_factory=list)
    enable_render: bool = True
    enable_install: bool = True
    install_method: Optional[str] = None
    target_directory: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    pre_install: Optional[str] = None
    post_install: Optional[str] = None
    pre_update: Optional[str] = None
    post_update: Optional[str] = None
    post_render: Optional[str] = None
    hook_timeout: int = 120

    def __post_init__(self) -> None:
        """Handles path expansion on load/initialization."""
        if self.target_directory:
            self.target_directory = Path(self.target_directory).expanduser()

    def validate(self) -> None:
        """Validates configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Package config must have a non-empty 'name'.")
        if not isinstance(self.source_files, list):
            raise TypeError(f"source_files must be a list for package '{self.name}'.")
        for file in self.source_files:
            if not isinstance(file, Path):
                raise TypeError(f"source_files entries must be Path objects for package '{self.name}'.")
        if self.install_method is not None and self.install_method not in ("stow", "copy"):
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
        """Checks if the given file path is a package config file, its local override, or their template versions."""
        return file_path in self.source_files

    def get_target_directory(self, workspace_config: WorkspaceConfig) -> Path:
        return (self.target_directory or workspace_config.default_target_path).expanduser()

    def get_install_method(self, workspace_config: WorkspaceConfig) -> str:
        return self.install_method or workspace_config.default_install_method

    @classmethod
    def from_dict(cls,
                  data: dict,
                  default_name: Optional[str] = None,
                  source_files: Optional[Sequence[Optional[Path]]] = None) -> "PackageConfig":
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
            "pre_install",
            "post_install",
            "pre_update",
            "post_update",
            "post_render",
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
        if not isinstance(raw_timeout, int):
            raise TypeError(f"hook_timeout must be an integer for package '{name}'.")

        # Mapping for backward compatibility
        post_install = package_data.get("post_install")
        post_update = package_data.get("post_update")

        config = cls(
            name=str(name),
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=package_data.get("install_method"),
            target_directory=target_dir,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[Path(d) for d in fcd],
            pre_install=package_data.get("pre_install"),
            post_install=post_install,
            pre_update=package_data.get("pre_update"),
            post_update=post_update,
            post_render=package_data.get("post_render"),
            hook_timeout=raw_timeout
        )
        if source_files:
            config.source_files = [x for x in source_files if isinstance(x, Path)]
        config.validate()
        return config


def load_package_config_static(
    file_path: Path,
    default_name: Optional[str] = None
) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml."""
    if not file_path.exists():
        raise FileNotFoundError(f"Package configuration file not found: {file_path}")
    content = file_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    config = PackageConfig.from_dict(data, default_name=default_name, source_files=[file_path])
    return config


@dataclass
class PackageConfigFileInfo:
    """Represents file info for a found package config file (or template).

    This class is only used in this file, and is not part of the public API.
    Public API users should use the PackageConfig class instead.
    """
    type: str  # 'static' or 'template'
    path: Path  # path to the file/template
    engine: Optional[RenderEngineConfig] = None  # RenderEngineConfig instance (if 'template', otherwise None)


def get_package_config_file_info(
    package_dir: Path,
    workspace_config: WorkspaceConfig
) -> Tuple[Optional[PackageConfigFileInfo], Optional[PackageConfigFileInfo]]:
    """Finds the package config file (or template) and its local override file (or template) in the given package directory.

    Returns a Tuple containing:
    1. Base PackageConfigFileInfo or None
    2. Local override PackageConfigFileInfo or None
    """
    # 1. Base config check
    base_res = workspace_config.find_source_file_for_rendered_names(package_dir, PACKAGE_CONFIG_FILE_NAME_LIST)
    base_info = None
    if base_res:
        base_info = PackageConfigFileInfo(
            type="static" if base_res.engine is None else "template",
            path=base_res.path,
            engine=base_res.engine,
        )

    # 2. Local config check
    local_names = ["drift_package.local.toml", "package.local.toml"]
    local_res = workspace_config.find_source_file_for_rendered_names(package_dir, local_names)
    local_info = None
    if local_res:
        local_info = PackageConfigFileInfo(
            type="static" if local_res.engine is None else "template",
            path=local_res.path,
            engine=local_res.engine,
        )

    return base_info, local_info


def render_or_load_toml(
    info: PackageConfigFileInfo,
    workspace_config: WorkspaceConfig,
    package_name: str
) -> dict:
    """Renders the package config file info to a temporary file (if it is a template)

    and returns its parsed TOML dictionary.
    """
    if info.type == "static":
        content = info.path.read_text(encoding="utf-8")
        return parse_toml(content)
    else:
        # It's a template, we need to render it!
        engine = info.engine
        if engine is None:
            raise ValueError(f"Template configuration file found, but render engine is not specified: {info.path}")

        # Create a temporary file
        fd, temp_path = tempfile.mkstemp(suffix=".toml", prefix=f"{package_name}_pkg_")
        temp_path_obj = Path(temp_path)
        os.close(fd) # Close immediately so render_template_to_file can write to it safely

        try:
            from .render_core import render_template_to_file
            render_template_to_file(
                engine_config=engine,
                drift_root=workspace_config.drift_root,
                template_file_path=info.path,
                output_file_path=temp_path_obj
            )
            content = temp_path_obj.read_text(encoding="utf-8")
            data = parse_toml(content)
        finally:
            if temp_path_obj.exists():
                temp_path_obj.unlink()

        return data


def locate_load_package_config_file_static(package_dir: Path, names: Sequence[str]) -> Tuple[dict, Optional[Path]]:
    """Locates and loads the local static package config override file if present.

    Propagates any read/parse errors if the file is found.
    """
    for filename in names:
        local_path = package_dir / filename
        if not local_path.is_file():
            continue 
        content = local_path.read_text(encoding="utf-8")
        return parse_toml(content), local_path
    return {}, None


def load_package_config_from_source_dir(
    package_dir: Path,
    package_name: str,
    workspace_config: Optional[WorkspaceConfig] = None
) -> PackageConfig:
    """Loads package configuration from a package directory, optionally rendering it if it is a template."""
    if workspace_config is None:
        logger.warning("WorkspaceConfig is not provided. Falling back to static loading without rendering.")
        # Fallback for backward compatibility/static loading without workspace settings
        base_dict, base_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_FILE_NAME_LIST)
        if not base_dict:
            raise FileNotFoundError(f"No package configuration file found in directory: {package_dir}")
        local_dict, local_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST)
        combined_dict = merge_toml(base_dict, local_dict)
        return PackageConfig.from_dict(combined_dict,
                                       default_name=package_name,
                                       source_files=[base_path, local_path])

    base_info, local_info = get_package_config_file_info(package_dir, workspace_config)
    logger.debug(f"Base package config info: {base_info}")
    logger.debug(f"Local package config info: {local_info}")
    if not base_info:
        raise FileNotFoundError(f"No package configuration file found in directory: {package_dir}")
    base_dict = render_or_load_toml(base_info, workspace_config, package_name)
    source_files = [base_info.path]
    if local_info:
        local_dict = render_or_load_toml(local_info, workspace_config, package_name)
        source_files.append(local_info.path)
    else:
        local_dict = {}
    combined_dict = merge_toml(base_dict, local_dict)

    # Determine output path: render/<package_name>/drift_package.toml
    output_file_path = workspace_config.render_path / package_name / PACKAGE_CONFIG_FILE_NAME
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    toml_str = dump_toml(combined_dict)
    output_file_path.write_text(toml_str, encoding="utf-8")

    # Load from the rendered path
    config = PackageConfig.from_dict(combined_dict,
                                     default_name=package_name,
                                     source_files=source_files)
    return config

