"""Declarative filesystem action models, formatting, backup stashing, WAL, and host execution.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 4: Write-Ahead Log (WAL) Reversal & Stash Management
    - rollback_package_wal(pkg_backup_root, sudo) -> bool
        Reverses recorded WAL actions in LIFO order, restores stashed backups, and clears wal/.
    - restore_wal_stash(pkg_backup_root, sudo) -> None
        Moves displaced prior backups from backup/<pkg>/wal/stash/ back to backup/<pkg>/.
    - clear_package_wal(pkg_backup_root, sudo) -> None
        Removes backup/<pkg>/wal/ and prunes empty package backup directory.

Layer 3: Host Execution & Backup Subsystem
    - backup_item(src, backup_dest, backup_pkg_root, sudo, resolve_symlinks) -> None
        Backs up a file, symlink, or directory tree to backup_dest, stashing any existing backup.
    - execute_single_action(context, action) -> None
        Deterministically executes a single planned action on host disk and appends to WAL if enabled.
    - execute_delivery_actions(context, actions) -> None
        Sequentially dispatches a list of planned actions to execute_single_action.

Layer 2: Action Formatting & Summarization
    - format_action_line(action, drift_root) -> str
        Formats a single FileAction into a styled terminal line with icons.
    - format_action_summary(actions) -> str
        Formats a concise summary string of action counts across action types.
    - is_mutating_action(action) -> bool
        Returns True if the action represents a physical mutation (not in NO_CHANGE_ACTION_TYPES).
    - filter_display_actions(actions, verbose) -> List[FileAction]
        Filters a sequence of FileActions for display based on verbosity.

Layer 1: Domain Action Types & Context Models
    - FileActionType (str, Enum):
        Universal enumeration of filesystem operations (CREATE_COPY, UPDATE_COPY,
        CREATE_SYMLINK, RENDER_ITEM, WRITE_CONFIG, ENSURE_DIR, DELETE_ITEM, etc.).
    - FileAction / NormalFileAction / BackupFileAction:
        Generic and specialized declarative specifications of filesystem operations.
    - FileActionExecutionContext:
        Runtime flags for host execution (sudo, resolve_symlinks, wal_dir).
    - rebase_file_action(action, old_base, new_base) -> FileAction:
        Returns a copy of an action with src_path and dst_path rebased.
===============================================================================
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, FrozenSet, Generic, List, Mapping, Optional, Sequence, TypeVar

from .constants import BackupSubfolder, DirMode, DRIFT_KEEP_FILE_NAME, WAL_ACTIONS_FILE_NAME
from .folder_diff import compare_folders, list_folder_paths
from .serialization import SerializableModel
from ..utils.file_inspect import is_concrete_dir
from ..utils.file_ops import (
    copy_file,
    copy_permissions,
    create_symlink,
    ensure_dir,
    move_tree,
    prune_empty_parents,
    remove_file_or_empty_dir,
    remove_tree,
    write_file,
)
from ..utils.path_utils import compute_relative_symlink_target, to_relative_posix, rebase_path

logger = logging.getLogger(__name__)

_SrcT = TypeVar("_SrcT", bound=Optional[Path])
_DstT = TypeVar("_DstT", bound=Optional[Path])
_ActionT = TypeVar("_ActionT", bound="FileAction")


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

    def __str__(self) -> str:
        return self.value


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

WAL_RECORDED_ACTION_TYPES: FrozenSet[FileActionType] = frozenset(
    CREATE_ACTION_TYPES | BACKUP_ACTION_TYPES | {FileActionType.ENSURE_DIR}
)

NO_CHANGE_ACTION_TYPES: FrozenSet[FileActionType] = frozenset({
    FileActionType.SKIP_IDENTICAL,
    FileActionType.INFO_MESSAGE,
})
NON_MUTATING_ACTION_TYPES: FrozenSet[FileActionType] = NO_CHANGE_ACTION_TYPES


@dataclass
class FileAction(SerializableModel, Generic[_SrcT, _DstT]):
    """Generic base class for declarative filesystem operations."""
    action_type: FileActionType
    src_path: _SrcT
    dst_path: _DstT
    reason: str

    def __post_init__(self) -> None:
        if self.action_type == FileActionType.SKIP_IDENTICAL and self.src_path is None:
            raise ValueError(f"FileActionType.SKIP_IDENTICAL must have src_path set, got dst_path='{self.dst_path}'")

    def rebased(self: _ActionT, old_base: Path, new_base: Path) -> _ActionT:
        """Returns a copy of this action with src_path and dst_path rebased from old_base to new_base."""
        return rebase_file_action(self, old_base, new_base)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FileAction":
        """Reconstructs a NormalFileAction or BackupFileAction from a serialized dictionary."""
        action_type = FileActionType(data["action_type"])
        if action_type in BACKUP_ACTION_TYPES and data.get("backup_pkg_root") is not None:
            return BackupFileAction.from_dict(data)
        return NormalFileAction.from_dict(data)


@dataclass
class NormalFileAction(FileAction[Optional[Path], Optional[Path]]):
    """Declarative specification of a single file/directory operation on the host system."""
    src_path: Optional[Path] = None
    dst_path: Optional[Path] = None
    reason: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NormalFileAction":
        """Reconstructs a NormalFileAction from a serialized dictionary."""
        src_raw = data.get("src_path")
        dst_raw = data.get("dst_path")
        return cls(
            action_type=FileActionType(data["action_type"]),
            src_path=Path(src_raw) if src_raw is not None else None,
            dst_path=Path(dst_raw) if dst_raw is not None else None,
            reason=str(data.get("reason") or ""),
        )


@dataclass
class BackupFileAction(FileAction[Path, Path]):
    """Declarative specification of a backup operation (BACKUP_OVERWRITE or BACKUP_PRUNE) with non-optional fields."""
    backup_pkg_root: Path

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BackupFileAction":
        """Reconstructs a BackupFileAction from a serialized dictionary."""
        return cls(
            action_type=FileActionType(data["action_type"]),
            src_path=Path(data["src_path"]),
            dst_path=Path(data["dst_path"]),
            reason=str(data.get("reason") or ""),
            backup_pkg_root=Path(data["backup_pkg_root"]),
        )


def rebase_file_action(action: _ActionT, old_base: Path, new_base: Path) -> _ActionT:
    """Returns a new action instance with both src_path and dst_path rebased from old_base to new_base."""
    rebased_src = rebase_path(action.src_path, old_base, new_base)
    rebased_dst = rebase_path(action.dst_path, old_base, new_base)
    if isinstance(action, BackupFileAction):
        assert rebased_src is not None and rebased_dst is not None
    return replace(
        action,
        src_path=rebased_src,
        dst_path=rebased_dst,
    )


@dataclass(frozen=True)
class FileActionExecutionContext:
    """Encapsulates host filesystem permissions, symlink resolution flags, and optional WAL directory."""
    sudo: bool = False
    resolve_symlinks: bool = True
    wal_dir: Optional[Path] = None


# =============================================================================
# Centralized Action Formatting Helpers
# =============================================================================

def _display_action_path(path: Optional[Path], drift_root: Optional[Path] = None) -> str:
    if path is None:
        return ""
    if drift_root is None:
        return path.as_posix()
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
        return f"    📌 [CREATE_KEEP]     {dst_str}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_COPY:
        return f"    ✏️ [UPDATE_COPY]     {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_PERMISSION:
        return f"    🔒 [UPDATE_PERMISSION] {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.RENDER_ITEM:
        return f"    🎨 [RENDER]          {src_str} -> {dst_str}{reason_str}"
    elif action.action_type == FileActionType.WRITE_CONFIG:
        return f"    ⚙️ [WRITE_CONFIG]    {dst_str}{reason_str}"
    elif action.action_type == FileActionType.SKIP_IDENTICAL:
        if src_str and src_str != dst_str:
            return f"    ⏭️ [SKIP_IDENTICAL]  {src_str} -> {dst_str}{reason_str}"
        return f"    ⏭️ [SKIP_IDENTICAL]  {dst_str}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_OVERWRITE:
        return f"    🛡️ [BACKUP_OVERWRITE]  {src_str}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_PRUNE:
        return f"    📦 [BACKUP_PRUNE]    {src_str}{reason_str}"
    elif action.action_type == FileActionType.DELETE_ITEM:
        return f"    🗑️ [DELETE_ITEM]     {dst_str}{reason_str}"
    elif action.action_type == FileActionType.DELETE_TREE:
        return f"    🗑️ [DELETE_TREE]     {dst_str}{reason_str}"
    elif action.action_type == FileActionType.INFO_MESSAGE:
        return f"    📢 [INFO]            {action.reason}"
    action_name = action.action_type.value if hasattr(action.action_type, "value") else action.action_type
    return f"    [{action_name}] {src_str} -> {dst_str}{reason_str}"


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


def is_mutating_action(action: FileAction) -> bool:
    """Returns True if the action represents a physical host or repository mutation."""
    return action.action_type not in NO_CHANGE_ACTION_TYPES


def filter_display_actions(
    actions: Optional[Sequence[_ActionT]],
    verbose: bool = False,
) -> List[_ActionT]:
    """Filters a sequence of FileActions for display, omitting NO_CHANGE actions unless verbose is True."""
    if not actions:
        return []
    if verbose:
        return list(actions)
    return [a for a in actions if is_mutating_action(a)]


# =============================================================================
# Backup Item & Stash Operations
# =============================================================================

def _stash_existing_backup(
    backup_dest: Path,
    backup_pkg_root: Path,
    sudo: bool,
) -> None:
    """Preserves an existing backup_dest in backup/<pkg>/wal/stash/ before overwriting it.

    Computes the stash destination via rebase_path(backup_dest, backup_pkg_root, stash_root).
    If a stash entry already exists at stash_dest (from an earlier step within the same
    uncommitted transaction), the earliest stash entry is preserved and the intermediate
    backup_dest is removed.
    """
    if not (backup_dest.exists() or backup_dest.is_symlink()):
        return

    stash_root = backup_pkg_root / BackupSubfolder.WAL / BackupSubfolder.STASH
    stash_dest = rebase_path(backup_dest, backup_pkg_root, stash_root)
    if stash_dest is None or stash_dest == backup_dest or stash_dest.exists() or stash_dest.is_symlink():
        remove_tree(backup_dest, sudo)
        return

    ensure_dir(stash_dest.parent, sudo)
    move_tree(backup_dest, stash_dest, sudo, resolve_symlinks=False)


def backup_item(
    src: Path,
    backup_dest: Path,
    backup_pkg_root: Path,
    sudo: bool,
    resolve_symlinks: bool = True,
) -> None:
    """Recursively backs up target src to backup_dest, stashing any prior backup at backup_dest.

    Resolves symlinks when resolve_symlinks is True. Broken symlinks are backed up as-is
    without resolving.
    """
    if not src.exists() and not src.is_symlink():
        return

    _stash_existing_backup(backup_dest, backup_pkg_root, sudo)

    if not resolve_symlinks:
        move_tree(src, backup_dest, sudo, resolve_symlinks=False)
        return

    diff = compare_folders(src, backup_dest, resolve_symlinks=True)

    for rel_file in diff.added + diff.modified:
        target_src = src / rel_file if rel_file != Path("") else src
        target_dst = backup_dest / rel_file if rel_file != Path("") else backup_dest

        if is_concrete_dir(target_src):
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
            move_tree(target_src, target_dst, sudo, resolve_symlinks=False)
        else:
            move_tree(target_src, target_dst, sudo, resolve_symlinks=True)

    if is_concrete_dir(src):
        remove_file_or_empty_dir(src, sudo)


# =============================================================================
# Write-Ahead Log (WAL) Recording & Reversal Helpers
# =============================================================================

def _find_missing_dir_chain(dst_path: Path) -> List[Path]:
    """Returns the ordered chain (shallowest to deepest) of directories that do not yet exist for dst_path."""
    missing: List[Path] = []
    curr = dst_path
    while curr and not (curr.exists() or curr.is_symlink()):
        missing.append(curr)
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    missing.reverse()
    return missing


def _build_missing_dir_wal_actions(
    dir_path: Path,
    wal_dir: Optional[Path],
    reason: str = "",
) -> List[FileAction]:
    """Returns ENSURE_DIR WAL actions from shallowest to deepest for missing directories in dir_path."""
    if wal_dir is None:
        return []
    return [
        NormalFileAction(action_type=FileActionType.ENSURE_DIR, dst_path=d, reason=reason)
        for d in _find_missing_dir_chain(dir_path)
    ]


def _append_wal_actions(
    wal_dir: Path,
    actions: Sequence[FileAction],
) -> None:
    """Appends executed reversible actions as JSON lines to wal_dir/actions.json."""
    if not actions:
        return
    lines = "".join(f"{json.dumps(a.to_dict())}\n" for a in actions)
    ensure_dir(wal_dir)
    wal_file = wal_dir / WAL_ACTIONS_FILE_NAME
    with wal_file.open("a", encoding="utf-8") as f:
        f.write(lines)
        f.flush()


def _parse_wal_line(line: str, wal_file: Path) -> Optional[FileAction]:
    """Parses a single non-empty JSON line from a WAL file into a FileAction, or None if malformed."""
    try:
        loaded = json.loads(line)
        if isinstance(loaded, Mapping):
            return FileAction.from_dict(loaded)
    except Exception as exc:
        logger.warning(f"Skipping malformed WAL entry in '{wal_file}': {exc}")
    return None


def load_wal_actions(wal_file: Path) -> List[FileAction]:
    """Loads serialized NormalFileAction / BackupFileAction entries from an append-only WAL actions.json file."""
    raw_lines = filter(None, map(str.strip, wal_file.read_text(encoding="utf-8").splitlines()))
    parsed = (_parse_wal_line(line, wal_file) for line in raw_lines)
    return [action for action in parsed if action is not None]


def _undo_single_wal_action(
    action: FileAction,
    pkg_backup_root: Path,
    sudo: bool = False,
) -> None:
    """Reverses a single executed WAL action on the host filesystem."""
    if action.action_type in CREATE_ACTION_TYPES:
        if action.dst_path and (action.dst_path.exists() or action.dst_path.is_symlink()):
            remove_file_or_empty_dir(action.dst_path, sudo)

    elif action.action_type in BACKUP_ACTION_TYPES:
        if action.src_path and action.dst_path and (action.dst_path.exists() or action.dst_path.is_symlink()):
            ensure_dir(action.src_path.parent, sudo)
            if action.src_path.exists() or action.src_path.is_symlink():
                remove_tree(action.src_path, sudo)
            move_tree(action.dst_path, action.src_path, sudo, resolve_symlinks=False)
            if pkg_backup_root.is_dir():
                prune_empty_parents(action.dst_path.parent, pkg_backup_root)

    elif action.action_type == FileActionType.ENSURE_DIR:
        if action.dst_path and (action.dst_path.exists() or action.dst_path.is_symlink()):
            remove_file_or_empty_dir(action.dst_path, sudo)


def _restore_single_stash_path(
    rel_path: Path,
    stash_root: Path,
    pkg_backup_root: Path,
    sudo: bool,
) -> None:
    """Restores a single relative path (file, symlink, or empty directory) from stash_root to pkg_backup_root."""
    stash_item = stash_root / rel_path
    dest_item = pkg_backup_root / rel_path
    if is_concrete_dir(stash_item):
        if not (dest_item.exists() or dest_item.is_symlink()):
            ensure_dir(dest_item, sudo)
        return
    ensure_dir(dest_item.parent, sudo)
    if dest_item.exists() or dest_item.is_symlink():
        remove_tree(dest_item, sudo)
    move_tree(stash_item, dest_item, sudo, resolve_symlinks=False)


def restore_wal_stash(pkg_backup_root: Path, sudo: bool = False) -> None:
    """Moves all stashed backup items from backup/<pkg>/wal/stash/ back to backup/<pkg>/."""
    stash_root = pkg_backup_root / BackupSubfolder.WAL / BackupSubfolder.STASH
    if not stash_root.is_dir():
        return

    stash_rel_paths = list_folder_paths(
        stash_root,
        resolve_symlinks=False,
        dir_mode=DirMode.ONLY_EMPTY_DIR,
    )
    for rel_path in stash_rel_paths:
        _restore_single_stash_path(rel_path, stash_root, pkg_backup_root, sudo)

    remove_tree(stash_root, sudo)


def clear_package_wal(pkg_backup_root: Path, sudo: bool = False) -> None:
    """Removes backup/<pkg>/wal/ and prunes backup/<pkg>/ if empty."""
    wal_dir = pkg_backup_root / BackupSubfolder.WAL
    if wal_dir.exists() or wal_dir.is_symlink():
        remove_tree(wal_dir, sudo)
    for subfolder in (BackupSubfolder.OVERWRITTEN, BackupSubfolder.DELETED):
        sub_dir = pkg_backup_root / subfolder.value
        if sub_dir.is_dir() and not any(sub_dir.iterdir()):
            remove_file_or_empty_dir(sub_dir, sudo)
    if pkg_backup_root.is_dir() and not any(pkg_backup_root.iterdir()):
        remove_file_or_empty_dir(pkg_backup_root, sudo)


def rollback_package_wal(pkg_backup_root: Path, sudo: bool = False) -> bool:
    """Reverses recorded WAL actions in LIFO order, restores stashed backups, and removes wal/.

    If the WAL folder or actions.json file does not exist, logs a warning and skips WAL rollback.
    Returns True if a WAL file was found and reversed, False otherwise.
    """
    wal_dir = pkg_backup_root / BackupSubfolder.WAL
    wal_file = wal_dir / WAL_ACTIONS_FILE_NAME
    if not wal_dir.is_dir() or not wal_file.is_file():
        logger.warning(f"WAL file not found at '{wal_file}'; skipping WAL rollback.")
        if wal_dir.is_dir():
            restore_wal_stash(pkg_backup_root, sudo=sudo)
            clear_package_wal(pkg_backup_root, sudo=sudo)
        return False

    actions = load_wal_actions(wal_file)
    for action in reversed(actions):
        _undo_single_wal_action(action, pkg_backup_root=pkg_backup_root, sudo=sudo)

    restore_wal_stash(pkg_backup_root, sudo=sudo)
    clear_package_wal(pkg_backup_root, sudo=sudo)
    return True


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

    wal_entries_to_record: List[FileAction] = []

    if isinstance(action, BackupFileAction):
        logger.debug(f"   Backing up to: {action.dst_path}")
        backup_item(
            src=action.src_path,
            backup_dest=action.dst_path,
            backup_pkg_root=action.backup_pkg_root,
            sudo=context.sudo,
            resolve_symlinks=context.resolve_symlinks,
        )
        if action.action_type == FileActionType.BACKUP_PRUNE:
            remove_file_or_empty_dir(action.src_path, context.sudo)
        else:
            remove_tree(action.src_path, context.sudo)
        wal_entries_to_record.append(action)

    elif action.action_type == FileActionType.ENSURE_DIR:
        if action.dst_path:
            if action.dst_path.is_symlink() or (action.dst_path.exists() and not action.dst_path.is_dir()):
                raise NotADirectoryError(
                    f"Cannot ensure directory '{action.dst_path}': path exists and is not a directory."
                )
            wal_entries_to_record.extend(
                _build_missing_dir_wal_actions(action.dst_path, context.wal_dir, reason=action.reason)
            )
            dir_existed = action.dst_path.is_dir()
            ensure_dir(action.dst_path, context.sudo)
            if dir_existed:
                logger.debug(action_line)
            else:
                logger.info(action_line)

    elif action.action_type == FileActionType.CREATE_SYMLINK:
        if action.src_path and action.dst_path:
            wal_entries_to_record.extend(
                _build_missing_dir_wal_actions(action.dst_path.parent, context.wal_dir)
            )
            wal_entries_to_record.append(action)
            relative_target = compute_relative_symlink_target(action.src_path, action.dst_path.parent)
            create_symlink(relative_target, action.dst_path, context.sudo)

    elif action.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY):
        if action.src_path and action.dst_path:
            if action.action_type == FileActionType.CREATE_COPY:
                wal_entries_to_record.extend(
                    _build_missing_dir_wal_actions(action.dst_path.parent, context.wal_dir)
                )
                wal_entries_to_record.append(action)
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
            wal_entries_to_record.extend(
                _build_missing_dir_wal_actions(action.dst_path.parent, context.wal_dir)
            )
            wal_entries_to_record.append(action)
            ensure_dir(action.dst_path.parent, context.sudo)
            write_file(action.dst_path, b"", sudo=context.sudo)

    if context.wal_dir is not None and wal_entries_to_record:
        _append_wal_actions(context.wal_dir, wal_entries_to_record)


def execute_delivery_actions(
    context: FileActionExecutionContext,
    actions: Sequence[FileAction],
) -> None:
    """Deterministically executes a sequence of planned delivery actions on the host system."""
    for action in actions:
        execute_single_action(context, action)

