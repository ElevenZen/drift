import sys
import time
import logging
import shlex
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast, Optional, List, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig, SettingsConfig
    from ..config.render_engine_config import RenderEngineRegistry

from ..config.package_config import PackageConfig
from ..utils.process_utils import run_command
from ..core.result_models import HookResult
from ..core.constants import (
    DEFAULT_HOOK_COMMON_ENVS,
    DEFAULT_HOOK_NON_INTERACTIVE_EXTERNAL_ENVS,
    DEFAULT_HOOK_NON_INTERACTIVE_ENVS,
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
)
from ..utils.env_utils import env_scope
from ..core.exceptions import HookExecutionError, mark_logged

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
        settings: Optional["SettingsConfig"] = None,
    ) -> "HookExecFlags":
        """Resolves execution flags, falling back to workspace SettingsConfig defaults if flags are omitted."""
        if flags is not None:
            return flags
        if settings is not None:
            return cls(
                inject_non_interactive_envs=settings.hook_inject_non_interactive_envs
            )
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
    except (OSError, ValueError) as exc:
        logger.debug(f"Could not parse shebang from '{hook_path}': {exc}")

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


@dataclass(frozen=True)
class HookFailureDetails:
    """Diagnostic details extracted from a hook execution failure or timeout."""
    exit_code: int
    headline: str
    stdout: str
    stderr: str


def _extract_str(val: Optional[Union[str, bytes]]) -> str:
    """Normalizes optional str or bytes from process output to a clean string."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def _extract_hook_failure_details(
    exc: Union[subprocess.TimeoutExpired, subprocess.CalledProcessError, Exception],
    pkg: str,
    hook_name: str,
    timeout_seconds: int,
) -> HookFailureDetails:
    """Extracts exit code, headline message, stdout, and stderr from a subprocess failure."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return HookFailureDetails(
            exit_code=124,
            headline=f"Lifecycle hook '{hook_name}' for package '{pkg}' timed out after {timeout_seconds} seconds.",
            stdout=_extract_str(getattr(exc, "stdout", None) or getattr(exc, "output", None)),
            stderr=_extract_str(getattr(exc, "stderr", None)),
        )
    if isinstance(exc, subprocess.CalledProcessError):
        return HookFailureDetails(
            exit_code=exc.returncode,
            headline=f"Lifecycle hook '{hook_name}' for package '{pkg}' failed with exit code {exc.returncode}.",
            stdout=_extract_str(exc.stdout),
            stderr=_extract_str(exc.stderr),
        )
    return HookFailureDetails(
        exit_code=1,
        headline=f"Lifecycle hook '{hook_name}' for package '{pkg}' failed: {exc}",
        stdout="",
        stderr="",
    )


def _format_hook_error_message(
    headline: str,
    cmd: List[str],
    stdout_str: str,
    stderr_str: str,
) -> str:
    """Formats a detailed multiline error message for hook failures."""
    err_msg = (
        f"{headline}\n"
        f"Command: {shlex.join(cmd)}\n"
    )
    if stdout_str.strip():
        err_msg += f"Stdout:\n{stdout_str.strip()}\n"
    if stderr_str.strip():
        err_msg += f"Stderr:\n{stderr_str.strip()}\n"
    return err_msg


def _should_rollback_on_hook_failure(
    metadata: Optional[PackageConfig],
    hook_name: str,
) -> bool:
    """Determines whether hook failure triggers workspace rollback based on package config."""
    if metadata is not None:
        return metadata.hooks.should_rollback_on_failure(hook_name)
    return True


def handle_hook_execution_failure(
    exc: Union[subprocess.TimeoutExpired, subprocess.CalledProcessError, Exception],
    pkg: str,
    hook_name: str,
    hook_path: Path,
    cmd: List[str],
    cwd: Path,
    duration_ms: float,
    timeout_seconds: int,
    metadata: Optional[PackageConfig],
    exec_flags: HookExecFlags,
) -> HookResult:
    """Handles lifecycle hook process failure or timeout, logs diagnostics, and returns HookResult or raises HookExecutionError."""
    details = _extract_hook_failure_details(
        exc=exc,
        pkg=pkg,
        hook_name=hook_name,
        timeout_seconds=timeout_seconds,
    )
    err_msg = _format_hook_error_message(
        headline=details.headline,
        cmd=cmd,
        stdout_str=details.stdout,
        stderr_str=details.stderr,
    )
    logger.error(err_msg)

    should_rollback = _should_rollback_on_hook_failure(metadata, hook_name)

    if exec_flags.raise_on_error:
        raise mark_logged(HookExecutionError(
            package=pkg,
            hook_name=hook_name,
            message=err_msg,
            requires_rollback=should_rollback,
            exit_code=details.exit_code,
        )) from exc

    return HookResult(
        command="hook",
        package=pkg,
        hook_name=hook_name,
        status="FAILED",
        exit_code=details.exit_code,
        hook_path=str(hook_path),
        cwd=str(cwd),
        sudo=False,
        duration_ms=duration_ms,
        stdout=details.stdout,
        stderr=details.stderr,
        error_message=err_msg,
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

    env_injections = {
        **DEFAULT_HOOK_COMMON_ENVS,
        **(DEFAULT_HOOK_NON_INTERACTIVE_EXTERNAL_ENVS if exec_flags.inject_non_interactive_envs else {}),
    }
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
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            return handle_hook_execution_failure(
                exc=e,
                pkg=pkg,
                hook_name=hook_name,
                hook_path=hook_path,
                cmd=cmd,
                cwd=cwd,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                metadata=metadata,
                exec_flags=exec_flags,
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
    exec_flags = HookExecFlags.resolve(flags, settings=workspace_config.settings)
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
            pkg_config = PackageConfig.from_source_dir(
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

    # Determine relative path if package-internal hook, otherwise treat as external hook
    rel_hook_path = pkg_config.hooks.get_relative_path(hook_name)

    if rel_hook_path is None:
        # If hook is an external absolute path outside package hierarchy, execute it directly
        source_hook_path = Path(hook_file_val)
    else:
        # If hook is inside the package directory hierarchy, check if it exists in the source directory first
        # If not, check if it can be rendered from the source directory using the package's render engines
        hook_parent_in_src = src_pkg_dir / rel_hook_path.parent
        static_candidate = hook_parent_in_src / rel_hook_path.name
        if static_candidate.exists():
            source_hook_path = static_candidate
        else:
            if engines_override is not None:
                hook_engines = engines_override
            else:
                hook_engines = pkg_config.package_render_engines(workspace_config)

            match_info = hook_engines.find_source_file_for_rendered_names(
                hook_parent_in_src,
                [rel_hook_path.name]
            )
            source_hook_path = match_info.path if match_info else None

    if not source_hook_path or not source_hook_path.exists():
        err_msg = f"Lifecycle hook file specified for '{hook_name}' in package '{package_name}' not found: {source_hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    if not source_hook_path.is_file():
        err_msg = f"Lifecycle hook path specified for '{hook_name}' in package '{package_name}' is not a regular file: {source_hook_path}"
        logger.error(err_msg)
        raise FileNotFoundError(err_msg)

    def _execute() -> HookResult:
        # Check if source hook path is inside src_pkg_dir
        if rel_hook_path is not None:
            from ..render.render_package import render_subfolder_entries, prepare_package_render_engines
            target_render_dir = workspace_config.render_path / package_name
            hook_dest_dir = target_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
            if engines_override is not None:
                effective_engines = engines_override
            else:
                effective_engines = prepare_package_render_engines(
                    workspace_config=workspace_config,
                    pkg_config=pkg_config,
                    render_pkg_dir=target_render_dir,
                )
            hooks_src_dir = src_pkg_dir / DRIFT_HOOKS_DIR_NAME
            if hooks_src_dir.is_dir():
                render_subfolder_entries(
                    src_dir=hooks_src_dir,
                    dest_dir=hook_dest_dir,
                    drift_root=workspace_config.drift_root,
                    pkg_config=pkg_config,
                    render_engines=effective_engines,
                    skip_drift_hooks=False,
                )
            sub_rel = rel_hook_path.relative_to(DRIFT_HOOKS_DIR_NAME)
            hook_exec_path = hook_dest_dir / sub_rel
            if not hook_exec_path.exists():
                raise RuntimeError(
                    f"Lifecycle hook file '{rel_hook_path}' was not produced after rendering."
                )
        else:
            hook_exec_path = source_hook_path

        effective_cwd = custom_cwd or hook_exec_path.parent

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
        with pkg_config.package_envs():
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


