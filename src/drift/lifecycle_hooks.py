import sys
import time
import logging
import shlex
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast, Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig
    from .render_engine_config import RenderEngineRegistry

from .package_config import PackageConfig
from .file_utils import is_relative_to
from .process_utils import run_command
from .result_models import HookResult
from .constants import DEFAULT_HOOK_NON_INTERACTIVE_ENVS, INITIAL_ENV
from .env_utils import env_scope
from .exceptions import HookExecutionError

logger = logging.getLogger(__name__)


@dataclass
class HookExecFlags:
    """Execution control flags for lifecycle hooks.

    Attributes:
        no_hooks: When True, skips hook execution and returns a SKIPPED HookResult.
        streaming: When True, streams stdout and stderr in real time.
        inject_non_interactive_envs: When True, injects non-interactive environment
            variables (e.g. PAGER=cat, CI=true) into the hook environment.
        raise_on_error: When True, raises RuntimeError if hook fails or times out.
        load_envs: When True, loads package-defined environments and secrets.
    """
    no_hooks: bool = False
    streaming: bool = True
    inject_non_interactive_envs: bool = True
    raise_on_error: bool = True
    load_envs: bool = True

    @classmethod
    def resolve(
        cls,
        flags: Optional["HookExecFlags"] = None,
    ) -> "HookExecFlags":
        """Resolves execution flags, falling back to defaults if not provided."""
        if flags is not None:
            return flags
        return cls()


def build_hook_execution_command_win32(hook_path: Path) -> List[str]:
    """Generates Windows invocation command based on file extension."""
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


def build_hook_execution_command_posix(hook_path: Path) -> List[str]:
    """Generates POSIX invocation command based on permissions, file extension, and shebang."""
    try:
        is_exec = bool(hook_path.stat().st_mode & 0o111)
    except Exception:
        is_exec = True

    if is_exec:
        return [str(hook_path)]

    # If not executable on disk, fallback to interpreter to avoid mutating disk permissions at runtime
    ext = hook_path.suffix.lower()
    if ext in (".sh", ".bash"):
        return ["/bin/bash", str(hook_path)]
    elif ext == ".py":
        return [sys.executable, str(hook_path)]

    # Check shebang line
    try:
        with hook_path.open("r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()
            if first_line.startswith("#!"):
                shebang = first_line[2:].strip()
                shebang_args = shlex.split(shebang)
                if shebang_args:
                    return shebang_args + [str(hook_path)]
    except Exception:
        pass

    return ["/bin/bash", str(hook_path)]


def build_hook_execution_command(hook_path: Path) -> List[str]:
    """Generates cross-platform invocation command based on file extension, shebang, permissions, and OS."""
    if sys.platform == "win32":
        return build_hook_execution_command_win32(hook_path)
    return build_hook_execution_command_posix(hook_path)


def execute_hook_command(
    cmd: List[str],
    cwd: Path,
    timeout_seconds: int,
    streaming: bool = True,
) -> subprocess.CompletedProcess:
    """Executes a lifecycle hook command in user space with timeout."""
    return run_command(
        cmd,
        cwd=str(cwd),
        text=True,
        timeout=timeout_seconds,
        streaming=streaming
    )


def execute_hook_script(
    hook_path: Path,
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    cwd: Path,
    custom_timeout: Optional[int] = None,
    flags: Optional[HookExecFlags] = None,
) -> HookResult:
    """Executes a hook script in user space with cwd validation, full environment inheritance, and timeout/error handling.

    Returns:
        HookResult: Structured result with status ("SUCCESS" or "FAILED"), duration_ms, exit code,
            hook path, CWD, stdout, stderr, and sudo elevation flag (always False).

    Raises:
        FileNotFoundError: If the hook script file does not exist on disk.
        RuntimeError: If flags.raise_on_error is True and the hook script command times out or exits with a non-zero return code.
    """
    if not hook_path.exists():
        err_msg = f"Lifecycle hook file specified for '{hook_name}' in package '{pkg}' not found: {hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    assert cwd.is_absolute(), f"Working directory '{cwd}' must be absolute."

    logger.info(f"🪝  Triggering hook: {hook_name} ({pkg})")
    logger.debug(f"   Script: {hook_path}")
    logger.debug(f"   CWD:    {cwd}")

    cmd = build_hook_execution_command(hook_path)
    timeout_seconds = custom_timeout if custom_timeout is not None else metadata.hooks.timeout

    start_time = time.perf_counter()
    exec_flags = HookExecFlags.resolve(flags)

    env_injections = DEFAULT_HOOK_NON_INTERACTIVE_ENVS if exec_flags.inject_non_interactive_envs else {}
    with env_scope(env_injections, overwrite=True):
        try:
            proc = execute_hook_command(
                cmd=cmd,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                streaming=exec_flags.streaming,
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
                sudo=False,
                duration_ms=duration_ms,
                stdout=proc.stdout if proc else None,
                stderr=proc.stderr if proc else None
            )
        except subprocess.TimeoutExpired as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            stdout_str = cast(str, e.stdout) or ""
            stderr_str = cast(str, e.stderr) or ""
            display_cmd = cmd
            err_msg = (
                f"Lifecycle hook '{hook_name}' for package '{pkg}' timed out after {timeout_seconds} seconds.\n"
                f"Command: {shlex.join(display_cmd)}\n"
            )
            if stdout_str.strip():
                err_msg += f"Stdout:\n{stdout_str.strip()}\n"
            if stderr_str.strip():
                err_msg += f"Stderr:\n{stderr_str.strip()}\n"
            logger.error(err_msg)
            should_rollback = True
            if metadata is not None and hasattr(metadata, "hooks") and metadata.hooks is not None:
                should_rollback = metadata.hooks.should_rollback_on_failure(hook_name)
            if exec_flags.raise_on_error:
                raise HookExecutionError(
                    package=pkg,
                    hook_name=hook_name,
                    message=err_msg,
                    requires_rollback=should_rollback,
                    exit_code=124,
                ) from e
            return HookResult(
                command="hook",
                package=pkg,
                hook_name=hook_name,
                status="FAILED",
                exit_code=124,
                hook_path=str(hook_path),
                cwd=str(cwd),
                sudo=False,
                duration_ms=duration_ms,
                stdout=stdout_str,
                stderr=stderr_str,
                error_message=err_msg
            )
        except subprocess.CalledProcessError as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            stdout_str = e.stdout or ""
            stderr_str = e.stderr or ""
            display_cmd = cmd
            err_msg = (
                f"Lifecycle hook '{hook_name}' for package '{pkg}' failed with exit code {e.returncode}.\n"
                f"Command: {shlex.join(display_cmd)}\n"
            )
            if stdout_str.strip():
                err_msg += f"Stdout:\n{stdout_str.strip()}\n"
            if stderr_str.strip():
                err_msg += f"Stderr:\n{stderr_str.strip()}\n"
            logger.error(err_msg)
            should_rollback = True
            if metadata is not None and hasattr(metadata, "hooks") and metadata.hooks is not None:
                should_rollback = metadata.hooks.should_rollback_on_failure(hook_name)
            if exec_flags.raise_on_error:
                raise HookExecutionError(
                    package=pkg,
                    hook_name=hook_name,
                    message=err_msg,
                    requires_rollback=should_rollback,
                    exit_code=e.returncode,
                ) from e
            return HookResult(
                command="hook",
                package=pkg,
                hook_name=hook_name,
                status="FAILED",
                exit_code=e.returncode,
                hook_path=str(hook_path),
                cwd=str(cwd),
                sudo=False,
                duration_ms=duration_ms,
                stdout=stdout_str,
                stderr=stderr_str,
                error_message=err_msg
            )


def trigger_package_hook_with_render(
    workspace_config: "WorkspaceConfig",
    package_name: str,
    hook_name: str,
    custom_cwd: Optional[Path] = None,  
    custom_timeout: Optional[int] = None,
    flags: Optional[HookExecFlags] = None,
    pkg_config_override: Optional[PackageConfig] = None,
    engines_override: Optional["RenderEngineRegistry"] = None,
) -> HookResult:
    """Executes a package lifecycle hook in the source directory with automatic template rendering.

    If the hook file is located inside the source package directory and matched by a template engine,
    it is rendered into the render sandbox directory first before execution.
    Otherwise it will be executed directly without rendering.

    custom_cwd: Optional working directory for hook execution. If not provided, defaults to the package source directory.
    pkg_config_override: Optional pre-loaded PackageConfig. If not provided, loads from package source dir.
    engines_override: Optional pre-prepared RenderEngineRegistry (skips re-rendering engine input templates).
    """
    exec_flags = HookExecFlags.resolve(flags)
    if exec_flags.no_hooks:
        return HookResult.skipped(package=package_name, hook_name=hook_name)

    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists() or not src_pkg_dir.is_dir():
        raise FileNotFoundError(
            f"Package '{package_name}' source directory not found: {src_pkg_dir}"
        )

    pkg_config = pkg_config_override
    if pkg_config is None:
        try:
            from .package_config import load_package_config_from_source_dir
            pkg_config = load_package_config_from_source_dir(
                package_dir=src_pkg_dir,
                workspace_config=workspace_config
            )
        except FileNotFoundError:
            # Package has no drift_package.toml -> no lifecycle hooks configured
            return HookResult.skipped(
                package=package_name,
                hook_name=hook_name,
                cwd=src_pkg_dir,
                hook_base_dir=src_pkg_dir
            )

    hook_file_val = getattr(pkg_config.hooks, hook_name, None)
    if not hook_file_val:
        return HookResult.skipped(
            package=package_name,
            hook_name=hook_name,
            cwd=src_pkg_dir,
            hook_base_dir=src_pkg_dir
        )

    hook_file_path = Path(hook_file_val)

    # Resolve hook file path relative to package source directory if not absolute
    # Determine the relative subpath of the hook within the package hierarchy
    rel_hook_path: Optional[Path] = None
    for candidate_base in (
        workspace_config.install_path / package_name,
        workspace_config.render_path / package_name,
        src_pkg_dir,
    ):
        if is_relative_to(hook_file_path, candidate_base):
            rel_hook_path = hook_file_path.relative_to(candidate_base)
            break

    if rel_hook_path is None:
        # If hook is outside the package directory hierarchy, it is shared across packages and executed directly
        nominal_hook_path = hook_file_path
    else:
        # If hook is inside the package directory hierarchy, check if it exists in the source directory first
        # If not, check if it can be rendered from the source directory using the package's render engines
        hook_parent_in_src = src_pkg_dir / rel_hook_path.parent
        static_candidate = hook_parent_in_src / rel_hook_path.name
        if static_candidate.exists():
            nominal_hook_path = static_candidate
        else:
            if engines_override is not None:
                hook_engines = engines_override
            else:
                hook_engines = pkg_config.package_render_engines(workspace_config)

            match_info = hook_engines.find_source_file_for_rendered_names(
                hook_parent_in_src,
                [rel_hook_path.name]
            )
            nominal_hook_path = match_info.path if match_info else None

    if not nominal_hook_path or not nominal_hook_path.exists():
        err_msg = f"Lifecycle hook file specified for '{hook_name}' in package '{package_name}' not found: {nominal_hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    if not nominal_hook_path.is_file():
        err_msg = f"Lifecycle hook path specified for '{hook_name}' in package '{package_name}' is not a regular file: {nominal_hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    effective_cwd = custom_cwd or src_pkg_dir

    def _execute() -> HookResult:
        # Check if nominal hook path is inside src_pkg_dir
        if is_relative_to(nominal_hook_path, src_pkg_dir):
            from .render_package import render_or_copy_file, prepare_package_render_engines
            target_render_dir = workspace_config.render_path / package_name
            if engines_override is not None:
                effective_engines = engines_override
            else:
                effective_engines = prepare_package_render_engines(
                    workspace_config=workspace_config,
                    pkg_config=pkg_config,
                    render_pkg_dir=target_render_dir,
                )
            dest_rel_path, _ = render_or_copy_file(
                file_path=nominal_hook_path,
                package_dir=src_pkg_dir,
                render_pkg_dir=target_render_dir,
                workspace_config=workspace_config,
                pkg_config=pkg_config,
                render_engines=effective_engines,
            )
            hook_exec_path = target_render_dir / dest_rel_path
        else:
            hook_exec_path = nominal_hook_path

        res = execute_hook_script(
            hook_path=hook_exec_path,
            pkg=package_name,
            hook_name=hook_name,
            metadata=pkg_config,
            cwd=effective_cwd,
            custom_timeout=custom_timeout,
            flags=exec_flags,
        )
        res.hook_base_dir = str(src_pkg_dir)
        return res

    if exec_flags.load_envs:
        with pkg_config.package_envs(workspace_config):
            return _execute()
    return _execute()


def trigger_pre_source_hook(
    workspace_config: "WorkspaceConfig",
    package_name: str,
    flags: Optional[HookExecFlags] = None,
    pkg_config_override: Optional[PackageConfig] = None,
    engines_override: Optional["RenderEngineRegistry"] = None,
) -> HookResult:
    """Executes the pre_source hook for a package in the source directory."""
    return trigger_package_hook_with_render(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name="pre_source",
        flags=flags,
        pkg_config_override=pkg_config_override,
        engines_override=engines_override,
    )


def trigger_probe_hook(
    workspace_config: "WorkspaceConfig",
    package_name: str,
    flags: Optional[HookExecFlags] = None,
    pkg_config_override: Optional[PackageConfig] = None,
    engines_override: Optional["RenderEngineRegistry"] = None,
) -> HookResult:
    """Executes the probe hook for a package in the source directory."""
    exec_flags = replace(flags, raise_on_error=False) if flags is not None else HookExecFlags(raise_on_error=False)
    return trigger_package_hook_with_render(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name="probe",
        flags=exec_flags,
        pkg_config_override=pkg_config_override,
        engines_override=engines_override,
    )


def trigger_package_hook(
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    cwd: Optional[Path] = None,
    custom_timeout: Optional[int] = None,
    flags: Optional[HookExecFlags] = None,
) -> HookResult:
    """Executes a package hook script if specified and found.

    This function automatically checks if the hook is configured on the package metadata.
    If the hook is not set, it returns a HookResult with status="SKIPPED".
    The `no_hooks` flag is handled upstream in PackageHooks.trigger().

    Returns:
        HookResult detailing execution status ("SUCCESS", "FAILED", or "SKIPPED"), duration, CWD, and script path.

    Raises:
        FileNotFoundError: If the configured hook script file does not exist on disk.
        RuntimeError: If flags.raise_on_error is True and the hook script execution fails or times out.
    """
    exec_flags = HookExecFlags.resolve(flags)
    if exec_flags.no_hooks:
        return HookResult.skipped(
            package=pkg,
            hook_name=hook_name,
            cwd=cwd,
        )

    hook_file = getattr(metadata.hooks, hook_name, None) if metadata and metadata.hooks else None
    if not hook_file:
        logger.debug(f"Hook '{hook_name}' is not configured for package '{pkg}', skipping.")
        return HookResult.skipped(
            package=pkg,
            hook_name=hook_name,
            cwd=cwd,
        )

    hook_path = Path(hook_file)
    effective_cwd = cwd or hook_path.parent

    res = execute_hook_script(
        hook_path=hook_path,
        pkg=pkg,
        hook_name=hook_name,
        metadata=metadata,
        cwd=effective_cwd,
        custom_timeout=custom_timeout,
        flags=exec_flags,
    )
    res.hook_base_dir = str(hook_path.parent)
    return res


