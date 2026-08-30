import sys
import time
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig

from .package_config import PackageConfig
from .file_utils import run_command, run_sudo_command, is_relative_to
from .constants import SUDO_ELIGIBLE_HOOKS
from .result_models import HookResult

logger = logging.getLogger(__name__)


def build_hook_execution_command(hook_path: Path) -> List[str]:
    """Generates cross-platform invocation command based on file extension and OS."""
    if sys.platform == "win32":
        ext = hook_path.suffix.lower()
        if ext == ".exe":
            return [str(hook_path)]
        elif ext == ".ps1":
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(hook_path)]
        elif ext in (".bat", ".cmd"):
            return ["cmd.exe", "/c", str(hook_path)]
        elif ext == ".py":
            return [sys.executable, str(hook_path)]
        elif ext in (".sh", ".bash"):
            # Fallback to Git Bash / bash if present in PATH
            return ["bash.exe", str(hook_path)]
        else:
            return [str(hook_path)]
    else:
        return [str(hook_path)]


def execute_hook_command(
    cmd: List[str],
    cwd: Path,
    timeout_seconds: int,
    use_sudo: bool = False
) -> subprocess.CompletedProcess:
    """Executes a lifecycle hook command with sudo handling and timeout."""
    return run_sudo_command(
        cmd,
        sudo=use_sudo,
        cwd=str(cwd),
        text=True,
        timeout=timeout_seconds
    )


def execute_hook_script(
    hook_path: Path,
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    cwd: Path
) -> HookResult:
    """Executes a hook script with chmod, sudo handling, cwd validation, and timeout/error handling.

    This function always returns a successful HookResult (status="SUCCESS") upon completion,
    or raises an Exception (FileNotFoundError, RuntimeError) if the hook script file is missing,
    times out, or exits with a non-zero status code.

    Returns:
        HookResult: Structured result with status="SUCCESS", execution duration_ms, exit code,
            hook path, CWD, and sudo elevation flag.

    Raises:
        FileNotFoundError: If the hook script file does not exist on disk.
        RuntimeError: If the hook script command times out or exits with a non-zero return code.
    """
    if not hook_path.exists():
        err_msg = f"Lifecycle hook file specified for '{hook_name}' in package '{pkg}' not found: {hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    try:
        current_mode = hook_path.stat().st_mode
        if not (current_mode & 0o111):
            logger.warning(
                f"⚠️  Lifecycle hook script '{hook_path}' does not have executable permission. "
                f"Automatically adding executable permission (chmod 0o755)."
            )
            hook_path.chmod(current_mode | 0o755)
    except Exception as e:
        logger.warning(f"Could not check or set executable permission on hook '{hook_path}': {e}")

    assert cwd.is_absolute(), f"Working directory '{cwd}' must be absolute."

    logger.info(f"🪝  Triggering hook: {hook_name} ({pkg})")
    logger.debug(f"   Script: {hook_path}")
    logger.debug(f"   CWD:    {cwd}")

    cmd = build_hook_execution_command(hook_path)
    use_sudo = bool(metadata.sudo and hook_name in SUDO_ELIGIBLE_HOOKS)
    timeout_seconds = metadata.hook_timeout

    start_time = time.perf_counter()

    try:
        proc = execute_hook_command(
            cmd=cmd,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            use_sudo=use_sudo
        )
        duration_ms = (time.perf_counter() - start_time) * 1000
        return HookResult(
            command="hook",
            package=pkg,
            hook_name=hook_name,
            status="SUCCESS",
            exit_code=proc.returncode if proc else 0,
            hook_path=str(hook_path),
            cwd=str(cwd),
            sudo=use_sudo,
            duration_ms=duration_ms
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        display_cmd = ["sudo"] + cmd if use_sudo and sys.platform != "win32" else cmd
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' timed out after {timeout_seconds} seconds.\n"
            f"Command: {shlex.join(display_cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e
    except subprocess.CalledProcessError as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        display_cmd = ["sudo"] + cmd if use_sudo and sys.platform != "win32" else cmd
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' failed with exit code {e.returncode}.\n"
            f"Command: {shlex.join(display_cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e


def trigger_pre_source_lifecycle_hook(
    workspace_config: "WorkspaceConfig",
    package_name: str,
    pkg_config: Optional[PackageConfig] = None,
    load_envs: bool = False,
    no_hooks: bool = False,
) -> HookResult:
    """
    Executes the pre_source lifecycle hook for a package in the source directory.

    Returns:
        HookResult detailing execution status ("SUCCESS", "SKIPPED", or "FAILED"), duration, and paths.

    Raises:
        FileNotFoundError: If a declared hook script file does not exist.
        RuntimeError: If hook execution fails or times out.
    """
    if no_hooks:
        return HookResult.skipped(package=package_name, hook_name="pre_source")

    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists() or not src_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"Package '{package_name}' source directory not found: {src_pkg_dir}"
        )

    if pkg_config is None:
        try:
            from .package_config import load_package_config_from_source_dir
            pkg_config = load_package_config_from_source_dir(
                package_dir=src_pkg_dir,
                workspace_config=workspace_config
            )
        except FileNotFoundError:
            # Package has no drift_package.toml -> no lifecycle hooks configured
            # It's used for incomplete packages, static packages.
            return HookResult.skipped(
                package=package_name,
                hook_name="pre_source",
                cwd=src_pkg_dir,
                hook_base_dir=src_pkg_dir
            )

    if not pkg_config or not pkg_config.hooks or not pkg_config.hooks.pre_source:
        return HookResult.skipped(
            package=package_name,
            hook_name="pre_source",
            cwd=src_pkg_dir,
            hook_base_dir=src_pkg_dir
        )

    hook_file = pkg_config.hooks.pre_source
    hook_file_path = Path(hook_file)
    if hook_file_path.is_absolute():
        nominal_hook_path = hook_file_path
    else:
        nominal_hook_path = src_pkg_dir / hook_file_path

    if not nominal_hook_path.exists():
        err_msg = f"Lifecycle hook file specified for 'pre_source' in package '{package_name}' not found: {nominal_hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    def _execute() -> HookResult:
        # Check if nominal hook path is inside src_pkg_dir
        if is_relative_to(nominal_hook_path, src_pkg_dir):
            from .render_package import render_or_copy_file
            target_render_dir = workspace_config.render_path / package_name
            dest_rel_path, _ = render_or_copy_file(
                file_path=nominal_hook_path,
                package_dir=src_pkg_dir,
                render_pkg_dir=target_render_dir,
                workspace_config=workspace_config
            )
            hook_exec_path = target_render_dir / dest_rel_path
        else:
            hook_exec_path = nominal_hook_path

        res = execute_hook_script(
            hook_path=hook_exec_path,
            pkg=package_name,
            hook_name="pre_source",
            metadata=pkg_config,
            cwd=src_pkg_dir
        )
        res.hook_base_dir = str(src_pkg_dir)
        return res

    if load_envs:
        with pkg_config.package_envs(workspace_config):
            return _execute()
    else:
        return _execute()


def trigger_package_lifecycle_hook(
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    hook_base_dir: Path,
    cwd: Path,
) -> HookResult:
    """Executes a package lifecycle hook script if specified and found.

    This function automatically checks if the hook is configured on the package metadata.
    If the hook is not set, it returns a HookResult with status="SKIPPED".
    The `no_hooks` flag is handled upstream in PackageHooks.trigger().

    Returns:
        HookResult detailing execution status ("SUCCESS" or "SKIPPED"), duration, CWD, and script path.

    Raises:
        FileNotFoundError: If the configured hook script file does not exist on disk.
        RuntimeError: If the hook script execution fails or times out.
    """
    hook_file = getattr(metadata.hooks, hook_name, None) if metadata and metadata.hooks else None
    if not hook_file:
        return HookResult.skipped(
            package=pkg,
            hook_name=hook_name,
            cwd=cwd,
            hook_base_dir=hook_base_dir
        )

    hook_file_path = Path(hook_file)
    hook_path = hook_file_path if hook_file_path.is_absolute() else hook_base_dir / hook_file_path

    res = execute_hook_script(
        hook_path=hook_path,
        pkg=pkg,
        hook_name=hook_name,
        metadata=metadata,
        cwd=cwd
    )
    res.hook_base_dir = str(hook_base_dir)
    return res

