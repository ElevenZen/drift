import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .toml_parser import parse_toml


@dataclass
class RenderEngineConfig:
    """Represents a render engine configuration inside workspace configuration."""
    name: str
    input_file: str
    suffix: str
    render_command: str

    def validate(self) -> None:
        """Validates render engine configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Render engine must have a non-empty 'name'.")
        if not self.input_file or not isinstance(self.input_file, str):
            raise ValueError("input_file must be a non-empty string.")
        if not self.suffix or not isinstance(self.suffix, str):
            raise ValueError("suffix must be a non-empty string.")
        if not self.render_command or not isinstance(self.render_command, str):
            raise ValueError("render_command must be a non-empty string.")


@dataclass
class WorkspaceConfig:
    """Represents the global workspace configurations inside config/drift.toml."""
    source_directory: str = "src"
    render_directory: str = "render"
    install_directory: str = "install"
    backup_directory: str = "backup"
    default_target_directory: str = "~"
    packages_enable: Dict[str, bool] = field(default_factory=dict)
    packages_enable_default: bool = False
    render_engine_config: Dict[str, RenderEngineConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Handles path expansion on load/initialization."""
        if self.default_target_directory and self.default_target_directory.startswith("~"):
            self.default_target_directory = os.path.expanduser(self.default_target_directory)

    def validate(self) -> None:
        """Validates workspace configuration values."""
        if not isinstance(self.source_directory, str) or not self.source_directory:
            raise ValueError("source_directory must be a non-empty string.")
        if not isinstance(self.render_directory, str) or not self.render_directory:
            raise ValueError("render_directory must be a non-empty string.")
        if not isinstance(self.install_directory, str) or not self.install_directory:
            raise ValueError("install_directory must be a non-empty string.")
        if not isinstance(self.backup_directory, str) or not self.backup_directory:
            raise ValueError("backup_directory must be a non-empty string.")
        if not isinstance(self.default_target_directory, str) or not self.default_target_directory:
            raise ValueError("default_target_directory must be a non-empty string.")
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
    def packages(self) -> Dict[str, bool]:
        """Alias property for packages_enable to support backward compatibility."""
        return self.packages_enable

    @property
    def render_engine_configs(self) -> Dict[str, RenderEngineConfig]:
        """Alias property for render_engine_config to support backward compatibility."""
        return self.render_engine_config

    @classmethod
    def from_dict(cls, data: dict) -> "WorkspaceConfig":
        """Builds a WorkspaceConfig instance from a parsed TOML dictionary."""
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
                
        # Parse render engines configurations under [render.*]
        render_data = data.get("render", {})
        render_engine_config = {}
        for name, config_dict in render_data.items():
            if isinstance(config_dict, dict):
                render_engine_config[name] = RenderEngineConfig(
                    name=name,
                    input_file=str(config_dict.get("input_file", "")),
                    suffix=str(config_dict.get("suffix", "")),
                    render_command=str(config_dict.get("render_command", ""))
                )

        # Expand home directory for default_target_directory on load
        default_target_dir = str(workspace_data.get("default_target_directory", "~"))
        if default_target_dir.startswith("~"):
            default_target_dir = os.path.expanduser(default_target_dir)

        config = cls(
            source_directory=str(workspace_data.get("source_directory", "src")),
            render_directory=str(workspace_data.get("render_directory", "render")),
            install_directory=str(workspace_data.get("install_directory", "install")),
            backup_directory=str(workspace_data.get("backup_directory", "backup")),
            default_target_directory=default_target_dir,
            packages_enable=packages,
            packages_enable_default=bool(packages_enable_data.get("DEFAULT", False)),
            render_engine_config=render_engine_config,
        )
        config.validate()
        return config


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/package.toml."""
    name: str
    enable_render: bool = True
    enable_install: bool = True
    install_method: str = "stow"
    target_directory: Optional[str] = None
    sudo: bool = False
    fully_controlled_dirs: List[str] = field(default_factory=list)
    on_install: Optional[str] = None
    on_update: Optional[str] = None

    def __post_init__(self) -> None:
        """Handles path expansion on load/initialization."""
        if self.target_directory and self.target_directory.startswith("~"):
            self.target_directory = os.path.expanduser(self.target_directory)

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
            if not isinstance(d, str):
                raise TypeError(f"fully_controlled_dirs entries must be strings for package '{self.name}'.")

    @classmethod
    def from_dict(cls, data: dict, default_name: Optional[str] = None) -> "PackageConfig":
        """Builds a PackageConfig instance from a parsed TOML dictionary."""
        package_data = data.get("package", {})
        
        name = package_data.get("name", default_name)
        if not name:
            raise ValueError("Package configuration is missing the required 'name' field.")
            
        fcd = package_data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd = [fcd]
        elif not isinstance(fcd, list):
            fcd = []

        # Expand home directory for target_directory on load
        target_dir = package_data.get("target_directory")
        if target_dir and str(target_dir).startswith("~"):
            target_dir = os.path.expanduser(str(target_dir))
            
        config = cls(
            name=str(name),
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=str(package_data.get("install_method", "stow")),
            target_directory=target_dir,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[str(d) for d in fcd],
            on_install=package_data.get("on_install"),
            on_update=package_data.get("on_update")
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


def render_workspace_config_toml(envst_path: str) -> str:
    """Renders the drift.envst.toml template using env variables and writes to a temporary file,

    returning the path to the temporary file.
    """
    with open(envst_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Perform envsubst rendering
    rendered_content = render_envsubst_string(content)
    
    # Create temporary file
    fd, temp_path = tempfile.mkstemp(suffix=".toml", prefix="drift_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(rendered_content)
    return temp_path


def load_workspace_config(file_path: str) -> WorkspaceConfig:
    """Loads and parses the workspace configuration from drift.toml."""
    actual_path = file_path
    if not os.path.exists(file_path):
        # Look for .envst.toml alternative
        base, ext = os.path.splitext(file_path)
        envst_path = base + ".envst" + ext
        if os.path.exists(envst_path):
            actual_path = render_workspace_config_toml(envst_path)
        else:
            raise FileNotFoundError(f"Workspace configuration file not found: {file_path}")
            
    print(f"Workspace config is loaded from: {actual_path}")
    with open(actual_path, "r", encoding="utf-8") as f:
        content = f.read()
    data = parse_toml(content)
    return WorkspaceConfig.from_dict(data)


def load_package_config(file_path: str, default_name: Optional[str] = None) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml or package.toml."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Package configuration file not found: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    data = parse_toml(content)
    return PackageConfig.from_dict(data, default_name=default_name)


def find_package_config_file(package_dir: str) -> Optional[str]:
    """Finds the drift_package.toml or package.toml in a given package directory."""
    for filename in ("drift_package.toml", "package.toml"):
        path = os.path.join(package_dir, filename)
        if os.path.isfile(path):
            return path
    return None


def load_package_config_from_dir(package_dir: str, package_name: str) -> PackageConfig:
    """Loads package configuration from a package directory."""
    config_file = find_package_config_file(package_dir)
    if config_file:
        return load_package_config(config_file, default_name=package_name)
    else:
        raise FileNotFoundError(f"No package configuration file found in directory: {package_dir}")
