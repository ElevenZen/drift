"""Package lifecycle hook direct trigger engine."""

import logging
from pathlib import Path

from .workspace_config import WorkspaceConfig
from .package_config import (
    load_package_config_from_source_dir,
    load_package_config_rendered,
)
from .lifecycle_hooks import trigger_pre_source_lifecycle_hook
from .file_utils import ensure_dir_exists_with_sudo
from .constants import (
    PACKAGE_CONFIG_FILE_NAME,
    LIFECYCLE_HOOK_NAMES,
    INSTALL_CWD_HOOK_NAMES,
)
from .exceptions import ConfigError
from .result_models import HookResult

logger = logging.getLogger(__name__)


def _trigger_pre_source_hook(
    workspace_config: WorkspaceConfig,
    package_name: str,
) -> HookResult:
    """Triggers the pre_source hook from the package source directory."""
    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists() or not src_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"Package '{package_name}' source directory not found: '{src_pkg_dir}'"
        )

    pkg_config = load_package_config_from_source_dir(
        package_dir=src_pkg_dir,
        workspace_config=workspace_config
    )

    if not pkg_config.hooks.pre_source:
        raise ConfigError(
            f"No 'pre_source' hook configured for package '{package_name}'."
        )

    return trigger_pre_source_lifecycle_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        pkg_config=pkg_config,
        load_envs=True
    )


def _trigger_post_render_hook(
    workspace_config: WorkspaceConfig,
    package_name: str,
) -> HookResult:
    """Triggers the post_render hook from the compiled render sandbox directory."""
    render_pkg_dir = workspace_config.render_path / package_name
    if not render_pkg_dir.exists() or not render_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"Package '{package_name}' has not been rendered. Render directory not found: '{render_pkg_dir}'. "
            f"Please run 'drift render {package_name}' first."
        )

    config_file = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not config_file.exists():
        raise FileNotFoundError(
            f"Package configuration file '{PACKAGE_CONFIG_FILE_NAME}' not found in render base of package '{package_name}': '{config_file}'. Please run 'drift render {package_name}' first."
        )

    pkg_config = load_package_config_rendered(config_file)
    if not pkg_config.hooks.post_render:
        raise ConfigError(
            f"No 'post_render' hook configured for package '{package_name}'."
        )

    with pkg_config.package_envs(workspace_config):
        return pkg_config.hooks.trigger_post_render(render_dir=render_pkg_dir)


def _trigger_install_stage_hook(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
) -> HookResult:
    """Triggers an install-stage hook from the install state database directory."""
    install_pkg_dir = workspace_config.install_path / package_name
    if not install_pkg_dir.exists() or not install_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"Package '{package_name}' is not installed in the state database. "
            f"Install directory not found: '{install_pkg_dir}'. "
            f"Please run 'drift deploy {package_name}' or 'drift apply {package_name}' first."
        )

    config_file = install_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not config_file.exists():
        raise FileNotFoundError(
            f"Package configuration file '{PACKAGE_CONFIG_FILE_NAME}' not found in install base of package '{package_name}': '{config_file}'."
        )

    pkg_config = load_package_config_rendered(config_file)
    hook_file = getattr(pkg_config.hooks, hook_name, None)
    if not hook_file:
        raise ConfigError(
            f"No '{hook_name}' hook configured for package '{package_name}'."
        )

    if hook_name in INSTALL_CWD_HOOK_NAMES:
        cwd = install_pkg_dir
    else:
        cwd = pkg_config.get_target_directory(workspace_config)

    # Ensure cwd directory exists (especially target_dir for post_* and health hooks)
    ensure_dir_exists_with_sudo(cwd, sudo=pkg_config.sudo)

    with pkg_config.package_envs(workspace_config):
        return pkg_config.hooks.trigger(hook_name, hook_base_dir=install_pkg_dir, cwd=cwd)


def run_primitive_trigger_hook(
    workspace_config: WorkspaceConfig,
    package_name: str,
    hook_name: str,
) -> HookResult:
    """Executes a single lifecycle hook for a specific package directly.

    Args:
        workspace_config: The active Drift workspace configuration.
        package_name: Target package name.
        hook_name: The lifecycle hook name to execute (e.g. pre_source, post_render,
            pre_install, post_install, pre_update, post_update, pre_uninstall, post_uninstall, health).

    Returns:
        HookResult detailing execution status, duration, CWD, hook script path, and exit status.
    """
    if hook_name not in LIFECYCLE_HOOK_NAMES:
        valid_hooks = ", ".join(LIFECYCLE_HOOK_NAMES)
        raise ConfigError(
            f"Invalid lifecycle hook '{hook_name}'. Valid hook names are: {valid_hooks}"
        )

    if hook_name == "pre_source":
        return _trigger_pre_source_hook(workspace_config, package_name)
    elif hook_name == "post_render":
        return _trigger_post_render_hook(workspace_config, package_name)
    else:
        return _trigger_install_stage_hook(workspace_config, package_name, hook_name)
