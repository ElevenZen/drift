"""Dynamic Python workspace hook loader and executor for Drift."""

import os
import sys
import logging
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional

from .constants import (
    CONFIG_DIR_NAME,
    DEFAULT_WORKSPACE_HOOK_FILE_NAME,
    WORKSPACE_HOOK_FUNCTION_NAME,
)
from .exceptions import ConfigError

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


def load_workspace_hook_module(hook_path: Path) -> Any:
    """Dynamically loads the workspace hook Python module from disk."""
    if not hook_path.is_file():
        raise ConfigError(f"Workspace hook file not found at '{hook_path}'.")
    try:
        spec = importlib.util.spec_from_file_location("drift_workspace_hook", hook_path)
        if spec is None or spec.loader is None:
            raise ConfigError(f"Could not load module specification for workspace hook at '{hook_path}'.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Failed to load workspace hook at '{hook_path}': {e}") from e


def execute_workspace_hook(hook_path: Path, context: WorkspaceHookContext) -> Dict[str, Any]:
    """Executes the configure_workspace() function from the hook module."""
    module = load_workspace_hook_module(hook_path)

    hook_func = getattr(module, WORKSPACE_HOOK_FUNCTION_NAME, None)
    if hook_func is None or not callable(hook_func):
        raise ConfigError(
            f"Workspace hook at '{hook_path}' must define a callable '{WORKSPACE_HOOK_FUNCTION_NAME}(context)' function."
        )

    try:
        logger.debug(f"Executing workspace hook at '{hook_path}'...")
        res = hook_func(context)
        if res is None:
            raise ConfigError(
                f"Workspace hook '{hook_path}' returned None. It must explicitly return the transformed configuration dictionary."
            )
        if not isinstance(res, dict):
            raise ConfigError(
                f"Workspace hook '{hook_path}' must return a dictionary, got {type(res).__name__}."
            )
        return res
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Error executing workspace hook at '{hook_path}': {e}") from e


def apply_workspace_hook(drift_root: Path, config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Resolves and applies the workspace Python hook if configured or present."""
    workspace_section = config_dict.get("workspace", {})
    custom_hook = workspace_section.get("hook_file")

    if custom_hook is None:
        # Check standard default location (config/drift_workspace.py relative to workspace root)
        hook_path = drift_root / DEFAULT_WORKSPACE_HOOK_FILE_NAME
        if not hook_path.is_file():
            return config_dict
    else:
        # If a custom hook file is specified, it must exist; otherwise, raise an error
        hook_path = drift_root / Path(custom_hook)
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
