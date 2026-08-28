"""Core physical file deployment and host installation operations using pathlib."""

import os
import re
import logging
import subprocess
import datetime
import shlex
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig, load_package_config_rendered
from .constants import PACKAGE_CONFIG_FILE_NAME, MANAGED_CONFIG_FILES, STOW_LOCAL_IGNORE_FILE_NAME
from .exceptions import CollisionError
from .ignore import DriftIgnore
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .folder_diff import compare_folders, list_folder_paths, find_links_pointing_into
from .stage_repo import PackageStageChanges
from .file_utils import (
        resolve_system_target,
        translate_dot_prefixes_reverse,
        copy_file_contents_with_sudo,
        create_symlink_manually_with_sudo,
        get_relative_path,
        get_symlinked_parent,
        ensure_directory_writable,
        ensure_dir_exists_with_sudo,
        remove_file_or_dir_with_sudo,
        is_relative_to,
        run_command,
)
from .sync_ops import backup_file_or_dir_external
from .result_models import FileOperations, PackageInstallResult, InstallDeploymentResult

logger = logging.getLogger(__name__)


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


def load_config_for_install(install_base: Path, pkg: str) -> PackageConfig:
    """Loads package configuration strictly from the install/ base directory."""
    install_config_file = install_base / pkg / PACKAGE_CONFIG_FILE_NAME
    if not install_config_file.exists():
        raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in install base of package '{pkg}'.")
    try:
        return load_package_config_rendered(install_config_file)
    except Exception as e:
        raise RuntimeError(f"Failed to load package configuration for '{pkg}' from install base: {e}")


from .lifecycle_hooks import trigger_package_lifecycle_hook


def handle_collision_error(
    pkg: str,
    rel_path: Union[Path, str],
    system_target: Path,
    workspace_config: WorkspaceConfig,
    sudo: bool,
    reason: str,
    resolve_symlinks: bool,
    backup_subfolder: str = "overwritten"
) -> None:
    """Helper to backup and report a collision/error at a system target path."""
    # Ensure backup path respects relative structure
    backup_path = workspace_config.backup_path / pkg / backup_subfolder / rel_path
    logger.warning(f"🛡️  [COLLISION] {reason} at '{system_target}'")
    logger.debug(f"   Backing up to: {backup_path}")
    backup_file_or_dir_external(system_target, backup_path, sudo, resolve_symlinks=resolve_symlinks)
    
    # After backup, remove the colliding item to clear the way
    remove_file_or_dir_with_sudo(system_target, sudo)


def handle_internal_symlink_conflicts(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    metadata: PackageConfig,
    ignore_handler: DriftIgnore,
    target_dir: Path,
    resolve_symlinks: bool,
    processed_paths: set
) -> None:
    """Detects and backs up symlinks in target_dir pointing into drift_root that conflict with install_pkg_dir files."""

    # not necessary to follow_symlinks here,
    # because dir pointing into drift_root will always trigger backup and removal.
    # so dig deep inside is meaningless.
    links = find_links_pointing_into(target_dir, workspace_config.drift_root, follow_symlinks=False)
    for link in links:
        try:
            rel_in_target = link.relative_to(target_dir)
        except ValueError:
            continue

        repo_rel = translate_dot_prefixes_reverse(rel_in_target)
        repo_path = install_pkg_dir / repo_rel

        # Check if the relative path matches a non-ignored file/dir in install_pkg_dir
        if not (repo_path.exists() or repo_path.is_symlink()):
            continue
        if ignore_handler.match_path(repo_rel):
            continue

        # If install method is stow and link points into our pkg install dir, it's valid for this package
        if metadata.get_install_method(workspace_config) == "stow":
            try:
                # stow command can only handle relative paths,
                # so only relative links pointing to the same install_pkg_dir are valid stow links.
                # And we restrict the result to not contain linked dir as parent dir.
                # so dir pointing to the same install_pkg_dir is considered invalid, and trigger backup and removal.
                # Other cases must trigger backup and removal, including symlinked directories.
                link_content = Path(os.readlink(link))
                if not link_content.is_absolute() and not link.is_dir():
                    link_target = (link.parent / os.readlink(link)).resolve()
                    if is_relative_to(link_target, install_pkg_dir.resolve()):
                        continue
            except Exception:
                pass

        # Otherwise, it is a link conflict and must be backed up & removed
        processed_paths.add(repo_rel)

        # Add all children of this repo_rel to processed_paths to avoid double handling
        if repo_path.is_dir():
            for child_rel in list_folder_paths(
                src_dir=repo_path,
                base_rel=repo_rel,
                ignore_handler=ignore_handler,
                resolve_symlinks=resolve_symlinks,
                translate_mode="forward"
            ):
                processed_paths.add(child_rel)

        handle_collision_error(
            pkg=pkg,
            rel_path=repo_rel,
            system_target=link,
            workspace_config=workspace_config,
            sudo=metadata.sudo,
            reason="Internal symlink parent error",
            resolve_symlinks=resolve_symlinks
        )

        # If repo expects a directory here, recreate it as physical to avoid cycles
        if repo_path.is_dir() and not repo_path.is_symlink():
            ensure_dir_exists_with_sudo(link, metadata.sudo)


def run_collision_guard(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    metadata: PackageConfig,
    ignore_handler: DriftIgnore,
    target_dir: Path,
    is_first_time: bool,
    resolve_symlinks: bool,
    install_base: Path
) -> None:
    """Handles collision backing up before any file deployment using FolderDiff."""
    # 0. Safety Abort Check for parents ABOVE or AT target_dir
    # This detects if our target base itself is a symlink into drift_root
    parent_symlink = get_symlinked_parent(target_dir, workspace_config.drift_root)
    if parent_symlink:
         raise CollisionError(
            f"Safety Abort: Parent directory '{parent_symlink}' (resolved to '{parent_symlink.resolve()}') "
            f"is a symlink pointing into drift workspace root '{workspace_config.drift_root}', "
            f"but lies outside the package target directory '{target_dir}'. "
            f"Resolving this automatically is unsafe. Please resolve manually."
        )

    processed_paths = set()

    # 1. Check and resolve internal symlink conflicts inside target_dir
    handle_internal_symlink_conflicts(
        workspace_config=workspace_config,
        pkg=pkg,
        install_pkg_dir=install_pkg_dir,
        metadata=metadata,
        ignore_handler=ignore_handler,
        target_dir=target_dir,
        resolve_symlinks=resolve_symlinks,
        processed_paths=processed_paths
    )

    # 2. Recursive Audit using FolderDiff
    diff = compare_folders(
        src_dir=install_pkg_dir,
        dst_dir=target_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=resolve_symlinks,
        translate_mode="forward",
        src_only=True,
    )

    # 3. Handle Deleted items (type mismatches where system files block repo dirs)
    for rel in diff.deleted:
        if rel in processed_paths:
            continue
        processed_paths.add(rel)
        
        system_target = resolve_system_target(rel, target_dir)
        if ignore_handler.match_path(rel):
            # Clean up now-ignored files
            handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo,
                                   "Ignored file cleanup", resolve_symlinks, backup_subfolder="deleted_files")
        else:
            # Type mismatch (e.g. System has file, Repo has dir)
            handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo,
                                   "Type mismatch collision", resolve_symlinks)

    # 4. Handle Modified items (collisions that need overwrite)
    for rel in diff.modified:
        if rel in processed_paths:
            continue
        processed_paths.add(rel)
        
        system_target = resolve_system_target(rel, target_dir)
        
        # If the file is modified, then it cannot pointing to the same file.
        # If the symlink points to anywhere inside install_pkg_dir but not the same pkg_install_dir,
        # it is handled earlier in internal symlink conflicts.
        # So if it's symlink
        #   1. it is a broken symlink
        #   2. it is a symlink pointing outside install_pkg_dir 
        #   3. it is pointing inside the same pkg_install_dir, but not the same file.
        # We can skip if the system target is a symlink pointing to another file in same install_pkg_dir.
        # If it is not a symlink or a broken link, we need to backup and remove it, because it is a collision.
        if (metadata.get_install_method(workspace_config) == "stow"
                and system_target.is_symlink() and system_target.exists()
                and is_relative_to(system_target.resolve(), install_pkg_dir.resolve())):
            continue

        # Copy mode check: skip backup if the system target is not a symlink and it's not the first time installation (i.e., it's an update).
        if (metadata.get_install_method(workspace_config) == "copy"
                and not system_target.is_symlink() and not is_first_time):
            continue

        # conditions include:
        # stow mode: system target file is not a symlink, or is broken link, or pointing outside install_pkg_dir
        # copy mode: first installation, or system target is a symlink (broken or not)
        handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo,
                               "Deployment collision", resolve_symlinks)

    # 5. Handle Content Match items (Stow specific: physical file matching repo content is STILL a collision)
    if metadata.get_install_method(workspace_config) == "stow":
        for rel in diff.matches:
            if rel in processed_paths:
                continue
            processed_paths.add(rel)
            
            system_target = resolve_system_target(rel, target_dir)
            if not system_target.is_symlink():
                handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo,
                                       "Stow physical collision", resolve_symlinks)


def run_full_copy_deployment(
    src_pkg_dir: Path,
    target_dir: Path,
    sudo: bool,
    deployable_files: Optional[List[Path]] = None
) -> None:
    """Runs high-level copying deployment using rsync if available, otherwise cp -r.
    If deployable_files is provided, it uses rsync --files-from or manual loop to respect ignores.
    """
    ensure_dir_exists_with_sudo(target_dir, sudo)
    pkg = src_pkg_dir.name

    if deployable_files is not None:
        # Optimized path: use rsync --files-from to only copy deployable files.
        # This automatically respects ignores because deployable_files is already filtered.
        rsync_cmd = ["rsync", "-av", "--files-from=-", str(src_pkg_dir) + "/", str(target_dir) + "/"]
        if sudo:
            rsync_cmd.insert(0, "sudo")

        try:
            logger.info(f"🚚 Syncing files: {pkg} (copy)")
            logger.debug(f"   Command: {shlex.join(rsync_cmd)}")
            file_list = "\n".join(str(f) for f in deployable_files)
            run_command(rsync_cmd, input=file_list, text=True)
            return
        except Exception as e:
            logger.warning(f"Filtered rsync failed or not available, falling back to manual loop: {e}")
            for rel_file in deployable_files:
                deploy_single_copy_file(rel_file, src_pkg_dir, target_dir, sudo)
            return

    # Fallback/Default path (blind copy)
    rsync_cmd = ["rsync", "-av", str(src_pkg_dir) + "/", str(target_dir) + "/"]
    if sudo:
        rsync_cmd.insert(0, "sudo")
        
    try:
        logger.info(f"🚚 Syncing files: {pkg} (copy)")
        logger.debug(f"   Command: {shlex.join(rsync_cmd)}")
        run_command(rsync_cmd)
        return
    except Exception as e:
        logger.warning(f"rsync failed or not available, falling back to cp: {e}")
        
    # cp -r fallback
    cp_cmd = ["cp", "-R", str(src_pkg_dir) + "/.", str(target_dir) + "/"]
    if sudo:
        cp_cmd.insert(0, "sudo")
    logger.info(f"🚚 Syncing files: {pkg} (copy/cp fallback)")
    logger.debug(f"   Command: {shlex.join(cp_cmd)}")
    run_command(cp_cmd)
    

def run_stow_deployment(install_base: Path, target_dir: Path, pkg: str, sudo: bool, stow_sufficient: bool) -> None:
    """Invokes GNU Stow for package deployment."""
    if stow_sufficient:
        ensure_dir_exists_with_sudo(target_dir, sudo)
        stow_cmd = [
            "stow",
            "--no-folding",
            "--dotfiles",
            "-d", str(install_base),
            "-t", str(target_dir),
            pkg
        ]
        if sudo:
            stow_cmd.insert(0, "sudo")
        logger.info(f"🔗 Linking files: {pkg} (stow)")
        logger.debug(f"   Command: {shlex.join(stow_cmd)}")
        run_command(stow_cmd, cwd=str(install_base))
    else:
        raise RuntimeError("Stow version is insufficient (< 2.4.1) or not installed.")


def deploy_single_stow_file(
    rel_file: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Stow method."""
    src_file = install_pkg_dir / rel_file
    system_target = resolve_system_target(rel_file, target_dir)
    relative_target = get_relative_path(system_target.parent, src_file)
    create_symlink_manually_with_sudo(relative_target, system_target, sudo)


def deploy_single_copy_file(
    rel_file: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Copy method."""
    src_file = install_pkg_dir / rel_file
    system_target = resolve_system_target(rel_file, target_dir)
    copy_file_contents_with_sudo(src_file, system_target, sudo)


def delete_single_system_file(
    rel_file: Path,
    target_dir: Path,
    sudo: bool
) -> None:
    """Helper to delete a single file on host system (for incremental deletions)."""
    system_target = resolve_system_target(rel_file, target_dir)
    remove_file_or_dir_with_sudo(system_target, sudo)


def reconcile_orphaned_files(
    pkg: str,
    target_dir: Path,
    current_files: List[Path],
    state_registry: StateRegistry,
    workspace_config: WorkspaceConfig,
    metadata: PackageConfig,
    resolve_symlinks: bool
) -> None:
    """Reconciles historical deployment files to prune orphaned files from active system target."""
    previous_files = state_registry.get_package_deployed_files(pkg)
    orphaned_files = set(previous_files) - set(current_files)
    if not orphaned_files:
        return
    logger.info(f"🔍 Reconciling desired state: Pruning {len(orphaned_files)} orphaned files")
    for orphaned in sorted(orphaned_files):
        system_target = resolve_system_target(orphaned, target_dir)
        if system_target.exists() or system_target.is_symlink():
            backup_path = workspace_config.backup_path / pkg / "deleted_files" / orphaned
            logger.info(f"🧹 [PRUNE] Orphaned file '{orphaned}' removed. Backing up and deleting.")
            logger.debug(f"   Backing up to: {backup_path}")
            backup_file_or_dir_external(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
            # Ensure the system target itself is removed
            remove_file_or_dir_with_sudo(system_target, metadata.sudo)


def run_full_file_delivery(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_base: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    metadata: PackageConfig,
    deployable_files: List[Path],
    stow_sufficient: bool
) -> None:
    """Handles full file delivery during initial or clean redeployment."""
    if metadata.get_install_method(workspace_config) == "copy":
        run_full_copy_deployment(install_pkg_dir, target_dir, metadata.sudo, deployable_files=deployable_files)
        return
    if metadata.get_install_method(workspace_config) == "stow":
        if stow_sufficient:
            run_stow_deployment(install_base, target_dir, pkg, metadata.sudo, stow_sufficient)
            return
        logger.warning("GNU Stow version is insufficient (< 2.4.1) or not installed. Falling back to manual symlinking.")
        for rel_file in deployable_files:
            deploy_single_stow_file(
                rel_file=rel_file,
                install_pkg_dir=install_pkg_dir,
                target_dir=target_dir,
                sudo=metadata.sudo
            )


def run_incremental_file_delivery(
    workspace_config: WorkspaceConfig,
    package_changes: PackageStageChanges,
    install_pkg_dir: Path,
    target_dir: Path,
    metadata: PackageConfig
) -> None:
    """Handles incremental deployment applying Stage Changes additions, modifications, and deletions."""
    # A. Process Deletions on active host system
    for rel_file in package_changes.deleted_files:
        delete_single_system_file(rel_file, target_dir, metadata.sudo)

    # B. Process Additions and Modifications
    for rel_file in package_changes.added_files + package_changes.modified_files:
        if metadata.get_install_method(workspace_config) == "stow":
            deploy_single_stow_file(
                rel_file=rel_file,
                install_pkg_dir=install_pkg_dir,
                target_dir=target_dir,
                sudo=metadata.sudo
            )
        elif metadata.get_install_method(workspace_config) == "copy":
            deploy_single_copy_file(
                rel_file=rel_file,
                install_pkg_dir=install_pkg_dir,
                target_dir=target_dir,
                sudo=metadata.sudo
            )


def sync_deployed_files_manifest(
    state_registry: StateRegistry,
    pkg: str,
    current_files: List[Path],
    full_redeploy: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> None:
    """Updates the deployed_files manifest list for a package in the state registry."""
    if full_redeploy:
        state_registry.set_package_deployed_files(pkg, current_files)
    else:
        new_deployed = set(state_registry.get_package_deployed_files(pkg))
        if package_changes:
            for rel in package_changes.deleted_files:
                new_deployed.discard(rel)
            for rel in package_changes.added_files:
                new_deployed.add(rel)
        state_registry.set_package_deployed_files(pkg, sorted(list(new_deployed)))


def update_state_registry_post_deployment(
    state_registry: StateRegistry,
    state_file: Path,
    pkg: str,
    install_method: str,
    current_files: List[Path],
    full_redeploy: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> None:
    """Updates and persists the package deployment state and deployed files manifest in state.toml."""
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(
        pkg, "installed", last_deployed=now_str, install_method=install_method
    )

    sync_deployed_files_manifest(
        state_registry=state_registry,
        pkg=pkg,
        current_files=current_files,
        full_redeploy=full_redeploy,
        package_changes=package_changes
    )

    save_state_registry(state_file, state_registry)


def execute_package_deployment(
    workspace_config: WorkspaceConfig,
    pkg: str,
    metadata: PackageConfig,
    target_dir: Path,
    install_pkg_dir: Path,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    is_first_time: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> PackageInstallResult:
    """Executes collision audit, lifecycle hooks, file deliveries, and state registry updates."""
    install_base = workspace_config.install_path
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)
    
    # 1. Collision Guard
    run_collision_guard(
        workspace_config=workspace_config,
        pkg=pkg,
        install_pkg_dir=install_pkg_dir,
        metadata=metadata,
        ignore_handler=ignore_handler,
        target_dir=target_dir,
        is_first_time=is_first_time,
        resolve_symlinks=resolve_symlinks,
        install_base=install_base
    )

    if metadata.get_install_method(workspace_config) == "stow":
        stow_ignore_path = install_pkg_dir / STOW_LOCAL_IGNORE_FILE_NAME
        stow_ignore_content = ignore_handler.generate_stow_local_ignore_content()
        if not stow_ignore_path.exists() or stow_ignore_path.read_text(encoding="utf-8") != stow_ignore_content:
            stow_ignore_path.write_text(stow_ignore_content, encoding="utf-8")
    
    # Remove full_redeploy parameter and rely on package_changes to determine deployment mode
    full_redeploy = (package_changes is None)
    
    # Calculate current desired files list
    # Actually, this filter process is already done in stage_repo phase.
    current_files = ignore_handler.filter_deployable_files(install_pkg_dir)

    if full_redeploy:
        reconcile_orphaned_files(
            pkg=pkg,
            target_dir=target_dir,
            current_files=current_files,
            state_registry=state_registry,
            workspace_config=workspace_config,
            metadata=metadata,
            resolve_symlinks=resolve_symlinks
        )

    # Persist the full target file manifest to state.toml before hooks & physical delivery
    # so that midway crashes have an authoritative list of files to restore or uninstall
    sync_deployed_files_manifest(
        state_registry=state_registry,
        pkg=pkg,
        current_files=current_files,
        full_redeploy=full_redeploy,
        package_changes=package_changes
    )
    save_state_registry(state_file, state_registry)
    
    # 3. Lifecycle Hooks & State registry update
    if is_first_time:
        metadata.hooks.trigger_pre_install(install_pkg_dir, install_pkg_dir)
    else:
        metadata.hooks.trigger_pre_update(install_pkg_dir, install_pkg_dir)

    # 2. Physical Deployment Execution
    stow_version = get_stow_version() if metadata.get_install_method(workspace_config) == "stow" else None
    stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False
    
    if full_redeploy:
        run_full_file_delivery(
            workspace_config=workspace_config,
            pkg=pkg,
            install_base=install_base,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata,
            deployable_files=current_files,
            stow_sufficient=stow_sufficient
        )
    else:
        assert package_changes is not None
        run_incremental_file_delivery(
            workspace_config=workspace_config,
            package_changes=package_changes,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata
        )

    logger.debug(f"   File delivery completed via {metadata.get_install_method(workspace_config)}")
    
    # Post Hooks
    if is_first_time:
        metadata.hooks.trigger_post_install(install_pkg_dir, target_dir)
    else:
        metadata.hooks.trigger_post_update(install_pkg_dir, target_dir)
        
    update_state_registry_post_deployment(
        state_registry=state_registry,
        state_file=state_file,
        pkg=pkg,
        install_method=metadata.get_install_method(workspace_config),
        current_files=current_files,
        full_redeploy=full_redeploy,
        package_changes=package_changes
    )

    logger.info(f"✨ Package '{pkg}' deployed successfully.")

    ops = FileOperations()
    if package_changes is not None:
        ops.added = [str(p) for p in package_changes.added_files]
        ops.modified = [str(p) for p in package_changes.modified_files]
        ops.deleted = [str(p) for p in package_changes.deleted_files]
    else:
        ops.added = [str(p) for p in current_files]

    return PackageInstallResult(
        package=pkg,
        install_method=metadata.get_install_method(workspace_config),
        target_directory=str(target_dir),
        operations=ops,
        is_first_time=is_first_time,
        status="SUCCESS"
    )


def deploy_package_impl(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> PackageInstallResult:
    """Core function to deploy a single package configuration."""
    install_base = workspace_config.install_path
    
    metadata = load_config_for_install(install_base, pkg)
    if not (force or metadata.enable_install):
        logger.info(f"Skipping package '{pkg}' during deployment (enable_install is False).")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(metadata.get_target_directory(workspace_config)),
            status="SKIPPED",
            error="enable_install is False"
        )
        
    target_dir = metadata.get_target_directory(workspace_config)
    # target dir should be absolute.
    assert target_dir.is_absolute(), f"Target directory '{target_dir}' must be absolute."
    
    # Safety Check: Target directory cannot be inside or equal to drift_root.
    # We want to prevent accidental drift_root nesting in config files.
    # Just a naming safety check, not a symlink resolution check,
    # because symlink tools may create symlinked install target dir that points into drift_root,
    # and the linked parent check will catch that later.
    abs_target = target_dir.absolute()
    abs_drift_root = workspace_config.drift_root.absolute()
    if abs_target == abs_drift_root or is_relative_to(abs_target, abs_drift_root):
        raise ValueError(
            f"Safety Abort: The target directory written in config '{target_dir}' "
            f"cannot be inside or equal to the drift workspace root '{abs_drift_root}'."
        )
    
    # Check target folder writability
    ensure_directory_writable(target_dir, metadata.sudo)
    
    # Check if first time before setting state to deploying
    current_state = state_registry.get_package_state(pkg)
    if not force and current_state in ("staging", "deploying"):
        raise RuntimeError(
            f"Safety Abort: Package '{pkg}' is currently in '{current_state}' state, "
            f"indicating a previous operation failed midway. "
            f"Please run 'drift rollback {pkg}' to restore a clean state before retrying."
        )
    
    # stage-repo will set state to 'staged'.
    pkg_state = state_registry.packages.get(pkg)
    is_first_time = (pkg_state is None or pkg_state.last_deployed is None)
    
    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
        logger.warning(f"⚠️  Package installation directory '{install_pkg_dir}' does not exist. Skipping.")
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(target_dir),
            status="SKIPPED",
            error=f"Package installation directory '{install_pkg_dir}' does not exist."
        )

    # Verify hook files exist and are regular files in install/
    metadata.hooks.check_hook_files(install_pkg_dir)
    
    # Set package state to "deploying" before actual deployment
    state_registry.set_package_state(pkg, "deploying", install_method=metadata.get_install_method(workspace_config))
    save_state_registry(state_file, state_registry)
    
    logger.info(f"🚀 Deploying package: {pkg}")
    
    with metadata.package_envs(workspace_config):
        return execute_package_deployment(
            workspace_config=workspace_config,
            pkg=pkg,
            metadata=metadata,
            target_dir=target_dir,
            install_pkg_dir=install_pkg_dir,
            state_registry=state_registry,
            state_file=state_file,
            resolve_symlinks=resolve_symlinks,
            is_first_time=is_first_time,
            package_changes=package_changes
        )


def deploy_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> PackageInstallResult:
    """Core function to deploy a single package configuration with subcommand error output reporting."""
    try:
        return deploy_package_impl(
            workspace_config=workspace_config,
            pkg=pkg,
            state_registry=state_registry,
            state_file=state_file,
            resolve_symlinks=resolve_symlinks,
            force=force,
            package_changes=package_changes
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
        raise RuntimeError(err_msg) from e


def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    packages_to_redeploy: Optional[List[str]] = None,
    resolve_symlinks: bool = True,
    force: bool = False,
    package_changes: Optional[List[PackageStageChanges]] = None
) -> InstallDeploymentResult:
    """Applies changes from the install/ state database to the active host system (Primitive 5)."""
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    
    state_registry = load_state_registry(state_file)
    
    discovered_packages = workspace_config.get_discovered_packages(
        custom_dir=workspace_config.install_path,
        target_pkgs=packages_to_redeploy,
    )

    # Pre-check hook files in install/ for all packages before starting deployment
    pkg_metadata_map = { pkg: load_config_for_install(install_base, pkg)
                        for pkg in discovered_packages
                        if (install_base / pkg).is_dir() }
    for pkg, metadata in pkg_metadata_map.items():
        if force or metadata.enable_install:
            metadata.hooks.check_hook_files(install_base / pkg)
    
    results: List[PackageInstallResult] = []
    for pkg in discovered_packages:
        # find corresponding PackageStageChanges for this package if provided
        if package_changes:
            pkg_change = next((c for c in package_changes if c.package_name == pkg), None)
        else:
            pkg_change = None
        pkg_res = deploy_package(
            workspace_config=workspace_config,
            pkg=pkg,
            state_registry=state_registry,
            state_file=state_file,
            resolve_symlinks=resolve_symlinks,
            force=force,
            package_changes=pkg_change
        )
        results.append(pkg_res)

    return InstallDeploymentResult(
        status="SUCCESS",
        packages=results
    )


def run_primitive_6_commit_install_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    target_pkgs: Optional[List[str]] = None
) -> None:
    """Stages and commits changes inside the install/ state Git repository (Primitive 6).

    If target_pkgs is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    from .git_utils import commit_repo_changes
    
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
