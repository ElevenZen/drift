"""Feature implementation for staging render sandbox into install state database."""

import os
import shutil
import logging
from typing import List, Union, Optional
from dataclasses import dataclass, field

from .constants import PACKAGE_CONFIG_FILE_NAME, IGNORED_FILENAMES
from .workspace_config import WorkspaceConfig
from .package_config import load_package_config_static, PackageConfig
from .file_utils import tree_relative_files, file_contents_differ, backup_and_delete_file
from .ignore import DriftIgnore
from .check_repo import has_uncommitted_modifications
from .state_registry import load_state_registry, save_state_registry

logger = logging.getLogger(__name__)


@dataclass
class PackageStageChanges:
    """Represents staging changes for a single package."""
    package_name: str
    added_files: List[str] = field(default_factory=list)
    modified_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)


def load_active_packages(
    discovered: List[str],
    target_pkgs: Optional[Union[str, List[str]]],
    workspace_config: WorkspaceConfig,
    force: bool = False
) -> List[str]:
    """Initializes active packages for staging.

    Raises:
        ValueError if any target package is not discovered (unless force is True).
    """
    if target_pkgs is not None:
        if isinstance(target_pkgs, str):
            target_pkgs = [target_pkgs]
            
        active_packages = []
        for pkg in target_pkgs:
            if pkg in discovered or force:
                active_packages.append(pkg)
            else:
                raise ValueError(
                    f"Target package '{pkg}' was not discovered in render directory '{workspace_config.render_directory}'. "
                    "Use --force flag to force target_pkg processing."
                )
        return active_packages

    active_packages = []
    for pkg in discovered:
        if workspace_config.is_package_enabled(pkg):
            active_packages.append(pkg)
    return active_packages


def create_stow_ignore_file(install_pkg_dir: str, render_ignore_path: str) -> None:
    """Copies render's .drift_ignore to install's .stow-local-ignore,
    and appends '^/.drift_ignore' to it so Stow ignores it during stowing.
    """
    stow_ignore_path = os.path.join(install_pkg_dir, ".stow-local-ignore")
    
    # Overwrite/remove if exists and is directory or symlink
    if os.path.lexists(stow_ignore_path):
        if os.path.isdir(stow_ignore_path) and not os.path.islink(stow_ignore_path):
            shutil.rmtree(stow_ignore_path)
        else:
            os.remove(stow_ignore_path)
            
    os.makedirs(install_pkg_dir, exist_ok=True)
    shutil.copy2(render_ignore_path, stow_ignore_path)
    
    # Read and append "^/.drift_ignore" to .stow-local-ignore
    content = ""
    if os.path.exists(stow_ignore_path):
        with open(stow_ignore_path, "r", encoding="utf-8") as f:
            content = f.read()
            
    # Check lines to prevent substring false positives
    lines = [line.strip() for line in content.splitlines()]
    if "^/.drift_ignore" not in lines:
        with open(stow_ignore_path, "a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write("^/.drift_ignore\n")
    logger.info(f"Created Stow ignore copy and appended '^/.drift_ignore' at {stow_ignore_path}")


def process_package_deletions(
    pkg: str,
    install_base: str,
    render_base: str,
    backup_base: str,
    changes: PackageStageChanges
) -> None:
    """Processes deletions for a single package (present in install/ but missing in render/)."""
    install_pkg_dir = os.path.join(install_base, pkg)
    render_pkg_dir = os.path.join(render_base, pkg)
    backup_dir = os.path.join(backup_base, pkg, "deleted_files")

    # Load ignore patterns from RENDER directory only.
    ignore_handler = DriftIgnore.load_from_dir(render_pkg_dir)

    if os.path.exists(install_pkg_dir) and os.path.isdir(install_pkg_dir):
        relative_install_files = tree_relative_files(install_pkg_dir)
        for rel_file in relative_install_files:
            # Skip if it is an internal system metadata file, or it will be matched in ignore_handler.
            if os.path.basename(rel_file) in IGNORED_FILENAMES:
                continue

            # Check if it should be deleted (either because it is ignored under .drift_ignore now, OR missing in render/)
            is_deleted = False
            if ignore_handler.match_path(rel_file):
                is_deleted = True
            else:
                render_file = os.path.join(render_pkg_dir, rel_file)
                if not os.path.exists(render_file):
                    is_deleted = True
            
            if is_deleted:
                install_file = os.path.join(install_pkg_dir, rel_file)
                backup_file = os.path.join(backup_dir, rel_file)
                logger.info(f"Moving deleted file to backup: {backup_file}")
                backup_and_delete_file(install_file, backup_file, limit_dir=install_pkg_dir)
                changes.deleted_files.append(rel_file)


def copy_ignore_and_config_files(install_pkg_dir: str, render_pkg_dir: str) -> None:
    """Copies ignore and package config files from render/ to install/ and sets up Stow ignores."""
    # 1. Copy the physical .drift_ignore file to install/pkg dir if it was rendered in render/
    render_ignore = os.path.join(render_pkg_dir, ".drift_ignore")
    if os.path.isfile(render_ignore):
        install_ignore = os.path.join(install_pkg_dir, ".drift_ignore")
        os.makedirs(install_pkg_dir, exist_ok=True)
        shutil.copy2(render_ignore, install_ignore)
        # Create physical .stow-local-ignore and append exclusion pattern
        create_stow_ignore_file(install_pkg_dir, render_ignore)

    # 2. Copy the drift_package.toml to install/pkg dir, this file must exist or an Error will be raised.
    render_config = os.path.join(render_pkg_dir, PACKAGE_CONFIG_FILE_NAME)
    if not os.path.isfile(render_config):
        raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in render sandbox of package.")
        
    install_config = os.path.join(install_pkg_dir, PACKAGE_CONFIG_FILE_NAME)
    os.makedirs(install_pkg_dir, exist_ok=True)
    shutil.copy2(render_config, install_config)


def process_package_additions_modifications(
    pkg: str,
    install_base: str,
    render_base: str,
    changes: PackageStageChanges
) -> None:
    """Processes additions and modifications for a single package (present in render/ but missing/different in install/)."""
    install_pkg_dir = os.path.join(install_base, pkg)
    render_pkg_dir = os.path.join(render_base, pkg)
    
    # If the render directory doesn't exist, raise an Error because the package has no render output
    if not os.path.exists(render_pkg_dir):
        raise RuntimeError(f"Render sandbox directory for package '{pkg}' does not exist. Please render first.")

    ignore_handler = DriftIgnore.load_from_dir(render_pkg_dir)

    if os.path.exists(render_pkg_dir) and os.path.isdir(render_pkg_dir):
        relative_render_files = tree_relative_files(render_pkg_dir)
        for rel_file in relative_render_files:
            if ignore_handler.match_path(rel_file):
                continue
            
            src = os.path.join(render_pkg_dir, rel_file)
            dst = os.path.join(install_pkg_dir, rel_file)
            
            if not os.path.exists(dst):
                logger.info(f"Adding new file: {pkg}/{rel_file}")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                changes.added_files.append(rel_file)
            elif file_contents_differ(src, dst):
                logger.info(f"Modifying file: {pkg}/{rel_file}")
                shutil.copy2(src, dst)
                changes.modified_files.append(rel_file)

    # Extract copying and configuring internal files to a separate function
    copy_ignore_and_config_files(install_pkg_dir, render_pkg_dir)


def load_config_from_render(render_base: str, pkg: str, force: bool = False) -> PackageConfig:
    pkg_render_dir = os.path.join(render_base, pkg)
    try:
        # the drift_package.toml should exist as a static package config.
        config_file = os.path.join(pkg_render_dir, PACKAGE_CONFIG_FILE_NAME)
        if not os.path.exists(config_file):
            raise RuntimeError(f"Failed to find drift_package.toml for '{pkg}' in render sandbox");
        else:
            metadata = load_package_config_static(config_file, default_name=pkg)
        return metadata

    except Exception as e:
        if not force:
            raise RuntimeError(f"Failed to load package configuration for '{pkg}' from render sandbox: {e}")
        logger.warning(f"Skipping package '{pkg}' during staging as config loading failed (force enabled): {e}")
        metadata = PackageConfig(pkg)
        metadata.enable_render = True
        metadata.enable_install = True
        return metadata


def ensure_install_pkg_dir_clean(install_base: str, pkg: str) -> None:
    install_pkg_dir = os.path.join(install_base, pkg)
    if not os.path.isdir(install_pkg_dir):
        return
    if has_uncommitted_modifications(install_base, install_pkg_dir):
        raise RuntimeError(
            f"Package '{pkg}' in install directory has uncommitted local modifications. "
            "Please commit or stash your changes before staging, or use --force flag to bypass this check."
        )


def run_primitive_4_stage_render_to_install(
    workspace_config: WorkspaceConfig,
    target_pkgs: Optional[Union[str, List[str]]] = None,
    force: bool = False
) -> List[PackageStageChanges]:
    """Reconciles the sandbox render/ folder into the install/ database (Primitive 4).

    Returns:
        A list of PackageStageChanges objects representing package changes.
    """
    discovered = workspace_config.get_package_names_from_render_dir()
    
    # Load active packages
    active_packages = load_active_packages(
        discovered=discovered,
        target_pkgs=target_pkgs,
        workspace_config=workspace_config,
        force=force
    )

    # If active_packages is empty, we should just return empty lists and not proceed further.
    if not active_packages:
        logger.info("No active packages selected for staging. Skipping.")
        return []

    render_base = os.path.join(workspace_config.drift_root_path, workspace_config.render_directory)
    install_base = os.path.join(workspace_config.drift_root_path, workspace_config.install_directory)
    backup_base = os.path.join(workspace_config.drift_root_path, workspace_config.backup_directory)

    # 1. First find all active packages to process and load their metadata from RENDER directory
    # Filter out packages that are not enabled for installation/deployment.
    pkg_metadata = {}
    for pkg in active_packages:
        metadata = load_config_from_render(render_base, pkg, force=force)
        if not (force or metadata.enable_install):
            continue
        pkg_metadata[pkg] = metadata

    # Check if active_packages is empty after filtering by enable_install, and if so, raise an error
    if not pkg_metadata:
        raise RuntimeError("No active packages are enabled for installation/deployment.")

    # Check every package folder in install/ if it has uncommitted local modifications.
    # If so and the force flag is not present, raise an Error.
    if not force:
        for pkg in pkg_metadata.keys():
            ensure_install_pkg_dir_clean(install_base, pkg)

    logger.info(f"Staging the following packages from render/ to install/: {list(pkg_metadata.keys())}")

    # Set state of packages to "deploying" before staging to prevent partial staging issues
    state_file = os.path.join(install_base, "state.toml")
    state_registry = load_state_registry(state_file)
    for pkg in pkg_metadata.keys():
        metadata = pkg_metadata[pkg]
        state_registry.set_package_state(pkg, "deploying", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)

    pkg_changes = {pkg: PackageStageChanges(package_name=pkg) for pkg in pkg_metadata.keys()}

    # 2. Process Deletions
    for pkg, metadata in pkg_metadata.items():
        process_package_deletions(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
            backup_base=backup_base,
            changes=pkg_changes[pkg]
        )

    # 3. Process Additions and Modifications
    for pkg, metadata in pkg_metadata.items():
        process_package_additions_modifications(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
            changes=pkg_changes[pkg]
        )

    pkg_changes_with_actual_changes = [
            change for change in pkg_changes.values()
            if change.added_files or change.modified_files or change.deleted_files]
    logger.info("Staging completed. Summary of changes:")
    for pkg_change in pkg_changes_with_actual_changes:
        logger.info(f"Package '{pkg_change.package_name}': Added: {len(pkg_change.added_files)}, Modified: {len(pkg_change.modified_files)}, Deleted: {len(pkg_change.deleted_files)}")

    # Set state of packages to "installed" after successful staging
    for pkg in pkg_metadata.keys():
        metadata = pkg_metadata[pkg]
        import datetime
        now_str = datetime.datetime.now().isoformat()
        state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)

    # Return only the packages that have actual changes
    return pkg_changes_with_actual_changes
