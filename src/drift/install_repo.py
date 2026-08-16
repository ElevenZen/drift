import os
import re
import logging
import subprocess
import datetime
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig, load_package_config_static
from .constants import PACKAGE_CONFIG_FILE_NAME, IGNORED_FILENAMES
from .ignore import DriftIgnore
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .stage_repo import PackageStageChanges

logger = logging.getLogger(__name__)


def ensure_directory_writable(path: str, sudo: bool) -> None:
    """Checks if a directory path (or its closest existing parent) is writable."""
    if sudo:
        return  # With sudo, we assume target is writable or handled by elevation
    curr = os.path.abspath(path)
    while curr:
        if os.path.exists(curr):
            if os.access(curr, os.W_OK):
                return
            else:
                raise PermissionError(
                    f"Directory '{curr}' is not writable. "
                    "Please check permissions or configure sudo for this package."
                )
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    raise PermissionError(f"Target directory path '{path}' is invalid or inaccessible.")


def resolve_system_target(relative_path: str, target_dir: str) -> str:
    """Resolves relative file path in install/ folder to system target path, applying dot prefix conversion."""
    target_dir = os.path.expanduser(target_dir)
    parts = relative_path.split(os.sep)
    translated_parts = ["." + p[4:] if p.startswith("dot-") else p for p in parts]
    return os.path.join(target_dir, *translated_parts)


def get_stow_version() -> Optional[str]:
    """Retrieves the installed GNU Stow version string if available."""
    try:
        res = subprocess.run(["stow", "--version"], capture_output=True, text=True, check=True)
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


def get_symlinked_parent(system_target: str, install_base: str) -> Optional[str]:
    """Returns the symlinked parent directory of system_target if it is a symlink pointing to install/."""
    parent_dir = os.path.dirname(system_target)
    home_dir = os.path.expanduser("~")
    while parent_dir and parent_dir != "/" and parent_dir != home_dir:
        if os.path.islink(parent_dir):
            try:
                link_target = os.readlink(parent_dir)
                abs_link = os.path.abspath(os.path.join(os.path.dirname(parent_dir), link_target))
                if "install/" in abs_link or abs_link.startswith(os.path.abspath(install_base)):
                    return parent_dir
            except Exception:
                pass
        parent = os.path.dirname(parent_dir)
        if parent == parent_dir:
            break
        parent_dir = parent
    return None


def is_stow_linked_parent(system_target: str, install_base: str) -> bool:
    """Infinite Loop Protection: checks if any parent directory of system_target is a symlink into install/."""
    return get_symlinked_parent(system_target, install_base) is not None


def ensure_dir_exists_with_sudo(path: str, sudo: bool) -> None:
    """Ensures directory exists, creating with sudo if requested."""
    if os.path.exists(path):
        return
    if sudo:
        subprocess.run(["sudo", "mkdir", "-p", path], check=True, capture_output=True)
    else:
        os.makedirs(path, exist_ok=True)


def create_symlink_manually_with_sudo(src: str, dst: str, sudo: bool) -> None:
    """Creates a symlink from src to dst manually, cleaning up existing file/link with sudo if requested."""
    ensure_dir_exists_with_sudo(os.path.dirname(dst), sudo)
    
    if os.path.lexists(dst):
        cmd_rm = ["rm", "-rf" if os.path.isdir(dst) and not os.path.islink(dst) else "-f", dst]
        if sudo:
            cmd_rm.insert(0, "sudo")
        subprocess.run(cmd_rm, check=True, capture_output=True)
        
    cmd = ["ln", "-s", src, dst]
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True, capture_output=True)


def copy_file_contents_with_sudo(src: str, dst: str, sudo: bool) -> None:
    """Copies a physical file from src to dst, with sudo if requested."""
    ensure_dir_exists_with_sudo(os.path.dirname(dst), sudo)
    
    if os.path.lexists(dst):
        cmd_rm = ["rm", "-rf" if os.path.isdir(dst) and not os.path.islink(dst) else "-f", dst]
        if sudo:
            cmd_rm.insert(0, "sudo")
        subprocess.run(cmd_rm, check=True, capture_output=True)
        
    cmd = ["cp", src, dst]
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True, capture_output=True)


def copy_or_move_file(src: str, dst: str, sudo: bool, move: bool = False, resolve_symlinks: bool = True) -> None:
    """Backs up or copies file/directory, using sudo if requested and resolving symlinks recursively if resolve_symlinks is True."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    
    if move and not resolve_symlinks:
        cmd = ["mv", src, dst]
    else:
        if os.path.isdir(src):
            cmd = ["cp", "-RP" if not resolve_symlinks else "-RL", src, dst]
        else:
            cmd = ["cp", "-P" if not resolve_symlinks else "-L", src, dst]
            
    if sudo:
        cmd.insert(0, "sudo")
        
    subprocess.run(cmd, check=True, capture_output=True)
    
    if sudo:
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid is not None and gid is not None:
                chown_cmd = ["sudo", "chown", "-R", f"{uid}:{gid}", dst]
                subprocess.run(chown_cmd, check=True, capture_output=True)
        except Exception as e:
            logger.warning(f"Failed to chown backup to process owner: {e}")
            
    if move:
        del_cmd = ["rm", "-rf", src]
        if sudo:
            del_cmd.insert(0, "sudo")
        subprocess.run(del_cmd, check=True, capture_output=True)


def backup_file_or_dir(src: str, backup_dest: str, sudo: bool, resolve_symlinks: bool = True) -> None:
    """Recursively backs up target src to backup_dest, resolving symlinks if resolve_symlinks is True."""
    if not os.path.lexists(src):
        return
        
    if os.path.islink(src):
        if resolve_symlinks:
            real_target = os.path.realpath(src)
            if os.path.exists(real_target):
                backup_file_or_dir(real_target, backup_dest, sudo, resolve_symlinks=True)
        else:
            copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=False)
            
    elif os.path.isdir(src):
        if resolve_symlinks:
            os.makedirs(backup_dest, exist_ok=True)
            for item in os.listdir(src):
                item_src = os.path.join(src, item)
                item_dst = os.path.join(backup_dest, item)
                # Recursively resolve and backup each item in the directory
                backup_file_or_dir(item_src, item_dst, sudo, resolve_symlinks=True)
            # Remove original dir
            del_cmd = ["rm", "-rf", src]
            if sudo:
                del_cmd.insert(0, "sudo")
            subprocess.run(del_cmd, check=True, capture_output=True)
        else:
            copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=False)
    else:
        # then it's a normal file.
        copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=resolve_symlinks)


def run_full_copy_deployment(src_pkg_dir: str, target_dir: str, sudo: bool) -> None:
    """Runs high-level copying deployment using rsync if available, otherwise cp -r."""
    ensure_dir_exists_with_sudo(target_dir, sudo)
    
    rsync_cmd = ["rsync", "-av", src_pkg_dir + "/", target_dir + "/"]
    if sudo:
        rsync_cmd.insert(0, "sudo")
        
    try:
        subprocess.run(rsync_cmd, check=True, capture_output=True)
        logger.info("Full copy deployment succeeded via rsync.")
        return
    except Exception as e:
        logger.warning(f"rsync failed or not available, falling back to cp: {e}")
        
    # cp -r fallback
    cp_cmd = ["cp", "-R", src_pkg_dir + "/.", target_dir + "/"]
    if sudo:
        cp_cmd.insert(0, "sudo")
    subprocess.run(cp_cmd, check=True, capture_output=True)
    
    # Prune config and stow metadata files
    for filename in IGNORED_FILENAMES:
        dest_ignored = os.path.join(target_dir, filename)
        if os.path.exists(dest_ignored):
            rm_cmd = ["rm", "-rf", dest_ignored]
            if sudo:
                rm_cmd.insert(0, "sudo")
            subprocess.run(rm_cmd, check=True, capture_output=True)


def load_config_for_install(install_base: str, pkg: str) -> PackageConfig:
    """Loads package configuration strictly from the install/ base directory."""
    install_config_file = os.path.join(install_base, pkg, PACKAGE_CONFIG_FILE_NAME)
    if os.path.exists(install_config_file):
        try:
            return load_package_config_static(install_config_file, default_name=pkg)
        except Exception as e:
            raise RuntimeError(f"Failed to load package configuration for '{pkg}' from install base: {e}")
    raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in install base of package '{pkg}'.")


def trigger_package_lifecycle_hook(pkg: str, hook_name: str, metadata: PackageConfig, workspace_config: WorkspaceConfig) -> None:
    """Executes a package lifecycle hook script if specified and found."""
    hook_file = getattr(metadata, hook_name, None)
    if not hook_file:
        return
        
    # Always read from the install/ directory to avoid executing hooks from the source repo.
    hook_path = os.path.join(workspace_config.drift_root_path, workspace_config.install_directory, pkg, hook_file)
    if not os.path.exists(hook_path):
        logger.warning(f"Lifecycle hook file specified but not found: {hook_path}")
        return
        
    try:
        os.chmod(hook_path, 0o755)
    except Exception:
        pass
        
    logger.info(f"Triggering lifecycle hook '{hook_name}' for package '{pkg}': {hook_path}")
    
    cmd = [hook_path]
    if metadata.sudo:
        cmd.insert(0, "sudo")
        
    subprocess.run(cmd, check=True, cwd=os.path.dirname(hook_path))


def handle_symlinked_parent_error(
    system_target: str,
    pkg: str,
    install_base: str,
    workspace_config: WorkspaceConfig,
    sudo: bool,
    resolve_symlinks: bool = True
) -> None:
    """Treated as an error, backups the symlinked parent folder, removes it, and recreates it as a physical folder."""
    parent_symlink = get_symlinked_parent(system_target, install_base)
    if not parent_symlink:
        return
    backup_path = os.path.join(
        workspace_config.drift_root_path,
        workspace_config.backup_directory,
        pkg,
        "overwritten",
        os.path.basename(parent_symlink)
    )
    logger.warning(
        f"[RECOVERY] Symlinked parent directory error. "
        f"Backing up parent symlink '{parent_symlink}' to '{backup_path}'..."
    )
    backup_file_or_dir(parent_symlink, backup_path, sudo, resolve_symlinks=resolve_symlinks)
    
    # Remove parent symlink
    cmd_rm = ["rm", "-rf" if os.path.isdir(parent_symlink) and not os.path.islink(parent_symlink) else "-f", parent_symlink]
    if sudo:
        cmd_rm.insert(0, "sudo")
    subprocess.run(cmd_rm, check=True, capture_output=True)
    
    # Recreate parent directory as a physical folder
    ensure_dir_exists_with_sudo(parent_symlink, sudo)


def run_single_file_collision_guard(
    rel_file: str,
    pkg: str,
    metadata: PackageConfig,
    target_dir: str,
    install_base: str,
    workspace_config: WorkspaceConfig,
    is_first_time: bool,
    resolve_symlinks: bool
) -> None:
    """Collision guard logic for a single configuration file."""
    system_target = resolve_system_target(rel_file, target_dir)
    
    # Check for symlinked parent directories and handle them as errors, for any install method.
    if is_stow_linked_parent(system_target, install_base):
        handle_symlinked_parent_error(
            system_target=system_target,
            pkg=pkg,
            install_base=install_base,
            workspace_config=workspace_config,
            sudo=metadata.sudo,
            resolve_symlinks=resolve_symlinks
        )

    if not os.path.lexists(system_target):
        return

    pkg_install_dir = os.path.join(install_base, pkg)

    # Condition 1: Stow Mode - physical non-link file or folder/symlink exists at target
    if metadata.install_method == "stow":
        if os.path.islink(system_target):
            try:
                link_target = os.readlink(system_target)
                abs_link_target = os.path.abspath(os.path.join(os.path.dirname(system_target), link_target))
                if abs_link_target.startswith(os.path.abspath(install_base)):
                    # Skip backup if symlink already points to the SAME package's install path
                    if abs_link_target.startswith(os.path.abspath(pkg_install_dir)):
                        return
                    # Otherwise, it points to a DIFFERENT package's install path, so treat as collision and backup!
            except Exception:
                # Readlink failed. Treat as collision, backup symlink itself without resolving its content
                logger.warning(f"Failed to read symlink target of '{system_target}'. Backing up symlink itself.")
                backup_path = os.path.join(
                    workspace_config.drift_root_path,
                    workspace_config.backup_directory,
                    pkg,
                    "overwritten",
                    rel_file
                )
                backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=False)
                return

        backup_path = os.path.join(
            workspace_config.drift_root_path,
            workspace_config.backup_directory,
            pkg,
            "overwritten",
            rel_file
        )
        logger.warning(f"[COLLISION GUARD] Stow conflict at '{system_target}'. Backing up...")
        backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
            
    # Condition 2: Copy Mode - copy conflict on very first installation of this package
    elif metadata.install_method == "copy" and is_first_time:
        backup_path = os.path.join(
            workspace_config.drift_root_path,
            workspace_config.backup_directory,
            pkg,
            "overwritten",
            rel_file
        )
        logger.warning(f"[COLLISION GUARD] Copy conflict at '{system_target}' (first install). Backing up...")
        backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)


def run_collision_guard(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: str,
    metadata: PackageConfig,
    target_dir: str,
    is_first_time: bool,
    resolve_symlinks: bool,
    install_base: str
) -> None:
    """Handles collision backing up before any file deployment."""
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)
    
    for root, _, files in os.walk(install_pkg_dir):
        for file in files:
            rel_file = os.path.relpath(os.path.join(root, file), install_pkg_dir)
            if rel_file in IGNORED_FILENAMES or ignore_handler.match_path(rel_file):
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


def run_stow_deployment(install_base: str, target_dir: str, pkg: str, sudo: bool, stow_sufficient: bool) -> None:
    """Invokes GNU Stow for package deployment."""
    if stow_sufficient:
        stow_cmd = [
            "stow",
            "--no-folding",
            "--dotfiles",
            "-t", target_dir,
            pkg
        ]
        if sudo:
            stow_cmd.insert(0, "sudo")
        subprocess.run(stow_cmd, check=True, cwd=install_base, capture_output=True)
        logger.info("Full stow deployment succeeded via GNU Stow.")
    else:
        raise RuntimeError("Stow version is insufficient (< 2.4.1) or not installed.")


def deploy_single_stow_file(
    rel_file: str,
    install_pkg_dir: str,
    target_dir: str,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Stow method."""
    src_file = os.path.join(install_pkg_dir, rel_file)
    system_target = resolve_system_target(rel_file, target_dir)
    create_symlink_manually_with_sudo(src_file, system_target, sudo)


def deploy_single_copy_file(
    rel_file: str,
    install_pkg_dir: str,
    target_dir: str,
    sudo: bool
) -> None:
    """Helper to deploy a single file using Copy method."""
    src_file = os.path.join(install_pkg_dir, rel_file)
    system_target = resolve_system_target(rel_file, target_dir)
    copy_file_contents_with_sudo(src_file, system_target, sudo)


def delete_single_system_file(
    rel_file: str,
    target_dir: str,
    install_method: str,
    sudo: bool
) -> None:
    """Helper to delete a single file on host system (for incremental deletions)."""
    system_target = resolve_system_target(rel_file, target_dir)
    if install_method == "stow":
        if os.path.islink(system_target):
            cmd_rm = ["rm", "-f", system_target]
            if sudo:
                cmd_rm.insert(0, "sudo")
            subprocess.run(cmd_rm, check=True, capture_output=True)
    elif install_method == "copy":
        if os.path.lexists(system_target):
            cmd_rm = ["rm", "-rf" if os.path.isdir(system_target) else "-f", system_target]
            if sudo:
                cmd_rm.insert(0, "sudo")
            subprocess.run(cmd_rm, check=True, capture_output=True)


def deploy_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: str,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> None:
    """Core function to deploy a single package configuration."""
    install_base = os.path.join(workspace_config.drift_root_path, workspace_config.install_directory)
    
    metadata = load_config_for_install(install_base, pkg)
    if not (force or metadata.enable_install):
        logger.info(f"Skipping package '{pkg}' during deployment (enable_install is False).")
        return
        
    target_dir = metadata.target_directory or workspace_config.default_target_directory
    target_dir = os.path.expanduser(target_dir)
    
    # Check target folder writability
    ensure_directory_writable(target_dir, metadata.sudo)
    
    # Check if first time before setting state to deploying
    current_state = state_registry.get_package_state(pkg)
    is_first_time = (current_state is None)
    
    # Set package state to "deploying" before actual deployment
    state_registry.set_package_state(pkg, "deploying", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)
    
    install_pkg_dir = os.path.join(install_base, pkg)
    if not os.path.isdir(install_pkg_dir):
        logger.warning(f"Package installation directory '{install_pkg_dir}' does not exist. Skipping.")
        return
        
    logger.info(f"Deploying package configurations for '{pkg}'...")
    
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
    
    # 2. Physical Deployment Execution
    stow_version = get_stow_version() if metadata.install_method == "stow" else None
    stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False
    
    # Remove full_redeploy parameter and rely on package_changes to determine deployment mode
    full_redeploy = (package_changes is None)
    
    if full_redeploy:
        if metadata.install_method == "stow":
            if stow_sufficient:
                run_stow_deployment(install_base, target_dir, pkg, metadata.sudo, stow_sufficient)
            else:
                logger.warning("GNU Stow version is insufficient (< 2.4.1) or not installed. Falling back to manual symlinking.")
                for root, _, files in os.walk(install_pkg_dir):
                    for file in files:
                        rel_file = os.path.relpath(os.path.join(root, file), install_pkg_dir)
                        if rel_file in IGNORED_FILENAMES or ignore_handler.match_path(rel_file):
                            continue
                        deploy_single_stow_file(
                            rel_file=rel_file,
                            install_pkg_dir=install_pkg_dir,
                            target_dir=target_dir,
                            sudo=metadata.sudo
                        )
        elif metadata.install_method == "copy":
            run_full_copy_deployment(install_pkg_dir, target_dir, metadata.sudo)
            
    else:
        # Incremental Deployment (Only files from Primitive 4 output)
        if package_changes:
            # A. Process Deletions on active host system
            for rel_file in package_changes.deleted_files:
                delete_single_system_file(rel_file, target_dir, metadata.install_method, metadata.sudo)

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
                    
    # 3. Lifecycle Hooks & State registry update
    if is_first_time:
        trigger_package_lifecycle_hook(pkg, "on_install", metadata, workspace_config)
    else:
        trigger_package_lifecycle_hook(pkg, "on_update", metadata, workspace_config)
        
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)


def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    packages_to_redeploy: List[str],
    resolve_symlinks: bool = True,
    force: bool = False,
    package_changes: Optional[List[PackageStageChanges]] = None
) -> None:
    """Applies changes from the install/ state database to the active host system (Primitive 5)."""
    install_base = os.path.join(workspace_config.drift_root_path, workspace_config.install_directory)
    state_file = os.path.join(install_base, "state.toml")
    
    state_registry = load_state_registry(state_file)
    
    for pkg in packages_to_redeploy:
        pkg_change = None
        # find corresponding PackageStageChanges for this package if provided
        if package_changes:
            for c in package_changes:
                if c.package_name == pkg:
                    pkg_change = c
                    break
                    
        deploy_package(
            workspace_config=workspace_config,
            pkg=pkg,
            state_registry=state_registry,
            state_file=state_file,
            resolve_symlinks=resolve_symlinks,
            force=force,
            package_changes=pkg_change
        )
