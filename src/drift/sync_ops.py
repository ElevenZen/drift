"""High-level synchronization and backup operations using FolderDiff."""

import logging
import shutil
from pathlib import Path
from typing import Optional

from .folder_diff import compare_folders
from .file_utils import (
    remove_file_or_dir,
    remove_file_or_dir_with_sudo,
    copy_or_move_file_or_dir_external,
    sync_broken_symlink,
    run_command,
)
from .constants import MANAGED_CONFIG_FILES
from .ignore import DriftIgnore


logger = logging.getLogger(__name__)


def reverse_sync_file_or_dir(src: Path, dst: Path, ignore_handler: Optional[DriftIgnore] = None) -> None:
    """
    Performs reverse sync for a single file, directory, or link from src (typically on the system)
    back to dst (typically in the local install state database).
    """
    diff = compare_folders(src, dst, ignore_handler=ignore_handler, resolve_symlinks=True)
    
    # Process deletions
    for rel_file in diff.deleted:
        if rel_file.name in MANAGED_CONFIG_FILES:
            continue
        target_dst = dst / rel_file if rel_file != Path("") else dst
        logger.info(f"System Deletion: '{src / rel_file if rel_file != Path('') else src}' is missing. Deleting counterpart '{target_dst}' from install/...")
        remove_file_or_dir(target_dst)

    # Process additions and modifications
    for rel_file in diff.added + diff.modified:
        if rel_file.name in MANAGED_CONFIG_FILES:
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


def backup_file_or_dir_external(src: Path, backup_dest: Path, sudo: bool, resolve_symlinks: bool = True) -> None:
    """
    Recursively backs up target src to backup_dest, resolving symlinks if resolve_symlinks is True.
    If broken symlinks are encountered, they are backed up as-is without resolving.
    """
    if not src.exists() and not src.is_symlink():
        return

    # Safely remove backup_dest if it already exists, to avoid conflicts.
    remove_file_or_dir_with_sudo(backup_dest, sudo)

    if not resolve_symlinks:
        copy_or_move_file_or_dir_external(src, backup_dest, sudo, move=True, resolve_symlinks=False)
        return

    # resolve_symlinks is True: Use FolderDiff to plan recursive backup/move
    diff = compare_folders(src, backup_dest, resolve_symlinks=True)

    # For backup, we only care about added and modified (content from src to backup_dest)
    # diff.deleted would mean something exists in backup_dest but not in src, 
    # but we just removed backup_dest above.
    
    for rel_file in diff.added + diff.modified:
        target_src = src / rel_file if rel_file != Path("") else src
        target_dst = backup_dest / rel_file if rel_file != Path("") else backup_dest

        if target_src.is_dir() and not target_src.is_symlink():
            # Directory creation is handled by copy_or_move_file_or_dir_external or mkdir
            target_dst.mkdir(parents=True, exist_ok=True)
            continue
            
        # Handle broken symlinks manually when resolve_symlinks is True
        is_broken = False
        if target_src.is_symlink():
            try:
                if not target_src.resolve().exists():
                    is_broken = True
            except Exception:
                is_broken = True

        if is_broken:
            # For broken links, we can't resolve, so we copy the link itself and then remove source
            copy_or_move_file_or_dir_external(target_src, target_dst, sudo, move=True, resolve_symlinks=False)
        else:
            # For normal files and healthy links: move them (copy then remove source)
            copy_or_move_file_or_dir_external(target_src, target_dst, sudo, move=True, resolve_symlinks=True)

    # After moving all children, if src was a directory, we need to remove the empty directory shell
    if src.is_dir() and not src.is_symlink():
        remove_file_or_dir_with_sudo(src, sudo)
