"""Declarative filesystem action models, formatting, and host execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Host Execution Subsystem
    - execute_single_action(context, action) -> None
        Deterministically executes a single planned FileAction on host disk.
    - execute_delivery_actions(context, actions) -> None
        Sequentially dispatches a list of planned FileActions to execute_single_action.

Layer 2: Action Formatting & Summarization
    - format_action_line(action, drift_root) -> str
        Formats a single FileAction into a styled terminal line with icons.
    - format_action_summary(actions) -> str
        Formats a concise summary string of action counts across action types.

Layer 1: Domain Action Types & Context Models
    - FileActionType (str, Enum):
        Universal enumeration of filesystem operations (CREATE_COPY, UPDATE_COPY,
        CREATE_SYMLINK, RENDER_ITEM, WRITE_CONFIG, ENSURE_DIR, DELETE_ITEM, etc.).
    - FileAction:
        Declarative specification of an action with src_path, dst_path, and reason.
    - FileActionExecutionContext:
        Runtime flags for host execution (sudo, resolve_symlinks).
===============================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import FrozenSet, Optional, Sequence

from .constants import BackupSubfolder, DRIFT_KEEP_FILE_NAME
from .serialization import SerializableModel
from .sync_ops import backup_file_or_dir_external
from ..utils.file_ops import (
    copy_file,
    copy_permissions,
    create_symlink,
    ensure_dir,
    remove_file_or_empty_dir,
    remove_tree,
    write_file,
)
from ..utils.path_utils import compute_relative_symlink_target, to_relative_posix

logger = logging.getLogger(__name__)


# =============================================================================
# Action Enums & Context Models
# =============================================================================

class FileActionType(str, Enum):
    """Specific filesystem operation planned or executed during delivery."""
    # Creations & Updates
    CREATE_SYMLINK = "CREATE_SYMLINK"
    CREATE_COPY = "CREATE_COPY"
    CREATE_KEEP_FILE = "CREATE_KEEP_FILE"
    UPDATE_COPY = "UPDATE_COPY"
    UPDATE_PERMISSION = "UPDATE_PERMISSION"
    ENSURE_DIR = "ENSURE_DIR"

    # Skips (already matching desired state)
    SKIP_IDENTICAL = "SKIP_IDENTICAL"

    # Render Actions (Evaluated in Render DAG; not executable by host folder delivery)
    RENDER_ITEM = "RENDER_ITEM"
    WRITE_CONFIG = "WRITE_CONFIG"

    # Collisions & Cleanups
    BACKUP_OVERWRITE = "BACKUP_OVERWRITE"  # Existing host node backed up and removed
    BACKUP_PRUNE = "BACKUP_PRUNE"          # Historical host orphan backed up and removed
    DELETE_ITEM = "DELETE_ITEM"            # Direct file/symlink/empty dir deletion without backup
    DELETE_TREE = "DELETE_TREE"            # Direct directory tree deletion without backup

    # Workflow Notifications
    INFO_MESSAGE = "INFO_MESSAGE"          # Informational message logged during execution


RENDER_ACTION_TYPES: FrozenSet[FileActionType] = frozenset({
    FileActionType.RENDER_ITEM,
    FileActionType.WRITE_CONFIG,
})

BACKUP_ACTION_TYPES: FrozenSet[FileActionType] = frozenset({
    FileActionType.BACKUP_OVERWRITE,
    FileActionType.BACKUP_PRUNE,
})

DELETE_ACTION_TYPES: FrozenSet[FileActionType] = frozenset({
    FileActionType.DELETE_ITEM,
    FileActionType.DELETE_TREE,
})

BACKUP_OR_DELETE_ACTION_TYPES: FrozenSet[FileActionType] = frozenset(
    BACKUP_ACTION_TYPES | DELETE_ACTION_TYPES
)

CREATE_ACTION_TYPES: FrozenSet[FileActionType] = frozenset({
    FileActionType.CREATE_SYMLINK,
    FileActionType.CREATE_COPY,
    FileActionType.CREATE_KEEP_FILE,
})


@dataclass
class FileAction(SerializableModel):
    """Declarative specification of a single file/directory operation on the host system."""
    action_type: FileActionType
    src_path: Optional[Path] = None
    dst_path: Optional[Path] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.action_type == FileActionType.SKIP_IDENTICAL and self.src_path is None:
            raise ValueError(f"FileActionType.SKIP_IDENTICAL must have src_path set, got dst_path='{self.dst_path}'")


@dataclass(frozen=True)
class FileActionExecutionContext:
    """Encapsulates host filesystem permissions and symlink resolution flags for executing file actions."""
    sudo: bool = False
    resolve_symlinks: bool = True


# =============================================================================
# Centralized Action Formatting Helpers
# =============================================================================

def _display_action_path(path: Optional[Path], drift_root: Optional[Path] = None) -> str:
    if path is None:
        return ""
    if drift_root is None:
        return str(path)
    return to_relative_posix(path, drift_root)


def format_action_line(action: FileAction, drift_root: Optional[Path] = None) -> str:
    """Formats a single FileAction into a clean terminal line."""
    src_str = _display_action_path(action.src_path, drift_root) if action.src_path else None
    dst_str = _display_action_path(action.dst_path, drift_root) if action.dst_path else None
    reason_str = f" ({action.reason})" if action.reason else ""

    if action.action_type == FileActionType.ENSURE_DIR:
        return f"    📁 [ENSURE_DIR]      {dst_str}{reason_str}"
    elif action.action_type == FileActionType.CREATE_SYMLINK:
        return f"    🔗 [CREATE_SYMLINK]  {src_str} -> {dst_str}"
    elif action.action_type == FileActionType.CREATE_COPY:
        return f"    ➕ [CREATE_COPY]     {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.CREATE_KEEP_FILE:
        return f"    📌 [CREATE_KEEP_FILE] {dst_str}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_COPY:
        return f"    ✏️ [UPDATE_COPY]     {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_PERMISSION:
        return f"    🔒 [UPDATE_PERMISSION] {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.RENDER_ITEM:
        return f"    🎨 [RENDER]          {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.WRITE_CONFIG:
        return f"    ⚙️ [CONFIG]          {dst_str}{reason_str}"
    elif action.action_type == FileActionType.SKIP_IDENTICAL:
        return f"    ⏭️ [SKIP_IDENTICAL]  {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_OVERWRITE:
        return f"    🛡️ [BACKUP_OVERWRITE] {src_str}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_PRUNE:
        return f"    📦 [BACKUP_PRUNE]    {src_str}{reason_str}"
    elif action.action_type in DELETE_ACTION_TYPES:
        return f"    🗑️ [{action.action_type}]    {dst_str}{reason_str}"
    elif action.action_type == FileActionType.INFO_MESSAGE:
        return f"    📢 [INFO]            {action.reason}"
    return f"    [{action.action_type}] {src_str} -> {dst_str}{reason_str}"


def format_action_summary(actions: Sequence[FileAction]) -> str:
    """Formats a concise summary string of action counts."""
    counts = []
    rendered = [a for a in actions if a.action_type == FileActionType.RENDER_ITEM]
    if rendered:
        counts.append(f"{len(rendered)} to render")
    configs = [a for a in actions if a.action_type == FileActionType.WRITE_CONFIG]
    if configs:
        counts.append(f"{len(configs)} config")
    created = [a for a in actions if a.action_type in CREATE_ACTION_TYPES]
    if created:
        counts.append(f"{len(created)} to create")
    updated = [a for a in actions if a.action_type == FileActionType.UPDATE_COPY]
    if updated:
        counts.append(f"{len(updated)} to update")
    permissions = [a for a in actions if a.action_type == FileActionType.UPDATE_PERMISSION]
    if permissions:
        counts.append(f"{len(permissions)} permissions to update")
    skipped = [a for a in actions if a.action_type == FileActionType.SKIP_IDENTICAL]
    if skipped:
        counts.append(f"{len(skipped)} up-to-date")
    removed = [a for a in actions if a.action_type in DELETE_ACTION_TYPES]
    if removed:
        counts.append(f"{len(removed)} to remove")
    backups = [a for a in actions if a.action_type in BACKUP_ACTION_TYPES]
    if backups:
        counts.append(f"{len(backups)} to backup")
    ensured = [a for a in actions if a.action_type == FileActionType.ENSURE_DIR]
    if ensured:
        counts.append(f"{len(ensured)} directories")
    return ", ".join(counts) if counts else "0 actions"


# =============================================================================
# Unified Plan Execution Engine
# =============================================================================

def execute_single_action(
    context: FileActionExecutionContext,
    action: FileAction,
) -> None:
    """Executes a single planned file/directory delivery action on the host system."""
    if action.action_type in RENDER_ACTION_TYPES:
        raise ValueError(
            f"Action type '{action.action_type}' is a render-time AST action "
            "and cannot be executed by the host delivery engine."
        )

    action_line = format_action_line(action)

    if action.action_type == FileActionType.SKIP_IDENTICAL:
        logger.debug(action_line)
        return

    if action.action_type == FileActionType.INFO_MESSAGE:
        logger.info(action_line)
        return

    if action.action_type != FileActionType.ENSURE_DIR:
        logger.info(action_line)

    if action.action_type in BACKUP_ACTION_TYPES:
        if action.src_path and action.dst_path:
            logger.debug(f"   Backing up to: {action.dst_path}")
            backup_file_or_dir_external(
                action.src_path,
                action.dst_path,
                context.sudo,
                resolve_symlinks=context.resolve_symlinks,
            )
        if action.src_path:
            if action.action_type == FileActionType.BACKUP_PRUNE:
                remove_file_or_empty_dir(action.src_path, context.sudo)
            else:
                remove_tree(action.src_path, context.sudo)

    elif action.action_type == FileActionType.ENSURE_DIR:
        if action.dst_path:
            if action.dst_path.is_symlink() or (action.dst_path.exists() and not action.dst_path.is_dir()):
                raise NotADirectoryError(
                    f"Cannot ensure directory '{action.dst_path}': path exists and is not a directory."
                )
            dir_existed = action.dst_path.is_dir()
            ensure_dir(action.dst_path, context.sudo)
            if dir_existed:
                logger.debug(action_line)
            else:
                logger.info(action_line)

    elif action.action_type == FileActionType.CREATE_SYMLINK:
        if action.src_path and action.dst_path:
            relative_target = compute_relative_symlink_target(action.src_path, action.dst_path.parent)
            create_symlink(relative_target, action.dst_path, context.sudo)

    elif action.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY):
        if action.src_path and action.dst_path:
            copy_file(action.src_path, action.dst_path, context.sudo, follow_symlinks=context.resolve_symlinks)

    elif action.action_type == FileActionType.UPDATE_PERMISSION:
        if action.src_path and action.dst_path:
            copy_permissions(action.src_path, action.dst_path, sudo=context.sudo)

    elif action.action_type == FileActionType.DELETE_TREE:
        if action.dst_path:
            remove_tree(action.dst_path, context.sudo)

    elif action.action_type == FileActionType.DELETE_ITEM:
        if action.dst_path:
            remove_file_or_empty_dir(action.dst_path, context.sudo)

    elif action.action_type == FileActionType.CREATE_KEEP_FILE:
        if action.dst_path:
            ensure_dir(action.dst_path.parent, context.sudo)
            write_file(action.dst_path, b"", sudo=context.sudo)


def execute_delivery_actions(
    context: FileActionExecutionContext,
    actions: Sequence[FileAction],
) -> None:
    """Deterministically executes a sequence of planned delivery actions on the host system."""
    for action in actions:
        execute_single_action(context, action)
