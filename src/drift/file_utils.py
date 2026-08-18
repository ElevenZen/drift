"""Utility functions for file and directory operations using pathlib."""

import os
import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)

def is_relative_to(path: Path, other: Path) -> bool:
    """Robust fallback implementation of Path.is_relative_to for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def run_command(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    """Logs the command before executing it with subprocess.run."""
    logger.info(f"Executing: {' '.join(cmd)}")
    params = {"check": True, "capture_output": True}
    params.update(kwargs)
    return subprocess.run(cmd, **params)


def resolve_system_target(relative_path: Path, relative_base: Path) -> Path:
    """
    Applies relative file path to base path,
    expanding user home and applying dot prefix conversion.
    """
    target_path = relative_base.expanduser()
    translated_parts = ["." + p[4:] if p.startswith("dot-") else p for p in relative_path.parts]
    return target_path.joinpath(*translated_parts)


def translate_dot_prefixes_reverse(relative_path: Path) -> Path:
    """
    Converts leading dots ('.') in path segments to 'dot-', which is the opposite of dot segment resolution.
    Skipping '.' and '..' segments.
    """
    translated_parts = ["dot-" + p[1:] if p.startswith(".") and p not in (".", "..") else p for p in relative_path.parts]
    return Path(*translated_parts)


def tree_relative_files(dir_path: Path) -> List[Path]:
    """Gets all files in dir_path recursively as Path objects relative to dir_path."""
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


def file_contents_differ(file1: Path, file2: Path) -> bool:
    """Returns True if the contents of file1 and file2 differ using stream comparison."""
    if not file1.exists() and not file2.exists():
        return False
    if not file1.exists() or not file2.exists():
        return True
    if not file1.is_file() or not file2.is_file():
        raise ValueError("Both paths must be files for content comparison.")
    if file1.resolve() == file2.resolve():
        return False
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


def backup_and_delete_one_file(
    file_path: Path,
    backup_dest: Path,
    limit_dir: Optional[Path] = None
) -> None:
    """Backs up a file to backup_dest, deletes it, and cleans up empty parent directories up to limit_dir."""
    if not file_path.exists():
        return

    # Safely remove backup_dest if it already exists, to avoid conflicts.
    remove_file_or_dir(backup_dest)

    backup_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, backup_dest)
    file_path.unlink()
    if limit_dir:
        rmdir_parents(file_path.parent, limit_dir)


def backup_file_or_dir_external(src: Path, backup_dest: Path, sudo: bool, resolve_symlinks: bool = True) -> None:
    """
    Recursively backs up target src to backup_dest, resolving symlinks if resolve_symlinks is True.
    If broken symlinks are encountered, they are backed up as-is without resolving.
    """
    if not src.exists() and not src.is_symlink():
        return

    # Safely remove backup_dest if it already exists, to avoid conflicts.
    remove_file_or_dir_with_sudo(backup_dest, sudo)
        
    if src.is_file() and not src.is_symlink():
        # then it's a normal file.
        copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=resolve_symlinks)
        return

    if src.is_dir():
        if resolve_symlinks:
            backup_dest.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                backup_file_or_dir_external(item, backup_dest / item.name, sudo, resolve_symlinks=True)
            # Remove original dir
            del_cmd = ["rm", "-rf", str(src)]
            if sudo:
                del_cmd.insert(0, "sudo")
            run_command(del_cmd)
        else:
            copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=False)
        return

    if src.is_symlink():
        if not resolve_symlinks:
            copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=False)
            return
        # Try to resolve and backup the content, if failed, fallback to backup the link.
        try:
            real_target = src.resolve()
            if not real_target.exists():
                copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=False)
                return
            backup_file_or_dir_external(real_target, backup_dest, sudo, resolve_symlinks=True)
            # Delete the symlink itself to clear the path
            remove_file_or_dir_with_sudo(src, sudo)
        except Exception:
            copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=False)
        return


def copy_or_move_file_or_dir_external(
    src: Path,
    dst: Path,
    sudo: bool,
    chown: bool = True,
    move: bool = False,
    resolve_symlinks: bool = True,
) -> None:
    """
    Use external utility commands to moves or copies file/directory,
    using sudo if requested, chown after copy if requested,
    resolving symlinks recursively if resolve_symlinks is True.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if move and not resolve_symlinks:
        cmd = ["mv", str(src), str(dst)]
    else:
        if src.is_dir():
            cmd = ["cp", "-RP" if not resolve_symlinks else "-RL", str(src), str(dst)]
        else:
            cmd = ["cp", "-P" if not resolve_symlinks else "-L", str(src), str(dst)]
            
    if sudo:
        cmd.insert(0, "sudo")
        
    run_command(cmd)
    
    if sudo and chown:
        # Attempt to chown the backup to the current process owner if sudo was used, to avoid permission issues later.
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid is not None and gid is not None:
                chown_cmd = ["sudo", "chown", "-R", f"{uid}:{gid}", str(dst)]
                run_command(chown_cmd)
        except Exception as e:
            logger.warning(f"Failed to chown backup to process owner: {e}")
            
    if move:
        del_cmd = ["rm", "-rf", str(src)]
        if sudo:
            del_cmd.insert(0, "sudo")
        run_command(del_cmd)


def ensure_directory_writable(path: Path, sudo: bool) -> None:
    """Checks if a directory path (or its closest existing parent) is writable."""
    if sudo:
        return  # With sudo, we assume target is writable or handled by elevation
    curr = path.resolve()
    while curr:
        if curr.exists():
            if os.access(curr, os.W_OK):
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
    """Ensures directory exists, creating with sudo if requested."""
    if path.exists():
        return
    if sudo:
        run_command(["sudo", "mkdir", "-p", str(path)])
    else:
        path.mkdir(parents=True, exist_ok=True)


def remove_file_or_dir(path: Path) -> None:
    """Safely removes a file, symlink, or directory tree using Python standard libraries."""
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def remove_file_or_dir_with_sudo(path: Path, sudo: bool) -> None:
    """Safely removes a file, symlink, or directory using sudo if requested."""
    if path.exists() or path.is_symlink():
        cmd_rm = ["rm", "-rf" if path.is_dir() and not path.is_symlink() else "-f", str(path)]
        if sudo:
            cmd_rm.insert(0, "sudo")
        run_command(cmd_rm)


def create_symlink_manually_with_sudo(src: Path, dst: Path, sudo: bool) -> None:
    """Creates a symlink from src to dst manually, cleaning up existing file/link with sudo if requested."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    remove_file_or_dir_with_sudo(dst, sudo)
        
    cmd = ["ln", "-s", str(src), str(dst)]
    if sudo:
        cmd.insert(0, "sudo")
    run_command(cmd)


def copy_file_contents_with_sudo(src: Path, dst: Path, sudo: bool) -> None:
    """Copies a physical file from src to dst, with sudo if requested."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    # remove ensure copy won't be contaminated by existing symlink, read-only file or directory.
    remove_file_or_dir_with_sudo(dst, sudo)
    cmd = ["cp", str(src), str(dst)]
    if sudo:
        cmd.insert(0, "sudo")
    run_command(cmd)


def sync_broken_symlink(src: Path, dst: Path) -> None:
    """
    Safely copies/syncs a broken symlink from src to dst.
    Reads the raw link value and recreates it at dst if it does not already match.
    """
    try:
        link_val = os.readlink(src)
        if not dst.is_symlink() or os.readlink(dst) != link_val:
            logger.info(f"Broken Link Sync: '{src}' is a broken symlink pointing to '{link_val}'. Copying symlink itself...")
            remove_file_or_dir(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(link_val)
    except Exception as e:
        logger.warning(f"Failed to copy broken symlink '{src}': {e}")


def reverse_sync_file_or_dir(src: Path, dst: Path, ignore_handler: Optional['DriftIgnore'] = None) -> None:
    """
    Performs reverse sync for a single file, directory, or link from src (typically on the system)
    back to dst (typically in the local install state database).
    """
    from .folder_diff import compare_folders
    from .constants import IGNORED_FILENAMES

    diff = compare_folders(src, dst, ignore_handler=ignore_handler, resolve_symlinks=True)
    
    # Process deletions
    for rel_file in diff.deleted:
        if rel_file.name in IGNORED_FILENAMES:
            continue
        target_dst = dst / rel_file if rel_file != Path("") else dst
        logger.info(f"System Deletion: '{src / rel_file if rel_file != Path('') else src}' is missing. Deleting counterpart '{target_dst}' from install/...")
        remove_file_or_dir(target_dst)

    # Process additions and modifications
    for rel_file in diff.added + diff.modified:
        if rel_file.name in IGNORED_FILENAMES:
            continue
        target_src = src / rel_file if rel_file != Path("") else src
        target_dst = dst / rel_file if rel_file != Path("") else dst

        if target_src.is_dir() and not target_src.is_symlink():
            target_dst.mkdir(parents=True, exist_ok=True)
            continue

        is_broken = False
        if target_src.is_symlink():
            try:
                if not target_src.resolve().exists():
                    is_broken = True
            except Exception:
                is_broken = True

        if is_broken:
            sync_broken_symlink(target_src, target_dst)
        else:
            logger.info(f"System Modification: '{target_src}' has drifted. Reverse-copying back to install/...")
            remove_file_or_dir(target_dst)
            target_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_src, target_dst)


