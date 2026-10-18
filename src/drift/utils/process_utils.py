"""Process execution and administrative privilege utilities."""

import re
import os
import sys
import shlex
import logging
import subprocess
import shutil
from typing import Optional, Union, List, Dict, Mapping, Tuple, Any

logger = logging.getLogger(__name__)

ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1B
    (?:
        [@-Z\\^_]                     # 2-character Fe escape sequences (excluding [ and ])
    |
        \[[0-?]*[ -/]*[@-~]           # CSI sequences (colors, cursor control, etc.)
    |
        \][^\x07\x1b]*(?:\x07|\x1b\\) # OSC sequences (window title, hyperlinks, etc.)
    |
        [()#%*+-][@-~]                # Charset and other 2-character sequences
    )
    """,
    re.VERBOSE,
)


def strip_ansi(text: Any) -> str:
    """Removes ANSI color and style decoration escape codes from text or decoded bytes."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return ANSI_ESCAPE_RE.sub("", str(text))


def clean_stream_val(stream_val: Any) -> Any:
    """Cleans ANSI escape codes from stream values, preserving original str or bytes type."""
    if stream_val is None:
        return None
    if isinstance(stream_val, str):
        return strip_ansi(stream_val)
    if isinstance(stream_val, bytes):
        return strip_ansi(stream_val.decode("utf-8", errors="replace")).encode("utf-8")
    return stream_val


def format_output(stream_val: Any) -> Optional[str]:
    """Formats raw process output (bytes or str) into a clean stripped string without ANSI styling."""
    if stream_val is None:
        return None
    cleaned = strip_ansi(stream_val).rstrip()
    return cleaned if cleaned.strip() else None


def has_admin_privileges() -> bool:
    """Checks if current process has root / administrator privileges."""
    if sys.platform == "win32":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    else:
        return os.geteuid() == 0


def assert_can_escalate() -> None:
    """Checks if administrative/root privileges are available before staging or installing.

    On Windows: ensures the current process is running in an elevated Administrator terminal.
    On POSIX: executes a test probe ('sudo -v' or 'sudo true') to authenticate and prompt the user early.
    If the user fails to authenticate, interrupts, or is not an administrator, raises PermissionError
    (specifically AdminPrivilegeError on Windows).
    """

    if sys.platform == "win32":
        if not has_admin_privileges():
            from ..core.exceptions import AdminPrivilegeError
            raise AdminPrivilegeError(
                "One or more packages require elevated Administrator privileges (sudo = true).\n"
                "Please run Drift from an elevated Administrator PowerShell / Command Prompt window."
            )
    else:
        if os.geteuid() == 0:
            return

        logger.info("Verifying sudo credentials before staging and installation...")
        try:
            # check=False is intentional: we inspect returncode to fall back to 'sudo true' before raising.
            res = subprocess.run(["sudo", "-v"], check=False)
            if res.returncode != 0:
                res = subprocess.run(["sudo", "true"], check=False)
            if res.returncode != 0:
                raise PermissionError(
                    "Failed to acquire sudo credentials. Operation aborted before modifying files."
                )
        except (subprocess.SubprocessError, FileNotFoundError, KeyboardInterrupt) as e:
            raise PermissionError(
                f"Sudo privilege check failed ({e}). Operation aborted."
            ) from e


def _find_windows_bash() -> Optional[str]:
    """Resolves the full path to 'bash' or 'bash.exe' using the current PATH on Windows."""
    return shutil.which("bash") or shutil.which("bash.exe")


def _resolve_windows_bash_executable(
    cmd: List[str],
) -> List[str]:
    """Resolves leading 'bash' or 'bash.exe' tokens to their full executable path on Windows.

    When subprocess.run executes with shell=False on Windows, CreateProcessW searches
    the parent process's Win32 environment rather than Python's updated os.environ["PATH"]
    (or custom env parameter) when lpApplicationName is omitted. Resolving 'bash' / 'bash.exe'
    via shutil.which ensures the exact bash binary on PATH is invoked.
    """
    if cmd and cmd[0].lower() in ("bash", "bash.exe"):
        bash_exe = _find_windows_bash()
        if bash_exe:
            return [bash_exe] + list(cmd[1:])
    return cmd


def normalize_windows_drift_env(
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Normalizes all 'drift_*' environment variable keys to lowercase for Windows child processes.

    On Windows, Python's os.environ is backed by a case-insensitive mapping that automatically
    uppercases all environment variable keys upon insertion (e.g., os.environ['drift_os'] is
    stored internally as 'DRIFT_OS'). When spawning POSIX-emulated child processes on Windows
    (such as Git Bash / MSYS2 hook scripts or template engines like envsubst), they inherit
    the raw environment block and treat variable names case-sensitively, seeing '$DRIFT_OS'
    instead of Drift's canonical lowercase '$drift_os'.

    Converting the environment into a plain case-preserving Python dict with all 'drift_*'
    keys explicitly lowercased ensures CreateProcessW passes exact lowercase 'drift_*' names
    to the child process.
    """
    source_env = base_env if base_env is not None else os.environ
    return {
        (k.lower() if k.lower().startswith("drift_") else k): v
        for k, v in source_env.items()
    }


def run_command(
    cmd: List[str],
    sudo: bool = False,
    streaming: bool = False,
    suppress_output: bool = False,
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Executes a command via subprocess with logging, ANSI cleanup, and optional privilege elevation.

    Strictly accepts an argument list (List[str]) and enforces shell=False.

    If sudo is True:
      - On Linux/macOS: prepends 'sudo' if user is not already root (euid != 0).
      - On Windows: verifies admin privileges, raising AdminPrivilegeError if not elevated.

    Modes:
      - streaming=False (default): subprocess.run is executed with capture_output=True (by default).
        Output is cleaned of ANSI escape codes. If suppress_output=False, stdout and stderr are dumped
        to logger.debug on success. On failure (CalledProcessError), error details are always logged.
      - streaming=True: Child processes inherit standard file descriptors (stdin, stdout, stderr) directly
        without in-memory buffering, allowing real-time terminal streaming and interactive user inputs (e.g.
        typing into interactive editor sessions, sudo passwords, or hook scripts). Consequently,
        CompletedProcess.stdout and CompletedProcess.stderr will be None. Supports standard timeout parameter,
        raising subprocess.TimeoutExpired on timeout.
    """
    if not isinstance(cmd, list):
        raise TypeError(f"run_command expects cmd to be a list of strings (List[str]), got {type(cmd).__name__}.")

    if sudo:
        if sys.platform == "win32":
            if not has_admin_privileges():
                from ..core.exceptions import AdminPrivilegeError
                raise AdminPrivilegeError(
                    "This operation requires elevated Administrator privileges (sudo = true). "
                    "Please run drift from an elevated Administrator terminal / PowerShell window."
                )
        else:
            if not has_admin_privileges():
                if not cmd or cmd[0] != "sudo":
                    cmd = ["sudo"] + list(cmd)

    kwargs["shell"] = False

    if sys.platform == "win32":
        cmd = _resolve_windows_bash_executable(cmd)
        kwargs["env"] = normalize_windows_drift_env(kwargs.get("env"))

    cmd_str = shlex.join(cmd)
    logger.debug(f"[External]  {cmd_str}")

    if streaming:
        params: Any = {"check": True}
        params.update(kwargs)
        params["shell"] = False
        params.pop("capture_output", None)
        if params.get("text") or params.get("universal_newlines"):
            params.setdefault("encoding", "utf-8")
            params.setdefault("errors", "replace")
        try:
            return subprocess.run(cmd, **params)
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed with exit code {e.returncode}: {cmd_str}")
            raise

    params = {"check": True, "capture_output": True}
    params.update(kwargs)
    params["shell"] = False
    if params.get("text") or params.get("universal_newlines"):
        params.setdefault("encoding", "utf-8")
        params.setdefault("errors", "replace")
    try:
        res = subprocess.run(cmd, **params)
        logger.debug(f"[External] Command exit code {res.returncode}: {cmd_str}")
        if not suppress_output:
            stdout_msg = format_output(res.stdout)
            if stdout_msg:
                logger.debug(f"stdout:\n{stdout_msg}")
            stderr_msg = format_output(res.stderr)
            if stderr_msg:
                logger.debug(f"stderr:\n{stderr_msg}")
        res.stdout = clean_stream_val(res.stdout)
        res.stderr = clean_stream_val(res.stderr)
        return res
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}: {cmd_str}")
        stdout_msg = format_output(e.stdout)
        if stdout_msg:
            logger.debug(f"stdout:\n{stdout_msg}")
        stderr_msg = format_output(e.stderr)
        if stderr_msg:
            logger.debug(f"stderr:\n{stderr_msg}")
        e.stdout = clean_stream_val(e.stdout)
        e.stderr = clean_stream_val(e.stderr)
        raise
