"""Workspace Python preprocessor hook loading and execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: High-Level Preprocessor Orchestration
    - apply_workspace_hook(): Resolves hook file path, constructs WorkspaceHookContext,
      and executes dynamic configuration transformation before variable stitching.

Layer 1: Low-Level Hook Loading & In-Memory Execution
    - execute_workspace_hook(): Invokes configure_workspace(context) via python_hook_utils.
    - load_workspace_hook_module(): Dynamically imports hook file from disk.
    - WorkspaceHookContext: Strongly-typed, inspectable context passed to configure_workspace().
===============================================================================
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Optional

from ..core.constants import (
    CONFIG_DIR_NAME,
    DEFAULT_WORKSPACE_HOOK_FILE_NAME,
    WORKSPACE_HOOK_FUNCTION_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.env_utils import parse_secrets_env
from ..utils.python_hook_utils import load_python_module, execute_python_hook
from ..utils.config_utils import get_nested_from

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceHookContext:
    """In-memory execution context passed to the dynamic workspace Python preprocessor hook.

    This context is supplied as the sole argument to ``configure_workspace(context)``
    defined in ``config/drift_workspace.py`` (or a custom hook file declared via
    ``workspace.hook_file``).

    Execution Lifecycle & Pipeline Stage:
        1. Discovery & Merging: ``drift_workspace.toml`` + ``drift_workspace.local.toml`` -> ``config_dict``.
        2. Workspace Hook (HERE): ``configure_workspace(context)`` executes in-memory.
           The hook can inspect host facts, environment snapshots, and discovered
           packages to dynamically mutate ``context.config`` (e.g., inject ``[env.default]``,
           ``[env.secrets]``, toggle ``[packages.enable]``, or rewrite paths).
        3. Kahn's Topological Sort & Variable Resolution: Evaluates ``${VAR}`` references.
        4. Cross-Section Interpolation: Replaces variables across all non-env sections.
        5. Schema Construction & Validation: Builds the strongly-typed ``WorkspaceConfig``.

    Attributes:
        config (Dict[str, Any]):
            The raw, mutable configuration dictionary merged from ``drift_workspace.toml``
            and ``drift_workspace.local.toml``. Modifications made to this dictionary directly
            shape the resulting ``WorkspaceConfig``.
        drift_root (Path):
            The canonical absolute ``Path`` to the root of the active Drift workspace repository.
        env (Dict[str, str]):
            A composite snapshot of environment variables resolved through Drift's 6-tier
            precedence hierarchy (Tier 1 overrides > Tier 2 system facts > Tier 3 ambient
            ``os.environ`` > Tier 4 secrets). Reading this dictionary provides accurate environment
            data with zero side effects or pollution of global ``os.environ``.
        discovered_packages (List[str]):
            A pre-scanned, sorted list of package directory names located under the package
            source directory (e.g. ``src/``). Scanned via ``WorkspaceConfig.get_package_names_from_dir()``,
            this list automatically filters out hidden directories (starting with ``.``) and
            reserved system directories (``FORBIDDEN_PACKAGE_NAMES``). Provided so the preprocessor
            hook can inspect existing package assets on disk before ``WorkspaceConfig`` is built.

    Properties:
        facts (Dict[str, str]):
            Auto-detected host system facts filtered from ``env`` (keys matching ``get_cached_system_facts()``,
            such as ``drift_os``, ``drift_arch``, ``drift_distro``, ``drift_hostname``, ``drift_user``,
            ``drift_ip_addresses``).
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
        def configure_workspace(context: WorkspaceHookContext) -> Dict[str, Any]:
            cfg = context.config
            enable = cfg.setdefault("packages", {}).setdefault("enable", {})

            # Conditionally enable packages based on OS and discovered directories
            if context.os == "darwin":
                enable["macos_tools"] = True
            elif context.os == "linux" and "cuda_toolkit" in context.discovered_packages:
                enable["cuda_toolkit"] = True

            return cfg
        ```
    """
    config: Dict[str, Any]
    drift_root: Path
    env: Dict[str, str] = field(default_factory=dict)
    discovered_packages: List[str] = field(default_factory=list)

    @property
    def facts(self) -> Dict[str, str]:
        """Convenience accessor for auto-detected drift_* system facts."""
        from ..utils.host_facts import get_cached_system_facts
        cached_keys = get_cached_system_facts().keys()
        return {k: v for k, v in self.env.items() if k in cached_keys}

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


def apply_workspace_hook(
    drift_root: Path,
    config_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolves and applies the workspace Python hook if configured or present."""
    custom_hook = get_nested_from(config_dict, "workspace.hook_file")

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
    from ..config.workspace_config import WorkspaceConfig

    from ..utils.host_facts import get_cached_system_facts

    system_facts = get_cached_system_facts()
    secrets_file = parse_secrets_env(drift_root)
    raw_secrets = get_nested_from(config_dict, "env.secrets", default={})
    ws_secrets = {str(k): str(v) for k, v in raw_secrets.items()}
    raw_overrides = get_nested_from(config_dict, "env.override", default={})
    ws_overrides = {str(k): str(v) for k, v in raw_overrides.items()}
    # 6-Tier Precedence: Tier 1 overrides > Tier 2 facts > Tier 3 ambient os.environ > Tier 4 secrets
    real_env = {**secrets_file, **ws_secrets, **os.environ, **system_facts, **ws_overrides}

    source_dir_name = get_nested_from(config_dict, "workspace.source_directory", default="src")
    source_path = drift_root / source_dir_name

    context = WorkspaceHookContext(
        config=config_dict,
        drift_root=drift_root,
        env=real_env,
        discovered_packages=WorkspaceConfig.get_package_names_from_dir(source_path),
    )
    return execute_workspace_hook(hook_path, context)
