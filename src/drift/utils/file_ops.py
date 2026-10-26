"""Destructive file and directory operations: copy, write, remove, symlink.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

All functions in this module perform filesystem mutations. Every destructive
operation that may require elevated privileges accepts a `sudo: bool = False`
parameter for transparent privilege escalation on POSIX.

Atomic Operations (temp-file + rename, never leaves partial state):
    copy_file(src, dst, sudo, line_ending, follow_symlinks)
        copy_symlink(src, dst, sudo) [internal]
        copy_permissions(src, dst, sudo) [internal]
        _atomic_copy_local(src, dst, line_ending, follow_symlinks) [private]
        _atomic_copy_elevated(src, dst) [private]
    write_file(dst, content, sudo, permission) — Atomic write from string/bytes
        (permission=None preserves an existing file's mode; new files get 0o666 & ~umask).

Direct Operations:
    copy_symlink(src, dst, sudo) — Recreate symlink pointing to src's target.
    copy_permissions(src, dst, sudo) — chmod permissions sync.
    remove_tree(path, sudo) — Remove file/symlink/dir tree.
    remove_file_or_empty_dir(path, sudo) — Safely remove file, symlink, or empty dir.
    remove_with_parents(file_path, limit_dir) — Remove + prune empty ancestors.
    create_symlink(src, dst, sudo) — Create symlink with cleanup.
    ensure_dir(path, sudo) — mkdir -p with optional elevation.
    assert_writable(path, sudo) — Writability check walking up parents.
    prune_empty_parents(dir_path, limit_dir) — Remove empty dirs up to limit.
    clear_readonly(path) — Windows read-only attribute clearing (no-op on POSIX).

Bulk Operations (external commands):
    copy_tree(src, dst, sudo, chown, resolve_symlinks) — Directory tree copy.
    move_tree(src, dst, sudo, chown, resolve_symlinks) — Directory tree move.

===============================================================================
"""

import os
import sys
import stat
import shutil
import logging
import tempfile
from pathlib import Path
from typing import Optional, Union

from ..core.constants import LineEnding
from .func_utils import branch, choose, when_none
from .path_utils import is_relative_to
from .file_inspect import is_binary_file, normalize_newlines, is_mode_only_change, is_concrete_dir
from .process_utils import run_command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

def clear_readonly(path: Path) -> None:
    """Removes the Windows Read-Only file attribute (FILE_ATTRIBUTE_READONLY).

    On Windows, attempting to delete, overwrite, or move a read-only file/directory throws
    PermissionError: [WinError 5] Access is denied until the read-only bit is removed.
    If path is a directory, recursively unlocks all nested files and subdirectories.
    Does nothing on non-Windows platforms.
    """
    if sys.platform != "win32":
        return
    try:
        if path.is_symlink() or not path.exists():
            return
        if not path.is_dir():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            return

        # walk in given dir.
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        for root, dirs, files in os.walk(path):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except OSError as exc:
                    logger.debug(f"Failed to unlock directory '{os.path.join(root, d)}': {exc}")
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                except OSError as exc:
                    logger.debug(f"Failed to unlock file '{os.path.join(root, f)}': {exc}")
    except OSError as exc:
        logger.debug(f"Failed to walk/unlock '{path}': {exc}")


# ---------------------------------------------------------------------------
# Directory operations
# ---------------------------------------------------------------------------

def ensure_dir(path: Path, sudo: bool = False) -> None:
    """Ensures directory exists, creating with sudo on POSIX if requested, or pathlib on Windows.

    Raises:
        NotADirectoryError: If path exists and is not a concrete directory (e.g. regular file or symlink).
    """
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise NotADirectoryError(f"Cannot ensure directory '{path}': path exists and is not a directory.")
    if path.is_dir():
        return
    if sudo and sys.platform != "win32":
        run_command(["mkdir", "-p", path.as_posix()], sudo=True)
    else:
        path.mkdir(parents=True, exist_ok=True)


def assert_writable(path: Path, sudo: bool = False) -> None:
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


def prune_empty_parents(dir_path: Path, limit_dir: Path) -> None:
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


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def copy_permissions(src: Path, dst: Path, sudo: bool = False) -> None:
    """Copies file mode (permissions) from src to dst without rewriting file contents."""
    if sys.platform == "win32" or not src.exists() or not dst.exists():
        return
    mode = src.stat().st_mode
    if not sudo:
        try:
            dst.chmod(mode)
        except OSError as exc:
            logger.warning(f"Failed to copy file mode from '{src}' to '{dst}': {exc}")
    else:
        octal_mode = oct(mode & 0o777)[2:]
        cmd = ["chmod", octal_mode, dst.as_posix()]
        run_command(cmd, sudo=True)


# ---------------------------------------------------------------------------
# Remove operations
# ---------------------------------------------------------------------------

def remove_tree(path: Path, sudo: bool = False) -> None:
    """Safely removes a file, symlink, or directory tree.

    Uses sudo on POSIX if requested, or Python builtins on Windows / non-sudo.
    """
    clear_readonly(path)
    if not (path.exists() or path.is_symlink()):
        return
    if sudo and sys.platform != "win32":
        cmd_rm = ["rm", "-rf" if is_concrete_dir(path) else "-f", path.as_posix()]
        run_command(cmd_rm, sudo=True)
    else:
        if is_concrete_dir(path):
            shutil.rmtree(path)
        else:
            path.unlink()


def remove_file_or_empty_dir(path: Path, sudo: bool = False) -> bool:
    """Safely removes a file, symlink, or strictly empty directory.

    If path is a non-empty directory, it is left untouched and False is returned.
    Uses sudo on POSIX if requested.
    """
    clear_readonly(path)
    if not (path.exists() or path.is_symlink()):
        return False
    if is_concrete_dir(path):
        try:
            if any(path.iterdir()):
                logger.warning(f"Skipping removal of non-empty directory: {path}")
                return False
        except OSError as e:
            logger.warning(f"Could not inspect directory entries for '{path}': {e}")
            return False

        if sudo and sys.platform != "win32":
            run_command(["rmdir", path.as_posix()], sudo=True)
        else:
            path.rmdir()
        return True
    else:
        if sudo and sys.platform != "win32":
            run_command(["rm", "-f", path.as_posix()], sudo=True)
        else:
            path.unlink()
        return True


def remove_with_parents(file_path: Path, limit_dir: Optional[Path] = None) -> None:
    """Removes a file or symlink and cleans up empty parent directories up to limit_dir."""
    if not file_path.exists() and not file_path.is_symlink():
        return

    clear_readonly(file_path)
    remove_file_or_empty_dir(file_path)
    if limit_dir:
        prune_empty_parents(file_path.parent, limit_dir)


# ---------------------------------------------------------------------------
# Symlink operations
# ---------------------------------------------------------------------------

def copy_symlink(src: Path, dst: Path, sudo: bool = False) -> None:
    """Copies/recreates a symlink from src to dst pointing to src's readlink target."""
    if is_concrete_dir(dst):
        raise IsADirectoryError(f"Cannot copy symlink to '{dst}': destination exists and is a directory.")
    ensure_dir(dst.parent, sudo=sudo)
    clear_readonly(dst)
    remove_file_or_empty_dir(dst, sudo=sudo)

    link_target = os.readlink(src)
    if sys.platform == "win32" and link_target.startswith("\\\\?\\"):
        link_target = link_target[4:]
    if sys.platform == "win32" or not sudo:
        dst.symlink_to(link_target)
    else:
        cmd = ["ln", "-s", link_target, dst.as_posix()]
        run_command(cmd, sudo=True)


def create_symlink(src: Path, dst: Path, sudo: bool = False) -> None:
    """Creates a symlink from src to dst, cleaning up any existing file/link."""
    if is_concrete_dir(dst):
        raise IsADirectoryError(f"Cannot create symlink at '{dst}': destination exists and is a directory.")
    ensure_dir(dst.parent, sudo=sudo)
    clear_readonly(dst)
    remove_file_or_empty_dir(dst, sudo=sudo)

    if sys.platform == "win32" or not sudo:
        dst.symlink_to(src, target_is_directory=src.is_dir())
    else:
        cmd = ["ln", "-s", src.as_posix(), dst.as_posix()]
        run_command(cmd, sudo=True)


# ---------------------------------------------------------------------------
# Atomic file copy (unified sudo/non-sudo)
# ---------------------------------------------------------------------------

def _atomic_copy_local(
    src: Path,
    dst: Path,
    line_ending: LineEnding = LineEnding.PRESERVE,
    follow_symlinks: bool = True
) -> None:
    """Atomic file copy using temp file + os.replace (non-elevated)."""
    dst_parent = dst.parent
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=dst_parent, prefix=f".tmp_{dst.name}_", delete=False) as tf:
            temp_path = Path(tf.name)

        if line_ending != LineEnding.PRESERVE and not is_binary_file(src):
            raw_bytes = src.read_bytes()
            converted = normalize_newlines(raw_bytes, line_ending=line_ending)
            temp_path.write_bytes(converted)
            try:
                shutil.copymode(src, temp_path, follow_symlinks=follow_symlinks)
            except OSError as exc:
                logger.debug(f"Failed to copy file mode from '{src}' to '{temp_path}': {exc}")
        else:
            shutil.copy2(src, temp_path, follow_symlinks=follow_symlinks)

        clear_readonly(dst)
        os.replace(temp_path, dst)
    finally:
        if temp_path and (temp_path.exists() or temp_path.is_symlink()):
            try:
                temp_path.unlink()
            except OSError as exc:
                logger.debug(f"Failed to clean up temporary file '{temp_path}': {exc}")


def _atomic_copy_elevated(src: Path, dst: Path) -> None:
    """Atomic file copy using elevated mktemp + cp + mv, with direct cp fallback.

    Note: Invokes 'mktemp <full_template_path>' with a single positional path argument rather
    than 'mktemp -p <dir> <template>' because '-p' is a GNU coreutils extension unsupported by
    macOS/BSD mktemp, whereas a full-path template ending in 'XXXXXX' is portable across both.
    """
    temp_path_str = None
    try:
        # Pass full path template directly (portable across GNU and macOS/BSD mktemp; avoids GNU-only '-p')
        res = run_command(
            ["mktemp", (dst.parent / f".tmp_{dst.name}_XXXXXX").as_posix()],
            sudo=True,
            text=True
        )
        temp_path_str = str(res.stdout).strip()
        if temp_path_str:
            run_command(["cp", "-p", src.as_posix(), temp_path_str], sudo=True)
            run_command(["mv", "-f", temp_path_str, dst.as_posix()], sudo=True)
            return
    except Exception as exc:
        # Fallback to direct elevated cp if mktemp fails
        logger.debug(f"Atomic sudo copy via mktemp failed for '{dst}': {exc}, falling back to direct cp.")
    finally:
        # Clean up temporary file if an error occurred before mv
        if temp_path_str and Path(temp_path_str).name != dst.name:
            try:
                run_command(["rm", "-f", temp_path_str], sudo=True)
            except OSError as exc:
                logger.debug(f"Failed to clean up elevated temporary file '{temp_path_str}': {exc}")

    # Direct fallback
    cmd = ["cp", "-p", src.as_posix(), dst.as_posix()]
    run_command(cmd, sudo=True)


def copy_file(
    src: Path,
    dst: Path,
    sudo: bool = False,
    line_ending: LineEnding = LineEnding.PRESERVE,
    follow_symlinks: bool = True,
) -> None:
    """Atomically copies a single file from src to dst.

    Uses temp-file + atomic rename to guarantee dst is never left in a partial state.
    Elevates via sudo on POSIX when requested.

    Raises:
        IsADirectoryError: If src or dst exists and is a concrete directory.
    """
    if is_concrete_dir(dst):
        raise IsADirectoryError(f"Cannot copy file to '{dst}': destination exists and is a directory.")
    if is_concrete_dir(src):
        raise IsADirectoryError(f"Cannot copy file from '{src}': source is a directory, not a regular file.")

    ensure_dir(dst.parent, sudo=sudo)
    clear_readonly(dst)

    # Symlink passthrough
    if not follow_symlinks and src.is_symlink():
        copy_symlink(src, dst, sudo=sudo)
        return

    # Mode-only shortcut: content matches but permissions differ
    if dst.is_file() and not dst.is_symlink() and not src.is_symlink() and is_mode_only_change(src, dst):
        copy_permissions(src, dst, sudo=sudo)
        return

    # Line-ending conversion path (for sudo, we convert in-memory and write via elevated write_file)
    if sudo and sys.platform != "win32" and line_ending != LineEnding.PRESERVE and not is_binary_file(src):
        raw_bytes = src.read_bytes()
        converted = normalize_newlines(raw_bytes, line_ending=line_ending)
        perm = src.stat().st_mode if src.exists() else None
        write_file(dst, converted, sudo=True, permission=perm)
        return

    # Standard atomic copy
    if not sudo or sys.platform == "win32":
        _atomic_copy_local(src, dst, line_ending, follow_symlinks)
    else:
        _atomic_copy_elevated(src, dst)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _resolve_write_permission(dst: Path, permission: Optional[int]) -> Optional[int]:
    """Resolves the mode to apply after an atomic write.

    Returns `permission` when given; otherwise the current mode of an existing regular-file
    `dst` (symlinks are replaced, so their target's mode is not inherited); otherwise None,
    meaning "keep the umask-derived default of a newly created file".
    """
    if permission is not None:
        return permission
    try:
        st_mode = dst.lstat().st_mode
    except OSError as exc:
        logger.debug(f"Could not read existing permissions of '{dst}': {exc}")
        return None
    return choose(stat.S_ISREG(st_mode), stat.S_IMODE(st_mode), None)


def _to_bytes(content: Union[str, bytes]) -> bytes:
    """Encodes str content as UTF-8; passes bytes through unchanged."""
    return branch(isinstance(content, str), lambda: str(content).encode("utf-8"), lambda: bytes(content))


def _create_umask_temp_file(dst: Path, temp_dir: Optional[Path]) -> Path:
    """Creates an empty temp file for `dst` with mode 0o666 & ~umask (like a normal new file).

    `tempfile.NamedTemporaryFile` always creates 0o600 files, which would leak into the final
    file through `os.replace`; `os.open(..., 0o666)` lets the process umask apply instead.
    """
    base_dir = Path(when_none(temp_dir, tempfile.gettempdir))
    for _ in range(100):
        candidate = base_dir / f".tmp_{dst.name}_{os.urandom(4).hex()}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError(f"Could not create a unique temporary file for '{dst}' in '{base_dir}'.")


def write_file(
    dst: Path,
    content: Union[str, bytes],
    sudo: bool = False,
    permission: Optional[int] = None
) -> None:
    """Writes string or bytes content to dst file atomically with directory creation, sudo handling, and permission setting.

    Permission semantics:
        - `permission` given: the written file gets exactly that mode.
        - `permission=None` and `dst` is an existing regular file: its current mode is preserved.
        - `permission=None` and `dst` is new (or a symlink being replaced): `0o666 & ~umask`.

    Raises:
        IsADirectoryError: If dst exists and is a concrete directory.
    """
    if is_concrete_dir(dst):
        raise IsADirectoryError(f"Cannot write file to '{dst}': destination exists and is a directory.")
    ensure_dir(dst.parent, sudo=sudo)
    final_permission = _resolve_write_permission(dst, permission)
    clear_readonly(dst)

    # Use dst.parent for temporary file if it is writable; otherwise fallback to system temp dir (None)
    temp_dir = dst.parent if (dst.parent.exists() and os.access(dst.parent, os.W_OK)) else None
    temp_path = None
    try:
        temp_path = _create_umask_temp_file(dst, temp_dir)
        temp_path.write_bytes(_to_bytes(content))
        if final_permission is not None:
            try:
                temp_path.chmod(final_permission)
            except OSError as exc:
                logger.warning(f"Failed to set permissions ({oct(final_permission)}) on '{dst}': {exc}")
        if sys.platform == "win32" or not sudo:
            clear_readonly(dst)
            os.replace(temp_path, dst)
        else:
            # POSIX with sudo: mv -f performs atomic replacement with best effort (rename)
            cmd = ["mv", "-f", temp_path.as_posix(), dst.as_posix()]
            run_command(cmd, sudo=True)
    finally:
        if temp_path and (temp_path.exists() or temp_path.is_symlink()):
            try:
                temp_path.unlink()
            except OSError as exc:
                logger.debug(f"Failed to clean up temporary file '{temp_path}': {exc}")


# ---------------------------------------------------------------------------
# Bulk tree operations (external commands)
# ---------------------------------------------------------------------------

def _tree_op_windows(src: Path, dst: Path, move: bool, resolve_symlinks: bool) -> None:
    """Windows tree copy/move using Python standard library."""
    if move and not resolve_symlinks:
        if dst.exists() or dst.is_symlink():
            remove_tree(dst)
        shutil.move(src, dst)
        return

    if is_concrete_dir(src) or (resolve_symlinks and src.is_dir()):
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=not resolve_symlinks)
    else:
        copy_file(src, dst, follow_symlinks=resolve_symlinks)

    if move:
        remove_tree(src)


def _tree_op_posix(src: Path, dst: Path, sudo: bool, chown: bool, move: bool, resolve_symlinks: bool) -> None:
    """POSIX tree copy/move using external commands with optional sudo."""
    if move and not resolve_symlinks:
        cmd = ["mv", src.as_posix(), dst.as_posix()]
    else:
        if src.is_dir():
            cmd = ["cp", "-RP" if not resolve_symlinks else "-RL", src.as_posix(), dst.as_posix()]
        else:
            cmd = ["cp", "-P" if not resolve_symlinks else "-L", src.as_posix(), dst.as_posix()]

    run_command(cmd, sudo=sudo)

    if sudo and chown:
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid is not None and gid is not None:
                chown_cmd = ["chown", "-R", f"{uid}:{gid}", dst.as_posix()]
                run_command(chown_cmd, sudo=True)
        except Exception as e:
            logger.warning(f"Failed to chown backup to process owner: {e}")

    if move:
        del_cmd = ["rm", "-rf", src.as_posix()]
        run_command(del_cmd, sudo=sudo)


def copy_tree(
    src: Path,
    dst: Path,
    sudo: bool = False,
    chown: bool = True,
    resolve_symlinks: bool = True,
) -> None:
    """Copies a file or directory tree from src to dst.

    Uses external commands with optional sudo on POSIX, Python stdlib on Windows.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    clear_readonly(dst)

    if sys.platform == "win32":
        _tree_op_windows(src, dst, move=False, resolve_symlinks=resolve_symlinks)
    else:
        _tree_op_posix(src, dst, sudo, chown, move=False, resolve_symlinks=resolve_symlinks)


def move_tree(
    src: Path,
    dst: Path,
    sudo: bool = False,
    chown: bool = True,
    resolve_symlinks: bool = True,
) -> None:
    """Moves a file or directory tree from src to dst.

    Uses external commands with optional sudo on POSIX, Python stdlib on Windows.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    clear_readonly(dst)
    clear_readonly(src)

    if sys.platform == "win32":
        _tree_op_windows(src, dst, move=True, resolve_symlinks=resolve_symlinks)
    else:
        _tree_op_posix(src, dst, sudo, chown, move=True, resolve_symlinks=resolve_symlinks)
