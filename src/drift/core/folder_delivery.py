"""Core Folder Delivery Engine.

Provides unified filesystem action planning, ancestor directory conflict resolution,
and deterministic execution across package installation, uninstallation, backup restoration,
and symlink detachment.
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .constants import DEFAULT_INSTALL_METHOD, BackupSubfolder, InstallMethod
from .exceptions import InstallCollisionError
from .folder_diff import FolderDiff
from .serialization import SerializableModel
from ..utils.file_inspect import contents_differ, permissions_differ, tree_files, is_concrete_dir
from ..utils.file_ops import (
    copy_file,
    copy_permissions,
    create_symlink,
    ensure_dir,
    remove,
)
from .sync_ops import backup_file_or_dir_external
from ..utils.path_utils import (
    compute_relative_symlink_target,
    decode_dot_prefix,
    encode_dot_prefix,
    is_relative_to,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Action Enums & Context Models
# =============================================================================

class FileActionType(str, Enum):
    """Specific filesystem operation planned or executed during delivery."""
    # Creations & Updates
    CREATE_SYMLINK = "CREATE_SYMLINK"
    CREATE_COPY = "CREATE_COPY"
    UPDATE_COPY = "UPDATE_COPY"
    UPDATE_PERMISSION = "UPDATE_PERMISSION"
    ENSURE_DIR = "ENSURE_DIR"

    # Skips (already matching desired state)
    SKIP_IDENTICAL = "SKIP_IDENTICAL"

    # Collisions & Cleanups
    BACKUP_OVERWRITE = "BACKUP_OVERWRITE"  # Existing host node backed up and removed
    BACKUP_PRUNE = "BACKUP_PRUNE"          # Historical host orphan backed up and removed
    DELETE_FILE = "DELETE_FILE"            # Direct file/symlink deletion without backup

    # Workflow Notifications
    INFO_MESSAGE = "INFO_MESSAGE"          # Informational message logged during execution


@dataclass
class FileAction(SerializableModel):
    """Declarative specification of a single file/directory operation on the host system."""
    action_type: FileActionType
    src_path: Optional[Path] = None
    dst_path: Optional[Path] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class FileActionExecutionContext:
    """Encapsulates host filesystem permissions and symlink resolution flags for executing file actions."""
    sudo: bool = False
    resolve_symlinks: bool = True


@dataclass(frozen=True)
class DeliveryInspectionContext:
    """Encapsulates invariant target paths, source paths, install configurations, and backup policies for folder delivery planning."""
    target_dir: Path
    source_dir: Path
    drift_root: Path
    install_method: InstallMethod
    is_first_time: bool
    backup_pkg_dir: Optional[Path]
    backup_subfolder: BackupSubfolder
    reverse_mode: bool = False

    @property
    def abs_drift_root(self) -> Path:
        """Returns pre-resolved absolute Path to drift workspace root."""
        return self.drift_root.resolve()

    def resolve_backup_path(
        self,
        rel_path: Path,
        subfolder: Optional[BackupSubfolder] = None,
    ) -> Optional[Path]:
        """Resolves the backup destination path for a relative path if backup_pkg_dir is configured."""
        if not self.backup_pkg_dir:
            return None
        target_subfolder = subfolder if subfolder is not None else self.backup_subfolder
        subfolder_str = (
            target_subfolder.value
            if isinstance(target_subfolder, BackupSubfolder)
            else str(target_subfolder)
        )
        return self.backup_pkg_dir / subfolder_str / decode_dot_prefix(rel_path)

    def translate_target_rel_path(self, rel_path: Path) -> Path:
        """Translates relative path to its relative target Path representation respecting reverse_mode."""
        return decode_dot_prefix(rel_path) if self.reverse_mode else encode_dot_prefix(rel_path)

    def translate_source_rel_path(self, rel_path: Path) -> Path:
        """Translates relative path to its relative source Path representation respecting reverse_mode."""
        return encode_dot_prefix(rel_path) if self.reverse_mode else decode_dot_prefix(rel_path)

    def translate_target_path(self, rel_path: Path) -> Path:
        """Translates relative path to its absolute/prefixed target_dir Path respecting reverse_mode."""
        return self.target_dir / self.translate_target_rel_path(rel_path)

    def translate_source_path(self, rel_path: Path) -> Path:
        """Translates relative path to its absolute/prefixed source_dir Path respecting reverse_mode."""
        return self.source_dir / self.translate_source_rel_path(rel_path)

    def translate_path(self, rel_path: Path) -> Tuple[Path, Path]:
        """Translates a relative file path to (source_file, system_target) respecting reverse_mode."""
        return self.translate_source_path(rel_path), self.translate_target_path(rel_path)


# =============================================================================
# Centralized Action Formatting Helpers
# =============================================================================

def format_action_line(action: FileAction) -> str:
    """Formats a single FileAction into a clean terminal line."""
    reason_str = f" ({action.reason})" if action.reason else ""
    if action.action_type == FileActionType.ENSURE_DIR:
        return f"    📁 [ENSURE_DIR]      {action.dst_path}"
    elif action.action_type == FileActionType.CREATE_SYMLINK:
        return f"    🔗 [CREATE_SYMLINK]  {action.src_path} -> {action.dst_path}"
    elif action.action_type == FileActionType.CREATE_COPY:
        return f"    📄 [CREATE_COPY]     {action.src_path} -> {action.dst_path}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_COPY:
        return f"    📝 [UPDATE_COPY]     {action.src_path} -> {action.dst_path}{reason_str}"
    elif action.action_type == FileActionType.UPDATE_PERMISSION:
        return f"    🔒 [UPDATE_PERMISSION] {action.src_path} -> {action.dst_path}{reason_str}"
    elif action.action_type == FileActionType.SKIP_IDENTICAL:
        return f"    ⏭️ [SKIP_IDENTICAL]  {action.src_path} -> {action.dst_path}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_OVERWRITE:
        target = action.src_path or action.dst_path
        return f"    🛡️ [BACKUP_OVERWRITE] {target}{reason_str}"
    elif action.action_type == FileActionType.BACKUP_PRUNE:
        target = action.src_path or action.dst_path
        return f"    📦 [BACKUP_PRUNE]    {target}{reason_str}"
    elif action.action_type == FileActionType.DELETE_FILE:
        return f"    🗑️ [DELETE_FILE]    {action.dst_path}{reason_str}"
    elif action.action_type == FileActionType.INFO_MESSAGE:
        return f"    📢 [INFO]            {action.reason}"
    return f"    [{action.action_type}] {action.src_path} -> {action.dst_path}{reason_str}"


def format_action_summary(actions: Sequence[FileAction]) -> str:
    """Formats a concise summary string of action counts."""
    counts = []
    created = [a for a in actions if a.action_type in (FileActionType.CREATE_SYMLINK, FileActionType.CREATE_COPY)]
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
    removed = [a for a in actions if a.action_type == FileActionType.DELETE_FILE]
    if removed:
        counts.append(f"{len(removed)} to remove")
    backups = [a for a in actions if a.action_type in (FileActionType.BACKUP_OVERWRITE, FileActionType.BACKUP_PRUNE)]
    if backups:
        counts.append(f"{len(backups)} to backup")
    ensured = [a for a in actions if a.action_type == FileActionType.ENSURE_DIR]
    if ensured:
        counts.append(f"{len(ensured)} directories")
    return ", ".join(counts) if counts else "0 actions"





# =============================================================================
# Ancestor Directory & Collision Inspection Helpers
# =============================================================================

def assert_target_dir_outside_drift_root(target_dir: Path, drift_root: Path) -> None:
    """Guards against delivery into drift_root via direct path or symlink resolution."""
    abs_drift_root = drift_root.resolve()
    resolved_target = target_dir.resolve()
    if is_relative_to(resolved_target, abs_drift_root):
        raise InstallCollisionError(
            f"Safety Abort: Target directory '{target_dir}' (resolved to '{resolved_target}') "
            f"points inside drift workspace root '{drift_root}'. "
            f"Resolving this automatically is unsafe. Please resolve manually."
        )


def _expand_path_ancestors(rel: Path, context: DeliveryInspectionContext) -> Set[Path]:
    target_rel = context.translate_target_rel_path(rel)
    return {p for p in target_rel.parents if p != Path(".")}


def _plan_backup_or_delete(
    context: DeliveryInspectionContext,
    target: Path,
    rel_path: Path,
    reason: Optional[str] = None,
    is_orphan: bool = False,
) -> FileAction:
    """Plans BACKUP_OVERWRITE (or BACKUP_PRUNE if is_orphan) when backup is configured, or DELETE_FILE when absent."""
    subfolder = BackupSubfolder.DELETED_FILES if is_orphan else None
    backup_dst = context.resolve_backup_path(rel_path, subfolder=subfolder)
    if backup_dst:
        action_type = FileActionType.BACKUP_PRUNE if is_orphan else FileActionType.BACKUP_OVERWRITE
        return FileAction(
            action_type=action_type,
            src_path=target,
            dst_path=backup_dst,
            reason=reason,
        )
    return FileAction(
        action_type=FileActionType.DELETE_FILE,
        dst_path=target,
        reason=reason,
    )


def _inspect_single_ancestor(
    context: DeliveryInspectionContext,
    ancestor_target: Path,
    rel_path: Path,
) -> List[FileAction]:
    """Inspects a single ancestor directory path and returns necessary directory creation or backup actions."""
    if not (ancestor_target.exists() or ancestor_target.is_symlink()):
        return [FileAction(action_type=FileActionType.ENSURE_DIR, dst_path=ancestor_target)]

    if is_concrete_dir(ancestor_target):
        return []

    # Blocked by an internal symlink, foreign symlink, or physical file
    if ancestor_target.is_symlink():
        is_internal = False
        try:
            is_internal = is_relative_to(ancestor_target.resolve(), context.abs_drift_root)
        except Exception:
            is_internal = False
        reason = "Internal ancestor symlink conflict" if is_internal else "Symlink blocking directory"
    else:
        reason = "File blocking directory"

    return [
        _plan_backup_or_delete(context, ancestor_target, rel_path, reason=reason),
        FileAction(action_type=FileActionType.ENSURE_DIR, dst_path=ancestor_target),
    ]


def _check_has_backed_up_ancestor(target: Path, backed_up_targets: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of target has been backed up."""
    return any(parent in backed_up_targets for parent in target.parents)


def inspect_ancestor_directories(
    context: DeliveryInspectionContext,
    deployable_files: Iterable[Path],
    handled_targets: Set[Path],
    actions: List[FileAction],
) -> Set[Path]:
    """Inspects parent directories of deployable files from shallowest to deepest, planning necessary directory recreation or backups."""
    all_ancestors = {p for rel in deployable_files for p in _expand_path_ancestors(rel, context)}
    sorted_ancestors = sorted(all_ancestors, key=lambda p: len(p.parts))
    backed_up_ancestor_targets: Set[Path] = set()

    for p in sorted_ancestors:
        ancestor_target = context.target_dir / p
        if ancestor_target in handled_targets:
            continue
        handled_targets.add(ancestor_target)
        rel_path = p if context.reverse_mode else decode_dot_prefix(p)

        if _check_has_backed_up_ancestor(ancestor_target, backed_up_ancestor_targets):
            actions.append(FileAction(
                action_type=FileActionType.ENSURE_DIR,
                dst_path=ancestor_target,
            ))
            continue

        res = _inspect_single_ancestor(
            context=context,
            ancestor_target=ancestor_target,
            rel_path=rel_path,
        )
        if any(a.action_type in (FileActionType.BACKUP_OVERWRITE, FileActionType.DELETE_FILE) for a in res):
            backed_up_ancestor_targets.add(ancestor_target)
        actions.extend(res)

    return backed_up_ancestor_targets


def _check_symlink_points_to_source(system_target: Path, source_file: Path) -> bool:
    """Read-only check returning True if system_target symlink resolves or points to source_file."""
    try:
        link_raw = Path(os.readlink(system_target))
        expected_rel = compute_relative_symlink_target(source_file, system_target.parent)
        return link_raw == expected_rel or (system_target.parent / link_raw).resolve() == source_file.resolve()
    except Exception:
        return False


def _plan_file_creation(
    context: DeliveryInspectionContext,
    source_file: Path,
    system_target: Path,
    reason: Optional[str] = None,
) -> FileAction:
    """Returns CREATE_SYMLINK or CREATE_COPY depending on the package install method."""
    action_type = FileActionType.CREATE_SYMLINK if context.install_method == InstallMethod.SYMLINK else FileActionType.CREATE_COPY
    return FileAction(
        action_type=action_type,
        src_path=source_file,
        dst_path=system_target,
        reason=reason,
    )


def _inspect_symlink_leaf(
    context: DeliveryInspectionContext,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
) -> List[FileAction]:
    """Inspects a host symlink at destination and plans overwrite, skip, or re-link actions."""
    if not system_target.exists():
        # Broken symlink
        return [
            _plan_backup_or_delete(context, system_target, rel_file, reason="Broken symlink collision"),
            _plan_file_creation(context, source_file, system_target),
        ]


    # Check if existing symlink already points to source_file
    if _check_symlink_points_to_source(system_target, source_file):
        if context.install_method == InstallMethod.SYMLINK:
            return [FileAction(
                action_type=FileActionType.SKIP_IDENTICAL,
                src_path=source_file,
                dst_path=system_target,
                reason="Symlink already points to source",
            )]
        # Switching from symlink to copy: backup or delete existing symlink and create physical copy
        return [
            _plan_backup_or_delete(context, system_target, rel_file, reason="Replacing symlink with copy"),
            FileAction(
                action_type=FileActionType.CREATE_COPY,
                src_path=source_file,
                dst_path=system_target,
            ),
        ]

    # Symlink points elsewhere (internal conflict vs external collision)
    points_into_drift = False
    try:
        points_into_drift = is_relative_to(system_target.resolve(), context.abs_drift_root)
    except Exception:
        points_into_drift = False

    reason = "Conflicting internal symlink" if points_into_drift else "Colliding external symlink"
    return [
        _plan_backup_or_delete(context, system_target, rel_file, reason=reason),
        _plan_file_creation(context, source_file, system_target),
    ]


def _inspect_physical_file_leaf(
    context: DeliveryInspectionContext,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
) -> List[FileAction]:
    """Inspects a host regular physical file and plans overwrite, update, or skip actions."""
    if source_file.is_symlink():
        return [
            _plan_backup_or_delete(context, system_target, rel_file, reason="Type changed between symlink and file"),
            _plan_file_creation(context, source_file, system_target),
        ]

    if context.install_method == InstallMethod.SYMLINK:
        return [
            _plan_backup_or_delete(context, system_target, rel_file, reason="Physical file collides with symlink"),
            FileAction(
                action_type=FileActionType.CREATE_SYMLINK,
                src_path=source_file,
                dst_path=system_target,
            ),
        ]

    # COPY method
    if context.is_first_time:
        return [
            _plan_backup_or_delete(context, system_target, rel_file, reason="Pre-existing file collision"),
            FileAction(
                action_type=FileActionType.CREATE_COPY,
                src_path=source_file,
                dst_path=system_target,
            ),
        ]

    try:
        differs = contents_differ(source_file, system_target)
    except Exception:
        differs = True

    if differs:
        return [FileAction(
            action_type=FileActionType.UPDATE_COPY,
            src_path=source_file,
            dst_path=system_target,
            reason="File content updated",
        )]

    if permissions_differ(source_file, system_target):
        src_mode = oct(source_file.stat().st_mode & 0o777)
        dst_mode = oct(system_target.stat().st_mode & 0o777)
        return [FileAction(
            action_type=FileActionType.UPDATE_PERMISSION,
            src_path=source_file,
            dst_path=system_target,
            reason=f"Permissions differ ({dst_mode} -> {src_mode})",
        )]

    return [FileAction(
        action_type=FileActionType.SKIP_IDENTICAL,
        src_path=source_file,
        dst_path=system_target,
        reason="File content matches",
    )]


def _inspect_resolved_leaf_file(
    context: DeliveryInspectionContext,
    rel_file: Path,
    source_file: Path,
    system_target: Path,
    handled_targets: Set[Path],
    actions: List[FileAction],
    backed_up_ancestor_targets: Set[Path],
) -> None:
    """Inspects resolved source against system target and plans delivery actions."""
    # 1. Source and target are identical
    if source_file == system_target:
        actions.append(FileAction(
            action_type=FileActionType.SKIP_IDENTICAL,
            src_path=source_file,
            dst_path=system_target,
            reason="Source and target are identical",
        ))
        return

    # 2. Ancestor directory backed up / recreated or target does not exist yet
    if _check_has_backed_up_ancestor(system_target, backed_up_ancestor_targets):
        actions.append(_plan_file_creation(context, source_file, system_target))
        return

    target_exists_or_symlink = system_target.exists() or system_target.is_symlink()
    if not target_exists_or_symlink:
        actions.append(_plan_file_creation(context, source_file, system_target))
        return

    # 3. Target is concrete directory blocking a leaf file
    if is_concrete_dir(system_target):
        actions.append(_plan_backup_or_delete(
            context,
            system_target,
            rel_file,
            reason="Directory blocking file",
        ))
        actions.append(_plan_file_creation(context, source_file, system_target))
        return

    # 4. Target is a symlink
    if system_target.is_symlink():
        actions.extend(_inspect_symlink_leaf(
            context=context,
            rel_file=rel_file,
            system_target=system_target,
            source_file=source_file,
        ))
        return

    # 5. Regular physical file inspection
    actions.extend(_inspect_physical_file_leaf(
        context=context,
        rel_file=rel_file,
        system_target=system_target,
        source_file=source_file,
    ))


def _inspect_leaf_file(
    context: DeliveryInspectionContext,
    rel_file: Path,
    handled_targets: Set[Path],
    actions: List[FileAction],
    backed_up_ancestor_targets: Set[Path],
) -> None:
    """Translates paths, handles symlinks and host deletions, then delegates to _inspect_resolved_leaf_file."""
    source_file, system_target = context.translate_path(rel_file)
    handled_targets.add(system_target)

    # Handle symlinks
    if source_file.is_symlink():
        try:
            resolved_src = source_file.resolve()
            is_broken = not resolved_src.exists()
        except Exception:
            resolved_src = None
            is_broken = True

        if is_broken:
            if system_target.exists() or system_target.is_symlink():
                actions.append(_plan_backup_or_delete(
                    context,
                    system_target,
                    rel_file,
                    reason="Broken symlink on host" if context.reverse_mode else "Broken symlink collision",
                ))
            return

        assert resolved_src is not None, "Resolved source symlink should not be None after successful resolve"
        if resolved_src.is_dir():
            raise InstallCollisionError(
                f"Invalid deployable leaf file '{source_file}': symlink points to directory '{resolved_src}'. "
                "Deployable leaf files cannot be symlinks to directories."
            )

        if resolved_src == system_target or (system_target.exists() and resolved_src == system_target.resolve()):
            source_file = system_target
        else:
            source_file = resolved_src

    elif context.reverse_mode and not source_file.exists():
        # Physical file deleted on host
        if system_target.exists() or system_target.is_symlink():
            actions.append(_plan_backup_or_delete(
                context,
                system_target,
                rel_file,
                reason="File deleted on host",
            ))
        return

    # Delegate concrete resolved paths to inner core
    _inspect_resolved_leaf_file(
        context=context,
        rel_file=rel_file,
        source_file=source_file,
        system_target=system_target,
        handled_targets=handled_targets,
        actions=actions,
        backed_up_ancestor_targets=backed_up_ancestor_targets,
    )


def _plan_orphan_prune(
    context: DeliveryInspectionContext,
    orphan_rel: Path,
    reason: str,
) -> List[FileAction]:
    """Inspects an orphaned path on target and plans BACKUP_PRUNE or DELETE_FILE if present."""
    system_target = context.translate_target_path(orphan_rel)
    if not (system_target.exists() or system_target.is_symlink()):
        return []
    return [_plan_backup_or_delete(context, system_target, orphan_rel, reason=reason, is_orphan=True)]


def _inspect_orphans(
    context: DeliveryInspectionContext,
    deployable_files: Iterable[Path],
    deployed_files: Iterable[Path],
) -> List[FileAction]:
    """Plans BACKUP_PRUNE or DELETE_FILE actions for historical deployed files missing in candidate package."""
    if context.reverse_mode:
        deployable_norm = {decode_dot_prefix(p) for p in deployable_files}
        deployed_norm = {decode_dot_prefix(p) for p in deployed_files}
        orphaned_files = sorted(deployed_norm - deployable_norm)
    else:
        orphaned_files = sorted(set(deployed_files) - set(deployable_files))

    actions: List[FileAction] = []
    for orphaned in orphaned_files:
        actions.extend(_plan_orphan_prune(context, orphaned, f"Orphaned file '{orphaned}' prune"))
    return actions


# =============================================================================
# High-Level Planning Functions
# =============================================================================

def plan_folder_delivery(
    context: DeliveryInspectionContext,
    deployable_files: Iterable[Path],
    deployed_files: Iterable[Path] = (),
) -> List[FileAction]:
    """Plans declarative filesystem operations for delivering source_dir to target_dir."""
    if not context.reverse_mode:
        assert_target_dir_outside_drift_root(context.target_dir, context.drift_root)
    actions: List[FileAction] = []
    handled_targets: Set[Path] = set()

    deployable_file_list = list(deployable_files)

    # 1. Orphan Files Reconciliation
    actions.extend(_inspect_orphans(context, deployable_file_list, deployed_files))

    # 2. Intermediate Ancestor Directories Inspection
    backed_up_ancestor_targets = inspect_ancestor_directories(
        context=context,
        deployable_files=deployable_file_list,
        handled_targets=handled_targets,
        actions=actions,
    )

    # 3. Leaf Files Inspection
    for rel_file in deployable_file_list:
        _inspect_leaf_file(
            context=context,
            rel_file=rel_file,
            handled_targets=handled_targets,
            actions=actions,
            backed_up_ancestor_targets=backed_up_ancestor_targets,
        )

    return actions


def plan_backup_restoration(
    backup_overwritten_dir: Path,
    target_dir: Path,
    drift_root: Path,
) -> List[FileAction]:
    """Plans declarative actions for safely restoring historical backups to target_dir.

    Evaluates intermediate directories and leaf targets using full folder delivery
    planning with InstallMethod.COPY, ensuring non-colliding backup restoration.
    """
    assert_target_dir_outside_drift_root(target_dir, drift_root)
    if not backup_overwritten_dir.is_dir():
        return []

    backup_files = tree_files(backup_overwritten_dir)
    if not backup_files:
        return []

    actions: List[FileAction] = [
        FileAction(
            action_type=FileActionType.INFO_MESSAGE,
            reason="Restoring overwritten backups to host...",
        )
    ]

    context = DeliveryInspectionContext(
        target_dir=target_dir,
        source_dir=backup_overwritten_dir,
        install_method=InstallMethod.COPY,
        drift_root=drift_root,
        is_first_time=True,
        backup_pkg_dir=backup_overwritten_dir.parent,
        backup_subfolder=BackupSubfolder.DELETED_FILES,
    )
    restore_actions = plan_folder_delivery(
        context=context,
        deployable_files=backup_files,
        deployed_files=(),
    )
    actions.extend(restore_actions)
    return actions


def plan_symlink_conversions(
    deployed_files: Sequence[Path],
    target_dir: Path,
    install_pkg_dir: Path,
) -> List[FileAction]:
    """Plans declarative actions for detaching a symlinked package into concrete physical files."""
    symlinks_to_convert = [
        rel_file
        for rel_file in deployed_files
        if (target_dir / encode_dot_prefix(rel_file)).is_symlink()
    ]
    if not symlinks_to_convert:
        return []

    actions: List[FileAction] = []
    for rel_file in symlinks_to_convert:
        system_target = target_dir / encode_dot_prefix(rel_file)
        src_file = install_pkg_dir / rel_file
        if not src_file.is_file():
            logger.warning(f"Source file not found in install/ repository: {src_file}")
            continue

        actions.append(
            FileAction(
                action_type=FileActionType.DELETE_FILE,
                dst_path=system_target,
                reason="Remove symlink for detach",
            )
        )
        actions.append(
            FileAction(
                action_type=FileActionType.CREATE_COPY,
                src_path=src_file,
                dst_path=system_target,
                reason="Replace symlink with copy (detach)",
            )
        )

    return actions


def plan_file_removals(
    deployed_files: Sequence[Path],
    target_dir: Path,
) -> List[FileAction]:
    """Inspects deployed files on the host and plans removal in reverse order."""
    resolved = [
        target_dir / encode_dot_prefix(rel)
        for rel in sorted(deployed_files, reverse=True)
    ]
    return [
        FileAction(
            action_type=FileActionType.DELETE_FILE,
            dst_path=target,
            reason="Remove deployed file from host",
        )
        for target in resolved
        if target.exists() or target.is_symlink()
    ]


def plan_actions_from_folder_diff(
    diff: FolderDiff,
    source_dir: Path,
    target_dir: Path,
) -> List[FileAction]:
    """Compiles a FolderDiff into a deterministic sequence of FileActions."""
    actions: List[FileAction] = []

    # 1. Deletions first: cleanly clear obsolete paths to avoid directory/file type collisions
    for rel in sorted(diff.deleted, reverse=True):
        actions.append(FileAction(
            action_type=FileActionType.DELETE_FILE,
            dst_path=target_dir / rel,
            reason="Deleted in source",
        ))

    # 2. Additions
    for rel in diff.added:
        src = source_dir / rel
        dst = target_dir / rel
        if is_concrete_dir(src):
            actions.append(FileAction(
                action_type=FileActionType.ENSURE_DIR,
                dst_path=dst,
                reason="Directory created",
            ))
        else:
            actions.append(FileAction(
                action_type=FileActionType.CREATE_COPY,
                src_path=src,
                dst_path=dst,
                reason="New file",
            ))

    # 3. Modifications
    for rel in diff.modified:
        src = source_dir / rel
        dst = target_dir / rel
        if is_concrete_dir(src):
            actions.append(FileAction(
                action_type=FileActionType.ENSURE_DIR,
                dst_path=dst,
                reason="Directory modified",
            ))
        elif rel in diff.permissions_differ:
            actions.append(FileAction(
                action_type=FileActionType.UPDATE_PERMISSION,
                src_path=src,
                dst_path=dst,
                reason="Permissions updated",
            ))
        else:
            actions.append(FileAction(
                action_type=FileActionType.UPDATE_COPY,
                src_path=src,
                dst_path=dst,
                reason="Content modified",
            ))

    return actions


# =============================================================================
# Unified Plan Execution Engine
# =============================================================================

def execute_single_action(
    context: FileActionExecutionContext,
    action: FileAction,
) -> None:
    """Executes a single planned file/directory delivery action on the host system."""
    if action.action_type in (FileActionType.BACKUP_OVERWRITE, FileActionType.BACKUP_PRUNE):
        if action.src_path and action.dst_path:
            if action.reason:
                logger.warning(f"🛡️  [BACKUP] {action.reason} at '{action.src_path}'")
            logger.debug(f"   Backing up to: {action.dst_path}")
            backup_file_or_dir_external(
                action.src_path,
                action.dst_path,
                context.sudo,
                resolve_symlinks=context.resolve_symlinks,
            )
        if action.src_path:
            remove(action.src_path, context.sudo)

    elif action.action_type == FileActionType.ENSURE_DIR:
        if action.dst_path:
            if action.dst_path.is_symlink() or (action.dst_path.exists() and not action.dst_path.is_dir()):
                raise NotADirectoryError(
                    f"Cannot ensure directory '{action.dst_path}': path exists and is not a directory."
                )
            ensure_dir(action.dst_path, context.sudo)

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

    elif action.action_type == FileActionType.DELETE_FILE:
        if action.dst_path:
            remove(action.dst_path, context.sudo)

    elif action.action_type == FileActionType.SKIP_IDENTICAL:
        logger.debug(f"   Skipping '{action.dst_path}': already up-to-date")

    elif action.action_type == FileActionType.INFO_MESSAGE:
        if action.reason:
            logger.info(action.reason)


def execute_delivery_actions(
    context: FileActionExecutionContext,
    actions: Sequence[FileAction],
) -> None:
    """Deterministically executes a sequence of planned delivery actions on the host system."""
    for action in actions:
        execute_single_action(context, action)


def __getattr__(name: str):
    if name in ("PackageInstallPlan", "PackageUninstallPlan"):
        from .result_models import PackageInstallPlan, PackageUninstallPlan
        return PackageInstallPlan if name == "PackageInstallPlan" else PackageUninstallPlan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
