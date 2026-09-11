"""Dynamic Python workspace hook loader and executor for Drift."""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional

from .constants import (
    CONFIG_DIR_NAME,
    DEFAULT_WORKSPACE_HOOK_FILE_NAME,
    WORKSPACE_HOOK_FUNCTION_NAME,
)
from .exceptions import ConfigError
from .python_hook_utils import load_python_module, execute_python_hook

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceHookContext:
    """Context object passed to the configure_workspace() Python hook."""
    config: Dict[str, Any]
    drift_root: Path
    env: Dict[str, str] = field(default_factory=dict)
    discovered_packages: List[str] = field(default_factory=list)

    @property
    def facts(self) -> Dict[str, str]:
        """Convenience accessor for auto-detected drift_* system facts."""
        return {k: v for k, v in self.env.items() if k.startswith("drift_")}

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


def load_workspace_hook_module(hook_path: Path) -> Any:
    """Dynamically loads the workspace hook Python module from disk."""
    return load_python_module(hook_path, module_name="drift_workspace_hook", hook_desc="Workspace hook")


def execute_workspace_hook(hook_path: Path, context: WorkspaceHookContext) -> Dict[str, Any]:
    """Executes the configure_workspace() function from the hook module."""
    return execute_python_hook(
        module_path=hook_path,
        function_name=WORKSPACE_HOOK_FUNCTION_NAME,
        context=context,
        hook_desc="Workspace hook",
        module_name="drift_workspace_hook",
    )


def apply_workspace_hook(drift_root: Path, config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Resolves and applies the workspace Python hook if configured or present."""
    workspace_section = config_dict.get("workspace", {})
    custom_hook = workspace_section.get("hook_file")

    if custom_hook is None:
        # Check standard default location (config/drift_workspace.py)
        hook_path = drift_root / CONFIG_DIR_NAME / DEFAULT_WORKSPACE_HOOK_FILE_NAME
        if not hook_path.is_file():
            return config_dict
    else:
        # If a custom hook file is specified, resolve relative to config/ (pathlib handles absolute paths automatically)
        hook_path = drift_root / CONFIG_DIR_NAME / Path(custom_hook)
        if not hook_path.is_file():
            raise ConfigError(
                f"Configured workspace hook_file '{custom_hook}' not found at '{hook_path}'."
            )

    # hook_path is a valid file; execute the hook with context
    from .workspace_config import WorkspaceConfig
    context = WorkspaceHookContext(
        config=config_dict,
        drift_root=drift_root,
        env=dict(os.environ),
        discovered_packages=WorkspaceConfig.get_package_names_from_dir(drift_root / "src"),
    )
    return execute_workspace_hook(hook_path, context)
