"""Core Folder Delivery Planning Engine.

Provides unified filesystem action planning, ancestor directory conflict resolution,
backup routing, and declarative action generation across package installation,
uninstallation, backup restoration, and symlink detachment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .constants import DEFAULT_INSTALL_METHOD, BackupSubfolder, InstallMethod, DRIFT_KEEP_FILE_NAME
from .exceptions import InstallCollisionError
from .file_action import (
    FileAction,
    FileActionType,
    FileActionExecutionContext,
    BACKUP_ACTION_TYPES,
    BACKUP_OR_DELETE_ACTION_TYPES,
    CREATE_ACTION_TYPES,
    DELETE_ACTION_TYPES,
    format_action_line,
    format_action_summary,
)
from .folder_diff import FolderDiff
from ..utils.file_inspect import contents_differ, permissions_differ, tree_files, is_concrete_dir
from ..utils.path_utils import (
    compute_relative_symlink_target,
    decode_dot_prefix,
    encode_dot_prefix,
    is_relative_to,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Delivery Context Models
# =============================================================================

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
    is_tree: bool = False,
) -> FileAction:
    """Plans BACKUP_OVERWRITE (or BACKUP_PRUNE if is_orphan) when backup is configured, or DELETE_ITEM/DELETE_TREE when absent."""
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

    action_type = FileActionType.DELETE_TREE if is_tree else FileActionType.DELETE_ITEM
    return FileAction(
        action_type=action_type,
        dst_path=target,
        reason=reason,
    )


def _inspect_directory_node(
    context: DeliveryInspectionContext,
    target_dir: Path,
    source_dir: Optional[Path],
    rel_path: Path,
    has_backed_up_ancestor: bool = False,
    prune_keep_file: bool = False,
) -> List[FileAction]:
    """Inspects a directory destination against source directory, planning creation, permissions sync, or collision resolution."""
    if has_backed_up_ancestor or not (target_dir.exists() or target_dir.is_symlink()):
        return [FileAction(action_type=FileActionType.ENSURE_DIR, dst_path=target_dir)]

    # already a directory, check for permission differences.
    # in reverse mode, if the directory is populated, we should prune the .drift_keep file if it exists
    if is_concrete_dir(target_dir):
        node_actions: List[FileAction] = []
        if context.reverse_mode and prune_keep_file:
            keep_file = target_dir / DRIFT_KEEP_FILE_NAME
            if keep_file.is_file():
                node_actions.append(FileAction(
                    action_type=FileActionType.DELETE_ITEM,
                    dst_path=keep_file,
                    reason="Prune .drift_keep from populated directory",
                ))
        if source_dir is not None and source_dir.is_dir() and permissions_differ(source_dir, target_dir):
            src_mode = oct(source_dir.stat().st_mode & 0o777)
            dst_mode = oct(target_dir.stat().st_mode & 0o777)
            node_actions.append(FileAction(
                action_type=FileActionType.UPDATE_PERMISSION,
                src_path=source_dir,
                dst_path=target_dir,
                reason=f"Permissions differ ({dst_mode} -> {src_mode})",
            ))
        return node_actions

    # Blocked by an internal symlink, foreign symlink, or physical file
    if target_dir.is_symlink():
        is_internal = False
        try:
            is_internal = is_relative_to(target_dir.resolve(), context.abs_drift_root)
        except Exception:
            is_internal = False
        reason = "Internal ancestor symlink conflict" if is_internal else "Symlink blocking directory"
    else:
        reason = "File blocking directory"

    return [
        _plan_backup_or_delete(context, target_dir, rel_path, reason=reason),
        FileAction(action_type=FileActionType.ENSURE_DIR, dst_path=target_dir),
    ]


def _inspect_single_ancestor(
    context: DeliveryInspectionContext,
    ancestor_target: Path,
    rel_path: Path,
    has_backed_up_ancestor: bool = False,
) -> List[FileAction]:
    """Inspects a single ancestor directory path and returns necessary directory creation or backup actions."""
    source_dir_path = context.translate_source_path(rel_path)
    return _inspect_directory_node(
        context=context,
        target_dir=ancestor_target,
        source_dir=source_dir_path if source_dir_path.is_dir() else None,
        rel_path=rel_path,
        has_backed_up_ancestor=has_backed_up_ancestor,
        prune_keep_file=True,
    )


def _check_has_backed_up_ancestor(target: Path, backed_up_targets: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of target has been backed up."""
    return any(parent in backed_up_targets for parent in target.parents)


def inspect_ancestor_directories(
    context: DeliveryInspectionContext,
    deployable_files: Sequence[Path],
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

        has_backed_up = _check_has_backed_up_ancestor(ancestor_target, backed_up_ancestor_targets)
        res = _inspect_single_ancestor(
            context=context,
            ancestor_target=ancestor_target,
            rel_path=rel_path,
            has_backed_up_ancestor=has_backed_up,
        )
        if any(
            a.action_type in BACKUP_OR_DELETE_ACTION_TYPES
            and (a.src_path == ancestor_target or a.dst_path == ancestor_target)
            for a in res
        ):
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
            is_tree=True,
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


def _resolve_leaf_symlink(
    context: DeliveryInspectionContext,
    source_file: Path,
    system_target: Path,
    rel_file: Path,
    actions: List[FileAction],
) -> Optional[Path]:
    """Resolves a leaf symlink, plans cleanup for broken symlinks, and returns the resolved Path or None."""
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
        return None

    assert resolved_src is not None, "Resolved source symlink should not be None after successful resolve"
    assert not resolved_src.is_dir(), f"Unexpected directory symlink reached leaf file inspection: '{source_file}' -> '{resolved_src}'"

    if resolved_src == system_target or (system_target.exists() and resolved_src == system_target.resolve()):
        return system_target
    return resolved_src


def _inspect_directory_leaf(
    context: DeliveryInspectionContext,
    source_dir: Path,
    system_target: Path,
    rel_path: Path,
    backed_up_ancestor_targets: Set[Path],
) -> List[FileAction]:
    """Inspects a source directory leaf, planning directory creation, permission updates, or empty folder tracking."""
    has_backed_up_parent = _check_has_backed_up_ancestor(system_target, backed_up_ancestor_targets)
    dir_actions = _inspect_directory_node(
        context=context,
        target_dir=system_target,
        source_dir=source_dir,
        rel_path=rel_path,
        has_backed_up_ancestor=has_backed_up_parent,
        prune_keep_file=False,
    )
    if any(
        a.action_type in BACKUP_OR_DELETE_ACTION_TYPES
        and (a.src_path == system_target or a.dst_path == system_target)
        for a in dir_actions
    ):
        backed_up_ancestor_targets.add(system_target)

    actions = list(dir_actions)

    # In reverse_mode, if host folder is a concrete empty directory, track it with .drift_keep in install/
    if context.reverse_mode and not source_dir.is_symlink() and not any(source_dir.iterdir()):
        keep_file = system_target / DRIFT_KEEP_FILE_NAME
        if not keep_file.exists():
            actions.append(FileAction(
                action_type=FileActionType.CREATE_KEEP_FILE,
                dst_path=keep_file,
                reason="Track empty folder with .drift_keep",
            ))

    return actions


def _inspect_leaf(
    context: DeliveryInspectionContext,
    rel_file: Path,
    handled_targets: Set[Path],
    actions: List[FileAction],
    backed_up_ancestor_targets: Set[Path],
) -> None:
    """Translates paths, handles directories and symlinks, then delegates to _inspect_resolved_leaf_file."""
    source_file, system_target = context.translate_path(rel_file)
    if system_target in handled_targets:
        return
    handled_targets.add(system_target)

    # 1. Directory or symlink-to-directory inspection
    if source_file.is_dir():
        actions.extend(_inspect_directory_leaf(
            context=context,
            source_dir=source_file,
            system_target=system_target,
            rel_path=rel_file,
            backed_up_ancestor_targets=backed_up_ancestor_targets,
        ))
        return

    # 2. Leaf file symlink resolution
    if source_file.is_symlink():
        resolved_leaf = _resolve_leaf_symlink(
            context=context,
            source_file=source_file,
            system_target=system_target,
            rel_file=rel_file,
            actions=actions,
        )
        if resolved_leaf is None:
            return
        source_file = resolved_leaf

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

    # 3. Delegate concrete resolved paths to inner core
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
    """Inspects an orphaned path on target and plans BACKUP_PRUNE or DELETE_ITEM/DELETE_TREE if present.

    Safety Guard:
    DELETE_TREE is strictly reserved for reverse_mode (where target is Drift's internal install/
    state database, ensuring complete removal of deleted directories and .drift_keep stubs).
    In forward delivery mode, orphan pruning uses DELETE_ITEM (or BACKUP_PRUNE) so that
    remove_file_or_empty_dir safely preserves any directories on the host that have been populated
    with user files.
    """
    system_target = context.translate_target_path(orphan_rel)
    if not (system_target.exists() or system_target.is_symlink()):
        return []
    is_tree = context.reverse_mode and is_concrete_dir(system_target)
    return [_plan_backup_or_delete(context, system_target, orphan_rel, reason=reason, is_orphan=True, is_tree=is_tree)]


def _inspect_orphans(
    context: DeliveryInspectionContext,
    deployable_files: Sequence[Path],
    deployed_files: Sequence[Path],
) -> List[FileAction]:
    """Plans BACKUP_PRUNE or DELETE_ITEM actions for historical deployed files missing in candidate package."""
    if context.reverse_mode:
        deployable_norm = {decode_dot_prefix(p) for p in deployable_files}
        deployed_norm = {decode_dot_prefix(p) for p in deployed_files}
        orphaned_files = sorted(deployed_norm - deployable_norm)
    else:
        deployable_norm = set(deployable_files)
        orphaned_files = sorted(set(deployed_files) - set(deployable_files))

    deployable_ancestors = {
        parent
        for dep in deployable_norm
        for parent in dep.parents
        if parent != Path("")
    }

    actions: List[FileAction] = []
    for orphaned in orphaned_files:
        if orphaned in deployable_ancestors:
            continue
        actions.extend(_plan_orphan_prune(context, orphaned, f"Orphaned file '{orphaned}' prune"))
    return actions


# =============================================================================
# High-Level Planning Functions
# =============================================================================

def plan_folder_delivery(
    context: DeliveryInspectionContext,
    deployable_files: Sequence[Path],
    deployed_files: Sequence[Path] = (),
) -> List[FileAction]:
    """Plans declarative filesystem operations for delivering source_dir to target_dir."""
    if not context.reverse_mode:
        assert_target_dir_outside_drift_root(context.target_dir, context.drift_root)
    actions: List[FileAction] = []
    handled_targets: Set[Path] = set()

    # 1. Intermediate Ancestor Directories Inspection
    backed_up_ancestor_targets = inspect_ancestor_directories(
        context=context,
        deployable_files=deployable_files,
        handled_targets=handled_targets,
        actions=actions,
    )

    # 2. Leaf Files Inspection
    for rel_file in deployable_files:
        _inspect_leaf(
            context=context,
            rel_file=rel_file,
            handled_targets=handled_targets,
            actions=actions,
            backed_up_ancestor_targets=backed_up_ancestor_targets,
        )

    # 3. Orphan Files Reconciliation (additions before deletions)
    actions.extend(_inspect_orphans(context, deployable_files, deployed_files))

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
        if not (install_pkg_dir / rel_file).is_dir()
        and (target_dir / encode_dot_prefix(rel_file)).is_symlink()
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
                action_type=FileActionType.DELETE_ITEM,
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
            action_type=FileActionType.DELETE_ITEM,
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
            action_type=FileActionType.DELETE_ITEM,
            dst_path=target_dir / rel,
            reason="Deleted in rendered package",
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
                reason="File created",
            ))

    # 3. Modifications
    for rel in diff.modified:
        src = source_dir / rel
        dst = target_dir / rel
        if is_concrete_dir(src):
            if is_concrete_dir(dst) and permissions_differ(src, dst):
                src_mode = oct(src.stat().st_mode & 0o777)
                dst_mode = oct(dst.stat().st_mode & 0o777)
                actions.append(FileAction(
                    action_type=FileActionType.UPDATE_PERMISSION,
                    src_path=src,
                    dst_path=dst,
                    reason=f"Permissions changed ({dst_mode} -> {src_mode})",
                ))
            else:
                raise RuntimeError(
                    f"Impossible state in plan_actions_from_folder_diff: directory '{rel}' was marked as modified, "
                    f"but source '{src}' and destination '{dst}' do not represent a valid directory permission modification."
                )
        elif rel in diff.permissions_differ:
            if src.exists() and dst.exists():
                src_mode = oct(src.stat().st_mode & 0o777)
                dst_mode = oct(dst.stat().st_mode & 0o777)
                permission_reason = f"Permissions changed ({dst_mode} -> {src_mode})"
            else:
                permission_reason = "Permissions changed"
            actions.append(FileAction(
                action_type=FileActionType.UPDATE_PERMISSION,
                src_path=src,
                dst_path=dst,
                reason=permission_reason,
            ))
        else:
            actions.append(FileAction(
                action_type=FileActionType.UPDATE_COPY,
                src_path=src,
                dst_path=dst,
                reason="Content modified",
            ))

    return actions
