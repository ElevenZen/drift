"""Package lifecycle hook direct trigger engine."""

import logging
from pathlib import Path
from typing import Optional, Union

from .workspace_config import WorkspaceConfig
from .package_config import (
    PackageConfig,
    load_package_config_from_source_dir,
    load_config_for_install,
)
from .lifecycle_hooks import (
    trigger_package_hook_with_render,
    trigger_package_lifecycle_hook,
)
from .file_utils import ensure_dir_exists_with_sudo
from .constants import (
    PACKAGE_CONFIG_FILE_NAME,
    LIFECYCLE_HOOK_NAMES,
    SOURCE_CWD_HOOK_NAMES,
    RENDER_CWD_HOOK_NAMES,
    INSTALL_CWD_HOOK_NAMES,
    TARGET_CWD_HOOK_NAMES,
    PackageStage,
)
from .exceptions import ConfigError
from .result_models import HookResult

logger = logging.getLogger(__name__)


def _resolve_hook_cwd(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
    pkg_config: PackageConfig,
) -> Path:
    """Resolves and validates the execution working directory (CWD) for a given lifecycle hook."""
    if hook_name in RENDER_CWD_HOOK_NAMES:
        render_pkg_dir = workspace_config.render_path / package_name
        if not render_pkg_dir.exists() or not render_pkg_dir.is_dir():
            raise FileNotFoundError(
                f"Package '{package_name}' has not been rendered. Render directory not found: '{render_pkg_dir}'. "
                f"Please run 'drift render {package_name}' first."
            )
        cwd = render_pkg_dir
    elif hook_name in INSTALL_CWD_HOOK_NAMES:
        install_pkg_dir = workspace_config.install_path / package_name
        if not install_pkg_dir.exists() or not install_pkg_dir.is_dir():
            raise FileNotFoundError(
                f"Install directory not found: '{install_pkg_dir}'."
            )
        cwd = install_pkg_dir
    elif hook_name in SOURCE_CWD_HOOK_NAMES:
        src_pkg_dir = workspace_config.source_path / package_name
        if not src_pkg_dir.exists() or not src_pkg_dir.is_dir():
            raise FileNotFoundError(
                f"Package '{package_name}' source directory not found: '{src_pkg_dir}'"
            )
        cwd = src_pkg_dir
    else:  # TARGET_CWD_HOOK_NAMES
        cwd = pkg_config.get_target_directory(workspace_config)

    ensure_dir_exists_with_sudo(cwd, sudo=pkg_config.sudo)
    return cwd


def trigger_hook_from_source(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
) -> HookResult:
    """Loads configuration and executes a lifecycle hook from the package source directory (rendering templates if needed)."""
    src_pkg_dir = workspace_config.source_path / package_name

    if hook_name in RENDER_CWD_HOOK_NAMES:
        render_pkg_dir = workspace_config.render_path / package_name
        if not render_pkg_dir.exists() or not render_pkg_dir.is_dir():
            raise FileNotFoundError(
                f"Package '{package_name}' has not been rendered. Render directory not found: '{render_pkg_dir}'. "
                f"Please run 'drift render {package_name}' first."
            )

    try:
        pkg_config = load_package_config_from_source_dir(
            package_dir=src_pkg_dir,
            workspace_config=workspace_config
        )
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"Package '{package_name}' source directory not found: '{src_pkg_dir}'"
        ) from e

    cwd = _resolve_hook_cwd(workspace_config, package_name, hook_name, pkg_config)

    res = trigger_package_hook_with_render(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name=hook_name,
        pkg_config=pkg_config,
        custom_cwd=cwd,
        load_envs=True,
        raise_on_error=True
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
) -> HookResult:
    """Loads configuration and executes a lifecycle hook directly from the install state database directory."""
    install_pkg_dir = workspace_config.install_path / package_name

    try:
        pkg_config = load_config_for_install(workspace_config.install_path, package_name)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"Package '{package_name}' is not installed in the state database. "
            f"Install directory not found: '{install_pkg_dir}'. "
            f"Please run 'drift deploy {package_name}' or 'drift apply {package_name}' first."
        ) from e

    cwd = _resolve_hook_cwd(workspace_config, package_name, hook_name, pkg_config)

    with pkg_config.package_envs(workspace_config):
        res = trigger_package_lifecycle_hook(
            pkg=package_name,
            hook_name=hook_name,
            metadata=pkg_config,
            hook_base_dir=install_pkg_dir,
            cwd=cwd,
            raise_on_error=True
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
    from_stage: Optional[Union[str, PackageStage]] = None
) -> HookResult:
    """Executes a single lifecycle hook for a specific package directly.

    Args:
        workspace_config: The active Drift workspace configuration.
        package_name: Target package name.
        hook_name: The lifecycle hook name to execute (e.g. pre_source, post_render,
            pre_install, post_install, pre_update, post_update, pre_uninstall, post_uninstall, health).
        from_stage: Optional stage selector ('source' or 'install'). If omitted:
            - Hooks 'pre_source', 'probe', 'post_render' default to 'source'.
            - All other lifecycle hooks default to 'install'.

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
        if hook_name in SOURCE_CWD_HOOK_NAMES or hook_name in RENDER_CWD_HOOK_NAMES:
            stage = PackageStage.SOURCE
        else:
            stage = PackageStage.INSTALL

    if stage == PackageStage.SOURCE:
        return trigger_hook_from_source(workspace_config, package_name, hook_name)
    return trigger_hook_from_install(workspace_config, package_name, hook_name)
