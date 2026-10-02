from pathlib import Path

from .folder_diff import compare_folders
from ..utils.file_ops import (
    remove_tree,
    remove_file_or_empty_dir,
    move_tree,
)
from ..utils.file_inspect import is_concrete_dir


def backup_file_or_dir_external(src: Path, backup_dest: Path, sudo: bool, resolve_symlinks: bool = True) -> None:
    """
    Recursively backs up target src to backup_dest, resolving symlinks if resolve_symlinks is True.
    If broken symlinks are encountered, they are backed up as-is without resolving.
    """
    if not src.exists() and not src.is_symlink():
        return

    # Safely remove backup_dest if it already exists, to avoid conflicts.
    remove_tree(backup_dest, sudo)

    if not resolve_symlinks:
        move_tree(src, backup_dest, sudo, resolve_symlinks=False)
        return

    # resolve_symlinks is True: Use FolderDiff to plan recursive backup/move
    diff = compare_folders(src, backup_dest, resolve_symlinks=True)

    # For backup, we only care about added and modified (content from src to backup_dest)
    # diff.deleted would mean something exists in backup_dest but not in src, 
    # but we just removed backup_dest above.
    
    for rel_file in diff.added + diff.modified:
        target_src = src / rel_file if rel_file != Path("") else src
        target_dst = backup_dest / rel_file if rel_file != Path("") else backup_dest

        if is_concrete_dir(target_src):
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
            move_tree(target_src, target_dst, sudo, resolve_symlinks=False)
        else:
            # For normal files and healthy links: move them (copy then remove source)
            move_tree(target_src, target_dst, sudo, resolve_symlinks=True)

    # After moving all children, if src was a directory, we need to remove the empty directory shell
    if is_concrete_dir(src):
        remove_file_or_empty_dir(src, sudo)
