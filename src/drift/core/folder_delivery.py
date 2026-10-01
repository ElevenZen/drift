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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Union

from .constants import DEFAULT_INSTALL_METHOD, BackupSubfolder, InstallMethod
from .exceptions import InstallCollisionError
from .serialization import SerializableModel
from ..utils.file_inspect import contents_differ, file_mode_differs, tree_files
from ..utils.file_ops import (
    copy_file,
    copy_permissions,
    create_symlink,
    ensure_dir,
    prune_empty_parents,
    remove,
)
from .sync_ops import backup_file_or_dir_external
from ..utils.path_utils import (
    compute_relative_symlink_target,
    decode_dot_prefix,
    encode_dot_prefix,
    is_relative_to,
    resolve_target_path,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Action Enums & Context Models
# =============================================================================

class ActionType(str, Enum):
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
    REMOVE_DEPLOYED = "REMOVE_DEPLOYED"    # Deployed file/symlink removed from host

    # Workflow Notifications
    INFO_MESSAGE = "INFO_MESSAGE"          # Informational message logged during execution


@dataclass
class PlannedFileAction(SerializableModel):
    """Declarative specification of a single file/directory operation on the host system."""
    action_type: ActionType
    rel_path: Path
    system_target: Path
    reason: Optional[str] = None
    source_path: Optional[Path] = None


@dataclass(frozen=True)
class ActionExecutionContext:
    """Encapsulates host filesystem paths and permissions required to execute file actions."""
    target_dir: Path
    install_pkg_dir: Path
    backup_pkg_dir: Path
    sudo: bool = False
    resolve_symlinks: bool = True
    backup_subfolder: BackupSubfolder = BackupSubfolder.OVERWRITTEN


# =============================================================================
# Centralized Action Formatting Helpers
# =============================================================================

def format_action_line(action: PlannedFileAction) -> str:
    """Formats a single PlannedFileAction into a clean terminal line."""
    reason_str = f" ({action.reason})" if action.reason else ""
    if action.action_type == ActionType.ENSURE_DIR:
        return f"    📁 [ENSURE_DIR]      {action.system_target}"
    elif action.action_type == ActionType.CREATE_SYMLINK:
        return f"    🔗 [CREATE_SYMLINK]  {action.rel_path} -> {action.system_target}"
    elif action.action_type == ActionType.CREATE_COPY:
        return f"    📄 [CREATE_COPY]     {action.rel_path} -> {action.system_target}{reason_str}"
    elif action.action_type == ActionType.UPDATE_COPY:
        return f"    📝 [UPDATE_COPY]     {action.rel_path} -> {action.system_target}{reason_str}"
    elif action.action_type == ActionType.UPDATE_PERMISSION:
        return f"    🔒 [UPDATE_PERMISSION] {action.rel_path} -> {action.system_target}{reason_str}"
    elif action.action_type == ActionType.SKIP_IDENTICAL:
        return f"    ⏭️ [SKIP_IDENTICAL]  {action.rel_path} -> {action.system_target}{reason_str}"
    elif action.action_type == ActionType.BACKUP_OVERWRITE:
        return f"    🛡️ [BACKUP_OVERWRITE] {action.system_target}{reason_str}"
    elif action.action_type == ActionType.BACKUP_PRUNE:
        return f"    📦 [BACKUP_PRUNE]    {action.system_target}{reason_str}"
    elif action.action_type == ActionType.REMOVE_DEPLOYED:
        return f"    🗑️ [REMOVE_DEPLOYED] {action.system_target}{reason_str}"
    elif action.action_type == ActionType.INFO_MESSAGE:
        return f"    📢 [INFO]            {action.reason}"
    return f"    [{action.action_type}] {action.rel_path} -> {action.system_target}{reason_str}"


def format_action_summary(actions: Sequence[PlannedFileAction]) -> str:
    """Formats a concise summary string of action counts."""
    counts = []
    created = [a for a in actions if a.action_type in (ActionType.CREATE_SYMLINK, ActionType.CREATE_COPY)]
    if created:
        counts.append(f"{len(created)} to create")
    updated = [a for a in actions if a.action_type == ActionType.UPDATE_COPY]
    if updated:
        counts.append(f"{len(updated)} to update")
    permissions = [a for a in actions if a.action_type == ActionType.UPDATE_PERMISSION]
    if permissions:
        counts.append(f"{len(permissions)} permissions to update")
    skipped = [a for a in actions if a.action_type == ActionType.SKIP_IDENTICAL]
    if skipped:
        counts.append(f"{len(skipped)} up-to-date")
    removed = [a for a in actions if a.action_type == ActionType.REMOVE_DEPLOYED]
    if removed:
        counts.append(f"{len(removed)} to remove")
    backups = [a for a in actions if a.action_type in (ActionType.BACKUP_OVERWRITE, ActionType.BACKUP_PRUNE)]
    if backups:
        counts.append(f"{len(backups)} to backup")
    ensured = [a for a in actions if a.action_type == ActionType.ENSURE_DIR]
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


def _expand_path_ancestors(rel: Path) -> Set[Path]:
    target_rel = encode_dot_prefix(rel)
    return {p for p in target_rel.parents if p != Path(".")}


def _inspect_single_ancestor(
    ancestor_target: Path,
    rel_path: Path,
    abs_drift_root: Path,
) -> List[PlannedFileAction]:
    """Inspects a single ancestor directory path and returns necessary directory creation or backup actions."""
    if not (ancestor_target.exists() or ancestor_target.is_symlink()):
        return [PlannedFileAction(action_type=ActionType.ENSURE_DIR, rel_path=rel_path, system_target=ancestor_target)]

    if ancestor_target.is_dir() and not ancestor_target.is_symlink():
        return []

    # Blocked by an internal symlink, foreign symlink, or physical file
    if ancestor_target.is_symlink():
        is_internal = False
        try:
            is_internal = is_relative_to(ancestor_target.resolve(), abs_drift_root)
        except Exception:
            is_internal = False
        reason = "Internal ancestor symlink conflict" if is_internal else "Symlink blocking directory"
    else:
        reason = "File blocking directory"

    return [
        PlannedFileAction(action_type=ActionType.BACKUP_OVERWRITE, rel_path=rel_path, system_target=ancestor_target, reason=reason),
        PlannedFileAction(action_type=ActionType.ENSURE_DIR, rel_path=rel_path, system_target=ancestor_target),
    ]


def _check_has_backed_up_ancestor(target: Path, backed_up_targets: Set[Path]) -> bool:
    """Read-only check returning True if any parent directory of target has been backed up."""
    return any(parent in backed_up_targets for parent in target.parents)


def inspect_ancestor_directories(
    target_dir: Path,
    active_files: Iterable[Path],
    drift_root: Path,
    handled_targets: Set[Path],
    actions: List[PlannedFileAction],
) -> Set[Path]:
    """Inspects parent directories of active files from shallowest to deepest, planning necessary directory recreation or backups."""
    all_ancestors = {p for rel in active_files for p in _expand_path_ancestors(rel)}
    sorted_ancestors = sorted(all_ancestors, key=lambda p: len(p.parts))
    abs_drift_root = drift_root.resolve()
    backed_up_ancestor_targets: Set[Path] = set()

    for p in sorted_ancestors:
        ancestor_target = target_dir / p
        if ancestor_target in handled_targets:
            continue
        handled_targets.add(ancestor_target)
        rel_path = decode_dot_prefix(p)

        if _check_has_backed_up_ancestor(ancestor_target, backed_up_ancestor_targets):
            actions.append(PlannedFileAction(
                action_type=ActionType.ENSURE_DIR,
                rel_path=rel_path,
                system_target=ancestor_target,
            ))
            continue

        res = _inspect_single_ancestor(ancestor_target, rel_path, abs_drift_root)
        if any(a.action_type == ActionType.BACKUP_OVERWRITE for a in res):
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
    install_method: InstallMethod,
    rel_file: Path,
    system_target: Path,
    source_path: Optional[Path] = None,
    reason: Optional[str] = None,
) -> PlannedFileAction:
    """Returns CREATE_SYMLINK or CREATE_COPY depending on the package install method."""
    action_type = ActionType.CREATE_SYMLINK if install_method == InstallMethod.SYMLINK else ActionType.CREATE_COPY
    return PlannedFileAction(
        action_type=action_type,
        rel_path=rel_file,
        system_target=system_target,
        source_path=source_path,
        reason=reason,
    )


def _inspect_symlink_leaf(
    install_method: InstallMethod,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
    abs_drift_root: Path,
) -> List[PlannedFileAction]:
    """Inspects a host symlink at destination and plans overwrite, skip, or re-link actions."""
    if not system_target.exists():
        # Broken symlink
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Broken symlink collision",
            ),
            _plan_file_creation(install_method, rel_file, system_target, source_path=source_file),
        ]

    # Check if existing symlink already points to source_file
    if _check_symlink_points_to_source(system_target, source_file):
        if install_method == InstallMethod.SYMLINK:
            return [PlannedFileAction(
                action_type=ActionType.SKIP_IDENTICAL,
                rel_path=rel_file,
                system_target=system_target,
                source_path=source_file,
                reason="Symlink already points to source",
            )]
        # Switching from symlink to copy: backup the existing symlink and create physical copy
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Replacing symlink with copy",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_COPY,
                rel_path=rel_file,
                system_target=system_target,
                source_path=source_file,
            ),
        ]

    # Symlink points elsewhere (internal conflict vs external collision)
    points_into_drift = False
    try:
        points_into_drift = is_relative_to(system_target.resolve(), abs_drift_root)
    except Exception:
        points_into_drift = False

    reason = "Conflicting internal symlink" if points_into_drift else "Colliding external symlink"
    return [
        PlannedFileAction(
            action_type=ActionType.BACKUP_OVERWRITE,
            rel_path=rel_file,
            system_target=system_target,
            reason=reason,
        ),
        _plan_file_creation(install_method, rel_file, system_target, source_path=source_file),
    ]


def _inspect_physical_file_leaf(
    install_method: InstallMethod,
    rel_file: Path,
    system_target: Path,
    source_file: Path,
    is_first_time: bool,
) -> List[PlannedFileAction]:
    """Inspects a host regular physical file and plans overwrite, update, or skip actions."""
    if install_method == InstallMethod.SYMLINK:
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Physical file collides with symlink",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_SYMLINK,
                rel_path=rel_file,
                system_target=system_target,
                source_path=source_file,
            ),
        ]

    # COPY method
    if is_first_time:
        return [
            PlannedFileAction(
                action_type=ActionType.BACKUP_OVERWRITE,
                rel_path=rel_file,
                system_target=system_target,
                reason="Pre-existing file collision",
            ),
            PlannedFileAction(
                action_type=ActionType.CREATE_COPY,
                rel_path=rel_file,
                system_target=system_target,
                source_path=source_file,
            ),
        ]

    try:
        differs = contents_differ(source_file, system_target)
    except Exception:
        differs = True

    if differs:
        return [PlannedFileAction(
            action_type=ActionType.UPDATE_COPY,
            rel_path=rel_file,
            system_target=system_target,
            source_path=source_file,
            reason="File content updated",
        )]

    if file_mode_differs(source_file, system_target):
        src_mode = oct(source_file.stat().st_mode & 0o777)
        dst_mode = oct(system_target.stat().st_mode & 0o777)
        return [PlannedFileAction(
            action_type=ActionType.UPDATE_PERMISSION,
            rel_path=rel_file,
            system_target=system_target,
            source_path=source_file,
            reason=f"Permissions differ ({dst_mode} -> {src_mode})",
        )]

    return [PlannedFileAction(
        action_type=ActionType.SKIP_IDENTICAL,
        rel_path=rel_file,
        system_target=system_target,
        source_path=source_file,
        reason="File content matches",
    )]


def _inspect_leaf_file(
    target_dir: Path,
    source_dir: Path,
    install_method: InstallMethod,
    drift_root: Path,
    is_first_time: bool,
    rel_file: Path,
    handled_targets: Set[Path],
    actions: List[PlannedFileAction],
    backed_up_ancestor_targets: Set[Path],
) -> None:
    """Inspects a single package file against host filesystem state and plans appropriate actions."""
    rel_file = decode_dot_prefix(rel_file)
    system_target = resolve_target_path(rel_file, target_dir)
    source_file = source_dir / rel_file
    handled_targets.add(system_target)
    abs_drift_root = drift_root.resolve()

    if _check_has_backed_up_ancestor(system_target, backed_up_ancestor_targets):
        actions.append(_plan_file_creation(install_method, rel_file, system_target, source_path=source_file))
        return

    target_exists_or_symlink = system_target.exists() or system_target.is_symlink()

    if not target_exists_or_symlink:
        actions.append(_plan_file_creation(install_method, rel_file, system_target, source_path=source_file))
        return

    if system_target.is_dir() and not system_target.is_symlink():
        actions.append(PlannedFileAction(
            action_type=ActionType.BACKUP_OVERWRITE,
            rel_path=rel_file,
            system_target=system_target,
            reason="Directory blocking file",
        ))
        actions.append(_plan_file_creation(install_method, rel_file, system_target, source_path=source_file))
        return

    if system_target.is_symlink():
        actions.extend(_inspect_symlink_leaf(install_method, rel_file, system_target, source_file, abs_drift_root))
        return

    actions.extend(_inspect_physical_file_leaf(install_method, rel_file, system_target, source_file, is_first_time))


def _plan_orphan_prune(
    target_dir: Path,
    orphan_rel: Path,
    reason: str,
) -> List[PlannedFileAction]:
    """Inspects an orphaned path on host and plans BACKUP_PRUNE if present."""
    system_target = resolve_target_path(orphan_rel, target_dir)
    if not (system_target.exists() or system_target.is_symlink()):
        return []

    decoded_rel = decode_dot_prefix(orphan_rel)
    return [
        PlannedFileAction(
            action_type=ActionType.BACKUP_PRUNE,
            rel_path=decoded_rel,
            system_target=system_target,
            reason=reason,
        )
    ]


def _inspect_orphans(
    target_dir: Path,
    deployable_files: Iterable[Path],
    deployed_files: Iterable[Path],
) -> List[PlannedFileAction]:
    """Plans BACKUP_PRUNE actions for historical deployed files missing in candidate package."""
    orphaned_files = sorted(set(deployed_files) - set(deployable_files))
    actions: List[PlannedFileAction] = []
    for orphaned in orphaned_files:
        actions.extend(_plan_orphan_prune(target_dir, orphaned, f"Orphaned file '{orphaned}' prune"))
    return actions


# =============================================================================
# High-Level Planning Functions
# =============================================================================

def plan_folder_delivery(
    target_dir: Path,
    source_dir: Path,
    install_method: InstallMethod,
    drift_root: Path,
    active_files: Iterable[Path],
    deployed_files: Iterable[Path] = (),
    is_first_time: bool = False,
) -> List[PlannedFileAction]:
    """Plans declarative filesystem operations for delivering source_dir to target_dir."""
    assert_target_dir_outside_drift_root(target_dir, drift_root)
    actions: List[PlannedFileAction] = []
    handled_targets: Set[Path] = set()

    active_file_list = list(active_files)

    # 1. Orphan Files Reconciliation
    actions.extend(_inspect_orphans(target_dir, active_file_list, deployed_files))

    # 2. Intermediate Ancestor Directories Inspection
    backed_up_ancestor_targets = inspect_ancestor_directories(
        target_dir=target_dir,
        active_files=active_file_list,
        drift_root=drift_root,
        handled_targets=handled_targets,
        actions=actions,
    )

    # 3. Leaf Files Inspection
    for rel_file in active_file_list:
        _inspect_leaf_file(
            target_dir=target_dir,
            source_dir=source_dir,
            install_method=install_method,
            drift_root=drift_root,
            is_first_time=is_first_time,
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
) -> List[PlannedFileAction]:
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

    actions: List[PlannedFileAction] = [
        PlannedFileAction(
            action_type=ActionType.INFO_MESSAGE,
            rel_path=Path("."),
            system_target=target_dir,
            reason="Restoring overwritten backups to host...",
        )
    ]

    restore_actions = plan_folder_delivery(
        target_dir=target_dir,
        source_dir=backup_overwritten_dir,
        install_method=InstallMethod.COPY,
        drift_root=drift_root,
        active_files=backup_files,
        deployed_files=(),
        is_first_time=False,
    )
    actions.extend(restore_actions)
    return actions


def plan_symlink_conversions(
    deployed_files: Sequence[Path],
    target_dir: Path,
    install_pkg_dir: Path,
    drift_root: Path,
) -> List[PlannedFileAction]:
    """Plans declarative actions for detaching a symlinked package into concrete physical files."""
    actions: List[PlannedFileAction] = []
    handled_targets: Set[Path] = set()

    symlinks_to_convert: List[Path] = []
    for rel_file in deployed_files:
        system_target = resolve_target_path(rel_file, target_dir)
        if system_target.is_symlink():
            symlinks_to_convert.append(rel_file)

    if not symlinks_to_convert:
        return []

    # 1. Ensure ancestor directories exist
    inspect_ancestor_directories(
        target_dir=target_dir,
        active_files=symlinks_to_convert,
        drift_root=drift_root,
        handled_targets=handled_targets,
        actions=actions,
    )

    # 2. Plan removing symlinks and replacing with concrete file copies
    for rel_file in symlinks_to_convert:
        system_target = resolve_target_path(rel_file, target_dir)
        src_file = install_pkg_dir / rel_file
        if not src_file.is_file():
            logger.warning(f"Source file not found in install/ repository: {src_file}")
            continue

        actions.append(
            PlannedFileAction(
                action_type=ActionType.REMOVE_DEPLOYED,
                rel_path=rel_file,
                system_target=system_target,
                reason="Remove symlink for detach",
            )
        )
        actions.append(
            PlannedFileAction(
                action_type=ActionType.CREATE_COPY,
                rel_path=rel_file,
                system_target=system_target,
                source_path=src_file,
                reason="Replace symlink with copy (detach)",
            )
        )

    return actions


def plan_file_removals(
    deployed_files: Sequence[Path],
    target_dir: Path,
) -> List[PlannedFileAction]:
    """Inspects deployed files on the host and plans removal in reverse order."""
    resolved = [
        (rel, resolve_target_path(rel, target_dir))
        for rel in sorted(deployed_files, reverse=True)
    ]
    return [
        PlannedFileAction(
            action_type=ActionType.REMOVE_DEPLOYED,
            rel_path=rel,
            system_target=target,
            reason="Remove deployed file from host",
        )
        for rel, target in resolved
        if target.exists() or target.is_symlink()
    ]


# =============================================================================
# Unified Plan Execution Engine
# =============================================================================

def backup_host_item(
    backup_pkg_dir: Path,
    system_target: Path,
    subfolder: BackupSubfolder,
    rel_path: Path,
    sudo: bool = False,
    reason: Optional[str] = None,
    resolve_symlinks: bool = True,
) -> None:
    """Safely backs up a host file, symlink, or directory to the package backup directory."""
    subfolder_str = subfolder.value if isinstance(subfolder, BackupSubfolder) else str(subfolder)
    backup_rel_path = decode_dot_prefix(rel_path)
    backup_path = backup_pkg_dir / subfolder_str / backup_rel_path
    if reason:
        logger.warning(f"🛡️  [BACKUP] {reason} at '{system_target}'")
    logger.debug(f"   Backing up to: {backup_path}")
    backup_file_or_dir_external(system_target, backup_path, sudo, resolve_symlinks=resolve_symlinks)


def execute_single_action(
    context: ActionExecutionContext,
    action: PlannedFileAction,
) -> None:
    """Executes a single planned file/directory delivery action on the host system."""
    if action.action_type == ActionType.BACKUP_OVERWRITE:
        backup_host_item(
            backup_pkg_dir=context.backup_pkg_dir,
            system_target=action.system_target,
            subfolder=context.backup_subfolder,
            rel_path=action.rel_path,
            sudo=context.sudo,
            reason=action.reason,
            resolve_symlinks=context.resolve_symlinks,
        )
        remove(action.system_target, context.sudo)

    elif action.action_type == ActionType.BACKUP_PRUNE:
        backup_host_item(
            backup_pkg_dir=context.backup_pkg_dir,
            system_target=action.system_target,
            subfolder=BackupSubfolder.DELETED_FILES,
            rel_path=action.rel_path,
            sudo=context.sudo,
            reason=action.reason,
            resolve_symlinks=context.resolve_symlinks,
        )
        remove(action.system_target, context.sudo)

    elif action.action_type == ActionType.ENSURE_DIR:
        if action.system_target.is_symlink() or (action.system_target.exists() and not action.system_target.is_dir()):
            raise NotADirectoryError(
                f"Cannot ensure directory '{action.system_target}': path exists and is not a directory."
            )
        ensure_dir(action.system_target, context.sudo)

    elif action.action_type == ActionType.CREATE_SYMLINK:
        source_file = action.source_path if action.source_path else (context.install_pkg_dir / action.rel_path)
        relative_target = compute_relative_symlink_target(source_file, action.system_target.parent)
        create_symlink(relative_target, action.system_target, context.sudo)

    elif action.action_type in (ActionType.CREATE_COPY, ActionType.UPDATE_COPY):
        source_file = action.source_path if action.source_path else (context.install_pkg_dir / action.rel_path)
        copy_file(source_file, action.system_target, context.sudo)

    elif action.action_type == ActionType.UPDATE_PERMISSION:
        source_file = action.source_path if action.source_path else (context.install_pkg_dir / action.rel_path)
        copy_permissions(source_file, action.system_target, sudo=context.sudo)

    elif action.action_type == ActionType.REMOVE_DEPLOYED:
        remove(action.system_target, context.sudo)
        try:
            limit_dir = action.system_target.parents[len(action.rel_path.parts) - 1]
        except IndexError:
            limit_dir = context.target_dir
        prune_empty_parents(action.system_target.parent, limit_dir)

    elif action.action_type == ActionType.SKIP_IDENTICAL:
        logger.debug(f"   Skipping '{action.system_target}': already up-to-date")

    elif action.action_type == ActionType.INFO_MESSAGE:
        if action.reason:
            logger.info(action.reason)


def execute_delivery_actions(
    context: ActionExecutionContext,
    actions: Sequence[PlannedFileAction],
) -> None:
    """Deterministically executes a sequence of planned delivery actions on the host system."""
    for action in actions:
        execute_single_action(context, action)


def __getattr__(name: str):
    if name in ("PackageInstallPlan", "PackageUninstallPlan"):
        from .result_models import PackageInstallPlan, PackageUninstallPlan
        return PackageInstallPlan if name == "PackageInstallPlan" else PackageUninstallPlan
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
