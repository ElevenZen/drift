"""Process execution and administrative privilege utilities."""

import re
import os
import sys
import shlex
import logging
import subprocess
from typing import Optional, Union, List, Any

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


def check_sudo_privilege(sudo_required: bool = True) -> None:
    """Checks if administrative/root privileges are available before staging or installing.

    On Windows: ensures the current process is running in an elevated Administrator terminal.
    On POSIX: executes a test probe ('sudo -v' or 'sudo true') to authenticate and prompt the user early.
    If the user fails to authenticate, interrupts, or is not an administrator, raises PermissionError.
    """
    if not sudo_required:
        return

    if sys.platform == "win32":
        if not has_admin_privileges():
            raise PermissionError(
                "One or more packages require elevated Administrator privileges (sudo = true).\n"
                "Please run Drift from an elevated Administrator PowerShell / Command Prompt window."
            )
    else:
        if os.geteuid() == 0:
            return

        logger.debug("Prompting / verifying sudo credentials before staging and installation...")
        try:
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


def run_command(
    cmd: Union[str, List[str]],
    streaming: bool = False,
    suppress_output: bool = False,
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Logs the command before executing it with subprocess.

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
    cmd_str = cmd if isinstance(cmd, str) else shlex.join(cmd)
    logger.debug(f"External: {cmd_str}")

    if streaming:
        params: Any = {"check": True}
        params.update(kwargs)
        params.pop("capture_output", None)
        try:
            return subprocess.run(cmd, **params)
        except subprocess.CalledProcessError as e:
            logger.error(f"Command failed with exit code {e.returncode}: {cmd_str}")
            raise

    params = {"check": True, "capture_output": True}
    params.update(kwargs)
    try:
        res = subprocess.run(cmd, **params)
        logger.debug(f"Command finished with exit code {res.returncode}: {cmd_str}")
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


def run_sudo_command(
    cmd: Union[str, List[str]],
    sudo: bool = True,
    streaming: bool = False,
    suppress_output: bool = False,
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Executes a command with cross-platform privilege handling.

    On Linux/macOS: prepends 'sudo' if sudo is True and user is not already root (euid != 0).
    On Windows: verifies admin privileges if sudo is True, or runs command directly.
    """
    if sudo:
        if sys.platform == "win32":
            if not has_admin_privileges():
                from ..core.exceptions import DriftError
                raise DriftError(
                    "This operation requires elevated Administrator privileges (sudo = true). "
                    "Please run drift from an elevated Administrator terminal / PowerShell window."
                )
        else:
            if not has_admin_privileges():
                if isinstance(cmd, list):
                    if not cmd or cmd[0] != "sudo":
                        cmd = ["sudo"] + list(cmd)
                elif isinstance(cmd, str):
                    if not cmd.startswith("sudo "):
                        cmd = f"sudo {cmd}"

    return run_command(cmd, streaming=streaming, suppress_output=suppress_output, **kwargs)
