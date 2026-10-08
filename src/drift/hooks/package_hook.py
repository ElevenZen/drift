"""Dynamic Python package preprocessor hook loading and execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: High-Level Preprocessor Orchestration
    - apply_package_hook(): Resolves package hook file path, resolves composite
      environment and package facts, builds PackageHookContext, and executes
      dynamic configuration transformation before variable stitching.

Layer 1: Low-Level Hook Loading, Path Resolution & Execution
    - resolve_package_hook_path(): Discovers hook file path from config or default convention.
    - execute_package_hook(): Invokes configure_package(context) via python_hook_utils.
    - load_package_hook_module(): Dynamically imports package hook file from disk.
    - PackageHookContext: Strongly-typed, inspectable context passed to configure_package().
===============================================================================
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig

from ..core.constants import (
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    DRIFT_PACKAGE_FACT_KEYS,
    PACKAGE_HOOK_FUNCTION_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.python_hook_utils import load_python_module, execute_python_hook
from ..utils.config_utils import get_nested_from

logger = logging.getLogger(__name__)


@dataclass
class PackageHookContext:
    """In-memory execution context passed to the dynamic package Python preprocessor hook.

    This context is supplied as the sole argument to ``configure_package(context)``
    defined in ``src/<package_name>/drift_package.py`` (or a custom hook file declared
    via ``package.hook_file``).

    Execution Lifecycle & Pipeline Stage:
        1. Layered Discovery & Merging: ``drift_package.toml`` + ``drift_package.local.toml``
           (or ``.envst.toml`` templates) -> ``config_dict``.
        2. Package Preprocessor Hook (HERE): ``configure_package(context)`` executes in-memory.
           The hook can inspect host facts, package metadata, workspace configuration, and
           environment snapshots to dynamically mutate ``context.config`` (e.g., inject
           ``[env.default]``, alter ``install_method``, inject dependencies ``[dependencies]``,
           or override ``target_directory``).
        3. 6-Tier Environment Evaluation & Variable Stitching: Resolves package-level variables.
        4. Cross-Section Interpolation: Replaces ``${VAR}`` across package configuration fields.
        5. Schema Construction & Validation: Builds the strongly-typed ``PackageConfig``.

    Attributes:
        config (Dict[str, Any]):
            The raw, mutable configuration dictionary merged from ``drift_package.toml``
            and ``drift_package.local.toml``. Modifications made to this dictionary directly
            shape the resulting ``PackageConfig``.
        package_name (str):
            Canonical name of the package being configured (e.g. ``"nvim"``, ``"tmux"``).
        package_dir (Path):
            Canonical absolute ``Path`` to the package source directory (e.g. ``src/<pkg>/``).
        drift_root (Optional[Path]):
            Canonical absolute ``Path`` to the root of the active Drift workspace repository,
            or ``None`` if operating in standalone/isolated package mode.
        workspace_config (Optional[WorkspaceConfig]):
            The fully resolved and validated ``WorkspaceConfig`` instance of the enclosing
            workspace (if available), providing access to workspace-wide settings, render cache,
            environment definitions, and sibling package states.
        env (Dict[str, str]):
            A composite snapshot of environment variables resolved through Drift's 6-tier
            precedence hierarchy, including system facts, workspace environment, and
            package-specific facts (``drift_package_*``). Reading this dictionary provides
            accurate environment data with zero mutation on global ``os.environ``.

    Properties:
        facts (Dict[str, str]):
            Auto-detected host system facts filtered from ``env`` (keys matching ``get_cached_system_facts()``,
            such as ``drift_os``, ``drift_arch``, ``drift_distro``, ``drift_hostname``, ``drift_user``,
            ``drift_ip_addresses``).
        package_facts (Dict[str, str]):
            Package-specific contextual variables filtered from ``env`` (keys matching ``DRIFT_PACKAGE_FACT_KEYS``,
            such as ``drift_package_name``, ``drift_package_source_dir``, ``drift_package_src_dir``,
            ``drift_package_render_dir``, ``drift_package_install_dir``, ``drift_package_install_method``,
            ``drift_package_target_dir``).
        os (str):
            Convenience shorthand for ``facts.get("drift_os", "")`` (e.g. ``"linux"``, ``"darwin"``).
        arch (str):
            Convenience shorthand for ``facts.get("drift_arch", "")`` (e.g. ``"x86_64"``, ``"aarch64"``).
        distro (str):
            Convenience shorthand for ``facts.get("drift_distro", "")`` (e.g. ``"ubuntu"``, ``"arch"``, ``"macos"``).
        hostname (str):
            Convenience shorthand for ``facts.get("drift_hostname", "")``.
        user (str):
            Convenience shorthand for ``facts.get("drift_user", "")``.

    Example:
        ```python
        def configure_package(context: PackageHookContext) -> Dict[str, Any]:
            cfg = context.config
            package = cfg.setdefault("package", {})

            # Adjust install strategy and dependencies based on host environment
            if context.os == "darwin":
                package["install_method"] = "symlink"
            elif context.distro == "nixos":
                package["install_method"] = "copy"

            return cfg
        ```
    """
    config: Dict[str, Any]
    package_name: str
    package_dir: Path
    drift_root: Optional[Path] = None
    workspace_config: Optional["WorkspaceConfig"] = None
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def facts(self) -> Dict[str, str]:
        """Convenience accessor for auto-detected drift_* system facts."""
        from ..utils.host_facts import get_cached_system_facts
        cached_keys = get_cached_system_facts().keys()
        return {k: v for k, v in self.env.items() if k in cached_keys}

    @property
    def package_facts(self) -> Dict[str, str]:
        """Convenience accessor for package-specific facts (e.g. drift_package_*)."""
        return {k: v for k, v in self.env.items() if k in DRIFT_PACKAGE_FACT_KEYS}

    @property
    def os(self) -> str:
        """Standardized operating system identifier ('linux', 'darwin', 'windows')."""
        return self.facts.get("drift_os", "")

    @property
    def arch(self) -> str:
        """Standardized processor architecture ('x86_64', 'aarch64', etc.)."""
        return self.facts.get("drift_arch", "")

    @property
    def distro(self) -> str:
        """Standardized OS distribution name ('ubuntu', 'debian', 'arch', 'macos', etc.)."""
        return self.facts.get("drift_distro", "")

    @property
    def hostname(self) -> str:
        """Network hostname of the current machine."""
        return self.facts.get("drift_hostname", "")

    @property
    def user(self) -> str:
        """Current login user name."""
        return self.facts.get("drift_user", "")


def load_package_hook_module(hook_path: Path, package_name: str = "") -> Any:
    """Dynamically loads the package hook Python module from disk."""
    module_name = f"drift_package_hook_{package_name}" if package_name else "drift_package_hook"
    desc = f"Package hook for '{package_name}'" if package_name else "Package hook"
    return load_python_module(hook_path, module_name=module_name, hook_desc=desc)


def execute_package_hook(hook_path: Path, context: PackageHookContext) -> Dict[str, Any]:
    """Executes the configure_package() function from the hook module."""
    module_name = f"drift_package_hook_{context.package_name}"
    desc = f"Package hook for '{context.package_name}'"
    return execute_python_hook(
        module_path=hook_path,
        function_name=PACKAGE_HOOK_FUNCTION_NAME,
        context=context,
        hook_desc=desc,
        module_name=module_name,
    )


def resolve_package_hook_path(
    package_dir: Path,
    config_dict: Dict[str, Any],
    package_name_override: Optional[str] = None
) -> Optional[Path]:
    """Resolves the package Python hook file path from config or default convention.

    Returns:
        The resolved Path to the hook file, or None if no hook is configured and
        no default hook file exists. Raises ConfigError if a custom hook_file is
        explicitly configured but does not exist on disk.
    """
    pkg_name = package_name_override or package_dir.name
    custom_hook = get_nested_from(config_dict, "package.hook_file")

    if custom_hook is None:
        # Check standard default location (src/<pkg>/drift_package.py)
        hook_path = package_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        if not hook_path.is_file():
            return None
        return hook_path
    else:
        # If a custom hook file is specified, resolve relative to package_dir (pathlib handles absolute paths automatically)
        hook_path = package_dir / Path(custom_hook)
        if not hook_path.is_file():
            raise ConfigError(
                f"Configured package hook_file '{custom_hook}' not found at '{hook_path}' for package '{pkg_name}'."
            )
        return hook_path


def apply_package_hook(
    package_dir: Path,
    config_dict: Dict[str, Any],
    workspace_config: Optional["WorkspaceConfig"],
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Resolves and applies the package Python hook if configured or present.

    Returns:
        A tuple of (transformed_config_dict, resolved_hook_path_or_None).
    """
    pkg_name = package_dir.name
    hook_path = resolve_package_hook_path(package_dir, config_dict, package_name_override=pkg_name)
    if hook_path is None:
        return config_dict, None

    drift_root = workspace_config.drift_root if workspace_config is not None else None

    from ..utils.env_utils import resolve_env_configs, EnvConfig
    env_res = resolve_env_configs(
        current_layer=(
            workspace_config.env_resolve.effective
            if workspace_config is not None
            else EnvConfig()
        ),
        lower_layer=None,
        package_facts=(
            workspace_config.get_drift_package_facts(pkg_name)
            if workspace_config is not None
            else {"drift_package_name": pkg_name}
        ),
    )

    real_env = env_res.impact.full_env(os.environ)
    context = PackageHookContext(
        config=config_dict,
        package_name=pkg_name,
        package_dir=package_dir,
        drift_root=drift_root,
        workspace_config=workspace_config,
        env=real_env,
    )
    transformed = execute_package_hook(hook_path, context)
    return transformed, hook_path
