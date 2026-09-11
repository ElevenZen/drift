"""Common utilities for dynamically loading and executing Python configuration hooks."""

import logging
import importlib.util
from pathlib import Path
from typing import Any, Dict

from .exceptions import ConfigError

logger = logging.getLogger(__name__)


def load_python_module(
    module_path: Path,
    module_name: str = "drift_dynamic_hook",
    hook_desc: str = "Python hook"
) -> Any:
    """Dynamically loads a Python module from disk with comprehensive error handling."""
    if not module_path.is_file():
        raise ConfigError(f"{hook_desc} file not found at '{module_path}'.")
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ConfigError(f"Could not load module specification for {hook_desc.lower()} at '{module_path}'.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Failed to load {hook_desc.lower()} at '{module_path}': {e}") from e


def execute_python_hook(
    module_path: Path,
    function_name: str,
    context: Any,
    hook_desc: str = "Hook",
    module_name: str = "drift_dynamic_hook"
) -> Dict[str, Any]:
    """Executes a target hook function with the provided context and validates the returned dictionary."""
    module = load_python_module(module_path, module_name=module_name, hook_desc=hook_desc)

    hook_func = getattr(module, function_name, None)
    if hook_func is None or not callable(hook_func):
        raise ConfigError(
            f"{hook_desc} at '{module_path}' must define a callable '{function_name}(context)' function."
        )

    try:
        logger.debug(f"Executing {hook_desc.lower()} at '{module_path}'...")
        res = hook_func(context)
        if res is None:
            raise ConfigError(
                f"{hook_desc} '{module_path}' returned None. It must explicitly return the transformed configuration dictionary."
            )
        if not isinstance(res, dict):
            raise ConfigError(
                f"{hook_desc} '{module_path}' must return a dictionary, got {type(res).__name__}."
            )
        return res
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Error executing {hook_desc.lower()} at '{module_path}': {e}") from e
