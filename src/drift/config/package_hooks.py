"""Package lifecycle hooks configuration, path normalization, and execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Hook Model, Path Normalization & Execution Triggers
    PackageHooks (Dataclass)
        - validate(): Schema and field integrity checks
        - from_dict(): Factory parser with stage base directory resolution
        - get_relative_path(): Package-internal relative path inspector
        - get_configured_hook_paths(): Multi-base path string resolver
        - trigger(), trigger_probe(), trigger_pre_source(), ...: Execution wrappers
        - assert_hooks_exist(): Read-only validation guard for hook files
    normalize_hook_value(val, base_dir): Normalizes hook string/Path or disabled state
===============================================================================
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    TYPE_CHECKING,
)

from ..core.constants import (
    DEFAULT_HOOK_TIMEOUT,
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    HOOK_CONFIG_OPTION_SET,
    INSTALLATION_HOOK_NAMES,
    LIFECYCLE_HOOK_NAMES,
    WINDOWS_PLATFORM_ALIASES,
)
from ..core.exceptions import ConfigError, HookMissingError
from ..core.result_models import HookResult
from ..utils.path_utils import is_relative_to
from ..utils.toml_utils import get_first_from, validate_known_keys

if TYPE_CHECKING:
    from ..hooks.lifecycle_hooks import HookExecFlags
    from .package_config import PackageConfig
    from .workspace_config import WorkspaceConfig

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
                    f"Lifecycle hook '{hook_name}' path '{raw_val}'{name_str} must be located within '{DRIFT_HOOKS_DIR_NAME}/' directory "
                    f"(hooks inside package directory are restricted to '{DRIFT_HOOKS_DIR_NAME}/'; external hooks outside package must use an absolute path). "
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
        from ..hooks.lifecycle_hooks import HookExecFlags, trigger_hook

        exec_flags = HookExecFlags.resolve(flags=flags)
        if exec_flags.no_hooks:
            return HookResult.skipped(
                package=self._package_config.name if self._package_config else "",
                hook_name=hook_name,
                cwd=cwd,
            )
        if self._package_config is None:
            raise RuntimeError("PackageHooks is not associated with a PackageConfig.")
        return trigger_hook(
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

    def check_missing_hooks(
        self,
        base_dir: Path,
        is_source: bool,
        hook_names: Sequence[str] = (),
    ) -> List[Tuple[str, Path, str]]:
        """Inspects configured lifecycle hook files and returns a list of (hook_name, hook_path, reason).

        Args:
            base_dir: Directory containing package files (e.g. src/<pkg>, render/<pkg>, or install/<pkg>).
            is_source: True if base_dir is the package source directory (src/<pkg>),
                False if base_dir is a compiled/staged directory (render/<pkg> or install/<pkg>).
            hook_names: Sequence of hook names to check. If empty/omitted, all LIFECYCLE_HOOK_NAMES are checked.

        Returns:
            A list of tuples (hook_name, hook_path, failure_reason) for any missing or invalid hook files.
        """
        pkg_name = self._package_config.name if self._package_config else "unknown"
        target_hooks = hook_names if hook_names else LIFECYCLE_HOOK_NAMES
        failures: List[Tuple[str, Path, str]] = []

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
                failures.append(
                    (
                        hook_name,
                        hook_path,
                        f"Lifecycle hook file specified for '{hook_name}' in package '{pkg_name}' does not exist: '{hook_path}'",
                    )
                )
            elif not hook_path.is_file():
                failures.append(
                    (
                        hook_name,
                        hook_path,
                        f"Lifecycle hook path specified for '{hook_name}' in package '{pkg_name}' is not a regular file: '{hook_path}'",
                    )
                )

        return failures

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
            HookMissingError: If a configured hook file does not exist or is not a regular file.
        """
        failures = self.check_missing_hooks(base_dir, is_source=is_source, hook_names=hook_names)
        if failures:
            pkg_name = self._package_config.name if self._package_config else "unknown"
            reasons = "; ".join(reason for _, _, reason in failures)
            raise HookMissingError(
                reasons,
                packages=[pkg_name],
                hook_name=failures[0][0],
            )

