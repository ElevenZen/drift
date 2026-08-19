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
from .package_config import PackageConfig, load_package_config_static
from .constants import PACKAGE_CONFIG_FILE_NAME, IGNORED_FILENAMES
from .ignore import DriftIgnore
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .stage_repo import PackageStageChanges
from .file_utils import (
        resolve_system_target,
        copy_file_contents_with_sudo,
        create_symlink_manually_with_sudo,
        get_relative_path,
        get_symlinked_parent,
        ensure_directory_writable,
        ensure_dir_exists_with_sudo,
        remove_file_or_dir_with_sudo,
        tree_relative_files,
        is_relative_to,
        run_command,
)
from .sync_ops import backup_file_or_dir_external

logger = logging.getLogger(__name__)


def get_stow_version() -> Optional[str]:
    """Retrieves the installed GNU Stow version string if available."""
    try:
        res = run_command(["stow", "--version"], text=True)
        # stow (GNU Stow) version 2.3.1 or 2.4.1
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", res.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
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
    if install_config_file.exists():
        try:
            return load_package_config_static(install_config_file, default_name=pkg)
        except Exception as e:
            raise RuntimeError(f"Failed to load package configuration for '{pkg}' from install base: {e}")
    raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in install base of package '{pkg}'.")


def trigger_package_lifecycle_hook(
    pkg: str,
    hook_name: str,
    metadata: PackageConfig,
    workspace_config: WorkspaceConfig,
    cwd_override: Optional[Path] = None
) -> None:
    """Executes a package lifecycle hook script if specified and found.

    The working directory defaults to the package's active target directory,
    but can be overridden (e.g. to install/pkg or render/pkg folders).
    If the hook execution fails or times out, detailed output logs are printed and a RuntimeError is raised.
    """
    hook_file = getattr(metadata, hook_name, None)
    if not hook_file:
        return
        
    hook_path = workspace_config.install_path / pkg / hook_file
    if not hook_path.exists():
        # Fallback to render_path if not in install_path (for post_render)
        hook_path = workspace_config.render_path / pkg / hook_file
        
    if not hook_path.exists():
        logger.warning(f"Lifecycle hook file specified but not found: {hook_path}")
        return
        
    try:
        hook_path.chmod(0o755)
    except Exception:
        pass
        
    target_dir = cwd_override or metadata.target_directory or workspace_config.default_target_path
    assert target_dir.is_absolute(), f"Target directory '{target_dir}' must be absolute."

    logger.info(f"🪝  Triggering hook: {hook_name} ({pkg})")
    logger.debug(f"   Script: {hook_path}")
    logger.debug(f"   CWD:    {target_dir}")
    
    cmd = [str(hook_path)]
    if metadata.sudo:
        cmd.insert(0, "sudo")
        
    timeout_seconds = metadata.hook_timeout
    try:
        run_command(
            cmd,
            cwd=str(target_dir),
            text=True,
            timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as e:
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' timed out after {timeout_seconds} seconds.\n"
            f"Command: {shlex.join(cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e
    except subprocess.CalledProcessError as e:
        stdout_str = e.stdout or ""
        stderr_str = e.stderr or ""
        err_msg = (
            f"Lifecycle hook '{hook_name}' for package '{pkg}' failed with exit code {e.returncode}.\n"
            f"Command: {shlex.join(cmd)}\n"
        )
        if stdout_str.strip():
            err_msg += f"Stdout:\n{stdout_str.strip()}\n"
        if stderr_str.strip():
            err_msg += f"Stderr:\n{stderr_str.strip()}\n"
        logger.error(err_msg)
        raise RuntimeError(err_msg) from e


def handle_symlinked_parent_error(
    system_target: Path,
    parent_symlink: Path,
    pkg: str,
    target_dir: Path,
    workspace_config: WorkspaceConfig,
    sudo: bool,
    resolve_symlinks: bool = True
) -> None:
    """
    System_target is the installation target for one file.
    If any parent directory of system_target is a symlink pointing into drift_root, this is treated as an error.
    Backups the symlinked parent folder, removes it, and recreates it as a physical folder.

    Raises severe error if parent symlink lies outside package's own target directory.
    """
    if not parent_symlink:
        return
        
    # Enforce severe safety guard: only allow automatic repair if parent_symlink lies inside target_dir. This prevents accidental deletion of unrelated system paths.
    # We don't care where thse symlink points to, we only care that the symlink itself is inside the package's target directory.
    # parent_symlink is a prefix of system_target, target_dir is also a prefix of system_target,
    # so we can check if parent_symlink is relative to target_dir.
    abs_parent = parent_symlink.absolute()
    abs_target = target_dir.absolute()
    if not is_relative_to(abs_parent, abs_target):
        raise RuntimeError(
            f"Safety Abort: Parent directory '{parent_symlink}' (resolved to '{parent_symlink.resolve()}' is a symlink pointing into "
            f"drift workspace root '{workspace_config.drift_root}', but lies outside "
            f"the package target directory '{target_dir}'. Resolving this automatically is "
            f"unsafe and could permanently delete unrelated system paths. Please resolve manually."
        )
        
    # Maintain nested relative path structure in overwriting backups
    rel_parent = parent_symlink.relative_to(target_dir)
    backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_parent
    logger.warning(f"⚠️  [RECOVERY] Symlinked parent directory error at '{parent_symlink}'")
    logger.debug(f"   Backing up parent symlink to: {backup_path}")
    backup_file_or_dir_external(parent_symlink, backup_path, sudo, resolve_symlinks=resolve_symlinks)
    
    # Remove parent symlink
    remove_file_or_dir_with_sudo(parent_symlink, sudo)
    
    # Recreate parent directory as a physical folder
    ensure_dir_exists_with_sudo(parent_symlink, sudo)


def run_single_file_collision_guard(
    rel_file: Path,
    pkg: str,
    metadata: PackageConfig,
    target_dir: Path,
    install_base: Path,
    workspace_config: WorkspaceConfig,
    is_first_time: bool,
    resolve_symlinks: bool
) -> None:
    """Collision guard logic for a single configuration file."""
    system_target = resolve_system_target(rel_file, target_dir)
    
    # Repo Pollution And Infinite Loop Protection: checks if any parent directory of system_target is a symlink into drift_root.
    parent_symlink = get_symlinked_parent(system_target.parent, workspace_config.drift_root)
    if parent_symlink:
        handle_symlinked_parent_error(
            system_target=system_target,
            parent_symlink= parent_symlink,
            pkg=pkg,
            target_dir=target_dir,
            workspace_config=workspace_config,
            sudo=metadata.sudo,
            resolve_symlinks=resolve_symlinks
        )

    if not system_target.exists() and not system_target.is_symlink():
        return

    pkg_install_dir = install_base / pkg

    # Condition 1: Stow Mode - physical non-link file or folder/symlink exists at target
    if metadata.install_method == "stow":
        if system_target.is_symlink():
            try:
                # do not resolve link here, it may be broken, backup_file_or_dir_external() will resolve it properly.
                link_str = system_target.readlink()
                abs_link_target = system_target.parent / link_str
                normalized_target = Path(os.path.normpath(abs_link_target.absolute()))
                
                if is_relative_to(normalized_target, pkg_install_dir.resolve()):
                    # Skip backup if symlink already points to the SAME package's install path
                    return
                drift_root_abs = workspace_config.drift_root.resolve()
                if is_relative_to(normalized_target, drift_root_abs):
                    # It points into drift_root but to a different path (e.g. dangling, or other package).
                    # Delete without backup to clear the collision.
                    logger.info(f"🧹 Removing internal/dangling drift-root symlink: {system_target.relative_to(target_dir)}")
                    remove_file_or_dir_with_sudo(system_target, metadata.sudo)
                    return
                # else fall to backup code below.
            except Exception as e:
                # Readlink or analysis failed.
                # If we fail to resolve/analyze, we treat it as an external collision and backup the symlink itself.
                logger.warning(f"⚠️  Failed to analyze symlink target of '{system_target}': {e}. Backing up symlink itself.")
                backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
                backup_file_or_dir_external(system_target, backup_path, metadata.sudo, resolve_symlinks=False)
                return

        backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
        logger.warning(f"🛡️  [COLLISION] Stow conflict at '{system_target}'.")
        logger.debug(f"   Backing up to: {backup_path}")
        backup_file_or_dir_external(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
        return
        
    # Condition 2: Copy Mode - copy conflict on very first installation of this package
    elif metadata.install_method == "copy" and is_first_time:
        backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
        logger.warning(f"🛡️  [COLLISION] Copy conflict at '{system_target}' (first install).")
        logger.debug(f"   Backing up to: {backup_path}")
        backup_file_or_dir_external(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
        return


def run_collision_guard(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    metadata: PackageConfig,
    target_dir: Path,
    is_first_time: bool,
    resolve_symlinks: bool,
    install_base: Path
) -> None:
    """Handles collision backing up before any file deployment."""
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)
    
    relative_files = tree_relative_files(install_pkg_dir)
    for rel_file in relative_files:
        if rel_file.name in IGNORED_FILENAMES:
            continue
            
        if ignore_handler.match_path(rel_file):
            # The ignore file is acting as delete instruction for the system target.
            system_target = resolve_system_target(rel_file, target_dir)
            if system_target.exists() or system_target.is_symlink():
                backup_path = workspace_config.backup_path / pkg / "deleted_files" / rel_file
                logger.info(f"🧹 [CLEANUP] Ignored file '{rel_file}' exists at system target. Backing up and deleting.")
                logger.debug(f"   Backing up to: {backup_path}")
                backup_file_or_dir_external(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
                # Ensure the system target itself is removed (if it was a symlink and we resolved symlinks, the symlink is still there)
                remove_file_or_dir_with_sudo(system_target, metadata.sudo)
            continue
            
        run_single_file_collision_guard(
            rel_file=rel_file,
            pkg=pkg,
            metadata=metadata,
            target_dir=target_dir,
            install_base=install_base,
            workspace_config=workspace_config,
            is_first_time=is_first_time,
            resolve_symlinks=resolve_symlinks
        )


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
    pkg: str,
    install_base: Path,
    install_pkg_dir: Path,
    target_dir: Path,
    metadata: PackageConfig,
    deployable_files: List[Path],
    stow_sufficient: bool
) -> None:
    """Handles full file delivery during initial or clean redeployment."""
    if metadata.install_method == "copy":
        run_full_copy_deployment(install_pkg_dir, target_dir, metadata.sudo, deployable_files=deployable_files)
        return
    if metadata.install_method == "stow":
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
        if metadata.install_method == "stow":
            deploy_single_stow_file(
                rel_file=rel_file,
                install_pkg_dir=install_pkg_dir,
                target_dir=target_dir,
                sudo=metadata.sudo
            )
        elif metadata.install_method == "copy":
            deploy_single_copy_file(
                rel_file=rel_file,
                install_pkg_dir=install_pkg_dir,
                target_dir=target_dir,
                sudo=metadata.sudo
            )


def deploy_package_impl(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> None:
    """Core function to deploy a single package configuration."""
    install_base = workspace_config.install_path
    
    metadata = load_config_for_install(install_base, pkg)
    if not (force or metadata.enable_install):
        logger.info(f"Skipping package '{pkg}' during deployment (enable_install is False).")
        return
        
    target_dir = metadata.target_directory or workspace_config.default_target_path
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
    
    is_first_time = (current_state is None)
    
    # Set package state to "deploying" before actual deployment
    state_registry.set_package_state(pkg, "deploying", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)
    
    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
        logger.warning(f"⚠️  Package installation directory '{install_pkg_dir}' does not exist. Skipping.")
        return
        
    logger.info(f"🚀 Deploying package: {pkg}")
    
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)
    
    # 1. Collision Guard
    run_collision_guard(
        workspace_config=workspace_config,
        pkg=pkg,
        install_pkg_dir=install_pkg_dir,
        metadata=metadata,
        target_dir=target_dir,
        is_first_time=is_first_time,
        resolve_symlinks=resolve_symlinks,
        install_base=install_base
    )
    
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
    
    # 3. Lifecycle Hooks & State registry update
    if is_first_time:
        trigger_package_lifecycle_hook(pkg, "pre_install", metadata, workspace_config, cwd_override=install_pkg_dir)
    else:
        trigger_package_lifecycle_hook(pkg, "pre_update", metadata, workspace_config, cwd_override=install_pkg_dir)

    # 2. Physical Deployment Execution
    stow_version = get_stow_version() if metadata.install_method == "stow" else None
    stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False
    
    if full_redeploy:
        run_full_file_delivery(
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
            package_changes=package_changes,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata
        )

    logger.debug(f"   File delivery completed via {metadata.install_method}")
    
    # Post Hooks
    if is_first_time:
        trigger_package_lifecycle_hook(pkg, "post_install", metadata, workspace_config)
    else:
        trigger_package_lifecycle_hook(pkg, "post_update", metadata, workspace_config)
        
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.install_method)
    
    # Save the updated list of deployed files to state.toml
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
        
    save_state_registry(state_file, state_registry)

    logger.info(f"✨ Package '{pkg}' deployed successfully.")


def deploy_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> None:
    """Core function to deploy a single package configuration with subcommand error output reporting."""
    try:
        deploy_package_impl(
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
) -> None:
    """Applies changes from the install/ state database to the active host system (Primitive 5)."""
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    
    state_registry = load_state_registry(state_file)
    
    discovered_packages = workspace_config.get_discovered_packages(
        custom_dir=workspace_config.install_path,
        target_pkgs=packages_to_redeploy,
    )
    
    for pkg in discovered_packages:
        # find corresponding PackageStageChanges for this package if provided
        if package_changes:
            pkg_change = next((c for c in package_changes if c.package_name == pkg), None)
        else:
            pkg_change = None
        deploy_package(
            workspace_config=workspace_config,
            pkg=pkg,
            state_registry=state_registry,
            state_file=state_file,
            resolve_symlinks=resolve_symlinks,
            force=force,
            package_changes=pkg_change
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
