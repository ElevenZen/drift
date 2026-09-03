"""Package-specific configuration loading and metadata parsing using pathlib."""

import logging
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import List, Sequence, Optional, Tuple, Dict, Iterator, Any, Union, Set, Mapping
from .toml_utils import parse_toml, merge_toml, dump_toml

from .constants import (
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME_LIST,
    PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST,
    LIFECYCLE_HOOK_NAMES,
    WINDOWS_PLATFORM_ALIASES,
    DEFAULT_HOOK_TIMEOUT,
)
from .workspace_config import RenderEngineConfig, WorkspaceConfig, load_env_settings
from .exceptions import ConfigError
from .file_utils import expand_user_and_env
from .result_models import HookResult

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def normalize_hook_value(val: Optional[Union[str, Path]]) -> Optional[Path]:
    """Normalizes a lifecycle hook configuration value to Optional[Path].

    Returns None if val is None, empty string "", or "disable" / "disabled" (case-insensitive).
    Returns Path instances as-is.
    """
    if val is None:
        return None
    if isinstance(val, Path):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s == "" or s.lower() in ("disable", "disabled"):
            return None
        return Path(s)
    raise TypeError(f"Hook value must be a string or Path, got {type(val).__name__}")


def match_ip_address(pattern: str, ip: str) -> bool:
    """Matches a single IP address against an exact IP, CIDR subnet, or wildcard pattern."""
    import ipaddress
    # Exact match
    if pattern == ip:
        return True
    # Wildcard match (e.g. 192.168.1.* or 10.0.*)
    if "*" in pattern:
        prefix = pattern.split("*")[0]
        if ip.startswith(prefix):
            return True
    # CIDR subnet match (e.g. 192.168.1.0/24 or 10.0.0.0/8)
    if "/" in pattern:
        try:
            net = ipaddress.ip_network(pattern, strict=False)
            addr = ipaddress.ip_address(ip)
            if addr in net:
                return True
        except ValueError:
            pass
    return False


def match_ip_addresses(patterns: Sequence[str], host_ips: Sequence[str]) -> bool:
    """Returns True if any host IP matches any of the given IP patterns."""
    for pattern in patterns:
        for host_ip in host_ips:
            if match_ip_address(pattern, host_ip):
                return True
    return False


@dataclass
class PackageRequirements:
    """Declarative host platform and environment requirements for a package."""
    os: List[str] = field(default_factory=list)
    arch: List[str] = field(default_factory=list)
    distro: List[str] = field(default_factory=list)
    binaries: List[str] = field(default_factory=list)
    env: List[str] = field(default_factory=list)
    ip: List[str] = field(default_factory=list)

    def check_requirements(self) -> Tuple[bool, Optional[str]]:
        """Evaluates declarative requirements against host facts and environment.

        Returns:
            Tuple of (is_satisfied: bool, failure_reason: Optional[str]).
        """
        from .host_facts import get_host_os, get_host_arch, get_host_distro

        # 1. Check OS
        if self.os:
            current_os = os.environ.get("drift_os") or get_host_os()
            if current_os not in self.os:
                return False, f"Host OS '{current_os}' not in required list: {self.os}"

        # 2. Check Architecture
        if self.arch:
            current_arch = os.environ.get("drift_arch") or get_host_arch()
            if current_arch not in self.arch:
                return False, f"Host architecture '{current_arch}' not in required list: {self.arch}"

        # 3. Check Linux Distro
        if self.distro:
            current_distro = os.environ.get("drift_distro") or get_host_distro()
            if current_distro not in self.distro:
                return False, f"Linux distribution '{current_distro}' not in required list: {self.distro}"

        # 4. Check Binaries in PATH
        for binary in self.binaries:
            if not shutil.which(binary):
                return False, f"Required binary '{binary}' not found in PATH"

        # 5. Check Environment Variables
        for env_var in self.env:
            if not os.environ.get(env_var):
                return False, f"Required environment variable '{env_var}' is unset or empty"

        # 6. Check Host LAN IP addresses
        if self.ip:
            raw_ips = os.environ.get("drift_ip_addresses")
            if raw_ips is not None:
                host_ips = [ip.strip() for ip in raw_ips.split(";") if ip.strip()]
            else:
                from .host_facts import get_host_ip_addresses
                host_ips = get_host_ip_addresses()

            if not match_ip_addresses(self.ip, host_ips):
                return False, f"Host IP addresses {host_ips} do not match any required IP pattern: {self.ip}"

        return True, None

    @classmethod
    def from_dict(cls, data: Any, package_name: str = "") -> "PackageRequirements":
        """Parses and validates a PackageRequirements instance from a dictionary."""
        if not data:
            return cls()
        if not isinstance(data, dict):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"[package.requirements] must be a table{name_str}.")

        known_keys = {"os", "arch", "distro", "binaries", "env", "ip", "ips", "ip_addresses"}
        for k in data:
            if k not in known_keys:
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"Unknown option under requirements: '{k}'{name_str}")

        def _to_list_str(val: Any, field_name: str) -> List[str]:
            if val is None:
                return []
            if isinstance(val, str):
                s = val.strip()
                return [s] if s else []
            if isinstance(val, (list, tuple)):
                res = []
                for item in val:
                    if not isinstance(item, str):
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise TypeError(f"Items in '{field_name}' must be strings{name_str}.")
                    s = item.strip()
                    if s:
                        res.append(s)
                return res
            name_str = f" for package '{package_name}'" if package_name else ""
            raise TypeError(f"'{field_name}' under requirements must be a string or list of strings{name_str}.")

        raw_ip = data.get("ip") or data.get("ips") or data.get("ip_addresses")

        return cls(
            os=_to_list_str(data.get("os"), "os"),
            arch=_to_list_str(data.get("arch"), "arch"),
            distro=_to_list_str(data.get("distro"), "distro"),
            binaries=_to_list_str(data.get("binaries"), "binaries"),
            env=_to_list_str(data.get("env"), "env"),
            ip=_to_list_str(raw_ip, "ip"),
        )


@dataclass
class PackageHooks:
    """Encapsulates lifecycle hook configurations and execution methods for a package."""
    probe: Optional[Path] = None
    pre_source: Optional[Path] = None
    pre_install: Optional[Path] = None
    post_install: Optional[Path] = None
    pre_update: Optional[Path] = None
    post_update: Optional[Path] = None
    pre_uninstall: Optional[Path] = None
    post_uninstall: Optional[Path] = None
    post_render: Optional[Path] = None
    health: Optional[Path] = None
    timeout: int = DEFAULT_HOOK_TIMEOUT
    _package_config: Optional["PackageConfig"] = field(default=None, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in LIFECYCLE_HOOK_NAMES:
            value = normalize_hook_value(value)
        super().__setattr__(name, value)

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
            if val is not None and not isinstance(val, Path):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise TypeError(f"{hook_name} must be a Path{name_str}.")
        if not isinstance(self.timeout, int):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise TypeError(f"timeout must be an integer{name_str}.")
        if self.timeout <= 0:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ValueError(f"timeout must be a positive integer{name_str}.")

    @classmethod
    def _validate_hook_dict(
        cls,
        hook_dict: Dict[str, Any],
        package_name: str = "",
        is_subtable: bool = False
    ) -> None:
        """Helper to validate unknown keys, value types, and platform sub-tables in a hook dictionary."""
        known_keys = set(LIFECYCLE_HOOK_NAMES) | {"timeout"}
        if not is_subtable:
            known_keys |= set(WINDOWS_PLATFORM_ALIASES)

        for key in hook_dict:
            if key not in known_keys:
                name_str = f" for package '{package_name}'" if package_name else ""
                context = "package [hooks]" if not is_subtable else "platform hooks sub-table"
                raise ConfigError(f"Unknown hook option in {context}: '{key}'{name_str}")

        for hook_name in LIFECYCLE_HOOK_NAMES:
            val = hook_dict.get(hook_name)
            if val is not None and not isinstance(val, (str, Path)):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise TypeError(f"{hook_name} must be a string{name_str}.")

        if not is_subtable:
            for alias in WINDOWS_PLATFORM_ALIASES:
                val = hook_dict.get(alias)
                if val is not None:
                    if not isinstance(val, dict):
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise TypeError(f"'{alias}' hooks sub-table must be a dictionary{name_str}.")
                    cls._validate_hook_dict(val, package_name=package_name, is_subtable=True)

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        package_name: str = ""
    ) -> "PackageHooks":
        """Parses, validates, and resolves a PackageHooks instance from a hooks dictionary.

        Args:
            data: The [hooks] dictionary.
            package_name: Optional name of the package for error messages.

        Returns:
            A validated PackageHooks instance.
        """
        hooks_dict = dict(data) if isinstance(data, dict) else {}

        # Validate top-level hooks table and any nested platform sub-tables
        cls._validate_hook_dict(hooks_dict, package_name=package_name)

        # On Windows, resolve platform-specific hook overrides from sub-tables
        effective_hooks = dict(hooks_dict)
        if sys.platform == "win32":
            for alias in WINDOWS_PLATFORM_ALIASES:
                windows_hooks = hooks_dict.get(alias)
                if isinstance(windows_hooks, dict):
                    for k, v in windows_hooks.items():
                        if k in LIFECYCLE_HOOK_NAMES or k == "timeout":
                            effective_hooks[k] = v
                    break

        raw_timeout = effective_hooks.get("timeout", DEFAULT_HOOK_TIMEOUT)
        if isinstance(raw_timeout, str) and raw_timeout.isdigit():
            raw_timeout = int(raw_timeout)
        if not isinstance(raw_timeout, int):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise TypeError(f"timeout must be an integer{name_str}.")
        if raw_timeout <= 0:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ValueError(f"timeout must be a positive integer{name_str}.")

        hooks = cls(
            probe=normalize_hook_value(effective_hooks.get("probe")),
            pre_source=normalize_hook_value(effective_hooks.get("pre_source")),
            pre_install=normalize_hook_value(effective_hooks.get("pre_install")),
            post_install=normalize_hook_value(effective_hooks.get("post_install")),
            pre_update=normalize_hook_value(effective_hooks.get("pre_update")),
            post_update=normalize_hook_value(effective_hooks.get("post_update")),
            pre_uninstall=normalize_hook_value(effective_hooks.get("pre_uninstall")),
            post_uninstall=normalize_hook_value(effective_hooks.get("post_uninstall")),
            post_render=normalize_hook_value(effective_hooks.get("post_render")),
            health=normalize_hook_value(effective_hooks.get("health")),
            timeout=raw_timeout
        )
        hooks.validate(package_name)
        return hooks

    def get_configured_hook_paths(self) -> Set[str]:
        """Returns a set of all normalized relative POSIX path strings for configured hooks."""
        paths = set()
        for hook_name in LIFECYCLE_HOOK_NAMES:
            val = getattr(self, hook_name, None)
            if val is not None:
                paths.add(val.as_posix())
        return paths

    def trigger(self, hook_name: str, hook_base_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Executes a package lifecycle hook script if specified and found."""
        if no_hooks:
            return HookResult.skipped(
                package=self._package_config.name if self._package_config else "",
                hook_name=hook_name,
                cwd=cwd,
                hook_base_dir=hook_base_dir
            )
        from .lifecycle_hooks import trigger_package_lifecycle_hook
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        return trigger_package_lifecycle_hook(
            pkg=self._package_config.name,
            hook_name=hook_name,
            metadata=self._package_config,
            hook_base_dir=hook_base_dir,
            cwd=cwd
        )

    def trigger_probe(
        self,
        package_dir: Path,
        workspace_config: "WorkspaceConfig",
        no_hooks: bool = False
    ) -> HookResult:
        """Executes the probe hook with template rendering into render sandbox directory."""
        pkg_name = self._package_config.name if self._package_config else ""
        if no_hooks or not self.probe:
            return HookResult.skipped(package=pkg_name, hook_name="probe", cwd=package_dir, hook_base_dir=package_dir)
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        from .lifecycle_hooks import trigger_probe_lifecycle_hook
        return trigger_probe_lifecycle_hook(
            workspace_config=workspace_config,
            package_name=self._package_config.name,
            pkg_config=self._package_config,
            load_envs=False,
            no_hooks=no_hooks
        )

    def trigger_pre_source(
        self,
        source_dir: Path,
        workspace_config: "WorkspaceConfig",
        no_hooks: bool = False
    ) -> HookResult:
        """Triggers the pre_source hook with workspace template rendering into the render sandbox directory."""
        return self.trigger_pre_source_with_render(
            source_dir=source_dir,
            workspace_config=workspace_config,
            no_hooks=no_hooks
        )

    def trigger_pre_source_with_render(
        self,
        source_dir: Path,
        workspace_config: "WorkspaceConfig",
        no_hooks: bool = False
    ) -> HookResult:
        """Triggers the pre_source hook with workspace template rendering into the render sandbox directory."""
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        from .lifecycle_hooks import trigger_pre_source_lifecycle_hook
        return trigger_pre_source_lifecycle_hook(
            workspace_config=workspace_config,
            package_name=self._package_config.name,
            pkg_config=self._package_config,
            load_envs=False,
            no_hooks=no_hooks
        )

    def trigger_pre_source_without_render(
        self,
        source_dir: Path,
        no_hooks: bool = False
    ) -> HookResult:
        """Triggers the pre_source hook directly inside source_dir without workspace template rendering."""
        return self.trigger("pre_source", hook_base_dir=source_dir, cwd=source_dir, no_hooks=no_hooks)

    def trigger_post_render(self, render_dir: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the post_render hook inside render_dir."""
        return self.trigger("post_render", hook_base_dir=render_dir, cwd=render_dir, no_hooks=no_hooks)

    def trigger_pre_install(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the pre_install hook."""
        return self.trigger("pre_install", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_post_install(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the post_install hook."""
        return self.trigger("post_install", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_pre_update(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the pre_update hook."""
        return self.trigger("pre_update", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_post_update(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the post_update hook."""
        return self.trigger("post_update", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_pre_uninstall(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the pre_uninstall hook.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        return self.trigger("pre_uninstall", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_post_uninstall(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the post_uninstall hook.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        return self.trigger("post_uninstall", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

    def trigger_health(self, install_dir: Path, cwd: Path, no_hooks: bool = False) -> HookResult:
        """Triggers the health probe hook."""
        return self.trigger("health", hook_base_dir=install_dir, cwd=cwd, no_hooks=no_hooks)

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


def parse_package_env_tables(env_data: Any, package_name: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Parses package environment configuration tables into (override_map, fallback_map)."""
    override_map: Dict[str, str] = {}
    fallback_map: Dict[str, str] = {}
    if isinstance(env_data, dict):
        for k, v in env_data.items():
            if k in ("override", "overwrite"):
                if not isinstance(v, dict):
                    raise ConfigError(f"[env.{k}] must be a table of key-value pairs for package '{package_name}'.")
                for sub_k, sub_v in v.items():
                    override_map[str(sub_k)] = str(sub_v)
            elif k == "fallback":
                if not isinstance(v, dict):
                    raise ConfigError(f"[env.fallback] must be a table of key-value pairs for package '{package_name}'.")
                for sub_k, sub_v in v.items():
                    fallback_map[str(sub_k)] = str(sub_v)
            else:
                if isinstance(v, dict):
                    raise ConfigError(f"Unknown sub-table [env.{k}] for package '{package_name}'.")
                override_map[str(k)] = str(v)
    elif env_data:
        raise ConfigError(f"[env] section must be a table for package '{package_name}'.")

    return override_map, fallback_map


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/drift_package.toml."""
    name: str
    source_files: List[Path] = field(default_factory=list)
    source_directory: Path = field(default_factory=lambda: Path("."))
    enable_render: bool = True
    enable_install: bool = True
    install_method: Optional[str] = None
    target_directory: Optional[Path] = None
    target_directory_windows: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    hooks: PackageHooks = field(default_factory=PackageHooks)
    requirements: PackageRequirements = field(default_factory=PackageRequirements)

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
        source_directory: Optional[Union[str, Path]] = None,
        enable_render: bool = True,
        enable_install: bool = True,
        install_method: Optional[str] = None,
        target_directory: Optional[Path] = None,
        target_directory_windows: Optional[Path] = None,
        sudo: bool = False,
        fully_controlled_dirs: Optional[List[Path]] = None,
        hooks: Optional[PackageHooks] = None,
        requirements: Optional[PackageRequirements] = None,
        env_override: Optional[Dict[str, str]] = None,
        env_fallback: Optional[Dict[str, str]] = None,
    ) -> None:
        self.name = name
        self.source_files = source_files if source_files is not None else []
        self.source_directory = Path(source_directory) if source_directory else Path(".")
        self.enable_render = enable_render
        self.enable_install = enable_install
        self.install_method = install_method
        self.target_directory = expand_user_and_env(target_directory) if target_directory else None
        self.target_directory_windows = expand_user_and_env(target_directory_windows) if target_directory_windows else None
        self.sudo = sudo
        self.fully_controlled_dirs = fully_controlled_dirs if fully_controlled_dirs is not None else []
        self.hooks = hooks if hooks is not None else PackageHooks()
        self.hooks.package_config = self
        self.requirements = requirements if requirements is not None else PackageRequirements()
        self.env_override: Dict[str, str] = {str(k): str(v) for k, v in env_override.items()} if env_override else {}
        self.env_fallback: Dict[str, str] = {str(k): str(v) for k, v in env_fallback.items()} if env_fallback else {}

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
        if not isinstance(self.source_directory, Path):
            raise TypeError(f"source_directory must be a Path for package '{self.name}'.")
        if self.source_directory.is_absolute():
            raise ConfigError(f"Package '{self.name}' source_directory '{self.source_directory}' must be a relative path.")
        if not isinstance(self.hooks, PackageHooks):
            raise TypeError(f"hooks must be a PackageHooks instance for package '{self.name}'.")
        self.hooks.validate(self.name)
        if not isinstance(self.requirements, PackageRequirements):
            raise TypeError(f"requirements must be a PackageRequirements instance for package '{self.name}'.")
        if not isinstance(self.env_override, dict):
            raise TypeError(f"env_override must be a dictionary for package '{self.name}'.")
        if not isinstance(self.env_fallback, dict):
            raise TypeError(f"env_fallback must be a dictionary for package '{self.name}'.")

    def evaluate_requirements(
        self,
        workspace_config: WorkspaceConfig,
        no_hooks: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """Evaluates declarative requirements and dynamic probe hooks for this package.

        Returns:
            Tuple of (is_satisfied: bool, failure_reason: Optional[str]).
        """
        # 1. Declarative requirements check
        is_satisfied, reason = self.requirements.check_requirements()
        if not is_satisfied:
            return False, reason

        # 2. Dynamic probe hook check (if configured and hooks enabled)
        if self.hooks.probe is not None and not no_hooks:
            src_pkg_dir = workspace_config.source_path / self.name
            with self.package_envs(workspace_config):
                res = self.hooks.trigger_probe(
                    package_dir=src_pkg_dir,
                    workspace_config=workspace_config,
                    no_hooks=no_hooks
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

        Raises:
            ConfigError: If source_directory is absolute or escapes package_dir.
        """
        if not self.source_directory or self.source_directory == Path(".") or str(self.source_directory) in (".", ""):
            return package_dir
        rel = self.source_directory
        if rel.is_absolute():
            raise ConfigError(
                f"Package '{self.name}' source_directory '{self.source_directory}' must be a relative path, not absolute."
            )
        # Note: We validate using os.path.normpath rather than Path.resolve().
        # On macOS (APFS firmlink architecture), calling .resolve() dereferences standard
        # root paths like /home or /tmp into /System/Volumes/Data/home or /private/tmp,
        # which mutates path prefixes unexpectedly and breaks prefix consistency.
        norm_rel = os.path.normpath(str(rel))
        if norm_rel == ".." or norm_rel.startswith(".." + os.sep) or norm_rel.startswith("../"):
            raise ConfigError(
                f"Package '{self.name}' source_directory '{self.source_directory}' escapes package root '{package_dir}'."
            )
        return package_dir / rel

    def get_target_directory(self, workspace_config: WorkspaceConfig) -> Path:
        if sys.platform == "win32" and self.target_directory_windows is not None:
            return expand_user_and_env(self.target_directory_windows)
        return expand_user_and_env(self.target_directory or workspace_config.default_target_path)

    def get_install_method(self, workspace_config: WorkspaceConfig) -> str:
        if sys.platform == "win32":
            return "copy"
        return self.install_method or workspace_config.default_install_method

    def load_package_envs(
        self,
        workspace_config: WorkspaceConfig,
        overwrite: bool = True
    ) -> Dict[str, Optional[str]]:
        """Loads package-specific environment variables into os.environ across tiers.

        Preemption order within package scope:
        - Tier 1: CLI / Host Shell (preserved via INITIAL_ENV)
        - Tier 2: Package [env.override] (overwrites lower tiers unless in INITIAL_ENV)
        - Tier 3: drift_package_* facts (overwrites lower tiers unless in INITIAL_ENV)
        - Tier 7: Package [env.fallback] (fills unset blanks only)

        Variables loaded:
            drift_package_name: Name of the package (directory name).
            drift_package_target_dir: Resolved absolute target directory path on the host system.
            drift_package_source_dir: Absolute path to the package's source directory in workspace.
            drift_package_render_dir: Absolute path to the package's compiled sandbox directory.
            drift_package_install_dir: Absolute path to the package's state database directory.
            drift_package_install_method: Resolved install method ('stow' or 'copy').

        Returns:
            A snapshot dictionary mapping modified keys to their original values.
        """
        from .constants import INITIAL_ENV
        from .env_utils import load_env_settings

        target_dir = self.get_target_directory(workspace_config)
        target_dir_str = str(target_dir)
        install_method_str = self.get_install_method(workspace_config)
        source_dir_str = str(workspace_config.source_path / self.name)
        render_dir_str = str(workspace_config.render_path / self.name)
        install_dir_str = str(workspace_config.install_path / self.name)

        pkg_facts = {
            "drift_package_name": self.name,
            "drift_package_target_dir": target_dir_str,
            "drift_package_source_dir": source_dir_str,
            "drift_package_render_dir": render_dir_str,
            "drift_package_install_dir": install_dir_str,
            "drift_package_install_method": install_method_str,
        }

        saved_envs: Dict[str, Optional[str]] = {}

        # 1. Tier 7: Load env_fallback without overwrite (only filling unset blanks)
        if self.env_fallback:
            for k, v in load_env_settings(self.env_fallback, overwrite=False).items():
                saved_envs.setdefault(k, v)

        # 2. Tier 3: Load package facts with overwrite enabled (preserving INITIAL_ENV)
        for k, v in load_env_settings(pkg_facts, overwrite=True, env_keep=INITIAL_ENV).items():
            saved_envs.setdefault(k, v)

        # 3. Tier 2: Load env_override with overwrite enabled (preserving INITIAL_ENV)
        if self.env_override:
            for k, v in load_env_settings(self.env_override, overwrite=True, env_keep=INITIAL_ENV).items():
                saved_envs.setdefault(k, v)

        return saved_envs

    def unload_package_envs(
        self,
        original_envs: Optional[Mapping[str, Optional[str]]]
    ) -> None:
        """Restores original environment variables using the snapshot returned by load_package_envs."""
        from .env_utils import unload_env_settings
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
        # Error for unknown top-level sections
        known_top_sections = {"package", "hooks", "env", "requirements"}
        for key in data:
            if key not in known_top_sections:
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"Unknown top-level package config section: '{key}'{name_str}")

        package_data = data.get("package", {})
        hooks_data = data.get("hooks", {})
        env_data = data.get("env", {})

        name = package_name
        if not name:
            raise ValueError("Package name must be provided when constructing PackageConfig.")

        # Error for unknown package options
        known_package_keys = {
            "name",
            "source_directory",
            "enable_render",
            "enable_install",
            "install_method",
            "target_directory",
            "sudo",
            "fully_controlled_dirs",
            "requirements"
        } | {f"target_directory_{alias}" for alias in WINDOWS_PLATFORM_ALIASES}
        for key in package_data:
            if key not in known_package_keys:
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"Unknown package option: '{key}'{name_str}")

        # Parse, validate, and resolve lifecycle hooks via PackageHooks.from_dict
        hooks = PackageHooks.from_dict(
            data.get("hooks", {}),
            package_name=str(name)
        )

        # Parse declarative requirements ([package.requirements] or top-level [requirements])
        req_data = package_data.get("requirements") or data.get("requirements") or {}
        requirements = PackageRequirements.from_dict(req_data, package_name=str(name))

        # Parse package environment variables ([env.override], [env.overwrite], [env.fallback])
        override_map, fallback_map = parse_package_env_tables(env_data, package_name=str(name))

        fcd = package_data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd = [fcd]
        elif not isinstance(fcd, list):
            fcd = []

        # Parse source_directory if provided
        src_dir_val = package_data.get("source_directory")
        if src_dir_val is not None:
            if not isinstance(src_dir_val, (str, Path)):
                raise TypeError(f"source_directory must be a string for package '{name}'.")
            source_dir = Path(str(src_dir_val).strip())
        else:
            source_dir = Path(".")

        # Expand home directory and env vars for target_directory on load
        target_dir = package_data.get("target_directory")
        if target_dir:
            target_dir = expand_user_and_env(target_dir)

        target_dir_windows = None
        for alias in WINDOWS_PLATFORM_ALIASES:
            val = package_data.get(f"target_directory_{alias}")
            if val:
                target_dir_windows = expand_user_and_env(val)
                break

        config = cls(
            name=str(name),
            source_directory=source_dir,
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=package_data.get("install_method"),
            target_directory=target_dir,
            target_directory_windows=target_dir_windows,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[Path(d) for d in fcd],
            hooks=hooks,
            requirements=requirements,
            env_override=override_map,
            env_fallback=fallback_map
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
    try:
        config = PackageConfig.from_dict(data, package_name=pkg_name, source_files=[package_toml_path])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid package configuration for '{pkg_name}' in '{package_toml_path}': {e}") from e
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

        from .constants import INITIAL_ENV
        from .env_utils import env_scope

        pkg_envs = {
            "drift_package_name": package_name,
            "drift_package_source_dir": str(workspace_config.source_path / package_name),
            "drift_package_render_dir": str(workspace_config.render_path / package_name),
            "drift_package_install_dir": str(workspace_config.install_path / package_name),
        }

        with env_scope(pkg_envs, overwrite=True, env_keep=INITIAL_ENV):
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
    """
    Loads package configuration from a package directory,
    including its local override if present,
    optionally rendering it if it is a template.
    """
    pkg_name = package_dir.name
    if workspace_config is None:
        logger.warning("WorkspaceConfig is not provided. Falling back to static loading without rendering.")
        # Fallback for backward compatibility/static loading without workspace settings
        base_dict, base_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_FILE_NAME_LIST)
        if not base_dict:
            raise FileNotFoundError(f"'{PACKAGE_CONFIG_FILE_NAME}' not found in directory: {package_dir}")
        local_dict, local_path = locate_load_package_config_file_static(package_dir, PACKAGE_CONFIG_LOCAL_FILE_NAME_LIST)
        combined_dict = merge_toml(base_dict, local_dict)
        try:
            return PackageConfig.from_dict(combined_dict,
                                           package_name=pkg_name,
                                           source_files=[base_path, local_path])
        except (TypeError, ValueError) as e:
            raise ConfigError(f"Invalid package configuration for '{pkg_name}' in '{package_dir}': {e}") from e

    # With workspace_config provided, we can render templates if needed.
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
    if base_info and base_info.path.exists():
        try:
            shutil.copymode(base_info.path, output_file_path)
        except Exception:
            pass

    # Load from the rendered path
    try:
        config = PackageConfig.from_dict(combined_dict,
                                         package_name=pkg_name,
                                         source_files=source_files)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid package configuration for '{pkg_name}' in '{package_dir}': {e}") from e
    return config

