"""Core physical file deployment and host installation operations using pathlib."""

import os
import re
import logging
import subprocess
import datetime
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .package_config import PackageConfig, load_package_config_static
from .constants import PACKAGE_CONFIG_FILE_NAME, IGNORED_FILENAMES
from .ignore import DriftIgnore
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .stage_repo import PackageStageChanges
from .file_utils import tree_relative_files, _is_relative_to

logger = logging.getLogger(__name__)


def ensure_directory_writable(path: Path, sudo: bool) -> None:
    """Checks if a directory path (or its closest existing parent) is writable."""
    if sudo:
        return  # With sudo, we assume target is writable or handled by elevation
    curr = path.resolve()
    while curr:
        if curr.exists():
            if os.access(curr, os.W_OK):
                return
            else:
                raise PermissionError(
                    f"Directory '{curr}' is not writable. "
                    "Please check permissions or configure sudo for this package."
                )
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    raise PermissionError(f"Target directory path '{path}' is invalid or inaccessible.")


def resolve_system_target(relative_path: Path, target_dir: Path) -> Path:
    """Resolves relative file path in install/ folder to system target path, applying dot prefix conversion."""
    target_path = target_dir.expanduser()
    translated_parts = ["." + p[4:] if p.startswith("dot-") else p for p in relative_path.parts]
    return target_path.joinpath(*translated_parts)


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


def get_symlinked_parent(system_target: Path, drift_root: Path) -> Optional[Path]:
    """Returns the symlinked parent directory of system_target if it is a symlink pointing into drift_root."""
    parent_dir = system_target.parent
    home_dir = Path.home()
    p_drift_root = drift_root.resolve()
    while parent_dir and parent_dir != Path("/") and parent_dir != home_dir:
        if parent_dir.is_symlink():
            try:
                link_target = parent_dir.readlink()
                if link_target.is_absolute():
                    abs_link = link_target.resolve()
                else:
                    abs_link = (parent_dir.parent / link_target).resolve()
                
                if _is_relative_to(abs_link, p_drift_root):
                    return parent_dir
            except Exception:
                pass
        # iterate up the directory tree
        parent = parent_dir.parent
        if parent == parent_dir:
            break
        parent_dir = parent
    return None


def is_stow_linked_parent(system_target: Path, drift_root: Path) -> bool:
    """Repo Pollution And Infinite Loop Protection: checks if any parent directory of system_target is a symlink into drift_root."""
    return get_symlinked_parent(system_target, drift_root) is not None


def ensure_dir_exists_with_sudo(path: Path, sudo: bool) -> None:
    """Ensures directory exists, creating with sudo if requested."""
    if path.exists():
        return
    if sudo:
        subprocess.run(["sudo", "mkdir", "-p", str(path)], check=True, capture_output=True)
    else:
        path.mkdir(parents=True, exist_ok=True)


def create_symlink_manually_with_sudo(src: Path, dst: Path, sudo: bool) -> None:
    """Creates a symlink from src to dst manually, cleaning up existing file/link with sudo if requested."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    
    if dst.exists() or dst.is_symlink():
        cmd_rm = ["rm", "-rf" if dst.is_dir() and not dst.is_symlink() else "-f", str(dst)]
        if sudo:
            cmd_rm.insert(0, "sudo")
        subprocess.run(cmd_rm, check=True, capture_output=True)
        
    cmd = ["ln", "-s", str(src), str(dst)]
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True, capture_output=True)


def copy_file_contents_with_sudo(src: Path, dst: Path, sudo: bool) -> None:
    """Copies a physical file from src to dst, with sudo if requested."""
    ensure_dir_exists_with_sudo(dst.parent, sudo)
    
    if dst.exists() or dst.is_symlink():
        cmd_rm = ["rm", "-rf" if dst.is_dir() and not dst.is_symlink() else "-f", str(dst)]
        if sudo:
            cmd_rm.insert(0, "sudo")
        subprocess.run(cmd_rm, check=True, capture_output=True)
        
    cmd = ["cp", str(src), str(dst)]
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True, capture_output=True)


def copy_or_move_file(
    src: Path,
    dst: Path,
    sudo: bool,
    move: bool = False,
    resolve_symlinks: bool = True
) -> None:
    """Backs up or copies file/directory, using sudo if requested and resolving symlinks recursively if resolve_symlinks is True."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    
    if move and not resolve_symlinks:
        cmd = ["mv", str(src), str(dst)]
    else:
        if src.is_dir():
            cmd = ["cp", "-RP" if not resolve_symlinks else "-RL", str(src), str(dst)]
        else:
            cmd = ["cp", "-P" if not resolve_symlinks else "-L", str(src), str(dst)]
            
    if sudo:
        cmd.insert(0, "sudo")
        
    subprocess.run(cmd, check=True, capture_output=True)
    
    if sudo:
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid is not None and gid is not None:
                chown_cmd = ["sudo", "chown", "-R", f"{uid}:{gid}", str(dst)]
                subprocess.run(chown_cmd, check=True, capture_output=True)
        except Exception as e:
            logger.warning(f"Failed to chown backup to process owner: {e}")
            
    if move:
        del_cmd = ["rm", "-rf", str(src)]
        if sudo:
            del_cmd.insert(0, "sudo")
        subprocess.run(del_cmd, check=True, capture_output=True)


def backup_file_or_dir(src: Path, backup_dest: Path, sudo: bool, resolve_symlinks: bool = True) -> None:
    """Recursively backs up target src to backup_dest, resolving symlinks if resolve_symlinks is True."""
    if not src.exists() and not src.is_symlink():
        return
        
    if src.is_symlink():
        if resolve_symlinks:
            real_target = src.resolve()
            if real_target.exists():
                backup_file_or_dir(real_target, backup_dest, sudo, resolve_symlinks=True)
        else:
            copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=False)
            
    elif src.is_dir():
        if resolve_symlinks:
            backup_dest.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                backup_file_or_dir(item, backup_dest / item.name, sudo, resolve_symlinks=True)
            # Remove original dir
            del_cmd = ["rm", "-rf", str(src)]
            if sudo:
                del_cmd.insert(0, "sudo")
            subprocess.run(del_cmd, check=True, capture_output=True)
        else:
            copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=False)
    else:
        # then it's a normal file.
        copy_or_move_file(src, backup_dest, sudo, move=True, resolve_symlinks=resolve_symlinks)


def run_full_copy_deployment(src_pkg_dir: Path, target_dir: Path, sudo: bool) -> None:
    """Runs high-level copying deployment using rsync if available, otherwise cp -r."""
    ensure_dir_exists_with_sudo(target_dir, sudo)
    
    rsync_cmd = ["rsync", "-av", str(src_pkg_dir) + "/", str(target_dir) + "/"]
    if sudo:
        rsync_cmd.insert(0, "sudo")
        
    try:
        subprocess.run(rsync_cmd, check=True, capture_output=True)
        logger.info("Full copy deployment succeeded via rsync.")
        return
    except Exception as e:
        logger.warning(f"rsync failed or not available, falling back to cp: {e}")
        
    # cp -r fallback
    cp_cmd = ["cp", "-R", str(src_pkg_dir) + "/.", str(target_dir) + "/"]
    if sudo:
        cp_cmd.insert(0, "sudo")
    subprocess.run(cp_cmd, check=True, capture_output=True)
    
    # Prune config and stow metadata files
    for filename in IGNORED_FILENAMES:
        dest_ignored = target_dir / filename
        if dest_ignored.exists():
            rm_cmd = ["rm", "-rf", str(dest_ignored)]
            if sudo:
                rm_cmd.insert(0, "sudo")
            subprocess.run(rm_cmd, check=True, capture_output=True)


def load_config_for_install(install_base: Path, pkg: str) -> PackageConfig:
    """Loads package configuration strictly from the install/ base directory."""
    install_config_file = install_base / pkg / PACKAGE_CONFIG_FILE_NAME
    if install_config_file.exists():
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
        
    hook_path = workspace_config.install_path / pkg / hook_file
    if not hook_path.exists():
        logger.warning(f"Lifecycle hook file specified but not found: {hook_path}")
        return
        
    try:
        hook_path.chmod(0o755)
    except Exception:
        pass
        
    logger.info(f"Triggering lifecycle hook '{hook_name}' for package '{pkg}': {hook_path}")
    
    cmd = [str(hook_path)]
    if metadata.sudo:
        cmd.insert(0, "sudo")
        
    subprocess.run(cmd, check=True, cwd=str(hook_path.parent))


def handle_symlinked_parent_error(
    system_target: Path,
    pkg: str,
    target_dir: Path,
    workspace_config: WorkspaceConfig,
    sudo: bool,
    resolve_symlinks: bool = True
) -> None:
    """Treated as an error, backups the symlinked parent folder, removes it, and recreates it as a physical folder.

    Raises severe error if parent symlink lies outside package's own target directory.
    """
    parent_symlink = get_symlinked_parent(system_target, workspace_config.drift_root)
    if not parent_symlink:
        return
        
    # Enforce severe safety guard: only allow automatic repair if parent_symlink lies inside target_dir
    abs_parent = parent_symlink.absolute()
    abs_target = target_dir.resolve()
    if not _is_relative_to(abs_parent, abs_target):
        raise RuntimeError(
            f"Safety Abort: Parent directory '{parent_symlink}' is a symlink pointing into "
            f"drift workspace root '{workspace_config.drift_root}', but lies outside "
            f"the package target directory '{target_dir}'. Resolving this automatically is "
            f"unsafe and could permanently delete unrelated system paths. Please resolve manually."
        )
        
    # Maintain nested relative path structure in overwriting backups
    rel_parent = parent_symlink.relative_to(target_dir)
    backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_parent
    logger.warning(
        f"[RECOVERY] Symlinked parent directory error. "
        f"Backing up parent symlink '{parent_symlink}' to '{backup_path}'..."
    )
    backup_file_or_dir(parent_symlink, backup_path, sudo, resolve_symlinks=resolve_symlinks)
    
    # Remove parent symlink
    cmd_rm = ["rm", "-rf" if parent_symlink.is_dir() and not parent_symlink.is_symlink() else "-f", str(parent_symlink)]
    if sudo:
        cmd_rm.insert(0, "sudo")
    subprocess.run(cmd_rm, check=True, capture_output=True)
    
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
    
    # Check for symlinked parent directories inside workspace_config.drift_root and handle them as errors
    if is_stow_linked_parent(system_target, workspace_config.drift_root):
        handle_symlinked_parent_error(
            system_target=system_target,
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
                link_target = system_target.readlink()
                if link_target.is_absolute():
                    abs_link_target = link_target.resolve()
                else:
                    abs_link_target = (system_target.parent / link_target).resolve()
                
                if _is_relative_to(abs_link_target, workspace_config.drift_root.resolve()):
                    # Skip backup if symlink already points to the SAME package's install path
                    if _is_relative_to(abs_link_target, pkg_install_dir.resolve()):
                        return
                    # Otherwise, it points to a DIFFERENT package's install path, so treat as collision and backup!
            except Exception:
                # Readlink failed. Treat as collision, backup symlink itself without resolving its content
                logger.warning(f"Failed to read symlink target of '{system_target}'. Backing up symlink itself.")
                backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
                backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=False)
                return

        backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
        logger.warning(f"[COLLISION GUARD] Stow conflict at '{system_target}'. Backing up...")
        backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
            
    # Condition 2: Copy Mode - copy conflict on very first installation of this package
    elif metadata.install_method == "copy" and is_first_time:
        backup_path = workspace_config.backup_path / pkg / "overwritten" / rel_file
        logger.warning(f"[COLLISION GUARD] Copy conflict at '{system_target}' (first install). Backing up...")
        backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)


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
                logger.info(f"[CLEANUP] Ignored file '{rel_file}' exists at system target. Backing up and deleting.")
                backup_file_or_dir(system_target, backup_path, metadata.sudo, resolve_symlinks=resolve_symlinks)
                # Ensure the system target itself is removed (if it was a symlink and we resolved symlinks, the symlink is still there)
                if system_target.exists() or system_target.is_symlink():
                    cmd_rm = ["rm", "-rf" if system_target.is_dir() and not system_target.is_symlink() else "-f", str(system_target)]
                    if metadata.sudo:
                        cmd_rm.insert(0, "sudo")
                    subprocess.run(cmd_rm, check=True, capture_output=True)
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
        subprocess.run(stow_cmd, check=True, cwd=str(install_base), capture_output=True)
        logger.info("Full stow deployment succeeded via GNU Stow.")
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
    create_symlink_manually_with_sudo(src_file, system_target, sudo)


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
    install_method: str,
    sudo: bool
) -> None:
    """Helper to delete a single file on host system (for incremental deletions)."""
    system_target = resolve_system_target(rel_file, target_dir)
    if install_method == "stow":
        if system_target.is_symlink():
            cmd_rm = ["rm", "-f", str(system_target)]
            if sudo:
                cmd_rm.insert(0, "sudo")
            subprocess.run(cmd_rm, check=True, capture_output=True)
    elif install_method == "copy":
        if system_target.exists() or system_target.is_symlink():
            cmd_rm = ["rm", "-rf" if system_target.is_dir() and not system_target.is_symlink() else "-f", str(system_target)]
            if sudo:
                cmd_rm.insert(0, "sudo")
            subprocess.run(cmd_rm, check=True, capture_output=True)


def deploy_package(
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
    
    # Check target folder writability
    ensure_directory_writable(target_dir, metadata.sudo)
    
    # Check if first time before setting state to deploying
    current_state = state_registry.get_package_state(pkg)
    is_first_time = (current_state is None)
    
    # Set package state to "deploying" before actual deployment
    state_registry.set_package_state(pkg, "deploying", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)
    
    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
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
                relative_files = tree_relative_files(install_pkg_dir)
                for rel_file in relative_files:
                    if rel_file.name in IGNORED_FILENAMES or ignore_handler.match_path(rel_file):
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

    logger.info(f"File deployment for package '{pkg}' completed successfully, with install method '{metadata.install_method}'. Now updating state registry and triggering lifecycle hooks.")
                    
    # 3. Lifecycle Hooks & State registry update
    if is_first_time:
        trigger_package_lifecycle_hook(pkg, "on_install", metadata, workspace_config)
    else:
        trigger_package_lifecycle_hook(pkg, "on_update", metadata, workspace_config)
        
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)

    logger.info(f"Deployment of package '{pkg}' completed successfully")


def load_active_install_packages(
    discovered: List[str],
    target_pkgs: Optional[Union[str, List[str]]],
    workspace_config: WorkspaceConfig,
    force: bool = False
) -> List[str]:
    """Initializes active packages for installation / deployment from the install/ state database.

    Raises:
        ValueError if any target package is not discovered (unless force is True).
    """
    if not target_pkgs:
        # Fallback: redeploy all active packages currently inside install/ that are enabled in workspace config
        return [pkg for pkg in discovered if workspace_config.is_package_enabled(pkg)]

    if isinstance(target_pkgs, str):
        target_pkgs = [target_pkgs]
    # filter input target packages to only those that are discovered or force is True
    # otherwise raise an error for missing packages
    active_packages = []
    for pkg in target_pkgs:
        if pkg in discovered or force:
            active_packages.append(pkg)
        else:
            raise ValueError(
                f"Target package '{pkg}' was not discovered in install directory '{workspace_config.install_directory}'. "
                f"Use --force to force {pkg} deployment."
            )
    return active_packages



def run_primitive_5_install_deployment(
    workspace_config: WorkspaceConfig,
    packages_to_redeploy: Optional[Union[str, List[str]]] = None,
    resolve_symlinks: bool = True,
    force: bool = False,
    package_changes: Optional[List[PackageStageChanges]] = None
) -> None:
    """Applies changes from the install/ state database to the active host system (Primitive 5)."""
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    
    state_registry = load_state_registry(state_file)
    
    discovered = workspace_config.get_package_names_from_install_dir()
    resolved_packages = load_active_install_packages(
        discovered=discovered,
        target_pkgs=packages_to_redeploy,
        workspace_config=workspace_config,
        force=force
    )
    
    for pkg in resolved_packages:
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
