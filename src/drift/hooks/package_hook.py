"""Dynamic Python package hook loader and context definitions for Drift packages."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig

from ..core.constants import (
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    DRIFT_SYSTEM_FACT_KEYS,
    DRIFT_PACKAGE_FACT_KEYS,
    INITIAL_ENV,
    PACKAGE_HOOK_FUNCTION_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.env_utils import update_env_dict
from ..utils.python_hook_utils import load_python_module, execute_python_hook
from ..utils.toml_utils import get_nested_from

logger = logging.getLogger(__name__)


@dataclass
class PackageHookContext:
    """Context object passed to the configure_package() Python hook."""
    config: Dict[str, Any]
    package_name: str
    package_dir: Path
    drift_root: Optional[Path] = None
    workspace_config: Optional["WorkspaceConfig"] = None
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def facts(self) -> Dict[str, str]:
        """Convenience accessor for auto-detected drift_* system facts."""
        return {k: v for k, v in self.env.items() if k in DRIFT_SYSTEM_FACT_KEYS}

    @property
    def package_facts(self) -> Dict[str, str]:
        """Convenience accessor for package-specific facts (e.g. drift_package_*)."""
        return {k: v for k, v in self.env.items() if k in DRIFT_PACKAGE_FACT_KEYS}

    @property
    def os(self) -> str:
        return self.facts.get("drift_os", "")

    @property
    def arch(self) -> str:
        return self.facts.get("drift_arch", "")

    @property
    def distro(self) -> str:
        return self.facts.get("drift_distro", "")

    @property
    def hostname(self) -> str:
        return self.facts.get("drift_hostname", "")

    @property
    def user(self) -> str:
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
            workspace_config.env_resolve.effective
                if workspace_config is not None else EnvConfig(),
            None,
            workspace_config.get_drift_package_facts(pkg_name)
                if workspace_config is not None else { 'drift_package_name': pkg_name },
    )

    real_env = dict(os.environ)
    update_env_dict(real_env, env_res.effective_dict, overwrite=True, env_keep=INITIAL_ENV)
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
