"""Module for managing package lifecycle hooks in the drift workspace."""

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

from .package_config import PackageConfig
from .file_utils import run_command

logger = logging.getLogger(__name__)


# Only installation and update hooks are allowed to be executed with sudo elevation
SUDO_ELIGIBLE_HOOKS = {"pre_install", "post_install", "pre_update", "post_update"}


def trigger_pre_source_hook(
    workspace_config: "WorkspaceConfig",
    package_name: str,
    load_envs: bool,
    pkg_config: Optional[PackageConfig] = None,
) -> None:
    """Triggers the pre_source lifecycle hook for a package in the source directory."""
    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists():
        return

    if pkg_config is None:
        try:
            from .package_config import load_package_config_from_source_dir
            pkg_config = load_package_config_from_source_dir(
                package_dir=src_pkg_dir,
                workspace_config=workspace_config
            )
        except FileNotFoundError:
            logger.error(f"Package Configuration file 'drift_package.toml' not found in '{src_pkg_dir}'. Skipping pre_source hook.")
            return

    if not pkg_config:
        logger.error(f"Package configuration file cannot be loaded from '{src_pkg_dir}'. Skipping pre_source hook.")
        return

    if pkg_config.hooks:
        if load_envs:
            with pkg_config.package_envs(workspace_config):
                pkg_config.hooks.trigger_pre_source(src_pkg_dir)
        else:
            pkg_config.hooks.trigger_pre_source(src_pkg_dir)


def trigger_post_render_hook(
    workspace_config: "WorkspaceConfig",
    pkg_config: PackageConfig
) -> None:
    """Triggers the post_render lifecycle hook for a package in the render directory."""
    render_pkg_dir = workspace_config.render_path / pkg_config.name
    if not render_pkg_dir.exists():
        return

    if pkg_config and pkg_config.hooks:
        pkg_config.hooks.trigger_post_render(render_pkg_dir)


def trigger_package_lifecycle_hook(
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    hook_dir: Path,
    cwd: Path
) -> None:
    """Executes a package lifecycle hook script if specified and found.

    The hook file is searched inside hook_dir.
    The script executes with working directory cwd.
    If the hook execution fails, times out, or the specified script is missing, a detailed error is raised.
    """
    hook_file = getattr(metadata, hook_name, None)
    if not hook_file:
        return

    hook_path = hook_dir / hook_file

    if not hook_path.exists():
        err_msg = f"Lifecycle hook file specified for '{hook_name}' in package '{pkg}' not found: {hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    try:
        hook_path.chmod(0o755)
    except Exception:
        pass

    assert cwd.is_absolute(), f"Working directory '{cwd}' must be absolute."

    logger.info(f"🪝  Triggering hook: {hook_name} ({pkg})")
    logger.debug(f"   Script: {hook_path}")
    logger.debug(f"   CWD:    {cwd}")

    cmd = [str(hook_path)]
    if metadata.sudo and hook_name in SUDO_ELIGIBLE_HOOKS:
        cmd.insert(0, "sudo")

    timeout_seconds = metadata.hook_timeout
    try:
        run_command(
            cmd,
            cwd=str(cwd),
            text=True,
            timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as e:
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' timed out after {timeout_seconds} seconds.\n"
            f"Command: {shlex.join(cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e
    except subprocess.CalledProcessError as e:
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' failed with exit code {e.returncode}.\n"
            f"Command: {shlex.join(cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e
