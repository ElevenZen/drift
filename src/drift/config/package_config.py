"""Package-specific configuration loading and metadata parsing using pathlib."""

import logging
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import (
    ClassVar,
    Iterable,
    List,
    Sequence,
    Optional,
    Tuple,
    Dict,
    Iterator,
    Any,
    Union,
    Set,
    Mapping,
    TYPE_CHECKING,
)
from ..utils.toml_utils import parse_toml, merge_toml, dump_toml, get_first_from, validate_known_keys

if TYPE_CHECKING:
    from ..hooks.lifecycle_hooks import HookExecFlags

from ..core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME_LIST,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    LIFECYCLE_HOOK_NAMES,
    INSTALLATION_HOOK_NAMES,
    HOOK_CONFIG_OPTION_SET,
    WINDOWS_PLATFORM_ALIASES,
    DEFAULT_HOOK_TIMEOUT,
    INITIAL_ENV,
    DRIFT_SYSTEM_FACT_KEYS,
    InstallMethod,
)
from .workspace_config import RenderEngineConfig, WorkspaceConfig
from .render_engine_config import RenderEngineRegistry
from ..utils.env_utils import resolve_env_references, interpolate_config_dict, update_env_dict, load_env_settings
from ..core.exceptions import ConfigError
from ..utils.path_utils import expand_path, is_relative_to
from ..core.result_models import HookResult

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def normalize_hook_value(
    val: Optional[Union[str, Path]],
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Normalizes a lifecycle hook configuration value to Optional[Path].

    Returns None if val is None, empty string "", or "disable" / "disabled" (case-insensitive).
    If base_dir is provided and the path is relative, resolves to absolute Path.
    """
    if val is None:
        return None
    if isinstance(val, (str, Path)):
        s = str(val).strip()
        if s == "" or s.lower() in ("disable", "disabled"):
            return None
        p = Path(s)
        if p.is_absolute() or base_dir is None:
            return p
        return (Path(base_dir) / p).resolve()
    raise ConfigError(f"Hook value must be a string or Path, got {type(val).__name__}")


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
        except (ValueError, TypeError):
            pass
    return False


def match_ip_addresses(patterns: Iterable[str], host_ips: Iterable[str]) -> bool:
    """Returns True if any host IP matches any of the given IP patterns."""
    resolved_host_ips = tuple(host_ips)
    return any(
        match_ip_address(p, ip)
        for p in patterns
        for ip in resolved_host_ips
    )


@dataclass
class PackageRequirements:
    """Declarative host platform and environment requirements for a package."""
    IP_KEYS: ClassVar[Tuple[str, ...]] = ("ip", "ips", "ip_addresses")
    KNOWN_KEYS: ClassVar[Tuple[str, ...]] = (
        "os",
        "arch",
        "distro",
        "binaries",
        "env",
        *IP_KEYS,
    )

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
        from ..utils.host_facts import get_host_os, get_host_arch, get_host_distro

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
                from ..utils.host_facts import get_host_ip_addresses
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

        name_str = f" for package '{package_name}'" if package_name else ""
        validate_known_keys(data, cls.KNOWN_KEYS, context="requirements", suffix=name_str)

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
                        raise ConfigError(f"Items in '{field_name}' must be strings{name_str}.")
                    s = item.strip()
                    if s:
                        res.append(s)
                return res
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"'{field_name}' under requirements must be a string or list of strings{name_str}.")

        raw_ip = get_first_from(data, cls.IP_KEYS)

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
    rollback_on_failure: Union[bool, List[str]] = True
    _relative_paths: Dict[str, Optional[Path]] = field(default_factory=dict, repr=False)
    _package_config: Optional["PackageConfig"] = field(default=None, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in LIFECYCLE_HOOK_NAMES:
            value = normalize_hook_value(value)
        super().__setattr__(name, value)

    def get_relative_path(self, hook_name: str) -> Optional[Path]:
        """Returns the relative path of a package-internal hook, or None if external/unset."""
        return self._relative_paths.get(hook_name)

    @property
    def configured_relative_paths(self) -> Set[str]:
        """Returns the set of POSIX relative path strings for all package-internal hooks."""
        return {p.as_posix() for p in self._relative_paths.values() if p is not None}

    @property
    def package_config(self) -> Optional["PackageConfig"]:
        return self._package_config

    @package_config.setter
    def package_config(self, value: Optional["PackageConfig"]) -> None:
        self._package_config = value

    def should_rollback_on_failure(self, hook_name: str) -> bool:
        """Returns whether failure of the specified hook requires a system rollback."""
        if hook_name not in INSTALLATION_HOOK_NAMES:
            return False
        if isinstance(self.rollback_on_failure, bool):
            return self.rollback_on_failure
        if isinstance(self.rollback_on_failure, (list, tuple, set)):
            return hook_name in self.rollback_on_failure
        return True

    def validate(self, package_name: str = "") -> None:
        """Validates hook configurations."""
        for hook_name in LIFECYCLE_HOOK_NAMES:
            val = getattr(self, hook_name)
            if val is not None:
                if not isinstance(val, Path):
                    name_str = f" for package '{package_name}'" if package_name else ""
                    raise ConfigError(f"{hook_name} must be a Path{name_str}.")
                if not val.is_absolute():
                    name_str = f" for package '{package_name}'" if package_name else ""
                    raise ConfigError(f"{hook_name} must be an absolute Path{name_str}.")
        if not isinstance(self.timeout, int):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"timeout must be an integer{name_str}.")
        if self.timeout <= 0:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"timeout must be a positive integer{name_str}.")
        if not isinstance(self.rollback_on_failure, bool):
            if isinstance(self.rollback_on_failure, (list, tuple, set)):
                for h in self.rollback_on_failure:
                    if not isinstance(h, str) or h not in INSTALLATION_HOOK_NAMES:
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise ConfigError(
                            f"Invalid hook name '{h}' in rollback_on_failure. "
                            f"Allowed installation hooks: {', '.join(INSTALLATION_HOOK_NAMES)}{name_str}."
                        )
            else:
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"rollback_on_failure must be a boolean or list of hook names{name_str}.")

    @classmethod
    def _validate_hook_dict(
        cls,
        hook_dict: Dict[str, Any],
        package_name: str = "",
        is_subtable: bool = False
    ) -> None:
        """Helper to validate unknown keys, value types, and platform sub-tables in a hook dictionary."""
        known_keys = set(HOOK_CONFIG_OPTION_SET)
        if not is_subtable:
            known_keys |= set(WINDOWS_PLATFORM_ALIASES)

        name_str = f" for package '{package_name}'" if package_name else ""
        context = "package [hooks]" if not is_subtable else "platform hooks sub-table"
        validate_known_keys(
            hook_dict,
            known_keys,
            message_prefix=f"Unknown hook option in {context}",
            suffix=name_str,
        )

        for hook_name in LIFECYCLE_HOOK_NAMES:
            val = hook_dict.get(hook_name)
            if val is not None and not isinstance(val, (str, Path)):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"{hook_name} must be a string{name_str}.")

        if "rollback_on_failure" in hook_dict:
            val = hook_dict["rollback_on_failure"]
            if not isinstance(val, bool) and not isinstance(val, (list, tuple, set)):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(f"'rollback_on_failure' must be a boolean or list of hook names{name_str}.")
            if isinstance(val, (list, tuple, set)):
                for h in val:
                    if not isinstance(h, str) or h not in INSTALLATION_HOOK_NAMES:
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise ConfigError(
                            f"Invalid hook name '{h}' in 'rollback_on_failure'. "
                            f"Allowed installation hooks: {', '.join(INSTALLATION_HOOK_NAMES)}{name_str}."
                        )

        if not is_subtable:
            for alias in WINDOWS_PLATFORM_ALIASES:
                val = hook_dict.get(alias)
                if val is not None:
                    if not isinstance(val, dict):
                        name_str = f" for package '{package_name}'" if package_name else ""
                        raise ConfigError(f"'{alias}' hooks sub-table must be a dictionary{name_str}.")
                    cls._validate_hook_dict(val, package_name=package_name, is_subtable=True)

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        package_name: str = "",
        base_dir: Optional[Path] = None,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageHooks":
        """Parses, validates, and resolves a PackageHooks instance from a hooks dictionary.

        Relationship and Meaning of base_dir vs. workspace_config:
            - workspace_config (Priority Context): When provided (with package_name), it supplies
              the full multi-stage workspace directory layout (source_path, render_path, install_path).
              Lifecycle hooks are bound to their respective canonical execution stage base directories:
                * probe, pre_source -> source directory (<workspace.source_path>/<package_name>)
                * post_render       -> render sandbox (<workspace.render_path>/<package_name>)
                * pre_install, post_install, pre_update, post_update, pre_uninstall,
                  post_uninstall, health -> install directory (<workspace.install_path>/<package_name>)
              Additionally, <workspace.source_path>/<package_name> defines the package source directory
              used to detect and normalize absolute hook paths pointing inside the package source tree into
              package-relative paths.
            - base_dir (Fallback Directory): Used as the fallback package directory when workspace_config
              is None (e.g., during isolated testing or standalone package parsing). When workspace_config
              is absent, all relative hook paths resolve uniformly against base_dir, and base_dir serves
              as the reference directory for detecting package-internal absolute paths.
            - Precedence: When both are provided, workspace_config takes precedence for multi-stage base
              mapping, while base_dir acts as a secondary fallback. If neither is provided and any hook
              specifies a relative path, a ConfigError is raised.

        Args:
            data: The [hooks] dictionary.
            package_name: Optional name of the package for error messages and stage path resolution.
            base_dir: Optional fallback package directory for resolving relative paths when workspace_config is absent.
            workspace_config: Optional WorkspaceConfig providing multi-stage directory paths and layout.

        Returns:
            A validated PackageHooks instance with canonical absolute execution Paths and auxiliary relative paths.
        """
        # Validate top-level hooks table and any nested platform sub-tables
        effective_hooks = data
        cls._validate_hook_dict(effective_hooks, package_name=package_name)

        # On Windows, resolve platform-specific hook overrides from sub-tables
        if sys.platform == "win32":
            win_hooks = get_first_from(data, WINDOWS_PLATFORM_ALIASES, default={})
            for k, v in win_hooks.items():
                if k in HOOK_CONFIG_OPTION_SET:
                    effective_hooks[k] = v

        raw_timeout = effective_hooks.get("timeout", DEFAULT_HOOK_TIMEOUT)
        if isinstance(raw_timeout, str) and raw_timeout.isdigit():
            raw_timeout = int(raw_timeout)
        if not isinstance(raw_timeout, int):
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"timeout must be an integer{name_str}.")
        if raw_timeout <= 0:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"timeout must be a positive integer{name_str}.")

        raw_rollback = effective_hooks.get("rollback_on_failure")
        if raw_rollback is None:
            resolved_rollback: Union[bool, List[str]] = True
        elif isinstance(raw_rollback, bool):
            resolved_rollback = raw_rollback
        elif isinstance(raw_rollback, (list, tuple, set)):
            resolved_rollback = [str(x).strip() for x in raw_rollback]
        else:
            name_str = f" for package '{package_name}'" if package_name else ""
            raise ConfigError(f"rollback_on_failure must be a boolean or list of hook names{name_str}.")

        if workspace_config is not None and package_name:
            package_src_dir = (workspace_config.source_path / package_name).resolve()
            render_base = (workspace_config.render_path / package_name).resolve()
            install_base = (workspace_config.install_path / package_name).resolve()
            hook_base_map: Dict[str, Path] = {
                "probe": render_base,
                "pre_source": render_base,
                "post_render": render_base,
                "pre_install": install_base,
                "post_install": install_base,
                "pre_update": install_base,
                "post_update": install_base,
                "pre_uninstall": install_base,
                "post_uninstall": install_base,
                "health": install_base,
            }
        elif base_dir is not None:
            package_src_dir = Path(base_dir).resolve()
            hook_base_map = {hook_name: package_src_dir for hook_name in LIFECYCLE_HOOK_NAMES}
        else:
            package_src_dir = None
            hook_base_map = {}

        def _process_hook(hook_name: str) -> Tuple[Optional[Path], Optional[Path]]:
            raw_val = effective_hooks.get(hook_name)
            norm_val = normalize_hook_value(raw_val)
            if norm_val is None:
                return None, None

            if norm_val.is_absolute():
                p_res = norm_val.resolve()
                if package_src_dir is not None and is_relative_to(p_res, package_src_dir):
                    norm_val = p_res.relative_to(package_src_dir)
                else:
                    return p_res, None
            else:
                if not hook_base_map:
                    name_str = f" for package '{package_name}'" if package_name else ""
                    raise ConfigError(
                        f"base_dir or workspace_config must be provided when constructing PackageHooks{name_str}."
                    )

            norm_val = Path(os.path.normpath(str(norm_val)))
            if not is_relative_to(norm_val, Path(DRIFT_HOOKS_DIR_NAME)):
                name_str = f" for package '{package_name}'" if package_name else ""
                raise ConfigError(
                    f"Lifecycle hook '{hook_name}' path '{raw_val}'{name_str} must be located within '{DRIFT_HOOKS_DIR_NAME}/' directory. "
                    f"If sharing hooks across packages, create a symlink inside '{DRIFT_HOOKS_DIR_NAME}/'."
                )

            sub_rel = norm_val.relative_to(DRIFT_HOOKS_DIR_NAME)
            stage_base = hook_base_map[hook_name]
            canonical_path = (stage_base / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / sub_rel).resolve()
            return canonical_path, norm_val

        proc_results = {h: _process_hook(h) for h in LIFECYCLE_HOOK_NAMES}
        abs_hooks = {h: res[0] for h, res in proc_results.items()}
        rel_hooks = {h: res[1] for h, res in proc_results.items()}

        hooks = cls(
            probe=abs_hooks.get("probe"),
            pre_source=abs_hooks.get("pre_source"),
            pre_install=abs_hooks.get("pre_install"),
            post_install=abs_hooks.get("post_install"),
            pre_update=abs_hooks.get("pre_update"),
            post_update=abs_hooks.get("post_update"),
            pre_uninstall=abs_hooks.get("pre_uninstall"),
            post_uninstall=abs_hooks.get("post_uninstall"),
            post_render=abs_hooks.get("post_render"),
            health=abs_hooks.get("health"),
            timeout=raw_timeout,
            rollback_on_failure=resolved_rollback,
            _relative_paths=rel_hooks,
        )
        hooks.validate(package_name)
        return hooks

    def get_configured_hook_paths(
        self,
        relative_to: Optional[Union[Path, Sequence[Path]]] = None
    ) -> Set[str]:
        """Returns a set of normalized POSIX path strings for configured hooks.

        If a hook path is inside one of the `relative_to` bases, its relative POSIX path
        is returned; otherwise, its absolute POSIX path is returned.
        """
        bases: List[Path] = (
            [Path(relative_to)] if isinstance(relative_to, (str, Path))
            else [Path(b) for b in relative_to] if isinstance(relative_to, (list, tuple, set))
            else []
        )

        def _resolve_hook_path_strings(val: Path) -> Set[str]:
            matching_rel_paths = {
                val.relative_to(b).as_posix()
                for b in bases
                if is_relative_to(val, b)
            }
            return matching_rel_paths or {val.as_posix()}

        configured_hooks = filter(None, (getattr(self, h, None) for h in LIFECYCLE_HOOK_NAMES))
        return {
            p
            for hook_val in configured_hooks
            for p in _resolve_hook_path_strings(hook_val)
        }

    def trigger(
        self,
        hook_name: str,
        cwd: Optional[Path] = None,
        flags: Optional["HookExecFlags"] = None,
    ) -> HookResult:
        """Executes a package lifecycle hook script if specified and found."""
        from ..hooks.lifecycle_hooks import HookExecFlags, trigger_package_hook

        exec_flags = HookExecFlags.resolve(flags=flags)
        if exec_flags.no_hooks:
            return HookResult.skipped(
                package=self._package_config.name if self._package_config else "",
                hook_name=hook_name,
                cwd=cwd,
            )
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        return trigger_package_hook(
            pkg=self._package_config.name,
            hook_name=hook_name,
            metadata=self._package_config,
            cwd=cwd,
            flags=exec_flags,
        )

    def trigger_probe(
        self,
        workspace_config: "WorkspaceConfig",
        flags: Optional["HookExecFlags"] = None,
    ) -> HookResult:
        """Executes the probe hook with template rendering into render sandbox directory."""
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        from ..hooks.lifecycle_hooks import trigger_probe_hook
        return trigger_probe_hook(
            workspace_config=workspace_config,
            package_name=self._package_config.name,
            flags=flags,
            pkg_config_override=self._package_config,
        )

    def trigger_pre_source(
        self,
        workspace_config: "WorkspaceConfig",
        flags: Optional["HookExecFlags"] = None,
    ) -> HookResult:
        """Triggers the pre_source hook with workspace template rendering into the render sandbox directory."""
        return self.trigger_pre_source_with_render(
            workspace_config=workspace_config,
            flags=flags,
        )

    def trigger_pre_source_with_render(
        self,
        workspace_config: "WorkspaceConfig",
        flags: Optional["HookExecFlags"] = None,
    ) -> HookResult:
        """Triggers the pre_source hook with workspace template rendering into the render sandbox directory."""
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        from ..hooks.lifecycle_hooks import trigger_pre_source_hook
        return trigger_pre_source_hook(
            workspace_config=workspace_config,
            package_name=self._package_config.name,
            flags=flags,
            pkg_config_override=self._package_config,
        )

    def trigger_pre_source_without_render(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the pre_source hook directly inside source directory without workspace template rendering."""
        effective_cwd = cwd_override or (self.pre_source.parent if self.pre_source else Path("."))
        return self.trigger(
            "pre_source",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_post_render(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the post_render hook inside render directory."""
        effective_cwd = cwd_override or (self.post_render.parent if self.post_render else Path("."))
        return self.trigger(
            "post_render",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_pre_install(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the pre_install hook in install directory."""
        effective_cwd = cwd_override or (self.pre_install.parent if self.pre_install else Path("."))
        return self.trigger(
            "pre_install",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_post_install(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the post_install hook with uniform script-parent working directory."""
        effective_cwd = cwd_override or (self.post_install.parent if self.post_install else Path("."))
        return self.trigger(
            "post_install",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_pre_update(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the pre_update hook in install directory."""
        effective_cwd = cwd_override or (self.pre_update.parent if self.pre_update else Path("."))
        return self.trigger(
            "pre_update",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_post_update(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the post_update hook with uniform script-parent working directory."""
        effective_cwd = cwd_override or (self.post_update.parent if self.post_update else Path("."))
        return self.trigger(
            "post_update",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_pre_uninstall(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the pre_uninstall hook with uniform script-parent working directory.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        effective_cwd = cwd_override or (self.pre_uninstall.parent if self.pre_uninstall else Path("."))
        return self.trigger(
            "pre_uninstall",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_post_uninstall(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the post_uninstall hook in install directory.

        Note:
            Uninstall hooks are only triggered if the package-level configuration
            file ('drift_package.toml') is available in the install/ directory.
        """
        effective_cwd = cwd_override or (self.post_uninstall.parent if self.post_uninstall else Path("."))
        return self.trigger(
            "post_uninstall",
            cwd=effective_cwd,
            flags=flags,
        )

    def trigger_health(
        self,
        flags: Optional["HookExecFlags"] = None,
        cwd_override: Optional[Path] = None,
    ) -> HookResult:
        """Triggers the health probe hook with uniform script-parent working directory."""
        effective_cwd = cwd_override or (self.health.parent if self.health else Path("."))
        return self.trigger(
            "health",
            cwd=effective_cwd,
            flags=flags,
        )

    def assert_hooks_exist(
        self,
        base_dir: Path,
        is_source: bool,
        hook_names: Sequence[str] = ()
    ) -> None:
        """Validates that configured lifecycle hook files exist in base_dir and are regular files.

        Args:
            base_dir: Directory containing package files (e.g. src/<pkg>, render/<pkg>, or install/<pkg>).
            is_source: True if base_dir is the package source directory (src/<pkg>),
                False if base_dir is a compiled/staged directory (render/<pkg> or install/<pkg>).
            hook_names: Sequence of hook names to check. If empty/omitted, all LIFECYCLE_HOOK_NAMES are checked.

        Raises:
            FileNotFoundError: If a configured hook file does not exist.
            ValueError: If a configured hook path is not a regular file.
        """
        pkg_name = self._package_config.name if self._package_config else "unknown"
        target_hooks = hook_names if hook_names else LIFECYCLE_HOOK_NAMES
        for hook_name in target_hooks:
            hook_val = getattr(self, hook_name, None)
            if hook_val is None:
                continue
            rel_hook = self.get_relative_path(hook_name)
            if rel_hook is not None:
                if is_relative_to(rel_hook, Path(DRIFT_HOOKS_DIR_NAME)):
                    sub_rel = rel_hook.relative_to(DRIFT_HOOKS_DIR_NAME)
                    if is_source:
                        hook_path = base_dir / DRIFT_HOOKS_DIR_NAME / sub_rel
                    else:
                        hook_path = base_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / sub_rel
                else:
                    hook_path = base_dir / rel_hook
            else:
                hook_path = hook_val

            if not hook_path.exists():
                raise FileNotFoundError(
                    f"Lifecycle hook file specified for '{hook_name}' in package '{pkg_name}' does not exist: '{hook_path}'"
                )
            if not hook_path.is_file():
                raise ValueError(
                    f"Lifecycle hook path specified for '{hook_name}' in package '{pkg_name}' is not a regular file: '{hook_path}'"
                )


def parse_package_env_tables(env_data: Any, package_name: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Parses package environment configuration tables into (override_map, fallback_map, secrets_map)."""
    if not isinstance(env_data, dict):
        raise ConfigError(f"[env] section must be a table for package '{package_name}'.")

    override_map: Dict[str, str] = {}
    fallback_map: Dict[str, str] = {}
    secrets_map: Dict[str, str] = {}
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
        elif k == "secrets":
            if not isinstance(v, dict):
                raise ConfigError(f"[env.secrets] must be a table of key-value pairs for package '{package_name}'.")
            for sub_k, sub_v in v.items():
                secrets_map[str(sub_k)] = str(sub_v)
        elif isinstance(v, dict):
            raise ConfigError(
                f"Unknown sub-table [env.{k}] for package '{package_name}'. "
                f"Expected [env.override], [env.fallback], or [env.secrets]."
            )
        else:
            raise ConfigError(
                f"Direct key-value pair '{k}' in [env] is not supported for package '{package_name}'. "
                f"Please define variables under [env.override], [env.fallback], or [env.secrets]."
            )

    return override_map, fallback_map, secrets_map


def resolve_and_interpolate_package_config(
    data: dict,
    package_name: str,
    workspace_config: Optional[WorkspaceConfig] = None,
) -> dict:
    """Resolves environment variables and interpolates references across a package config dictionary.

    Follows the 7-Tier Precedence Model:
    - Tier 1: CLI / Host Shell (preserved via INITIAL_ENV)
    - Tier 2: Package [env.override] (overwrites lower tiers unless in INITIAL_ENV)
    - Tier 3: drift_package_* facts (overwrites lower tiers unless in INITIAL_ENV)
    - Tier 4: drift_* system facts (preserved via DRIFT_SYSTEM_FACT_KEYS)
    - Tier 5: Secrets (Package [env.secrets] > Workspace [env.secrets] > config/secrets.env)
    - Tier 6: Workspace [env.default]
    - Tier 7: Package [env.fallback] (fills unset blanks only)

    Args:
        data: Parsed TOML dictionary of the package configuration.
        package_name: Name of the package.
        workspace_config: Optional workspace configuration for deriving directory facts and workspace secrets.

    Returns:
        Fully interpolated and stitched configuration dictionary ready for dump_toml or PackageConfig.from_dict.
    """
    env_data = data.get("env", {})

    # 1. Parse package environment tables ([env.override], [env.overwrite], [env.fallback], [env.secrets])
    override_map, fallback_map, package_secrets = parse_package_env_tables(env_data, package_name=package_name)

    # 2. Derive package facts available during package config parsing
    pkg_facts: Dict[str, str] = {
        "drift_package_name": package_name,
    }
    if workspace_config is not None:
        pkg_facts["drift_package_source_dir"] = str(workspace_config.source_path / package_name)
        pkg_facts["drift_package_src_dir"] = str(workspace_config.source_path / package_name)
        pkg_facts["drift_package_render_dir"] = str(workspace_config.render_path / package_name)
        pkg_facts["drift_package_install_dir"] = str(workspace_config.install_path / package_name)

    protected_facts = set(INITIAL_ENV) | set(DRIFT_SYSTEM_FACT_KEYS)

    # 3. Resolve package [env.secrets] against base (Tiers 1, 4, 6 from os.environ + workspace secrets + pkg_facts)
    ws_secrets = workspace_config.secrets if workspace_config is not None else {}
    tier5_base, _ = update_env_dict(
        dict(os.environ),
        ws_secrets,
        overwrite=True,
        env_keep=protected_facts
    )
    base_for_pkg_secrets, _ = update_env_dict(
        dict(tier5_base),
        pkg_facts,
        overwrite=True,
        env_keep=INITIAL_ENV
    )
    if package_secrets:
        resolved_package_secrets = resolve_env_references(
            package_secrets,
            base_env=base_for_pkg_secrets,
            error_cls=ConfigError
        )
    else:
        resolved_package_secrets = {}

    effective_secrets = {**ws_secrets, **resolved_package_secrets}

    # 4. Resolve fallback_map and override_map respecting 7-tier precedence:
    # - Fallback base: os.environ (Tiers 1, 4, 6) + effective_secrets (Tier 5) + pkg_facts (Tier 3)
    secrets_base, _ = update_env_dict(dict(os.environ), effective_secrets, overwrite=True, env_keep=protected_facts)
    fallback_base, _ = update_env_dict(dict(secrets_base), pkg_facts, overwrite=True, env_keep=INITIAL_ENV)

    if fallback_map:
        fallback_map = resolve_env_references(fallback_map, base_env=fallback_base, error_cls=ConfigError)

    # - Override base: fallback_base + resolved fallback (where unset)
    override_base, _ = update_env_dict(dict(fallback_base), fallback_map, overwrite=False)
    if override_map:
        override_map = resolve_env_references(override_map, base_env=override_base, error_cls=ConfigError)

    # 5. Build active_pkg_env for interpolating the rest of drift_package.toml across 7 tiers:
    active_pkg_env, _ = update_env_dict(dict(override_base), override_map, overwrite=True, env_keep=INITIAL_ENV)

    # 6. Interpolate ${VAR} across all other sections of package config using combined env
    interpolated_data = interpolate_config_dict(
        data,
        env=active_pkg_env,
        exclude_keys={"env"},
        error_cls=ConfigError
    )

    env_dict = {
        k: v
        for k, v in (
            ("override", override_map),
            ("fallback", fallback_map),
            ("secrets", resolved_package_secrets),
        )
        if v
    }
    return {
        **{k: v for k, v in interpolated_data.items() if k != "env"},
        **({"env": env_dict} if env_dict else {}),
    }


@dataclass
class PackageConfig:
    """Represents the package-specific configuration inside src/<pkg>/drift_package.toml."""
    KNOWN_TOP_SECTIONS: ClassVar[Tuple[str, ...]] = (
        "package",
        "hooks",
        "env",
        "requirements",
        "render",
    )
    KNOWN_PACKAGE_KEYS: ClassVar[Tuple[str, ...]] = (
        "name",
        "source_directory",
        "enable_render",
        "enable_install",
        "install_method",
        "target_directory",
        "sudo",
        "fully_controlled_dirs",
        "requirements",
        "hook_file",
        *(f"target_directory_{alias}" for alias in WINDOWS_PLATFORM_ALIASES),
    )

    name: str
    source_files: List[Path] = field(default_factory=list)
    source_directory: Path = field(default_factory=lambda: Path("."))
    enable_render: bool = True
    enable_install: bool = True
    install_method: Optional[InstallMethod] = None
    target_directory: Optional[Path] = None
    target_directory_windows: Optional[Path] = None
    sudo: bool = False
    fully_controlled_dirs: List[Path] = field(default_factory=list)
    hooks: PackageHooks = field(default_factory=PackageHooks)
    requirements: PackageRequirements = field(default_factory=PackageRequirements)
    hook_file: Optional[Path] = None
    env_override: Dict[str, str] = field(default_factory=dict)
    env_fallback: Dict[str, str] = field(default_factory=dict)
    secrets: Dict[str, str] = field(default_factory=dict)
    render_engine_configs: RenderEngineRegistry = field(default_factory=RenderEngineRegistry)

    def assert_hooks_exist(
        self,
        base_dir: Path,
        is_source: bool,
        hook_names: Sequence[str] = ()
    ) -> None:
        """Validates that configured lifecycle hook files exist in base_dir and are regular files."""
        self.hooks.assert_hooks_exist(base_dir, is_source=is_source, hook_names=hook_names)

    def __init__(
        self,
        name: str,
        source_files: Sequence[Path] = (),
        source_directory: Optional[Union[str, Path]] = None,
        enable_render: bool = True,
        enable_install: bool = True,
        install_method: Optional[InstallMethod] = None,
        target_directory: Optional[Path] = None,
        target_directory_windows: Optional[Path] = None,
        sudo: bool = False,
        fully_controlled_dirs: Sequence[Path] = (),
        hooks: Optional[PackageHooks] = None,
        requirements: Optional[PackageRequirements] = None,
        hook_file: Optional[Union[Path, str]] = None,
        env_override: Mapping[str, str] = {},
        env_fallback: Mapping[str, str] = {},
        secrets: Mapping[str, str] = {},
        render_engine_configs: Optional[RenderEngineRegistry] = None,
    ) -> None:
        if source_directory is not None and not isinstance(source_directory, (str, Path)):
            raise ConfigError(f"source_directory must be a Path or str, got {type(source_directory).__name__}")
        if target_directory is not None and not isinstance(target_directory, (str, Path)):
            raise ConfigError(f"target_directory must be a Path or str, got {type(target_directory).__name__}")
        if target_directory_windows is not None and not isinstance(target_directory_windows, (str, Path)):
            raise ConfigError(f"target_directory_windows must be a Path or str, got {type(target_directory_windows).__name__}")
        if install_method is not None and not isinstance(install_method, InstallMethod):
            raise ConfigError(f"install_method must be an InstallMethod instance, got {type(install_method).__name__}")
        if hooks is not None and not isinstance(hooks, PackageHooks):
            raise ConfigError(f"hooks must be a PackageHooks instance, got {type(hooks).__name__}")
        if requirements is not None and not isinstance(requirements, PackageRequirements):
            raise ConfigError(f"requirements must be a PackageRequirements instance, got {type(requirements).__name__}")
        if hook_file is not None and not isinstance(hook_file, (str, Path)):
            raise ConfigError(f"hook_file must be a Path or str, got {type(hook_file).__name__}")
        if not isinstance(env_override, (dict, Mapping)):
            raise ConfigError(f"env_override must be a dictionary or Mapping, got {type(env_override).__name__}")
        if not isinstance(env_fallback, (dict, Mapping)):
            raise ConfigError(f"env_fallback must be a dictionary or Mapping, got {type(env_fallback).__name__}")
        if not isinstance(secrets, (dict, Mapping)):
            raise ConfigError(f"secrets must be a dictionary or Mapping, got {type(secrets).__name__}")
        if render_engine_configs is not None and not isinstance(render_engine_configs, RenderEngineRegistry):
            raise ConfigError(f"render_engine_configs must be a RenderEngineRegistry instance, got {type(render_engine_configs).__name__}")

        self.name = name
        self.source_files = list(source_files) if source_files else []
        self.source_directory = Path(source_directory) if source_directory else Path(".")
        self.enable_render = enable_render
        self.enable_install = enable_install
        self.install_method = install_method
        self.target_directory = expand_path(target_directory) if target_directory else None
        self.target_directory_windows = expand_path(target_directory_windows) if target_directory_windows else None
        self.sudo = sudo
        self.fully_controlled_dirs = list(fully_controlled_dirs) if fully_controlled_dirs else []
        self.hooks = hooks if hooks is not None else PackageHooks()
        self.hooks.package_config = self
        self.requirements = requirements if requirements is not None else PackageRequirements()
        self.hook_file = Path(hook_file) if hook_file is not None else None
        self.env_override = {str(k): str(v) for k, v in env_override.items()}
        self.env_fallback = {str(k): str(v) for k, v in env_fallback.items()}
        self.secrets = {str(k): str(v) for k, v in secrets.items()}
        self.render_engine_configs = render_engine_configs if render_engine_configs is not None else RenderEngineRegistry()

    def validate(self) -> None:
        """Validates configuration values."""
        if not self.name or not isinstance(self.name, str):
            raise ConfigError("Package config must have a non-empty 'name'.")
        if not isinstance(self.source_files, list):
            raise ConfigError(f"source_files must be a list for package '{self.name}'.")
        for file in self.source_files:
            if not isinstance(file, Path):
                raise ConfigError(f"source_files entries must be Path objects for package '{self.name}'.")
        if self.install_method is not None and not isinstance(self.install_method, InstallMethod):
            raise ConfigError(
                f"Invalid install_method '{self.install_method}' for package '{self.name}'. "
                "Must be an InstallMethod instance."
            )
        if not isinstance(self.enable_render, bool):
            raise ConfigError(f"enable_render must be a boolean for package '{self.name}'.")
        if not isinstance(self.enable_install, bool):
            raise ConfigError(f"enable_install must be a boolean for package '{self.name}'.")
        if not isinstance(self.sudo, bool):
            raise ConfigError(f"sudo must be a boolean for package '{self.name}'.")
        if not isinstance(self.fully_controlled_dirs, list):
            raise ConfigError(f"fully_controlled_dirs must be a list for package '{self.name}'.")
        for d in self.fully_controlled_dirs:
            if not isinstance(d, Path):
                raise ConfigError(f"fully_controlled_dirs entries must be Path objects for package '{self.name}'.")
        if not isinstance(self.source_directory, Path):
            raise ConfigError(f"source_directory must be a Path for package '{self.name}'.")
        if self.source_directory.is_absolute():
            raise ConfigError(f"Package '{self.name}' source_directory '{self.source_directory}' must be a relative path.")
        # Note: We validate using os.path.normpath rather than Path.resolve().
        # On macOS (APFS firmlink architecture), calling .resolve() dereferences standard
        # root paths like /home or /tmp into /System/Volumes/Data/home or /private/tmp,
        # which mutates path prefixes unexpectedly and breaks prefix consistency.
        norm_src = os.path.normpath(str(self.source_directory))
        if norm_src == ".." or norm_src.startswith(".." + os.sep) or norm_src.startswith("../"):
            raise ConfigError(f"Package '{self.name}' source_directory '{self.source_directory}' escapes package root.")
        if not isinstance(self.hooks, PackageHooks):
            raise ConfigError(f"hooks must be a PackageHooks instance for package '{self.name}'.")
        self.hooks.validate(self.name)
        if not isinstance(self.requirements, PackageRequirements):
            raise ConfigError(f"requirements must be a PackageRequirements instance for package '{self.name}'.")
        if self.hook_file is not None:
            if not isinstance(self.hook_file, Path):
                raise ConfigError(f"hook_file must be a Path for package '{self.name}'.")
            if not self.hook_file.is_absolute():
                raise ConfigError(f"hook_file must be an absolute Path for package '{self.name}'.")
        if not isinstance(self.env_override, dict):
            raise ConfigError(f"env_override must be a dictionary for package '{self.name}'.")
        if not isinstance(self.env_fallback, dict):
            raise ConfigError(f"env_fallback must be a dictionary for package '{self.name}'.")
        if not isinstance(self.secrets, dict):
            raise ConfigError(f"secrets must be a dictionary for package '{self.name}'.")
        if not isinstance(self.render_engine_configs, RenderEngineRegistry):
            raise ConfigError(f"render_engine_configs must be a RenderEngineRegistry for package '{self.name}'.")

    def evaluate_requirements(
        self,
        workspace_config: WorkspaceConfig,
        flags: Optional["HookExecFlags"] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Evaluates declarative requirements and dynamic probe hooks for this package.

        Returns:
            Tuple of (is_satisfied: bool, failure_reason: Optional[str]).
        """
        # 1. Declarative requirements check
        is_satisfied, reason = self.requirements.check_requirements()
        if not is_satisfied:
            return False, reason

        from ..hooks.lifecycle_hooks import HookExecFlags
        exec_flags = HookExecFlags.resolve(flags)

        # 2. Dynamic probe hook check (if configured and hooks enabled)
        if self.hooks.probe is not None and not exec_flags.no_hooks:
            res = self.hooks.trigger_probe(
                workspace_config=workspace_config,
                flags=exec_flags,
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
        """
        if not self.source_directory or self.source_directory == Path(".") or str(self.source_directory) in (".", ""):
            return package_dir
        return package_dir / self.source_directory

    def get_target_directory(self, workspace_config: WorkspaceConfig) -> Path:
        if sys.platform == "win32"and self.target_directory_windows is not None:
            return expand_path(self.target_directory_windows)
        return expand_path(self.target_directory or workspace_config.default_target_path)

    def get_install_method(self, workspace_config: WorkspaceConfig) -> InstallMethod:
        if sys.platform == "win32":
            return InstallMethod.COPY
        return self.install_method or workspace_config.workspace.default_install_method

    def get_render_engines(self, workspace_config: WorkspaceConfig) -> RenderEngineRegistry:
        """Computes effective render engines by overlaying package engines onto workspace engines."""
        return workspace_config.render_engine_configs.overlay(self.render_engine_configs)

    def package_render_engines(self, workspace_config: WorkspaceConfig) -> RenderEngineRegistry:
        """Alias for get_render_engines."""
        return self.get_render_engines(workspace_config)

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
            drift_package_source_dir (alias: drift_package_src_dir): Absolute path to the package's source directory in workspace.
            drift_package_render_dir: Absolute path to the package's compiled sandbox directory.
            drift_package_install_dir: Absolute path to the package's state database directory.
            drift_package_install_method: Resolved install method ('stow' or 'copy').

        Returns:
            A snapshot dictionary mapping modified keys to their original values.
        """
        from ..core.constants import INITIAL_ENV
        from ..utils.env_utils import load_env_settings

        target_dir = self.get_target_directory(workspace_config)
        target_dir_str = str(target_dir)
        install_method_str = str(self.get_install_method(workspace_config))
        source_dir_str = str(workspace_config.source_path / self.name)
        render_dir_str = str(workspace_config.render_path / self.name)
        install_dir_str = str(workspace_config.install_path / self.name)

        pkg_facts = {
            "drift_package_name": self.name,
            "drift_package_target_dir": target_dir_str,
            "drift_package_source_dir": source_dir_str,
            "drift_package_src_dir": source_dir_str,
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
        original_envs: Mapping[str, Optional[str]] = {}
    ) -> None:
        """Restores original environment variables using the snapshot returned by load_package_envs."""
        from ..utils.env_utils import unload_env_settings
        unload_env_settings(original_envs)

    @contextmanager
    def package_envs(
        self,
        workspace_config: WorkspaceConfig,
        overwrite: bool = True
    ) -> Iterator[None]:
        """Context manager to activate package-specific environment variables and secrets in os.environ.

        Preemption order activated within package execution scope:
        - Tier 1: CLI / Host Shell (preserved via INITIAL_ENV)
        - Tier 2: Package [env.override] (overwrites lower tiers unless in INITIAL_ENV)
        - Tier 3: drift_package_* facts (overwrites lower tiers unless in INITIAL_ENV)
        - Tier 4: drift_* system facts
        - Tier 5: Secrets (Package [env.secrets] > Workspace [env.secrets] > config/secrets.env)
        - Tier 6: Workspace [env.default]
        - Tier 7: Package [env.fallback] (fills unset blanks only)
        """
        from ..utils.env_utils import secrets_env_scope
        merged_secrets = {**workspace_config.secrets, **self.secrets}
        with secrets_env_scope(merged_secrets):
            saved_envs = self.load_package_envs(workspace_config=workspace_config, overwrite=overwrite)
            try:
                yield
            finally:
                self.unload_package_envs(saved_envs)

    @classmethod
    def from_dict(
        cls,
        data: dict,
        package_name: str,
        base_dir: Path,
        source_files: Sequence[Optional[Path]] = (),
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Builds a strongly-typed PackageConfig instance from a parsed TOML dictionary.

        Relationship and Meaning of base_dir vs. workspace_config:
            - workspace_config (Global Workspace Context): Supplies workspace-wide configuration,
              multi-stage directory layouts (source_path, render_path, install_path), workspace defaults
              (default_target_path, default_install_method), and workspace render engine registries.
              workspace_config is required for lifecycle hooks to bind and execute properly across stage sandboxes
              (probe, pre_source, post_render in render/, pre_install, post_install, pre_update, post_update,
              pre_uninstall, post_uninstall, health in install/).
            - base_dir (Local Package Directory): Required concrete directory of the package (e.g., <source_path>/<package_name>
              or <render_path>/<package_name>). It is forwarded to sub-parsers such as RenderEngineRegistry.from_dict
              to resolve relative template input_file paths, and serves as the local package directory for hook_file resolution
              and fallback hook resolution when workspace_config is absent.
            - Precedence: When workspace_config is provided (with package_name), stage-specific directories
              and the package source directory (<workspace.source_path>/<package_name>) are derived automatically
              and take precedence for multi-stage hook binding and hook_file resolution. base_dir is always required
              as the local package directory context.

        Args:
            data: Parsed configuration dictionary from drift_package.toml.
            package_name: Unique name of the package.
            base_dir: Required local directory of the package for relative asset/engine resolution.
            source_files: Candidate or loaded source configuration files.
            workspace_config: Optional WorkspaceConfig providing workspace layout, defaults, and stage paths.
                Required for lifecycle hooks to execute and bind properly across stages.

        Returns:
            A validated and initialized PackageConfig instance.
        """
        if not package_name or not isinstance(package_name, str):
            raise ConfigError("Package name must be provided when constructing PackageConfig.")
        if not isinstance(data, dict):
            raise ConfigError(f"Package configuration data must be a dictionary for package '{package_name}'.")
        if base_dir is None or not isinstance(base_dir, (str, Path)):
            raise ConfigError(f"base_dir must be provided as a Path or string when constructing PackageConfig for package '{package_name}'.")

        base_dir_path = Path(base_dir).resolve()

        # Error for unknown top-level sections
        name_str = f" for package '{package_name}'" if package_name else ""
        validate_known_keys(
            data,
            cls.KNOWN_TOP_SECTIONS,
            message_prefix="Unknown top-level package config section",
            suffix=name_str,
        )

        package_data = data.get("package", {})
        hooks_data = data.get("hooks", {})
        env_data = data.get("env", {})
        render_data = data.get("render", {})

        name = package_name

        # Error for unknown package options
        validate_known_keys(
            package_data,
            cls.KNOWN_PACKAGE_KEYS,
            message_prefix="Unknown package option",
            suffix=name_str,
        )

        # Parse package environment tables ([env.override], [env.fallback], [env.secrets])
        override_map: Dict[str, str] = {}
        fallback_map: Dict[str, str] = {}
        secrets_map: Dict[str, str] = {}
        if env_data:
            override_map, fallback_map, secrets_map = parse_package_env_tables(env_data, package_name=str(name))

        # Resolve common package base directory for hooks and render engines
        # workspace_config takes priority over base_dir, matching PackageHooks.from_dict() precedence
        if workspace_config is not None and name:
            common_base_dir = (workspace_config.source_path / name).resolve()
        else:
            common_base_dir = base_dir_path

        # Parse, validate, and resolve lifecycle hooks via PackageHooks.from_dict
        hooks = PackageHooks.from_dict(
            hooks_data,
            package_name=str(name),
            base_dir=common_base_dir,
            workspace_config=workspace_config,
        )

        # Parse declarative requirements ([package.requirements] or top-level [requirements])
        req_data = package_data.get("requirements") or data.get("requirements") or {}
        requirements = PackageRequirements.from_dict(req_data, package_name=str(name))

        # Parse render engines configurations under [render.*]
        render_engine_configs = RenderEngineRegistry.from_dict(
            render_data,
            base_dir=common_base_dir
        )

        fcd = package_data.get("fully_controlled_dirs", [])
        if isinstance(fcd, str):
            fcd = [fcd]
        elif not isinstance(fcd, list):
            fcd = []

        # Parse source_directory if provided
        src_dir_val = package_data.get("source_directory")
        if src_dir_val is not None:
            if not isinstance(src_dir_val, (str, Path)):
                raise ConfigError(f"source_directory must be a string for package '{name}'.")
            raw_str = str(src_dir_val).strip()
            source_dir = Path(raw_str) if raw_str and raw_str != "." else Path(".")
        else:
            source_dir = Path(".")

        # Expand home directory and env vars for target_directory on load
        target_dir = (val := package_data.get("target_directory")) and expand_path(val)

        target_dir_windows_raw = get_first_from(package_data,
                (f"target_directory_{alias}" for alias in WINDOWS_PLATFORM_ALIASES))
        target_dir_windows = target_dir_windows_raw and expand_path(target_dir_windows_raw)

        # resolve relative hook_file path to absolute path if common_base_dir is provided
        raw_hook_file = package_data.get("hook_file")
        if raw_hook_file is not None:
            resolved_hook_file = Path(raw_hook_file)
            if not resolved_hook_file.is_absolute() and common_base_dir is not None:
                resolved_hook_file = (common_base_dir / resolved_hook_file).resolve()
        else:
            resolved_hook_file = None

        raw_install_method = package_data.get("install_method")
        parsed_install_method: Optional[InstallMethod] = None
        if raw_install_method is not None:
            try:
                parsed_install_method = InstallMethod.from_str(raw_install_method)
            except ValueError as e:
                raise ConfigError(f"Invalid install_method '{raw_install_method}' for package '{name}'. Must be 'stow' or 'copy'.") from e

        config = cls(
            name=str(name),
            source_directory=source_dir,
            enable_render=bool(package_data.get("enable_render", True)),
            enable_install=bool(package_data.get("enable_install", True)),
            install_method=parsed_install_method,
            target_directory=target_dir,
            target_directory_windows=target_dir_windows,
            sudo=bool(package_data.get("sudo", False)),
            fully_controlled_dirs=[Path(d) for d in fcd],
            hooks=hooks,
            requirements=requirements,
            hook_file=resolved_hook_file,
            env_override=override_map,
            env_fallback=fallback_map,
            secrets=secrets_map,
            render_engine_configs=render_engine_configs,
        )
        if source_files:
            config.source_files = [x for x in source_files if isinstance(x, Path)]
        config.validate()
        return config

    @classmethod
    def from_source_dir(
        cls,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ) -> "PackageConfig":
        """Loads and resolves package configuration from a package source directory."""
        return load_package_config_from_source_dir(
            package_dir=package_dir,
            workspace_config=workspace_config,
        )

    @classmethod
    def from_render_dir(
        cls,
        package_dir: Path,
    ) -> "PackageConfig":
        """Loads package configuration strictly from the render/ sandbox package directory.
        The name of package_dir is treated as the package name.
        """
        return load_package_config_from_render_dir(package_dir=package_dir)

    @classmethod
    def from_install_dir(
        cls,
        package_dir: Path,
    ) -> "PackageConfig":
        """Loads package configuration strictly from the install/ state database package directory.
        The name of package_dir is treated as the package name.
        """
        return load_package_config_for_install(package_dir=package_dir)

    @classmethod
    def from_rendered_file(
        cls,
        package_toml_path: Path,
        package_name: str,
        package_dir: Path,
    ) -> "PackageConfig":
        """Loads package configuration directly from a rendered drift_package.toml file."""
        return load_package_config_rendered(
            package_toml_path=package_toml_path,
            package_name=package_name,
            package_dir=package_dir,
        )


def load_package_config_rendered(
    package_toml_path: Path,
    package_name: str,
    package_dir: Path,
) -> PackageConfig:
    """Loads and parses a package configuration from drift_package.toml.

    Args:
        package_toml_path: Absolute path to the rendered drift_package.toml file.
        package_name: Required canonical name of the package.
        package_dir: Required package root directory (e.g. render/<pkg> or install/<pkg>).
    """
    if not package_toml_path.exists():
        raise FileNotFoundError(f"Package configuration file not found: {package_toml_path}")
    content = package_toml_path.read_text(encoding="utf-8")
    data = parse_toml(content)
    try:
        config = PackageConfig.from_dict(
            data,
            package_name=package_name,
            source_files=[package_toml_path],
            base_dir=package_dir,
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"Invalid package configuration for '{package_name}' in '{package_toml_path}': {e}") from e
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
    config_files: Iterable[Path],
    render_engines: RenderEngineRegistry,
) -> List[PackageConfigFileInfo]:
    """Finds the package config file (or template) for an arbitrary list of rendered candidate paths.

    Inspects each candidate path against the render engine registry, determining whether a static
    file exists or if a matching template counterpart (e.g. .envst.toml) is present.

    Args:
        config_files: Ordered list of candidate package configuration file paths.
        render_engines: Registry of available template render engines.

    Returns:
        List of PackageConfigFileInfo objects for all matched configuration files/templates.
    """
    result = []
    for file in config_files:
        file_match = render_engines.find_source_file_for_rendered_names(file.parent, [file.name])
        if not file_match:
            continue
        result.append(PackageConfigFileInfo(
            type="static" if file_match.engine is None else "template",
            path=file_match.path,
            engine=file_match.engine,
        ))
    return result


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

    # It's a template, we need to render it!
    engine = info.engine
    if engine is None:
        raise ValueError(f"Template configuration file found, but render engine is not specified: {info.path}")

    from ..core.constants import INITIAL_ENV
    from ..utils.env_utils import env_scope

    pkg_envs = {
        "drift_package_name": package_name,
        "drift_package_source_dir": str(workspace_config.source_path / package_name),
        "drift_package_src_dir": str(workspace_config.source_path / package_name),
        "drift_package_render_dir": str(workspace_config.render_path / package_name),
        "drift_package_install_dir": str(workspace_config.install_path / package_name),
    }

    with tempfile.TemporaryDirectory(prefix=f"{package_name}_pkg_") as tmpdir:
        temp_path_obj = Path(tmpdir) / "drift_package.toml"
        with env_scope(pkg_envs, overwrite=True, env_keep=INITIAL_ENV):
            from ..render.render_core import render_template_to_file
            render_template_to_file(
                engine_config=engine,
                drift_root=workspace_config.drift_root,
                template_file_path=info.path,
                output_file_path=temp_path_obj
            )
            content = temp_path_obj.read_text(encoding="utf-8")
            return parse_toml(content)


def load_package_config_dict(
    pkg_name: str,
    config_files: Sequence[Path],
    workspace_config: Optional[WorkspaceConfig] = None
) -> Tuple[dict, List[Path]]:
    """Sequentially loads, renders (if templated), and deep-merges an arbitrary sequence of package config files.

    Supports arbitrary multi-layer package configs without a hardcoded base/local limit.
    If workspace_config is None, falls back to direct static file parsing without template rendering.

    Args:
        pkg_name: Name of the package.
        config_files: Ordered sequence of configuration candidate paths.
        workspace_config: Optional WorkspaceConfig providing render engine registry.

    Returns:
        Tuple of (merged_config_dict, list_of_source_paths).

    Raises:
        FileNotFoundError: If none of the specified configuration files or templates exist.
    """
    combined_dict = {}
    source_list = []
    if workspace_config is None:
        # mainly used in tests, to load a config without rendering or writing out into the render/ directory.
        logger.warning("WorkspaceConfig is not provided. Falling back to static loading without rendering.")
        for file in config_files:
            if not file.is_file():
                continue
            source_list.append(file)
            f_dict = parse_toml(file.read_text(encoding="utf-8"))
            combined_dict = merge_toml(combined_dict, f_dict)
    else:
        # With workspace_config provided, we can render templates if needed.
        info_list = get_package_config_file_info(
                config_files, workspace_config.render_engine_configs)
        for info in info_list:
            source_list.append(info.path)
            f_dict = render_or_load_toml(info, workspace_config, pkg_name)
            combined_dict = merge_toml(combined_dict, f_dict)

    if not combined_dict or not source_list:
        raise FileNotFoundError(
                f"Package configuration file not found in [{', '.join(str(x) for x in config_files)}] "
                "or their templates."
        )
    return combined_dict, source_list


def load_package_config_from_source_dir(
    package_dir: Path,
    workspace_config: Optional[WorkspaceConfig] = None,
) -> PackageConfig:
    """Loads, transforms, and validates the package configuration from its source directory.

    Configuration Pipeline Execution Order:
    1. Multi-File Discovery & Merging: Discovers candidate files (drift_package.toml,
       drift_package.local.toml, and templates) and deep-merges them via load_package_config_dict.
    2. Dynamic Python Package Hook: Executes configure_package(context) from src/<pkg>/drift_package.py
       (or custom hook_file). The hook operates as a preprocessor on the raw dictionary with access to
       resolved context facts and environment.
    3. Variable Stitching & Topological Sort: Resolves [env.override] and [env.fallback] tables using
       Kahn's topological sort and Drift's 7-tier precedence model.
    4. Cross-Section Interpolation: Interpolates ${VAR} across all non-env sections.
    5. Render Staging: Writes stitched configuration to render/<pkg>/drift_package.toml.
    6. Schema Validation & Model Construction: Instantiates strongly-typed PackageConfig.

    Args:
        package_dir: Directory path of the package (e.g. src/<pkg>/).
        workspace_config: Optional active WorkspaceConfig instance.

    Returns:
        Fully resolved and validated PackageConfig instance.

    Raises:
        FileNotFoundError: If the package has no configuration file or template.
        ConfigError: If configuration syntax or schema is invalid.
    """
    pkg_name = package_dir.name
    from ..hooks.package_hook import apply_package_hook

    combined_dict, source_files = load_package_config_dict(
        pkg_name, [
            package_dir / PACKAGE_CONFIG_FILE_NAME,
            package_dir / PACKAGE_CONFIG_LOCAL_FILE_NAME,
        ], workspace_config)

    # Apply dynamic Python package hook (src/<pkg>/drift_package.py or custom hook_file)
    combined_dict, hook_path = apply_package_hook(
        package_dir, combined_dict, workspace_config, package_name_override=pkg_name)

    # Register dynamic hook file in source_files so is_package_config_file ignores it during copy/render
    if hook_path:
        source_files.append(hook_path)

    # 1. Resolve environment variables and stitch configuration sections
    stitched_dict = resolve_and_interpolate_package_config(
        combined_dict,
        package_name=pkg_name,
        workspace_config=workspace_config,
    )

    # 2. Determine output path: render/<package_name>/.drift/drift_package.toml
    # Writes the fully resolved, stitched configuration to the git-ignored render sandbox
    if workspace_config is not None:
        output_file_path = workspace_config.render_path / pkg_name / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        output_file_path.parent.mkdir(parents=True, exist_ok=True)
        toml_str = dump_toml(stitched_dict)
        output_file_path.write_text(toml_str, encoding="utf-8")

    # 3. Load PackageConfig from the stitched dictionary
    try:
        config = PackageConfig.from_dict(
            stitched_dict,
            package_name=pkg_name,
            source_files=source_files,
            base_dir=package_dir,
            workspace_config=workspace_config,
        )
    except (TypeError, ValueError) as e:
        package_dir_log = package_dir.relative_to(workspace_config.drift_root) if workspace_config else package_dir
        err_msg = (f"Invalid configuration for package '{pkg_name}' in '{package_dir_log}' "
                   f"from {[str(x.relative_to(package_dir)) for x in source_files]}: {e}")
        logger.error(f"❌ {err_msg}")
        raise ConfigError(err_msg) from e
    return config


def load_package_config_from_render_dir(package_dir: Path) -> PackageConfig:
    """Loads package configuration strictly from the render/ sandbox package directory.
    The name of package_dir is treated as the package name.
    """
    pkg_name = package_dir.name
    config_file = package_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
    if not config_file.exists():
        legacy_file = package_dir / PACKAGE_CONFIG_FILE_NAME
        if legacy_file.exists():
            logger.warning(
                f"⚠️ [DEPRECATION] Package '{pkg_name}' in render/ contains legacy root '{PACKAGE_CONFIG_FILE_NAME}'. "
                f"Please run 'drift repair' to migrate metadata into '{DRIFT_INTERNAL_DIR_NAME}/'."
            )
            config_file = legacy_file
        else:
            raise RuntimeError(f"Failed to find {PACKAGE_CONFIG_FILE_NAME} in .drift/ for '{pkg_name}' in render sandbox")
    try:
        return load_package_config_rendered(package_toml_path=config_file, package_name=pkg_name, package_dir=package_dir)
    except Exception as e:
        raise RuntimeError(f"Failed to load package configuration for '{pkg_name}' from render sandbox: {e}")


def load_package_config_for_install(package_dir: Path) -> PackageConfig:
    """Loads package configuration strictly from the install/ base package directory.
    The name of package_dir is treated as the package name.
    """
    pkg_name = package_dir.name
    install_config_file = package_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
    if not install_config_file.exists():
        legacy_file = package_dir / PACKAGE_CONFIG_FILE_NAME
        if legacy_file.exists():
            logger.warning(
                f"⚠️ [DEPRECATION] Package '{pkg_name}' in install/ contains legacy root '{PACKAGE_CONFIG_FILE_NAME}'. "
                f"Please run 'drift repair' to migrate metadata into '{DRIFT_INTERNAL_DIR_NAME}/'."
            )
            install_config_file = legacy_file
        else:
            raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in .drift/ of install base for '{pkg_name}'.")
    try:
        return load_package_config_rendered(package_toml_path=install_config_file, package_name=pkg_name, package_dir=package_dir)
    except Exception as e:
        raise RuntimeError(f"Failed to load package configuration for '{pkg_name}' from install base: {e}")


