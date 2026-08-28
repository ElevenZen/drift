"""Utility functions for file and directory operations using pathlib."""

import os
import sys
import stat
import hashlib
import logging
import shutil
import subprocess
import re
import shlex
import tempfile
from pathlib import Path
from typing import Optional, Union, List, Any

logger = logging.getLogger(__name__)

COMMON_WINDOWS_PATH_ENVS = {
    "USERPROFILE": lambda: str(Path.home()),
    "APPDATA": lambda: str(Path.home() / "AppData" / "Roaming"),
    "LOCALAPPDATA": lambda: str(Path.home() / "AppData" / "Local"),
    "PROGRAMDATA": lambda: r"C:\ProgramData",
    "HOMEDRIVE": lambda: Path.home().drive or "C:",
    "HOMEPATH": lambda: str(Path.home().relative_to(Path.home().anchor)) if Path.home().drive else str(Path.home()),
    "TEMP": lambda: os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home() / "AppData" / "Local" / "Temp"),
    "TMP": lambda: os.environ.get("TMP") or os.environ.get("TEMP") or str(Path.home() / "AppData" / "Local" / "Temp"),
    "SYSTEMROOT": lambda: os.environ.get("SYSTEMROOT") or r"C:\Windows",
    "WINDIR": lambda: os.environ.get("WINDIR") or r"C:\Windows",
    "ALLUSERSPROFILE": lambda: os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData",
    "PROGRAMFILES": lambda: os.environ.get("PROGRAMFILES") or r"C:\Program Files",
    "PROGRAMFILES(X86)": lambda: os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)",
}


def expand_user_and_env(path_input: Union[str, Path]) -> Path:
    """Expands '~', Windows-style '%VAR%', and standard '$VAR' in path inputs."""
    raw = str(path_input).strip()
    if not raw:
        return Path(".")

    # 1. Expand %VAR% syntax ONLY on Windows
    if sys.platform == "win32" and "%" in raw:
        def replace_win_env(match: re.Match) -> str:
            var_name = match.group(1)
            var_upper = var_name.upper()
            if var_name in os.environ:
                return os.environ[var_name]
            if var_upper in os.environ:
                return os.environ[var_upper]
            if var_upper in COMMON_WINDOWS_PATH_ENVS:
                return COMMON_WINDOWS_PATH_ENVS[var_upper]()
            return f"%{var_name}%"

        raw = re.sub(r"%([A-Za-z0-9_()]+)%", replace_win_env, raw)

    # 2. Expand $VAR / ${VAR} syntax
    raw = os.path.expandvars(raw)

    # 3. Expand ~ or ~/ or ~\ at beginning of path cleanly across platforms
    if raw == "~":
        return Path.home()
    if raw.startswith("~/") or raw.startswith("~\\"):
        subpath = raw[2:].lstrip("/\\")
        parts = [p for p in re.split(r"[/\\]+", subpath) if p]
        return Path.home().joinpath(*parts)

    # 4. Normalize backslashes to forward slashes for cross-platform consistency
    if sys.platform == "win32" and "\\" in raw:
        raw = raw.replace("\\", "/")

    # 5. Final expanduser call to ensure clean Path conversion
    return Path(raw).expanduser()


def safe_relative_to(path: Path, other: Path) -> Path:
    """Safely calculates relative path, returning path.resolve() if across different Windows drives."""
    try:
        return path.resolve().relative_to(other.resolve())
    except ValueError:
        return path.resolve()


def is_relative_to(path: Path, other: Path) -> bool:
    """Robust fallback implementation of Path.is_relative_to for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def run_command(cmd: Union[str, List[str]], **kwargs: Any) -> "subprocess.CompletedProcess[Any]":
    """Logs the command before executing it with subprocess.run."""
    logger.debug(f"External: {cmd if isinstance(cmd, str) else shlex.join(cmd)}")
    params: Any = {"check": True, "capture_output": True}
    params.update(kwargs)
    return subprocess.run(cmd, **params)


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


def run_sudo_command(
    cmd: Union[str, List[str]],
    sudo: bool = True,
    **kwargs: Any
) -> "subprocess.CompletedProcess[Any]":
    """Executes a command with cross-platform privilege handling.

    On Linux/macOS: prepends 'sudo' if sudo is True and user is not already root (euid != 0).
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

    return run_command(cmd, **kwargs)


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


def resolve_system_target(relative_path: Path, relative_base: Path) -> Path:
    """
    Applies relative file path to base path,
    expanding user home/envs and applying dot prefix conversion.
    """
    translated_path = translate_dot_prefixes(relative_path)
    target_path = expand_user_and_env(relative_base)
    return target_path / translated_path


def translate_dot_prefixes(relative_path: Path) -> Path:
    """
    Converts 'dot-' prefixes in path segments to leading dots ('.').
    Does not translate 'dot-' or 'dot-.' segments (only segments with suffix after 'dot-').
    """
    translated_parts = ["." + p[4:]
                        if p.startswith("dot-") and p not in ("dot-", "dot-.") else p
                        for p in relative_path.parts]
    assert len(translated_parts) == len(relative_path.parts), "Translation should not change the number of path segments."
    return Path(*translated_parts)


def translate_dot_prefixes_reverse(relative_path: Path) -> Path:
    """
    Converts leading dots ('.') in path segments to 'dot-', which is the opposite of translate_dot_prefixes().
    Does not translate '.' and '..' segments.
    """
    translated_parts = ["dot-" + p[1:]
                        if p.startswith(".") and p not in (".", "..") else p
                        for p in relative_path.parts]
    assert len(translated_parts) == len(relative_path.parts), "Reverse translation should not change the number of path segments."
    return Path(*translated_parts)


def tree_relative_files(dir_path: Path) -> List[Path]:
    """
    Gets all files in dir_path recursively as Path objects relative to dir_path.
    tree_relative_files() is used only when there is no symlink and no comparison.
    Otherwise, compare_folders() is used to detect untracked files and resolve links.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    relative_files = []
    for entry in dir_path.rglob("*"):
        if entry.is_file():
            relative_files.append(entry.relative_to(dir_path))
    return sorted(relative_files)


def get_relative_path(from_dir: Path, to_path: Path) -> Path:
    """Computes the relative path from from_dir to to_path using only Path objects."""
    abs_from = from_dir.resolve()
    abs_to = to_path.resolve()
    
    from_parts = abs_from.parts
    to_parts = abs_to.parts
    
    common_idx = 0
    while common_idx < len(from_parts) and common_idx < len(to_parts) and from_parts[common_idx] == to_parts[common_idx]:
        common_idx += 1
        
    ups = [".."] * (len(from_parts) - common_idx)
    downs = list(to_parts[common_idx:])
    
    return Path(*ups).joinpath(*downs)


def compute_file_hash(file_path: Path) -> str:
    """Computes md5 hash of a file for efficient change tracking."""
    h = hashlib.md5()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


from .constants import LineEnding


def is_binary_file(file_path: Path) -> bool:
    """Detects whether a file is binary by scanning for null bytes in the first 8KB."""
    try:
        with file_path.open("rb") as f:
            chunk = f.read(8192)
            return b"\x00" in chunk
    except Exception:
        return False


def normalize_newlines_bytes(content: bytes, line_ending: LineEnding = LineEnding.LF) -> bytes:
    """Translates newlines between LF, CRLF, or PRESERVE at the byte level.
    
    Preserves raw encoding and special characters without Unicode decoding errors.
    """
    if line_ending == LineEnding.CRLF:
        return content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    elif line_ending == LineEnding.LF:
        return content.replace(b"\r\n", b"\n")
    else:
        return content


def file_contents_differ(file1: Path, file2: Path, convert_line_endings: Optional[bool] = None) -> bool:
    """Returns True if the contents of file1 and file2 differ using stream comparison.
    
    If convert_line_endings is True (default on Windows), ignores LF vs CRLF line ending differences on text files.
    """
    if not file1.exists() and not file2.exists():
        return False
    if not file1.exists() or not file2.exists():
        return True
    if not file1.is_file() or not file2.is_file():
        raise ValueError("Both paths must be files for content comparison.")
    if file1.resolve() == file2.resolve():
        return False

    if convert_line_endings is None:
        convert_line_endings = (sys.platform == "win32")

    if convert_line_endings:
        # If either file is binary, compare byte sizes and stream chunks
        if is_binary_file(file1) or is_binary_file(file2):
            if file1.stat().st_size != file2.stat().st_size:
                return True
            with file1.open("rb") as f1, file2.open("rb") as f2:
                for chunk1, chunk2 in zip(iter(lambda: f1.read(65536), b""), iter(lambda: f2.read(65536), b"")):
                    if chunk1 != chunk2:
                        return True
            return False
        else:
            # Text files: compare LF-normalized bytes
            b1 = normalize_newlines_bytes(file1.read_bytes(), line_ending=LineEnding.LF)
            b2 = normalize_newlines_bytes(file2.read_bytes(), line_ending=LineEnding.LF)
            return b1 != b2
    else:
        if file1.stat().st_size != file2.stat().st_size:
            return True
        with file1.open("rb") as f1, file2.open("rb") as f2:
            for chunk1, chunk2 in zip(iter(lambda: f1.read(65536), b""), iter(lambda: f2.read(65536), b"")):
                if chunk1 != chunk2:
                    return True
        return False


def rmdir_parents(dir_path: Path, limit_dir: Path) -> None:
    """Recursively removes empty directories from dir_path up to limit_dir."""
    curr = dir_path.resolve()
    limit = limit_dir.resolve()
    while curr and curr != limit and is_relative_to(curr, limit):
        if curr.exists() and curr.is_dir() and not any(curr.iterdir()):
            try:
                curr.rmdir()
            except OSError:
                break
            curr = curr.parent
        else:
            break


def get_symlinked_parent(file_path: Path, link_target_range: Path) -> Optional[Path]:
    """
    If the file_path is a symlink and links to a target within link_target_range, returns file_path itself.
    Otherwise, traverses up the directory tree to find the nearest parent directory that is a symlink pointing into link_target_range.
    Returns None if no such symlinked parent is found.
    The value returned is the symlink Path object itself, not its resolved target. It's always a prefix of file_path.
    """
    # don't resolve at the beginning, we want to check the symlink itself, not its target.
    cursor = file_path
    home_dir = Path.home()
    abs_drift_root = link_target_range.resolve()
    while cursor and cursor != Path("/") and cursor != home_dir:
        if cursor.is_symlink():
            try:
                link_str = cursor.readlink()
                abs_link_target = (cursor.parent / link_str).resolve()
                
                if is_relative_to(abs_link_target, abs_drift_root):
                    return cursor
            except Exception:
                # because it's a parent dir, very unlikely to fail the resolve process,
                # but if it does, we ignore and continue up the tree
                pass
        # iterate up the directory tree
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return None



def unlock_file_or_dir_if_windows(path: Path) -> None:
    """Removes the Windows Read-Only file attribute (FILE_ATTRIBUTE_READONLY) if running on Windows.

    On Windows, attempting to delete, overwrite, or move a read-only file/directory throws
    PermissionError: [WinError 5] Access is denied until the read-only bit is removed (stat.S_IWRITE).
    If path is a directory, recursively unlocks all nested files and subdirectories.
    Does nothing on non-Windows platforms.
    """
    if sys.platform != "win32":
        return
    try:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                    except Exception:
                        pass
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass
        else:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


unlock_file_if_windows = unlock_file_or_dir_if_windows
clear_readonly_attribute = unlock_file_or_dir_if_windows


def atomic_copy_symlink(src: Path, dst: Path) -> None:
    """Atomically copies/recreates a symlink from src to dst using a temporary sibling link."""
    dst_parent = dst.parent
    dst_parent.mkdir(parents=True, exist_ok=True)
    unlock_file_or_dir_if_windows(dst)

    link_target = os.readlink(src)
    fd, temp_name = tempfile.mkstemp(dir=dst_parent, prefix=f".tmp_{dst.name}_")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        temp_path.unlink()
        temp_path.symlink_to(link_target)
        unlock_file_or_dir_if_windows(dst)
        os.replace(temp_path, dst)
    finally:
        if temp_path.exists() or temp_path.is_symlink():
            try:
                temp_path.unlink()
            except Exception:
                pass


def atomic_copy_file(
    src: Path,
    dst: Path,
    line_ending: LineEnding = LineEnding.PRESERVE,
    follow_symlinks: bool = True
) -> None:
    """Atomically copies a single file from src to dst using a temporary sibling file and atomic replacement.

    Guarantees destination is never left in a truncated, empty, or partially-written state.
    On POSIX, os.replace performs an atomic directory inode pointer swap (rename(2)).
    On Windows, clears read-only attributes before replacement.
    """
    dst_parent = dst.parent
    dst_parent.mkdir(parents=True, exist_ok=True)
    unlock_file_or_dir_if_windows(dst)

    if not follow_symlinks and src.is_symlink():
        atomic_copy_symlink(src, dst)
        return

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=dst_parent, prefix=f".tmp_{dst.name}_", delete=False) as tf:
            temp_path = Path(tf.name)

        if line_ending != LineEnding.PRESERVE and not is_binary_file(src):
            raw_bytes = src.read_bytes()
            converted = normalize_newlines_bytes(raw_bytes, line_ending=line_ending)
            temp_path.write_bytes(converted)
            try:
                shutil.copymode(src, temp_path, follow_symlinks=follow_symlinks)
            except Exception:
                pass
        else:
            shutil.copy2(src, temp_path, follow_symlinks=follow_symlinks)

        unlock_file_or_dir_if_windows(dst)
        os.replace(temp_path, dst)
    finally:
        if temp_path and (temp_path.exists() or temp_path.is_symlink()):
            try:
                temp_path.unlink()
            except Exception:
                pass


def backup_and_delete_one_file(
    file_path: Path,
    backup_dest: Path,
    limit_dir: Optional[Path] = None
) -> None:
    """Backs up a file to backup_dest atomically, deletes it, and cleans up empty parent directories up to limit_dir."""
    if not file_path.exists():
        return

    unlock_file_or_dir_if_windows(backup_dest)
    backup_dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy_file(file_path, backup_dest)
    unlock_file_or_dir_if_windows(file_path)
    remove_file_or_dir(file_path)
    if limit_dir:
        rmdir_parents(file_path.parent, limit_dir)


def copy_or_move_file_or_dir_external(
    src: Path,
    dst: Path,
    sudo: bool,
    chown: bool = True,
    move: bool = False,
    resolve_symlinks: bool = True,
) -> None:
    """Use external utility commands or Python builtins to move or copy file/directory,

    using sudo on POSIX if requested, chown after copy if requested,
    resolving symlinks recursively if resolve_symlinks is True.
    On Windows, falls back directly to Python standard library (shutil/pathlib).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    unlock_file_or_dir_if_windows(dst)
    if move:
        unlock_file_or_dir_if_windows(src)

    if sys.platform == "win32":
        if move:
            if dst.exists() or dst.is_symlink():
                remove_file_or_dir(dst)
            shutil.move(str(src), str(dst))
        else:
            if src.is_dir() and not src.is_symlink():
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True, symlinks=not resolve_symlinks)
            else:
                atomic_copy_file(src, dst, follow_symlinks=resolve_symlinks)
        return

    if move and not resolve_symlinks:
        cmd = ["mv", str(src), str(dst)]
    else:
        if src.is_dir():
            cmd = ["cp", "-RP" if not resolve_symlinks else "-RL", str(src), str(dst)]
        else:
            cmd = ["cp", "-P" if not resolve_symlinks else "-L", str(src), str(dst)]

    run_sudo_command(cmd, sudo=sudo)

    if sudo and chown:
        # Attempt to chown the backup to the current process owner if sudo was used, to avoid permission issues later.
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid is not None and gid is not None:
                chown_cmd = ["chown", "-R", f"{uid}:{gid}", str(dst)]
                run_sudo_command(chown_cmd, sudo=True)
        except Exception as e:
            logger.warning(f"Failed to chown backup to process owner: {e}")

    if move:
        del_cmd = ["rm", "-rf", str(src)]
        run_sudo_command(del_cmd, sudo=sudo)


def ensure_directory_writable(path: Path, sudo: bool) -> None:
    """Checks if a directory path (or its closest existing parent) is writable."""
    if sudo:
        return  # With sudo, we assume target is writable or handled by elevation
    curr = path.resolve()
    while curr:
        if curr.exists():
            if curr.is_dir() and os.access(curr, os.W_OK | os.X_OK):
                return
            else:
                raise PermissionError(
                    f"Directory '{curr}' is not writable. "
                    "Please check permissions or configure sudo for this package."
                )
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    raise PermissionError(f"Target directory path '{path}' is invalid or inaccessible.")


def ensure_dir_exists_with_sudo(path: Path, sudo: bool) -> None:
    """Ensures directory exists, creating with sudo on POSIX if requested, or pathlib on Windows."""
    if path.exists():
        return
    if sudo and sys.platform != "win32":
        run_sudo_command(["mkdir", "-p", str(path)], sudo=True)
    else:
        path.mkdir(parents=True, exist_ok=True)


def remove_file_or_dir(path: Path) -> None:
    """Safely removes a file, symlink, or directory tree using Python standard libraries."""
    unlock_file_or_dir_if_windows(path)
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def remove_file_or_dir_with_sudo(path: Path, sudo: bool) -> None:
    """Safely removes a file, symlink, or directory using sudo on POSIX if requested, or Python builtins on Windows."""
    unlock_file_or_dir_if_windows(path)
    if path.exists() or path.is_symlink():
        if sudo and sys.platform != "win32":
            cmd_rm = ["rm", "-rf" if path.is_dir() and not path.is_symlink() else "-f", str(path)]
            run_sudo_command(cmd_rm, sudo=True)
        else:
            remove_file_or_dir(path)


def create_symlink_manually_with_sudo(src: Path, dst: Path, sudo: bool) -> None:
    """Creates a symlink from src to dst manually, cleaning up existing file/link with sudo if requested."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    unlock_file_or_dir_if_windows(dst)
    remove_file_or_dir_with_sudo(dst, sudo)

    if sys.platform == "win32" or not sudo:
        dst.symlink_to(src, target_is_directory=src.is_dir())
    else:
        cmd = ["ln", "-s", str(src), str(dst)]
        run_sudo_command(cmd, sudo=True)


def write_file_contents_with_sudo(
    dst: Path,
    content: Union[str, bytes],
    sudo: bool = False,
    permission: Optional[int] = None
) -> None:
    """Writes string or bytes content to dst file atomically with directory creation, sudo handling, and permission setting."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    unlock_file_or_dir_if_windows(dst)

    temp_dir = dst.parent if not sudo else None
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=temp_dir, prefix=f".tmp_{dst.name}_", delete=False) as tf:
            temp_path = Path(tf.name)
            if isinstance(content, str):
                tf.write(content.encode("utf-8"))
            else:
                tf.write(content)
        if permission is not None:
            try:
                temp_path.chmod(permission)
            except Exception:
                pass
        if sys.platform == "win32" or not sudo:
            unlock_file_or_dir_if_windows(dst)
            os.replace(temp_path, dst)
        else:
            # POSIX with sudo: mv -f performs atomic replacement with best effort (rename)
            cmd = ["mv", "-f", str(temp_path), str(dst)]
            run_sudo_command(cmd, sudo=True)
    finally:
        if temp_path and (temp_path.exists() or temp_path.is_symlink()):
            try:
                temp_path.unlink()
            except Exception:
                pass


def copy_file_contents_with_sudo(
    src: Path,
    dst: Path,
    sudo: bool = False,
    line_ending: LineEnding = LineEnding.PRESERVE
) -> None:
    """Copies a physical file atomically from src to dst, with sudo on POSIX if requested.

    If line_ending is not LineEnding.PRESERVE and src is a text file, translates newlines.
    """
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    unlock_file_or_dir_if_windows(dst)

    if sys.platform == "win32" or not sudo:
        atomic_copy_file(src, dst, line_ending=line_ending)
        return

    # POSIX system and sudo
    if line_ending != LineEnding.PRESERVE and not is_binary_file(src):
        raw_bytes = src.read_bytes()
        converted = normalize_newlines_bytes(raw_bytes, line_ending=line_ending)
        perm = src.stat().st_mode if src.exists() else None
        write_file_contents_with_sudo(dst, converted, sudo=True, permission=perm)
        return

    # Guaranteed atomic copy for privileged destinations on POSIX:
    # 1. Create temporary sibling file in dst.parent via elevated mktemp
    # 2. Copy source content & permissions into temp file
    # 3. Atomically replace destination via elevated rename (mv -f)
    temp_path_str = None
    try:
        res = run_sudo_command(
            ["mktemp", "-p", str(dst.parent), f".tmp_{dst.name}_XXXXXX"],
            sudo=True,
            text=True
        )
        temp_path_str = str(res.stdout).strip()
        if temp_path_str:
            run_sudo_command(["cp", "-p", str(src), temp_path_str], sudo=True)
            run_sudo_command(["mv", "-f", temp_path_str, str(dst)], sudo=True)
            return
    except Exception:
        # Fallback to direct elevated cp if mktemp fails
        pass
    finally:
        # Clean up temporary file if an error occurred before mv
        if temp_path_str and Path(temp_path_str).name != dst.name:
            try:
                run_sudo_command(["rm", "-f", temp_path_str], sudo=True)
            except Exception:
                pass

    # Direct fallback from Exception
    cmd = ["cp", "-p", str(src), str(dst)]
    run_sudo_command(cmd, sudo=True)


def sync_broken_symlink(src: Path, dst: Path) -> None:
    """
    Safely copies/syncs a broken symlink from src to dst.
    Reads the raw link value and recreates it at dst if it does not already match.
    """
    try:
        link_val = os.readlink(src)
        if not dst.is_symlink() or os.readlink(dst) != link_val:
            logger.info(f"Broken Link Sync: '{src}' is a broken symlink pointing to '{link_val}'. Copying symlink itself...")
            unlock_file_or_dir_if_windows(dst)
            remove_file_or_dir(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(link_val)
    except Exception as e:
        logger.warning(f"Failed to copy broken symlink '{src}': {e}")


