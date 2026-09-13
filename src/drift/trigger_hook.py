"""Package lifecycle hook direct trigger engine."""

import logging
from pathlib import Path
from typing import Optional, Union

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig
from .lifecycle_hooks import (
    HookExecFlags,
    trigger_package_hook_with_render,
    trigger_package_hook,
)
from .constants import (
    LIFECYCLE_HOOK_NAMES,
    PackageStage,
)
from .exceptions import ConfigError
from .result_models import HookResult

logger = logging.getLogger(__name__)


def trigger_hook_from_source(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
    flags: Optional[HookExecFlags] = None,
    cwd_override: Optional[Path] = None,
) -> HookResult:
    """Loads configuration and executes a lifecycle hook from the package source directory (rendering templates if needed)."""
    src_pkg_dir = workspace_config.source_path / package_name

    try:
        pkg_config = PackageConfig.from_source_dir(
            package_dir=src_pkg_dir,
            workspace_config=workspace_config
        )
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"Package '{package_name}' source directory not found: '{src_pkg_dir}'"
        ) from e

    res = trigger_package_hook_with_render(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name=hook_name,
        custom_cwd=cwd_override,
        flags=flags,
        pkg_config_override=pkg_config,
    )
    if res.status == "SKIPPED":
        raise ConfigError(
            f"No '{hook_name}' hook configured for package '{package_name}'."
        )
    return res


def trigger_hook_from_install(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
    flags: Optional[HookExecFlags] = None,
    cwd_override: Optional[Path] = None,
) -> HookResult:
    """Loads configuration and executes a lifecycle hook directly from the install state database directory."""
    install_pkg_dir = workspace_config.install_path / package_name

    try:
        pkg_config = PackageConfig.from_install_dir(install_pkg_dir)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"Package '{package_name}' is not installed in the state database. "
            f"Install directory not found: '{install_pkg_dir}'. "
            f"Please run 'drift deploy {package_name}' or 'drift apply {package_name}' first."
        ) from e

    with pkg_config.package_envs(workspace_config):
        res = trigger_package_hook(
            pkg=package_name,
            hook_name=hook_name,
            metadata=pkg_config,
            cwd=cwd_override,
            flags=flags,
        )
    if res.status == "SKIPPED":
        raise ConfigError(
            f"No '{hook_name}' hook configured for package '{package_name}'."
        )
    return res


def run_primitive_trigger_hook(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
    from_stage: Optional[Union[str, PackageStage]] = None,
    flags: Optional[HookExecFlags] = None,
    cwd_override: Optional[Path] = None,
) -> HookResult:
    """Executes a single lifecycle hook for a specific package directly.

    Args:
        workspace_config: The active Drift workspace configuration.
        package_name: Target package name.
        hook_name: The lifecycle hook name to execute (e.g. pre_source, post_render,
            pre_install, post_install, pre_update, post_update, pre_uninstall, post_uninstall, health).
        from_stage: Optional stage selector ('source' or 'install'). If omitted:
            - Hooks 'probe', 'pre_source', 'post_render' default to 'source'.
            - All other lifecycle hooks default to 'install'.
        flags: Optional HookExecFlags controlling execution options (e.g. streaming, no_hooks).
        cwd_override: Optional working directory override.

    Returns:
        HookResult detailing execution status, duration, CWD, hook script path, and exit status.
    """
    if hook_name not in LIFECYCLE_HOOK_NAMES:
        valid_hooks = ", ".join(LIFECYCLE_HOOK_NAMES)
        raise ConfigError(
            f"Invalid lifecycle hook '{hook_name}'. Valid hook names are: {valid_hooks}"
        )

    if from_stage is not None:
        stage = PackageStage.from_str(from_stage)
    else:
        if hook_name in ("probe", "pre_source", "post_render"):
            stage = PackageStage.SOURCE
        else:
            stage = PackageStage.INSTALL

    if stage == PackageStage.SOURCE:
        return trigger_hook_from_source(workspace_config, package_name, hook_name, flags=flags, cwd_override=cwd_override)
    return trigger_hook_from_install(workspace_config, package_name, hook_name, flags=flags, cwd_override=cwd_override)
