"""Package-specific configuration loading and metadata parsing using pathlib."""

import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import List, Sequence, Optional, Tuple, Dict, Iterator
from .toml_utils import parse_toml, merge_toml, dump_toml

from .constants import (
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME_LIST,
    PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST,
    LIFECYCLE_HOOK_NAMES,
)
from .workspace_config import RenderEngineConfig, WorkspaceConfig, load_env_settings
from .exceptions import ConfigError
from .file_utils import expand_user_and_env

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PackageHooks:
    """Encapsulates lifecycle hook configurations and execution methods for a package."""
    pre_source: Optional[str] = None
    pre_install: Optional[str] = None
    post_install: Optional[str] = None
    pre_update: Optional[str] = None
    post_update: Optional[str] = None
    pre_uninstall: Optional[str] = None
    post_uninstall: Optional[str] = None
    post_render: Optional[str] = None
    health: Optional[str] = None
    timeout: int = 120
    _package_config: Optional["PackageConfig"] = field(default=None, repr=False, compare=False)

    @property
    def package_config(self) -> Optional["PackageConfig"]:
        return self._package_config

    @package_config.setter
    def package_config(self, value: Optional["PackageConfig"]) -> None:
        self._package_config = value

    def validate(self, package_name: str = "") -> None:
        """Validates hook configurations."""
        for hook_name in LIFECYCLE_HOOK_NAMES:
            val = getattr(self, hook_name)
            if val is not None and not isinstance(val, str):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise TypeError(f"{hook_name} must be a string{name_str}.")
        if not isinstance(self.timeout, int):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise TypeError(f"timeout must be an integer{name_str}.")
        if self.timeout <= 0:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ValueError(f"timeout must be a positive integer{name_str}.")

    def trigger(self, hook_name: str, hook_dir: Path, cwd: Path) -> None:
        """Executes a package lifecycle hook script if specified and found."""
        from .lifecycle_hooks import trigger_package_lifecycle_hook
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        trigger_package_lifecycle_hook(
            pkg=self._package_config.name,
            hook_name=hook_name,
            metadata=self._package_config,
            hook_dir=hook_dir,
            cwd=cwd
        )

    def trigger_pre_source(
        self,
        source_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None
    ) -> None:
        """Triggers the pre_source hook inside source_dir.

        If workspace_config is provided, the hook is executed via trigger_pre_source_lifecycle_hook
        (rendering or copying into render/ as needed). Otherwise, it executes directly from source_dir.
        """
        if self.pre_source:
            if self._package_config is None:
                raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
            if workspace_config is not None:
                from .lifecycle_hooks import trigger_pre_source_lifecycle_hook
                trigger_pre_source_lifecycle_hook(
                    workspace_config=workspace_config,
                    package_name=self._package_config.name,
                    load_envs=False,
                    pkg_config=self._package_config
                )
            else:
                from .lifecycle_hooks import execute_hook_script
                hook_file_path = Path(self.pre_source)
                hook_path = hook_file_path if hook_file_path.is_absolute() else source_dir / hook_file_path
                execute_hook_script(
                    hook_path=hook_path,
                    pkg=self._package_config.name,
                    hook_name="pre_source",
                    metadata=self._package_config,
                    cwd=source_dir
                )

    def trigger_post_render(self, render_dir: Path) -> None:
        """Triggers the post_render hook inside render_dir."""
        if self.post_render:
            self.trigger("post_render", hook_dir=render_dir, cwd=render_dir)

    def trigger_pre_install(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the pre_install hook."""
        if self.pre_install:
            self.trigger("pre_install", hook_dir=install_dir, cwd=cwd)

    def trigger_post_install(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the post_install hook."""
        if self.post_install:
            self.trigger("post_install", hook_dir=install_dir, cwd=cwd)

    def trigger_pre_update(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the pre_update hook."""
        if self.pre_update:
            self.trigger("pre_update", hook_dir=install_dir, cwd=cwd)

    def trigger_post_update(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the post_update hook."""
        if self.post_update:
            self.trigger("post_update", hook_dir=install_dir, cwd=cwd)

    def trigger_pre_uninstall(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the pre_uninstall hook.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        if self.pre_uninstall:
            self.trigger("pre_uninstall", hook_dir=install_dir, cwd=cwd)

    def trigger_post_uninstall(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the post_uninstall hook.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        if self.post_uninstall:
            self.trigger("post_uninstall", hook_dir=install_dir, cwd=cwd)

    def trigger_health(self, install_dir: Path, cwd: Path) -> None:
        """Triggers the health probe hook."""
        if self.health:
            self.trigger("health", hook_dir=install_dir, cwd=cwd)

    def check_hook_files(
        self,
        base_dir: Path,
        hook_names: Optional[Sequence[str]] = None
    ) -> None:
        """Checks that configured lifecycle hook files exist in base_dir and are regular files.

        Args:
            base_dir: Directory containing package files (e.g. render/<pkg> or install/<pkg>).
            hook_names: Optional sequence of hook names to check. If omitted, all LIFECYCLE_HOOK_NAMES are checked.

        Raises:
            FileNotFoundError: If a configured hook file does not exist.
            ValueError: If a configured hook path is not a regular file.
        """
        pkg_name = self._package_config.name if self._package_config else "unknown"
        target_hooks = hook_names if hook_names is not None else LIFECYCLE_HOOK_NAMES
        for hook_name in target_hooks:
            hook_rel = getattr(self, hook_name, None)
            if hook_rel:
                hook_path = base_dir / hook_rel
                if not hook_path.exists():
                    raise FileNotFoundError(
                        f"Lifecycle hook file specified for '{hook_name}' in package '{pkg_name}' does not exist: '{hook_path}'"
                    )
                if not hook_path.is_file():
                    raise ValueError(
                        f"Lifecycle hook path specified for '{hook_name}' in package '{pkg_name}' is not a regular file: '{hook_path}'"
                    )


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/drift_package.toml."""
    name: str
    source_files: List[Path] = field(default_factory=list)
    enable_render: bool = True
    enable_install: bool = True
    install_method: Optional[str] = None
    target_directory: Optional[Path] = None
    target_directory_winos: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    hooks: PackageHooks = field(default_factory=PackageHooks)

    def check_hook_files(
        self,
        base_dir: Path,
        hook_names: Optional[Sequence[str]] = None
    ) -> None:
        """Checks that configured lifecycle hook files exist in base_dir and are regular files."""
        self.hooks.check_hook_files(base_dir, hook_names=hook_names)

    def __init__(
        self,
        name: str,
        source_files: Optional[List[Path]] = None,
        enable_render: bool = True,
        enable_install: bool = True,
        install_method: Optional[str] = None,
        target_directory: Optional[Path] = None,
        target_directory_winos: Optional[Path] = None,
        sudo: bool = False,
        fully_controlled_dirs: Optional[List[Path]] = None,
        hooks: Optional[PackageHooks] = None,
        pre_source: Optional[str] = None,
        pre_install: Optional[str] = None,
        post_install: Optional[str] = None,
        pre_update: Optional[str] = None,
        post_update: Optional[str] = None,
        pre_uninstall: Optional[str] = None,
        post_uninstall: Optional[str] = None,
        post_render: Optional[str] = None,
        health: Optional[str] = None,
        hook_timeout: Optional[int] = None
    ) -> None:
        self.name = name
        self.source_files = source_files if source_files is not None else []
        self.enable_render = enable_render
        self.enable_install = enable_install
        self.install_method = install_method
        self.target_directory = expand_user_and_env(target_directory) if target_directory else None
        self.target_directory_winos = expand_user_and_env(target_directory_winos) if target_directory_winos else None
        self.sudo = sudo
        self.fully_controlled_dirs = fully_controlled_dirs if fully_controlled_dirs is not None else []

        effective_timeout = hook_timeout if hook_timeout is not None else 120
        if hooks is not None:
            self.hooks = hooks
            if pre_source is not None:
                self.hooks.pre_source = pre_source
            if pre_install is not None:
                self.hooks.pre_install = pre_install
            if post_install is not None:
                self.hooks.post_install = post_install
            if pre_update is not None:
                self.hooks.pre_update = pre_update
            if post_update is not None:
                self.hooks.post_update = post_update
            if pre_uninstall is not None:
                self.hooks.pre_uninstall = pre_uninstall
            if post_uninstall is not None:
                self.hooks.post_uninstall = post_uninstall
            if post_render is not None:
                self.hooks.post_render = post_render
            if health is not None:
                self.hooks.health = health
            if hook_timeout is not None:
                self.hooks.timeout = effective_timeout
        else:
            self.hooks = PackageHooks(
                pre_source=pre_source,
                pre_install=pre_install,
                post_install=post_install,
                pre_update=pre_update,
                post_update=post_update,
                pre_uninstall=pre_uninstall,
                post_uninstall=post_uninstall,
                post_render=post_render,
                health=health,
                timeout=effective_timeout
            )
        self.hooks.package_config = self

    @property
    def pre_source(self) -> Optional[str]:
        return self.hooks.pre_source

    @pre_source.setter
    def pre_source(self, val: Optional[str]) -> None:
        self.hooks.pre_source = val

    @property
    def pre_install(self) -> Optional[str]:
        return self.hooks.pre_install

    @pre_install.setter
    def pre_install(self, val: Optional[str]) -> None:
        self.hooks.pre_install = val

    @property
    def post_install(self) -> Optional[str]:
        return self.hooks.post_install

    @post_install.setter
    def post_install(self, val: Optional[str]) -> None:
        self.hooks.post_install = val

    @property
    def pre_update(self) -> Optional[str]:
        return self.hooks.pre_update

    @pre_update.setter
    def pre_update(self, val: Optional[str]) -> None:
        self.hooks.pre_update = val

    @property
    def post_update(self) -> Optional[str]:
        return self.hooks.post_update

    @post_update.setter
    def post_update(self, val: Optional[str]) -> None:
        self.hooks.post_update = val

    @property
    def pre_uninstall(self) -> Optional[str]:
        return self.hooks.pre_uninstall

    @pre_uninstall.setter
    def pre_uninstall(self, val: Optional[str]) -> None:
        self.hooks.pre_uninstall = val

    @property
    def post_uninstall(self) -> Optional[str]:
        return self.hooks.post_uninstall

    @post_uninstall.setter
    def post_uninstall(self, val: Optional[str]) -> None:
        self.hooks.post_uninstall = val

    @property
    def post_render(self) -> Optional[str]:
        return self.hooks.post_render

    @post_render.setter
    def post_render(self, val: Optional[str]) -> None:
        self.hooks.post_render = val

    @property
    def health(self) -> Optional[str]:
        return self.hooks.health

    @health.setter
    def health(self, val: Optional[str]) -> None:
        self.hooks.health = val

    @property
    def hook_timeout(self) -> int:
        return self.hooks.timeout

    @hook_timeout.setter
    def hook_timeout(self, val: int) -> None:
        self.hooks.timeout = val

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
        if not isinstance(self.hooks, PackageHooks):
            raise TypeError(f"hooks must be a PackageHooks instance for package '{self.name}'.")
        self.hooks.validate(self.name)

    def is_package_config_file(self, file_path: Path) -> bool:
        """Checks if the given file path is a package config file, its local override, or their template versions."""
        return file_path in self.source_files

    def get_target_directory(self, workspace_config: WorkspaceConfig) -> Path:
        if sys.platform == "win32" and self.target_directory_winos is not None:
            return expand_user_and_env(self.target_directory_winos)
        return expand_user_and_env(self.target_directory or workspace_config.default_target_path)

    def get_install_method(self, workspace_config: WorkspaceConfig) -> str:
        if sys.platform == "win32":
            return "copy"
        return self.install_method or workspace_config.default_install_method

    def load_package_envs(
        self,
        workspace_config: WorkspaceConfig,
        overwrite: bool = True
    ) -> Optional[List[Tuple[str, Optional[str]]]]:
        """Loads default package-specific environment variables into os.environ.

        Variables loaded:
            drift_package_name: Name of the package (directory name).
            drift_package_target_dir: Resolved absolute target directory path on the host system.
            drift_package_source_dir: Absolute path to the package's source directory in workspace.
            drift_package_render_dir: Absolute path to the package's compiled sandbox directory.
            drift_package_install_dir: Absolute path to the package's state database directory.
            drift_install_method: Resolved install method ('stow' or 'copy').

        Returns:
            A list of (key, original_value) tuples for modified variables.
        """
        target_dir = self.get_target_directory(workspace_config)
        target_dir_str = str(target_dir)
        install_method_str = self.get_install_method(workspace_config)
        source_dir_str = str(workspace_config.source_path / self.name)
        render_dir_str = str(workspace_config.render_path / self.name)
        install_dir_str = str(workspace_config.install_path / self.name)

        envs = [
            ("drift_package_name", self.name),
            ("drift_package_target_dir", target_dir_str),
            ("drift_package_source_dir", source_dir_str),
            ("drift_package_render_dir", render_dir_str),
            ("drift_package_install_dir", install_dir_str),
            ("drift_install_method", install_method_str),
        ]
        return load_env_settings(envs, overwrite=overwrite)

    def unload_package_envs(
        self,
        original_envs: Optional[List[Tuple[str, Optional[str]]]]
    ) -> None:
        """Restores original environment variables using the list returned by load_package_envs."""
        from .workspace_config import unload_env_settings
        unload_env_settings(original_envs)

    @contextmanager
    def package_envs(
        self,
        workspace_config: WorkspaceConfig,
        overwrite: bool = True
    ) -> Iterator[None]:
        """Context manager to activate package-specific environment variables in os.environ."""
        saved_envs = self.load_package_envs(workspace_config=workspace_config, overwrite=overwrite)
        try:
            yield
        finally:
            self.unload_package_envs(saved_envs)

    @classmethod
    def from_dict(cls, data: dict, package_name: str,
                  source_files: Optional[Sequence[Optional[Path]]] = None) -> "PackageConfig":
        """Builds a PackageConfig instance from a parsed TOML dictionary and package name."""
        # Warning for unknown top-level sections
        known_top_sections = {"package", "hooks"}
        for key in data:
            if key not in known_top_sections:
                logger.warning(f"Unknown top-level package config section: '{key}'")

        package_data = data.get("package", {})
        hooks_data = data.get("hooks", {})
        
        name = package_name
        if not name:
            raise ValueError("Package name must be provided when constructing PackageConfig.")

        # Warning for unknown package options
        known_package_keys = {
            "name",
            "enable_render",
            "enable_install",
            "install_method",
            "target_directory",
            "target_directory_winos",
            "sudo",
            "fully_controlled_dirs"
        }
        for key in package_data:
            if key not in known_package_keys:
                logger.warning(f"Unknown package option: '{key}'")

        # Warning for unknown hooks options
        known_hooks_keys = set(LIFECYCLE_HOOK_NAMES) | {"timeout"}
        for key in hooks_data:
            if key not in known_hooks_keys:
                logger.warning(f"Unknown hook option: '{key}'")
            
        fcd = package_data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd = [fcd]
        elif not isinstance(fcd, list):
            fcd = []

        # Expand home directory and env vars for target_directory on load
        target_dir = package_data.get("target_directory")
        if target_dir:
            target_dir = expand_user_and_env(target_dir)

        target_dir_winos = package_data.get("target_directory_winos")
        if target_dir_winos:
            target_dir_winos = expand_user_and_env(target_dir_winos)
            
        hooks_data = data.get("hooks", {})
        if not isinstance(hooks_data, dict):
            hooks_data = {}
        else:
            hooks_data = dict(hooks_data)

        if sys.platform == "win32":
            winos_hooks = hooks_data.get("winos")
            if not isinstance(winos_hooks, dict):
                winos_hooks = data.get("hooks_winos")
            if isinstance(winos_hooks, dict):
                for k, v in winos_hooks.items():
                    hooks_data[k] = v

        raw_timeout = hooks_data.get("timeout", 120)
        if isinstance(raw_timeout, str) and raw_timeout.isdigit():
            raw_timeout = int(raw_timeout)
        if not isinstance(raw_timeout, int):
            raise TypeError(f"timeout must be an integer for package '{name}'.")

        hooks = PackageHooks(
            pre_source=hooks_data.get("pre_source"),
            pre_install=hooks_data.get("pre_install"),
            post_install=hooks_data.get("post_install"),
            pre_update=hooks_data.get("pre_update"),
            post_update=hooks_data.get("post_update"),
            pre_uninstall=hooks_data.get("pre_uninstall"),
            post_uninstall=hooks_data.get("post_uninstall"),
            post_render=hooks_data.get("post_render"),
            health=hooks_data.get("health"),
            timeout=raw_timeout
        )

        config = cls(
            name=str(name),
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=package_data.get("install_method"),
            target_directory=target_dir,
            target_directory_winos=target_dir_winos,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[Path(d) for d in fcd],
            hooks=hooks
        )
        if source_files:
            config.source_files = [x for x in source_files if isinstance(x, Path)]
        config.validate()
        return config


def load_package_config_rendered(
    package_toml_path: Path,
    package_name_override: Optional[str] = None
) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml."""
    pkg_name = package_name_override or package_toml_path.parent.name
    if not package_toml_path.exists():
        raise FileNotFoundError(f"Package configuration file not found: {package_toml_path}")
    content = package_toml_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    config = PackageConfig.from_dict(data, package_name=pkg_name, source_files=[package_toml_path])
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
    workspace_config: Optional[WorkspaceConfig] = None,
) -> PackageConfig:
    """Loads package configuration from a package directory, optionally rendering it if it is a template."""
    pkg_name = package_dir.name
    if workspace_config is None:
        logger.warning("WorkspaceConfig is not provided. Falling back to static loading without rendering.")
        # Fallback for backward compatibility/static loading without workspace settings
        base_dict, base_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_FILE_NAME_LIST)
        if not base_dict:
            raise FileNotFoundError(f"'{PACKAGE_CONFIG_FILE_NAME}' not found in directory: {package_dir}")
        local_dict, local_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST)
        combined_dict = merge_toml(base_dict, local_dict)
        return PackageConfig.from_dict(combined_dict,
                                       package_name=pkg_name,
                                       source_files=[base_path, local_path])

    base_info, local_info = get_package_config_file_info(package_dir, workspace_config)
    logger.debug(f"Base package config info: {base_info}")
    logger.debug(f"Local package config info: {local_info}")
    if not base_info:
        raise FileNotFoundError(f"'{PACKAGE_CONFIG_FILE_NAME}' not found in directory: {package_dir}")
    base_dict = render_or_load_toml(base_info, workspace_config, pkg_name)
    source_files = [base_info.path]
    if local_info:
        local_dict = render_or_load_toml(local_info, workspace_config, pkg_name)
        source_files.append(local_info.path)
    else:
        local_dict = {}
    combined_dict = merge_toml(base_dict, local_dict)

    # Determine output path: render/<package_name>/drift_package.toml
    output_file_path = workspace_config.render_path / pkg_name / PACKAGE_CONFIG_FILE_NAME
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    toml_str = dump_toml(combined_dict)
    output_file_path.write_text(toml_str, encoding="utf-8")

    # Load from the rendered path
    config = PackageConfig.from_dict(combined_dict,
                                     package_name=pkg_name,
                                     source_files=source_files)
    return config

