"""Process execution and administrative privilege utilities."""

import re
import os
import sys
import shlex
import logging
import threading
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
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Logs the command before executing it with subprocess.

    If streaming=True, stdout and stderr are streamed to sys.stdout and sys.stderr in real time
    while executing, output is captured in CompletedProcess without ANSI escape codes, and no
    debug dump is logged after completion.

    If streaming=False, subprocess.run is used with capture_output=True, and clean stdout/stderr
    (stripped of ANSI styling) are logged to logger.debug after process finishes.
    """
    logger.debug(f"External: {cmd if isinstance(cmd, str) else shlex.join(cmd)}")

    if not streaming:
        params: Any = {"check": True, "capture_output": True}
        params.update(kwargs)
        try:
            res = subprocess.run(cmd, **params)
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
            stdout_msg = format_output(e.stdout)
            if stdout_msg:
                logger.debug(f"stdout:\n{stdout_msg}")
            stderr_msg = format_output(e.stderr)
            if stderr_msg:
                logger.debug(f"stderr:\n{stderr_msg}")
            e.stdout = clean_stream_val(e.stdout)
            e.stderr = clean_stream_val(e.stderr)
            raise

    # Streaming mode:
    params = dict(kwargs)
    check = params.pop("check", True)
    timeout = params.pop("timeout", None)
    text = params.pop("text", None)
    universal_newlines = params.pop("universal_newlines", None)
    params.pop("capture_output", None)

    is_text = True if text or universal_newlines else False

    # Force stdout and stderr to PIPE so we can read and forward them in real time
    params["stdout"] = subprocess.PIPE
    params["stderr"] = subprocess.PIPE
    if is_text:
        params["text"] = True

    proc = subprocess.Popen(cmd, **params)

    stdout_chunks: List[Any] = []
    stderr_chunks: List[Any] = []

    def _reader(pipe: Any, target_stream: Any, collector: List[Any]) -> None:
        try:
            if is_text:
                for line in iter(pipe.readline, ""):
                    collector.append(line)
                    try:
                        target_stream.write(line)
                        target_stream.flush()
                    except Exception:
                        pass
            else:
                for chunk in iter(lambda: pipe.read(4096), b""):
                    collector.append(chunk)
                    try:
                        if hasattr(target_stream, "buffer"):
                            target_stream.buffer.write(chunk)
                            target_stream.buffer.flush()
                        else:
                            target_stream.write(chunk.decode("utf-8", errors="replace"))
                            target_stream.flush()
                    except Exception:
                        pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, sys.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, sys.stderr, stderr_chunks), daemon=True)
    t_out.start()
    t_err.start()

    try:
        retcode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stdout_val = "".join(stdout_chunks) if is_text else b"".join(stdout_chunks)
        stderr_val = "".join(stderr_chunks) if is_text else b"".join(stderr_chunks)
        stdout_val = clean_stream_val(stdout_val)
        stderr_val = clean_stream_val(stderr_val)
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout_val, stderr=stderr_val)

    t_out.join()
    t_err.join()

    stdout_val = "".join(stdout_chunks) if is_text else b"".join(stdout_chunks)
    stderr_val = "".join(stderr_chunks) if is_text else b"".join(stderr_chunks)
    stdout_val = clean_stream_val(stdout_val)
    stderr_val = clean_stream_val(stderr_val)

    if check and retcode != 0:
        raise subprocess.CalledProcessError(retcode, cmd, output=stdout_val, stderr=stderr_val)

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=retcode,
        stdout=stdout_val,
        stderr=stderr_val
    )


def run_sudo_command(
    cmd: Union[str, List[str]],
    sudo: bool = True,
    streaming: bool = False,
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Executes a command with cross-platform privilege handling.

    On Linux/macOS: prepends \x27sudo\x27 if sudo is True and user is not already root (euid != 0).
    On Windows: verifies admin privileges if sudo is True, or runs command directly.
    """
    if sudo:
        if sys.platform == "win32":
            if not has_admin_privileges():
                from .exceptions import DriftError
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

    return run_command(cmd, streaming=streaming, **kwargs)
