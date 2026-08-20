"""Module for managing package lifecycle hooks in the drift workspace."""

import logging
import shlex
import subprocess
from pathlib import Path

from .package_config import PackageConfig
from .file_utils import run_command

logger = logging.getLogger(__name__)


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
    If the hook execution fails or times out, detailed output logs are printed and a RuntimeError is raised.
    """
    hook_file = getattr(metadata, hook_name, None)
    if not hook_file:
        return

    hook_path = hook_dir / hook_file

    if not hook_path.exists():
        logger.warning(f"Lifecycle hook file specified but not found: {hook_path}")
        return

    try:
        hook_path.chmod(0o755)
    except Exception:
        pass

    assert cwd.is_absolute(), f"Working directory '{cwd}' must be absolute."

    logger.info(f"🪝  Triggering hook: {hook_name} ({pkg})")
    logger.debug(f"   Script: {hook_path}")
    logger.debug(f"   CWD:    {cwd}")

    cmd = [str(hook_path)]
    if metadata.sudo:
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
