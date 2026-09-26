"""Primitive 5: Install Deployment (install/ -> active host system) & Primitive 6: Commit Install Repo.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Public Primitive Entry Points
    run_primitive_5_install_deployment(workspace_config, packages_to_redeploy, options)
        1. Discover & Inspect Packages:
            load_state_registry
            workspace_config.filter_install_packages_by_target
            PackageConfig.from_install_dir
        2. Pre-flight Validation & Pre-Transaction Conflict Audit:
            assert_packages_deployment_ready [Layer 4]
                assert_can_escalate (if any target requires sudo)
                assert_hooks_exist (lifecycle hooks)
                assert_no_cross_package_conflicts [Layer 4]
        3. Execute Single-Package Deployments:
            deploy_one_package_with_error_wrapping [Layer 4]
                deploy_one_package [Layer 4]
                    assert_one_package_deployment_ready [Layer 4] (midway transaction, drift root collisions, writable checks)
                    state_registry.set_package_state("installing") & save
                    pkg_config.package_envs context
                    deploy_one_package_impl [Layer 4]
                        Target Directory Migration Detection -> cleanup old target & force redeploy
                        run_collision_guard [Layer 3]
                        ignore_handler.create_stow_ignore_file (if stow method)
                        reconcile_orphaned_files [Layer 2] (if redeploy)
                        trigger pre_install / pre_update hook
                        state_registry.sync_deployed_files & save
                        Physical Delivery:
                            run_full_file_delivery [Layer 3] (redeploy)
                                run_full_copy_deployment [Layer 3] / run_stow_deployment [Layer 3]
                            run_incremental_file_delivery [Layer 3] (incremental redeploy)
                                deploy_single_stow_file [Layer 2] / deploy_single_copy_file [Layer 2]
                                delete_single_system_file_or_dir [Layer 1]
                        trigger post_install / post_update hook
                        update_state_registry_post_deployment [Layer 3]
                            state_registry.set_package_state("installed") & save
        4. Return Aggregated InstallDeploymentResult

    run_primitive_6_commit_install_repo(workspace_config, commit_message, target_pkgs)
        commit_repo_changes (commits state repository changes in install/)

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Version Probing & Atomic Host Actions
        get_stow_version
        is_stow_version_sufficient
        handle_collision_error
        delete_single_system_file_or_dir
    Layer 2: Single-File Delivery & Conflict Resolution Helpers
        find_internal_symlink_conflicts
        resolve_single_internal_symlink_conflict
        handle_internal_symlink_conflicts
        deploy_single_stow_file
        deploy_single_copy_file
        reconcile_orphaned_files
    Layer 3: Batch Delivery & Collision Audit
        run_collision_guard
        run_full_copy_deployment
        run_stow_deployment
        run_full_file_delivery
        run_incremental_file_delivery
        update_state_registry_post_deployment
    Layer 4: Single-Package Pipeline & Pre-flight Validation
        _gather_package_destination_targets
        assert_no_cross_package_conflicts
        assert_one_package_deployment_ready
        deploy_one_package_impl
        deploy_one_package
        deploy_one_package_with_error_wrapping
        assert_packages_deployment_ready
    Layer 5: Public Primitive Entry Points
        run_primitive_5_install_deployment
        run_primitive_6_commit_install_repo
===============================================================================
"""

import os
import sys
import re
import logging
import subprocess
import datetime
import shlex
import collections
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set, Sequence, Mapping, Iterable

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..core.constants import (
    LineEnding,
    InstallMethod,
    BackupSubfolder,
)
from ..core.exceptions import InstallCollisionError, HookExecutionError, mark_logged
from ..core.ignore import DriftIgnore
from ..hooks.lifecycle_hooks import HookExecFlags
from ..core.state_registry import load_state_registry, StateRegistry
from ..core.folder_diff import compare_folders, list_folder_paths
from .stage_repo import PackageStageChanges
from ..utils.path_utils import (
    resolve_target_path,
    encode_dot_prefix,
    decode_dot_prefix,
    relative_path_between,
    is_relative_to,
)
from ..utils.file_inspect import find_symlink_ancestor
from ..utils.file_ops import (
    copy_file,
    create_symlink,
    assert_writable,
    ensure_dir,
    remove,
)
from ..utils.process_utils import run_command
from ..core.sync_ops import backup_file_or_dir_external
from ..core.result_models import FileOperations, PackageInstallResult, InstallDeploymentResult

logger = logging.getLogger(__name__)


# =============================================================================
# Deployment Data Structures
# =============================================================================

@dataclass
class DeployOptions:
    """Options controlling package deployment behavior."""
    resolve_symlinks: bool = True
    force: bool = False
    redeploy: bool = True
    package_changes: Optional[Mapping[str, PackageStageChanges]] = None
    flags: Optional[HookExecFlags] = None

    def get_package_changes(self, pkg: str) -> Optional[PackageStageChanges]:
        """Retrieves stage changes for a specific package name from mapping."""
        if self.package_changes is not None:
            return self.package_changes.get(pkg)
        return None


@dataclass(frozen=True)
class PackageInstallContext:
    """Encapsulates resolved package metadata and filesystem paths for installation operations."""
    pkg_name: str
    install_pkg_dir: Path
    backup_pkg_dir: Path
    target_dir: Path
    install_method: InstallMethod
    ignore_handler: DriftIgnore
    sudo: bool
    is_first_time: bool
    drift_root: Path

    @classmethod
    def from_package(
        cls,
        workspace_config: WorkspaceConfig,
        state_registry: StateRegistry,
        metadata: PackageConfig,
    ) -> "PackageInstallContext":
        pkg = metadata.name
        target_dir = metadata.get_target_directory(workspace_config)
        install_pkg_dir = workspace_config.install_path / pkg
        backup_pkg_dir = workspace_config.backup_path / pkg
        pkg_state = state_registry.packages.get(pkg)
        is_first_time = (pkg_state is None or pkg_state.last_deployed is None)
        ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
        install_method = metadata.get_install_method(workspace_config)
        return cls(
            pkg_name=pkg,
            install_pkg_dir=install_pkg_dir,
            backup_pkg_dir=backup_pkg_dir,
            target_dir=target_dir,
            install_method=install_method,
            ignore_handler=ignore_handler,
            sudo=metadata.package.sudo,
            is_first_time=is_first_time,
            drift_root=workspace_config.drift_root,
        )


# =============================================================================
# Layer 1: Version Probing & Atomic Host Actions
# =============================================================================

def get_stow_version() -> Optional[str]:
    """Retrieves the installed GNU Stow version string if available."""
    try:
        res = run_command(["stow", "--version"], text=True)
        stdout_str = res.stdout if isinstance(res.stdout, str) else res.stdout.decode("utf-8", errors="replace")
        lines = stdout_str.splitlines()
        if not lines:
            return None
        first_line = lines[0]
        match = re.search(r"(\d+(\.\d+)+)", first_line)
        if match:
            return match.group(1)
        return first_line.strip() or None
    except Exception as e:
        logger.debug(f"GNU Stow is not found or failed to return version: {e}")
        return None


def is_stow_version_sufficient(version: str) -> bool:
    """Checks if the stow version is >= 2.4.1."""
    try:
        parts = [int(p) for p in version.split(".")]
        return parts >= [2, 4, 1]
    except Exception:
        return False


def handle_collision_error(
    context: PackageInstallContext,
    system_target: Path,
    backup_subfolder: BackupSubfolder,
    backup_rel_path: Path,
    reason: str,
    resolve_symlinks: bool,
    ops: Optional[FileOperations] = None,
) -> None:
    """Helper to backup and report a collision/error at a system target path."""
    subfolder_str = backup_subfolder.value if isinstance(backup_subfolder, BackupSubfolder) else str(backup_subfolder)
    backup_path = context.backup_pkg_dir / subfolder_str / backup_rel_path
    logger.warning(f"🛡️  [COLLISION] {reason} at '{system_target}'")
    logger.debug(f"   Backing up to: {backup_path}")
    backup_file_or_dir_external(system_target, backup_path, context.sudo, resolve_symlinks=resolve_symlinks)
    # After backup, remove the colliding item to clear the way
    remove(system_target, context.sudo)
    if ops is not None:
        if backup_subfolder == BackupSubfolder.DELETED_FILES:
            ops.deleted_backup.append(str(backup_rel_path))
        else:
            ops.overwritten_backup.append(str(backup_rel_path))


def delete_single_system_file_or_dir(
    rel_file: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to delete a single file on host system (for incremental deletions)."""
    system_target = resolve_target_path(rel_file, target_dir)
    remove(system_target, sudo)


# =============================================================================
# Layer 2: Single-File Delivery & Conflict Resolution Helpers
# =============================================================================

def _is_valid_stow_link(system_target: Path, abs_install_pkg: Path) -> bool:
    """Checks if system_target is a relative symlink pointing into abs_install_pkg."""
    try:
        # stow command can only handle relative paths,
        # so only relative links pointing to the file in the same install_pkg_dir are valid stow links.
        # And we restrict the result to not contain linked dir as parent dir.
        # so linked dirs are considered invalid, and trigger backup and removal.
        raw_link = os.readlink(system_target)
        link_content = Path(raw_link)
        if link_content.is_absolute() or system_target.is_dir():
            return False
        # Canonicalize the link target to check if it points inside the same install_pkg_dir
        link_target = (system_target.parent / link_content).resolve()
        return is_relative_to(link_target, abs_install_pkg)
    except Exception:
        return False


def find_internal_symlink_conflicts(
    context: PackageInstallContext,
) -> List[Tuple[Path, Path]]:
    """Detects symlinks in target_dir pointing into drift_root that conflict with install_pkg_dir files.

    Instead of recursively scanning the entire target_dir (which could be the whole HOME directory),
    we inspect only the specific target paths and ancestor directories covered by install_pkg_dir.
    Returns a list of (install_rel_path, system_target) tuples sorted by path depth.
    """
    if not (context.install_pkg_dir.exists() and context.install_pkg_dir.is_dir()):
        return []

    # 1. Collect all deployable relative paths inside the package
    pkg_items = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)

    def _expand_path_ancestors(rel: Path) -> Set[Path]:
        target_rel = encode_dot_prefix(rel)
        return {target_rel, *target_rel.parents} - {Path(".")}

    # 2. Build the set of target relative paths and all intermediate parent directories
    target_candidates = {p for install_rel in pkg_items for p in _expand_path_ancestors(install_rel)}
    
    # 3. Sort candidates from shallowest to deepest so parents are checked before children
    sorted_candidates = sorted(target_candidates, key=lambda p: len(p.parts))
    abs_drift_root = context.drift_root.resolve()

    def _is_internal_drift_link(system_target: Path) -> bool:
        if not system_target.is_symlink():
            return False
        try:
            return is_relative_to(system_target.resolve(), abs_drift_root)
        except Exception:
            return False

    return [
        (decode_dot_prefix(t_rel), context.target_dir / t_rel)
        for t_rel in sorted_candidates
        if _is_internal_drift_link(context.target_dir / t_rel)
    ]


def resolve_single_internal_symlink_conflict(
    context: PackageInstallContext,
    install_rel_path: Path,
    system_target: Path,
    resolve_symlinks: bool,
    processed_paths: set,
    ops: Optional[FileOperations] = None,
) -> None:
    """Processes a single detected internal symlink conflict: validates stow compatibility,
    marks paths as processed, backs up & removes conflicting symlinks, and recreates physical directories.
    """
    install_file_path = context.install_pkg_dir / install_rel_path
    abs_install_pkg = context.install_pkg_dir.resolve()

    # If install method is stow and link points into our pkg install dir, it's valid for this package
    if context.install_method == InstallMethod.STOW and _is_valid_stow_link(system_target, abs_install_pkg):
        return

    # Otherwise, it is a link conflict and must be backed up & removed
    processed_paths.add(install_rel_path)

    # Add all children of this install_rel_path to processed_paths to avoid double handling
    if install_file_path.is_dir():
        for child_rel in list_folder_paths(
            src_dir=install_file_path,
            base_rel=install_rel_path,
            ignore_handler=context.ignore_handler,
            resolve_symlinks=resolve_symlinks,
            translate_mode="forward"
        ):
            processed_paths.add(child_rel)

    handle_collision_error(
        context=context,
        system_target=system_target,
        backup_subfolder=BackupSubfolder.OVERWRITTEN,
        backup_rel_path=install_rel_path,
        reason="Internal symlink detected",
        resolve_symlinks=resolve_symlinks,
        ops=ops,
    )

    # If repo expects a directory here, recreate it as physical to avoid cycles
    if install_file_path.is_dir() and not install_file_path.is_symlink():
        ensure_dir(system_target, context.sudo)


def handle_internal_symlink_conflicts(
    context: PackageInstallContext,
    resolve_symlinks: bool,
    processed_paths: set,
    ops: Optional[FileOperations] = None,
) -> None:
    """Detects and backs up symlinks in target_dir pointing into drift_root that conflict with install_pkg_dir files."""
    links_detected = find_internal_symlink_conflicts(context=context)

    for install_rel_path, system_target in links_detected:
        resolve_single_internal_symlink_conflict(
            context=context,
            install_rel_path=install_rel_path,
            system_target=system_target,
            resolve_symlinks=resolve_symlinks,
            processed_paths=processed_paths,
            ops=ops,
        )

    # Check if the canonical path of target_dir still points inside drift_root after resolving conflicts
    abs_drift_root = context.drift_root.resolve()
    resolved_target = context.target_dir.resolve()
    if resolved_target == abs_drift_root or is_relative_to(resolved_target, abs_drift_root):
        raise InstallCollisionError(
            f"Safety Abort: Target directory '{context.target_dir}' (resolved to '{resolved_target}') "
            f"points inside drift workspace root '{context.drift_root}'. "
            f"Resolving this automatically is unsafe. Please resolve manually."
        )


def deploy_single_stow_file(
    rel_file: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Stow method."""
    src_file = install_pkg_dir / rel_file
    system_target = resolve_target_path(rel_file, target_dir)
    relative_target = relative_path_between(system_target.parent, src_file)

    # If the target already points into the source, do not create the symlink again
    if system_target.is_symlink():
        try:
            link_target_raw = Path(os.readlink(system_target))
            if (link_target_raw == relative_target
                    or (system_target.parent / link_target_raw).resolve() == src_file.resolve()):
                logger.debug(f"   Skipping symlink creation for '{system_target}' as it already points to '{relative_target}'")
                return
        except OSError:
            pass

    create_symlink(relative_target, system_target, sudo)


def deploy_single_copy_file(
    rel_file: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Copy method."""
    src_file = install_pkg_dir / rel_file
    system_target = resolve_target_path(rel_file, target_dir)
    copy_file(
        src_file,
        system_target,
        sudo,
        line_ending=(LineEnding.CRLF if sys.platform == "win32" else LineEnding.PRESERVE)
    )


def reconcile_orphaned_files(
    context: PackageInstallContext,
    deployable_files: Iterable[Path],
    deployed_files: Iterable[Path],
    resolve_symlinks: bool,
    ops: Optional[FileOperations] = None,
) -> None:
    """Reconciles historical deployment files to prune orphaned files from active system target."""
    orphaned_files = set(deployed_files) - set(deployable_files)
    if not orphaned_files:
        return
    logger.info(f"🔍 Reconciling desired state: Pruning {len(orphaned_files)} orphaned files")
    for orphaned in sorted(orphaned_files):
        system_target = resolve_target_path(orphaned, context.target_dir)
        if system_target.exists() or system_target.is_symlink():
            handle_collision_error(
                context=context,
                system_target=system_target,
                backup_subfolder=BackupSubfolder.DELETED_FILES,
                backup_rel_path=orphaned,
                reason=f"Orphaned file '{orphaned}' prune",
                resolve_symlinks=resolve_symlinks,
                ops=ops,
            )


# =============================================================================
# Layer 3: Batch Delivery & Collision Audit
# =============================================================================

def run_collision_guard(
    context: PackageInstallContext,
    resolve_symlinks: bool,
    ops: Optional[FileOperations] = None,
) -> None:
    """Handles collision backing up before any file deployment using FolderDiff."""
    # 0. Safety Abort Check for parents ABOVE or AT target_dir
    # This detects if our target base itself is a symlink into drift_root
    parent_symlink = find_symlink_ancestor(context.target_dir, context.drift_root)
    if parent_symlink:
        raise InstallCollisionError(
            f"Safety Abort: Parent directory '{parent_symlink}' (resolved to '{parent_symlink.resolve()}') "
            f"is a symlink pointing into drift workspace root '{context.drift_root}', "
            f"but lies outside the package target directory '{context.target_dir}'. "
            f"Resolving this automatically is unsafe. Please resolve manually."
        )

    processed_paths: Set[Path] = set()

    # 1. Check and resolve internal symlink conflicts inside target_dir
    handle_internal_symlink_conflicts(
        context=context,
        resolve_symlinks=resolve_symlinks,
        processed_paths=processed_paths,
        ops=ops,
    )

    # 2. Recursive Audit using FolderDiff
    diff = compare_folders(
        src_dir=context.install_pkg_dir,
        dst_dir=context.target_dir,
        ignore_handler=context.ignore_handler,
        resolve_symlinks=resolve_symlinks,
        translate_mode="forward",
        src_only=True,
    )

    # 3. Handle Deleted items (type mismatches where system files block repo dirs)
    for rel in diff.deleted:
        if rel in processed_paths:
            continue
        processed_paths.add(rel)

        system_target = resolve_target_path(rel, context.target_dir)
        if context.ignore_handler.match_path(rel):
            # Clean up now-ignored files
            handle_collision_error(
                context=context,
                system_target=system_target,
                backup_subfolder=BackupSubfolder.DELETED_FILES,
                backup_rel_path=rel,
                reason="Ignored file cleanup",
                resolve_symlinks=resolve_symlinks,
                ops=ops,
            )
        else:
            # Type mismatch (e.g. System has file, Repo has dir)
            handle_collision_error(
                context=context,
                system_target=system_target,
                backup_subfolder=BackupSubfolder.OVERWRITTEN,
                backup_rel_path=rel,
                reason="Type mismatch collision",
                resolve_symlinks=resolve_symlinks,
                ops=ops,
            )

    # 4. Handle Modified items (collisions that need overwrite)
    for rel in diff.modified:
        if rel in processed_paths:
            continue
        processed_paths.add(rel)

        system_target = resolve_target_path(rel, context.target_dir)

        # If the file is modified, then it cannot pointing to the same file.
        # If the symlink points to anywhere inside install_pkg_dir but not the same pkg_install_dir,
        # it is handled earlier in internal symlink conflicts.
        # So if it's symlink:
        #   1. it is a broken symlink
        #   2. it is a symlink pointing outside install_pkg_dir 
        #   3. it is pointing inside the same pkg_install_dir, but not the same file.
        # We can skip if the system target is a symlink pointing to another file in same install_pkg_dir.
        # If it is not a symlink or a broken link, we need to backup and remove it, because it is a collision.
        if (context.install_method == InstallMethod.STOW
                and system_target.is_symlink() and system_target.exists()
                and is_relative_to(system_target.resolve(), context.install_pkg_dir.resolve())):
            continue

        # Copy mode check: skip backup if the system target is not a symlink and it's not the first time installation (i.e., it's an update).
        if (context.install_method == InstallMethod.COPY
                and not system_target.is_symlink() and not context.is_first_time):
            continue

        # conditions include:
        # stow mode: system target file is not a symlink, or is broken link, or pointing outside install_pkg_dir
        # copy mode: first installation, or system target is a symlink (broken or not)
        handle_collision_error(
            context=context,
            system_target=system_target,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            backup_rel_path=rel,
            reason="Deployment collision",
            resolve_symlinks=resolve_symlinks,
            ops=ops,
        )

    # 5. Handle Content Match items (Stow specific: physical file matching repo content is STILL a collision)
    if context.install_method == InstallMethod.STOW:
        for rel in diff.matches:
            if rel in processed_paths:
                continue
            processed_paths.add(rel)

            system_target = resolve_target_path(rel, context.target_dir)
            if not system_target.is_symlink():
                handle_collision_error(
                    context=context,
                    system_target=system_target,
                    backup_subfolder=BackupSubfolder.OVERWRITTEN,
                    backup_rel_path=rel,
                    reason="Stow physical collision",
                    resolve_symlinks=resolve_symlinks,
                    ops=ops,
                )


def run_full_copy_deployment(
    src_pkg_dir: Path,
    target_dir: Path,
    sudo: bool,
    deployable_files: List[Path]
) -> None:
    """Executes copy deployment of deployable_files to target_dir."""
    ensure_dir(target_dir, sudo)
    pkg = src_pkg_dir.name
    logger.info(f"🚚 Syncing files: {pkg} (copy)")

    for rel_file in deployable_files:
        deploy_single_copy_file(rel_file, src_pkg_dir, target_dir, sudo)


def run_stow_deployment(install_base: Path, target_dir: Path, pkg: str, sudo: bool) -> None:
    """Invokes GNU Stow for package deployment."""
    ensure_dir(target_dir, sudo)
    stow_cmd = [
        "stow",
        "--no-folding",
        "--dotfiles",
        "-d", str(install_base),
        "-t", str(target_dir),
        pkg
    ]
    logger.info(f"🔗 Linking files: {pkg} (stow)")
    logger.debug(f"   Command: {shlex.join(stow_cmd)}")
    run_command(stow_cmd, sudo=sudo, cwd=str(install_base))


def run_full_file_delivery(
    context: PackageInstallContext,
    deployable_files: List[Path]
) -> None:
    """Handles full file delivery during initial or clean redeployment."""
    install_base = context.install_pkg_dir.parent
    if context.install_method == InstallMethod.COPY:
        run_full_copy_deployment(
            context.install_pkg_dir, context.target_dir, context.sudo,
            deployable_files=deployable_files
        )
        return
    if context.install_method == InstallMethod.STOW:
        stow_version = get_stow_version()
        stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False
        if stow_sufficient:
            run_stow_deployment(install_base, context.target_dir, context.pkg_name, context.sudo)
            return
        logger.warning("GNU Stow version is insufficient (< 2.4.1) or not installed. Falling back to manual symlinking.")
        for rel_file in deployable_files:
            deploy_single_stow_file(
                rel_file=rel_file,
                install_pkg_dir=context.install_pkg_dir,
                target_dir=context.target_dir,
                sudo=context.sudo
            )


def run_incremental_file_delivery(
    context: PackageInstallContext,
    package_changes: PackageStageChanges,
) -> None:
    """Handles incremental deployment applying Stage Changes additions, modifications, and deletions."""
    # A. Process Deletions on active host system
    for rel_file in package_changes.deployable_changes.deleted:
        delete_single_system_file_or_dir(rel_file, context.target_dir, context.sudo)

    # B. Process Additions and Modifications
    for rel_file in package_changes.deployable_changes.added + package_changes.deployable_changes.modified:
        if not context.install_pkg_dir.joinpath(rel_file).exists():
            logger.warning(f"⚠️  [BUG] Staged file '{rel_file}' does not exist in install package directory.")
            continue

        if context.install_pkg_dir.joinpath(rel_file).is_symlink():
            logger.warning(
                f"⚠️  [BUG] Staged file '{rel_file}' is a symlink in install package directory, which is not allowed."
            )
            continue

        if rel_file.is_dir():
            ensure_dir(
                resolve_target_path(rel_file, context.target_dir), context.sudo
            )
            continue

        if context.install_method == InstallMethod.STOW:
            deploy_single_stow_file(
                rel_file=rel_file,
                install_pkg_dir=context.install_pkg_dir,
                target_dir=context.target_dir,
                sudo=context.sudo
            )
        elif context.install_method == InstallMethod.COPY:
            deploy_single_copy_file(
                rel_file=rel_file,
                install_pkg_dir=context.install_pkg_dir,
                target_dir=context.target_dir,
                sudo=context.sudo
            )


def update_state_registry_post_deployment(
    state_registry: StateRegistry,
    pkg: str,
    target_directory: Path,
    install_method: InstallMethod,
    redeploy: bool,
    deployable_files: Iterable[Path] = (),
    package_changes: Optional[PackageStageChanges] = None,
) -> None:
    """Updates and persists the package deployment state, target directory, install method, and deployed files manifest in state.toml."""
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(
        pkg,
        "installed",
        last_deployed=now_str,
    )
    state_registry.sync_deployed_files(
        pkg=pkg,
        target_directory=target_directory,
        install_method=install_method,
        redeploy=redeploy,
        deployable_files=deployable_files,
        package_changes=package_changes,
    )
    state_registry.save()


# =============================================================================
# Layer 4: Single-Package Pipeline & Pre-flight Validation
# =============================================================================

def _gather_package_destination_targets(
    workspace_config: WorkspaceConfig,
    pkg: str,
    metadata: PackageConfig,
) -> List[Tuple[Path, Path]]:
    """Gathers (relative_source_file, absolute_host_target) for all deployable files in a package."""
    if not metadata.package.enable_install:
        return []
    install_pkg_dir = workspace_config.install_path / pkg
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
    deployable_files = ignore_handler.filter_deployable_files(install_pkg_dir)
    target_dir = metadata.get_target_directory(workspace_config)

    return [
        (rel_file, resolve_target_path(rel_file, target_dir))
        for rel_file in deployable_files
    ]


def assert_no_cross_package_conflicts(
    workspace_config: WorkspaceConfig,
    discovered_packages: Iterable[str],
    pkg_metadata_map: Mapping[str, PackageConfig],
    state_registry: StateRegistry,
) -> None:
    """Audits destination paths for cross-package collisions before executing deployments.

    Validates:
    1. Intra-batch conflicts: Two or more packages in current deployment batch claiming identical host paths.
    2. Inter-package conflicts: A package in current batch claiming a host path already owned
       by a different installed package recorded in state.toml (outside the current batch).

    Collects all conflicting destination targets across the workspace and reports them together.

    Raises:
        InstallCollisionError: When one or more cross-package collisions are detected.
    """
    discovered_set = set(discovered_packages)

    # 1. Gather destination claims from current batch packages
    batch_claims: List[Tuple[Path, Tuple[str, str]]] = [
        (dst_path, (pkg, "batch"))
        for pkg in discovered_set
        if (metadata := pkg_metadata_map.get(pkg)) and metadata.package.enable_install
        for _, dst_path in _gather_package_destination_targets(workspace_config, pkg, metadata)
    ]

    # 2. Gather destination claims from installed packages outside the current batch
    external_installed_ownership = state_registry.build_destination_ownership_map(
        exclude_packages=discovered_set
    )
    installed_claims: List[Tuple[Path, Tuple[str, str]]] = [
        (dst_path, (owner, "installed"))
        for dst_path, owner in external_installed_ownership.items()
    ]

    # 3. Group and aggregate claims by destination path
    claims_by_path: Dict[Path, List[Tuple[str, str]]] = collections.defaultdict(list)
    for dst_path, claim in itertools.chain(batch_claims, installed_claims):
        claims_by_path[dst_path].append(claim)

    # 4. Filter for paths with multiple competing claims involving the current batch
    conflicts = {
        dst: claims
        for dst, claims in claims_by_path.items()
        if len(claims) > 1 and any(src == "batch" for _, src in claims)
    }

    if not conflicts:
        return

    # 5. Format comprehensive diagnostic report for all collisions
    conflict_lines = [
        f"❌ Cross-package destination conflicts detected ({len(conflicts)} collision(s)):",
    ]
    for dst in sorted(conflicts.keys(), key=lambda p: str(p)):
        claims = conflicts[dst]
        batch_claimants = sorted(set(pkg for pkg, src in claims if src == "batch"))
        installed_owners = sorted(set(pkg for pkg, src in claims if src == "installed"))

        if len(batch_claimants) > 1:
            conflict_lines.append(
                f"  • '{dst}': Intra-batch collision between packages {batch_claimants}"
            )
        elif installed_owners:
            conflict_lines.append(
                f"  • '{dst}': Package '{batch_claimants[0]}' (current batch) collides with '{installed_owners[0]}' (already installed)"
            )

    raise InstallCollisionError("\n".join(conflict_lines))


def assert_one_package_deployment_ready(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    options: DeployOptions,
) -> Optional[PackageInstallResult]:
    """Runs pre-flight validation for a single package.

    Returns:
        A PackageInstallResult if the package should be skipped, or None if validation passed.
    Raises:
        InstallCollisionError: If the target directory is within drift root.
        RuntimeError: If the package is in a midway failed state and force is False.
    """
    pkg = metadata.name
    if not metadata.package.enable_install:
        logger.info(f"Skipping package '{pkg}' during deployment (enable_install is False).")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(metadata.get_target_directory(workspace_config)),
            status="SKIPPED",
            error="enable_install is False"
        )
        
    target_dir = metadata.get_target_directory(workspace_config)
    assert target_dir.is_absolute(), f"Target directory '{target_dir}' must be absolute."
    
    abs_drift_root = workspace_config.drift_root.absolute()
    if target_dir == abs_drift_root or is_relative_to(target_dir, abs_drift_root):
        raise InstallCollisionError(
            f"Safety Abort: The target directory written in config '{target_dir}' "
            f"cannot be inside or equal to the drift workspace root '{abs_drift_root}'."
        )
    
    assert_writable(target_dir, metadata.package.sudo)
    
    if not options.force and state_registry.is_package_in_midway_state(pkg):
        current_state = state_registry.get_package_state(pkg)
        raise RuntimeError(
            f"Safety Abort: Package '{pkg}' is currently in '{current_state}' state, "
            f"indicating a previous operation failed midway. "
            f"Please run 'drift rollback {pkg}' to restore a clean state before retrying."
        )
    
    install_pkg_dir = workspace_config.install_path / pkg
    if not install_pkg_dir.is_dir():
        logger.warning(f"⚠️  Package installation directory '{install_pkg_dir}' does not exist. Skipping.")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(target_dir),
            status="SKIPPED",
            error=f"Package installation directory '{install_pkg_dir}' does not exist."
        )

    hook_flags = HookExecFlags.resolve(options.flags, settings=workspace_config.settings)
    if not hook_flags.no_hooks:
        metadata.hooks.assert_hooks_exist(install_pkg_dir, is_source=False)

    # non-deployable changes is counted as changes and will trigger hooks even if no files are deployed.
    pkg_change = options.get_package_changes(pkg)
    if (options.redeploy == False
            and (pkg_change is None or not pkg_change.has_changes)
            and (state_registry.get_target_migrated_from(pkg, target_dir) is None)):
        logger.info(f"Skipping package '{pkg}' deployment (no changes detected and redeploy is False).")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(target_dir),
            status="SKIPPED",
            error="No changes detected and redeploy is False"
        )

    return None


def deploy_one_package_impl(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    metadata: PackageConfig,
    options: DeployOptions,
) -> PackageInstallResult:
    """Executes collision audit, lifecycle hooks, file deliveries, and state registry updates."""
    context = PackageInstallContext.from_package(
        workspace_config=workspace_config,
        state_registry=state_registry,
        metadata=metadata,
    )
    ops = FileOperations()

    # 1. Collision Guard
    run_collision_guard(
        context=context,
        resolve_symlinks=options.resolve_symlinks,
        ops=ops,
    )

    # Generate or update .stow-local-ignore file if using stow method
    if context.install_method == InstallMethod.STOW:
        context.ignore_handler.create_stow_ignore_file(context.install_pkg_dir)

    target_dir = context.target_dir
    target_migrated_from = state_registry.get_target_migrated_from(context.pkg_name, target_dir)
    redeploy = options.redeploy

    if target_migrated_from is not None:
        logger.info(
            f"🔄 [MIGRATE] Target directory for package '{context.pkg_name}' changed: "
            f"'{target_migrated_from}' -> '{target_dir}'. Undeploying from previous location."
        )
        old_deployed_files = state_registry.get_package_deployed_files(context.pkg_name)
        if old_deployed_files:
            from .uninstall_repo import remove_deployed_files
            remove_deployed_files(
                pkg=context.pkg_name,
                deployed_files=old_deployed_files,
                target_dir=target_migrated_from,
                sudo=context.sudo,
            )
        # Force redeploy to populate new target_dir completely
        redeploy = True

    package_changes = options.get_package_changes(context.pkg_name)
    
    # Calculate current desired files list
    deployable_files = context.ignore_handler.filter_deployable_files(context.install_pkg_dir)

    if redeploy:
        deployed_files = state_registry.get_package_deployed_files(context.pkg_name)
        reconcile_orphaned_files(
            context=context,
            deployable_files=deployable_files,
            deployed_files=deployed_files,
            resolve_symlinks=options.resolve_symlinks,
            ops=ops,
        )

    # 2. Lifecycle Hooks & State registry update
    hook_flags = HookExecFlags.resolve(options.flags, settings=workspace_config.settings)
    try:
        if context.is_first_time:
            metadata.hooks.trigger_pre_install(flags=hook_flags)
        else:
            metadata.hooks.trigger_pre_update(flags=hook_flags)
    except HookExecutionError as e:
        if not e.requires_rollback:
            if context.is_first_time:
                state_registry.packages.pop(context.pkg_name, None)
            else:
                state_registry.set_package_state(context.pkg_name, "installed")
            state_registry.save()
            logger.error(f"❌ Pre-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Deployment stopped (no rollback needed).")
        raise

    # Persist the target file manifest to state.toml before hooks & physical delivery
    # so that midway file deployment crashes have an authoritative list of files to uninstall/rollback
    state_registry.sync_deployed_files(
        pkg=context.pkg_name,
        target_directory=context.target_dir,
        install_method=context.install_method,
        redeploy=redeploy,
        deployable_files=deployable_files,
        package_changes=package_changes,
    )
    state_registry.save()
    
    # 3. Physical Deployment Execution
    if redeploy:
        run_full_file_delivery(
            context=context,
            deployable_files=deployable_files,
        )
    else:
        assert package_changes is not None
        run_incremental_file_delivery(
            context=context,
            package_changes=package_changes,
        )

    logger.debug(f"   File delivery completed via {context.install_method}")
    
    # Post Hooks
    success = False
    no_rollback_err = False
    try:
        if context.is_first_time:
            metadata.hooks.trigger_post_install(flags=hook_flags)
        else:
            metadata.hooks.trigger_post_update(flags=hook_flags)
        success = True
    except HookExecutionError as e:
        if not e.requires_rollback:
            no_rollback_err = True
            logger.error(f"❌ Post-deployment hook '{e.hook_name}' failed for package '{context.pkg_name}'. Files remain installed (no rollback needed).")
        raise
    finally:
        if success or no_rollback_err:
            update_state_registry_post_deployment(
                state_registry=state_registry,
                pkg=context.pkg_name,
                target_directory=context.target_dir,
                install_method=context.install_method,
                redeploy=redeploy,
                deployable_files=deployable_files,
                package_changes=package_changes,
            )

    logger.info(f"✨ Package '{context.pkg_name}' deployed successfully.")

    if package_changes is not None:
        ops.added = [str(p) for p in package_changes.deployable_changes.added]
        ops.modified = [str(p) for p in package_changes.deployable_changes.modified]
        ops.deleted = [str(p) for p in package_changes.deployable_changes.deleted]
    else:
        ops.added = [str(p) for p in deployable_files]

    return PackageInstallResult(
        package=context.pkg_name,
        install_method=context.install_method,
        target_directory=str(context.target_dir),
        operations=ops,
        is_first_time=context.is_first_time,
        status="SUCCESS"
    )


def deploy_one_package(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    pkg: str,
    options: Optional[DeployOptions] = None,
) -> PackageInstallResult:
    """Core function to deploy a single package configuration."""
    opts = options if options is not None else DeployOptions()
    install_base = workspace_config.install_path
    metadata = PackageConfig.from_install_dir(install_base / pkg, workspace_config)

    skip_res = assert_one_package_deployment_ready(
        workspace_config=workspace_config,
        state_registry=state_registry,
        metadata=metadata,
        options=opts,
    )
    if skip_res is not None:
        return skip_res
    
    logger.info(f"🚀 Deploying package: {pkg}")
    
    state_registry.set_package_state(pkg, "installing")
    state_registry.save()
    
    with metadata.package_envs():
        return deploy_one_package_impl(
            workspace_config=workspace_config,
            state_registry=state_registry,
            metadata=metadata,
            options=opts,
        )


def deploy_one_package_with_error_wrapping(
    workspace_config: WorkspaceConfig,
    state_registry: StateRegistry,
    pkg: str,
    options: Optional[DeployOptions] = None,
) -> PackageInstallResult:
    """Core function to deploy a single package configuration with subcommand error output reporting."""
    try:
        return deploy_one_package(
            workspace_config=workspace_config,
            state_registry=state_registry,
            pkg=pkg,
            options=options,
        )
    except subprocess.CalledProcessError as e:
        stderr_str = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr or "")
        stdout_str = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout or "")
        err_msg = (
            f"Subcommand failed during package '{pkg}' deployment.\n"
            f"Command: {shlex.join(e.cmd) if isinstance(e.cmd, list) else str(e.cmd)}\n"
            f"Exit Code: {e.returncode}"
        )
        if stderr_str.strip():
            err_msg += f"\nStderr:\n{stderr_str.strip()}"
        if stdout_str.strip():
            err_msg += f"\nStdout:\n{stdout_str.strip()}"
        logger.error(err_msg)
        raise mark_logged(RuntimeError(err_msg)) from e


def assert_packages_deployment_ready(
    workspace_config: WorkspaceConfig,
    discovered_packages: Iterable[str],
    pkg_metadata_map: Mapping[str, PackageConfig],
    hook_flags: HookExecFlags,
    state_registry: StateRegistry,
) -> None:
    """Pre-flight checks for permissions, lifecycle hook scripts, and cross-package file conflicts before deployment."""
    active_packages = [
        (pkg, metadata)
        for pkg, metadata in pkg_metadata_map.items()
        if metadata.package.enable_install
    ]

    if any(metadata.package.sudo for _, metadata in active_packages):
        from ..utils.process_utils import assert_can_escalate
        assert_can_escalate()

    if not hook_flags.no_hooks:
        for pkg, metadata in active_packages:
            metadata.hooks.assert_hooks_exist(
                workspace_config.install_path / pkg,
                is_source=False,
            )

    assert_no_cross_package_conflicts(
        workspace_config=workspace_config,
        discovered_packages=discovered_packages,
        pkg_metadata_map=pkg_metadata_map,
        state_registry=state_registry,
    )


# =============================================================================
# Layer 5: Public Primitive Entry Points
# =============================================================================

def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    packages_to_redeploy: Sequence[str] = (),
    options: Optional[DeployOptions] = None,
) -> InstallDeploymentResult:
    """Applies changes from the install/ state database to the active host system (Primitive 5).

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_redeploy: Specific package name(s) to deploy, or empty/omitted for all installed packages.
        options: Optional DeployOptions controlling deployment behavior (resolve_symlinks, force, redeploy, package_changes, flags).

    Returns:
        InstallDeploymentResult with detailed per-package deployment results.
    """
    opts = options if options is not None else DeployOptions()
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    hook_flags = HookExecFlags.resolve(opts.flags, settings=workspace_config.settings)
    
    state_registry = load_state_registry(state_file)
    
    discovered_packages = workspace_config.filter_install_packages_by_target(
        target_packages=packages_to_redeploy or None,
    )

    pkg_metadata_map = {
        pkg: PackageConfig.from_install_dir(install_base / pkg, workspace_config)
        for pkg in discovered_packages
    }

    assert_packages_deployment_ready(
        workspace_config=workspace_config,
        discovered_packages=discovered_packages,
        pkg_metadata_map=pkg_metadata_map,
        hook_flags=hook_flags,
        state_registry=state_registry,
    )

    results = [
        deploy_one_package_with_error_wrapping(
            workspace_config=workspace_config,
            state_registry=state_registry,
            pkg=pkg,
            options=opts,
        )
        for pkg in discovered_packages
    ]

    return InstallDeploymentResult(
        status="SUCCESS",
        packages=results
    )


def run_primitive_6_commit_install_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    target_pkgs: Sequence[str] = ()
) -> None:
    """Stages and commits changes inside the install/ state Git repository (Primitive 6).

    If target_pkgs is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    from ..utils.git_utils import commit_repo_changes
    
    install_dir = workspace_config.install_path
    
    committed = commit_repo_changes(
        repo_path=install_dir,
        commit_message=commit_message,
        target_pkgs=target_pkgs,
        repo_name="install repo"
    )
    
    if committed:
        logger.info(f"💾 Committed install repo changes: {commit_message}")
    else:
        logger.info("Nothing to commit, install repository is clean.")
