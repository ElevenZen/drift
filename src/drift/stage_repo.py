"""Feature implementation for staging render sandbox into install state database using pathlib."""

import datetime
import shutil
import logging
from pathlib import Path
from typing import List, Union, Optional
from dataclasses import dataclass, field

from .constants import PACKAGE_CONFIG_FILE_NAME, MANAGED_CONFIG_FILES, DRIFT_IGNORE_FILE_NAME
from .workspace_config import WorkspaceConfig
from .package_config import load_package_config_rendered, PackageConfig
from .file_utils import file_contents_differ, backup_and_delete_one_file, remove_file_or_dir
from .folder_diff import compare_folders
from .ignore import DriftIgnore
from .git_utils import has_uncommitted_modifications
from .state_registry import load_state_registry, save_state_registry

logger = logging.getLogger(__name__)


@dataclass
class PackageStageChanges:
    """Represents staging changes for a single package."""
    package_name: str
    added_files: List[Path] = field(default_factory=list)
    modified_files: List[Path] = field(default_factory=list)
    deleted_files: List[Path] = field(default_factory=list)


def ensure_install_pkg_dir_clean(install_base: Path, pkg: str) -> None:
    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
        return
    if has_uncommitted_modifications(install_base, install_pkg_dir):
        raise RuntimeError(
            f"Package '{pkg}' in install directory has uncommitted local modifications. "
            "Please commit or stash your changes before staging, or use --force flag to bypass this check."
        )


def load_config_from_render(render_base: Path, pkg: str, force: bool = False) -> PackageConfig:
    try:
        # the drift_package.toml should exist as a static package config.
        config_file = render_base / pkg / PACKAGE_CONFIG_FILE_NAME
        if not config_file.exists():
            raise RuntimeError(f"Failed to find drift_package.toml for '{pkg}' in render sandbox")
        else:
            metadata = load_package_config_rendered(config_file)
        return metadata
    except Exception as e:
        if not force:
            raise RuntimeError(f"Failed to load package configuration for '{pkg}' from render sandbox: {e}")
        logger.warning(f"Config load failed for '{pkg}' in render sandbox, but proceeding due to --force: {e}")
        metadata = PackageConfig(pkg)
        metadata.enable_render = True
        metadata.enable_install = True
        return metadata


def create_stow_ignore_file(install_pkg_dir: Path, render_ignore_path: Optional[Path]) -> None:
    """Copies render's .drift_ignore to install's .stow-local-ignore (if present),

    and appends '^/.drift_ignore' and '^/drift_package.toml' to it so Stow ignores them during stowing.
    """
    stow_ignore_path = install_pkg_dir / ".stow-local-ignore"
    
    # Overwrite/remove if exists and is directory or symlink
    if stow_ignore_path.exists() or stow_ignore_path.is_symlink():
        if stow_ignore_path.is_dir() and not stow_ignore_path.is_symlink():
            shutil.rmtree(stow_ignore_path)
        else:
            stow_ignore_path.unlink()
            
    install_pkg_dir.mkdir(parents=True, exist_ok=True)
    if render_ignore_path and render_ignore_path.is_file():
        shutil.copy2(render_ignore_path, stow_ignore_path)
    else:
        stow_ignore_path.write_text("", encoding="utf-8")
    
    # Read and append patterns to .stow-local-ignore
    content = stow_ignore_path.read_text(encoding="utf-8")
            
    # Check lines to prevent substring false positives
    lines = [line.strip() for line in content.splitlines()]
    
    append_patterns = [f"^/{DRIFT_IGNORE_FILE_NAME}", f"^/{PACKAGE_CONFIG_FILE_NAME}"]
    to_append = []
    for pattern in append_patterns:
        if pattern not in lines:
            to_append.append(pattern)
            
    if to_append:
        with stow_ignore_path.open("a", encoding="utf-8") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            for pattern in to_append:
                f.write(f"{pattern}\n")
    logger.debug(f"📝 Created/updated Stow ignore file at {stow_ignore_path} with patterns: {append_patterns}")


def process_package_changes(
    pkg: str,
    install_base: Path,
    render_base: Path,
    backup_base: Path,
    changes: PackageStageChanges
) -> None:
    """Processes deletions, additions, and modifications for a single package using FolderDiff.
    
    1. Runs compare_folders with drift_ignore to calculate deployable file changes for the return value.
    2. Runs compare_folders without drift_ignore to stage ALL physical files into install/ (including hooks).
    """
    install_pkg_dir = install_base / pkg
    render_pkg_dir = render_base / pkg
    backup_dir = backup_base / pkg / "deleted_files"

    if not render_pkg_dir.exists():
        raise RuntimeError(f"Render sandbox directory for package '{pkg}' does not exist. Please render first.")

    ignore_handler = DriftIgnore.load_from_dir(render_pkg_dir)

    # 1. Compute deployable changes with ignore_handler for the function output
    deploy_diff = compare_folders(
        src_dir=render_pkg_dir,
        dst_dir=install_pkg_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=True
    )
    for rel_file in deploy_diff.deleted:
        if rel_file.name not in MANAGED_CONFIG_FILES:
            changes.deleted_files.append(rel_file)
    for rel_file in deploy_diff.added:
        if rel_file.name not in MANAGED_CONFIG_FILES:
            changes.added_files.append(rel_file)
    for rel_file in deploy_diff.modified:
        if rel_file.name not in MANAGED_CONFIG_FILES:
            changes.modified_files.append(rel_file)

    # 2. Compute all physical file changes without ignore_handler to stage everything into install/
    all_diff = compare_folders(
        src_dir=render_pkg_dir,
        dst_dir=install_pkg_dir,
        ignore_handler=None,
        resolve_symlinks=True
    )

    # A. Process Deletions
    for rel_file in all_diff.deleted:
        if rel_file.name in MANAGED_CONFIG_FILES:
            continue
        install_file = install_pkg_dir / rel_file
        if install_file.is_dir() and not install_file.is_symlink():
            remove_file_or_dir(install_file)
            continue
        backup_file = backup_dir / rel_file
        logger.info(f"🗑️  Deleting: {pkg}/{rel_file}")
        logger.debug(f"   (Backup: {backup_file})")
        backup_and_delete_one_file(install_file, backup_file, limit_dir=install_pkg_dir)

    # B. Process Additions
    for rel_file in all_diff.added:
        src = render_pkg_dir / rel_file
        dst = install_pkg_dir / rel_file
        if src.is_dir() and not src.is_symlink():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        logger.info(f"📦 Adding: {pkg}/{rel_file}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # C. Process Modifications
    for rel_file in all_diff.modified:
        src = render_pkg_dir / rel_file
        dst = install_pkg_dir / rel_file
        if src.is_dir() and not src.is_symlink():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        logger.info(f"🔄 Modifying: {pkg}/{rel_file}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Copy ignore and config files (handles .stow-local-ignore and drift_package.toml)
    copy_ignore_and_config_files(install_pkg_dir, render_pkg_dir)


def copy_ignore_and_config_files(install_pkg_dir: Path, render_pkg_dir: Path) -> None:
    """Copies ignore and package config files from render/ to install/ and sets up Stow ignores."""
    # 1. Copy the physical .drift_ignore file to install/pkg dir if it was rendered in render/
    render_ignore = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
    if render_ignore.is_file():
        install_ignore = install_pkg_dir / DRIFT_IGNORE_FILE_NAME
        install_pkg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(render_ignore, install_ignore)
        # Create physical .stow-local-ignore and append exclusion pattern
        create_stow_ignore_file(install_pkg_dir, render_ignore)
    else:
        # Create stow-local-ignore even if no .drift_ignore exists to ignore drift_package.toml
        create_stow_ignore_file(install_pkg_dir, None)

    # 2. Copy the drift_package.toml to install/pkg dir, this file must exist or an Error will be raised.
    render_config = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not render_config.is_file():
        raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in render sandbox of package.")
        
    install_config = install_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    install_pkg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(render_config, install_config)


def run_primitive_4_stage_render_to_install(
    workspace_config: WorkspaceConfig,
    target_pkgs: Optional[Union[str, List[str]]] = None,
    force: bool = False
) -> List[PackageStageChanges]:
    """Reconciles the sandbox render/ folder into the install/ database (Primitive 4).

    Returns:
        A list of PackageStageChanges objects representing package changes.
    """
    if isinstance(target_pkgs, str):
        target_pkgs = [target_pkgs]

    # Load active packages
    active_packages = workspace_config.get_discovered_packages(
        custom_dir=workspace_config.render_path,
        target_pkgs=target_pkgs
    )

    # If active_packages is empty, we should just return empty lists and not proceed further.
    if not active_packages:
        logger.info("No active packages selected for staging. Skipping.")
        return []

    render_base = workspace_config.render_path
    install_base = workspace_config.install_path
    backup_base = workspace_config.backup_path

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

    logger.info(f"🔍 Staging {len(pkg_metadata)} packages: {', '.join(pkg_metadata.keys())}")

    # Set state of packages to "staging" before staging to prevent partial staging issues
    state_file = install_base / "state.toml"
    state_registry = load_state_registry(state_file)
    for pkg in pkg_metadata.keys():
        metadata = pkg_metadata[pkg]
        
        current_state = state_registry.get_package_state(pkg)
        if not force and current_state in ("staging", "deploying"):
            raise RuntimeError(
                f"Safety Abort: Package '{pkg}' is currently in '{current_state}' state, "
                f"indicating a previous operation failed midway. "
                f"Please run 'drift rollback {pkg}' to restore a clean state before retrying."
            )
            
        state_registry.set_package_state(pkg, "staging", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)

    pkg_changes = {pkg: PackageStageChanges(package_name=pkg) for pkg in pkg_metadata.keys()}

    # 2. Process deletions, additions, and modifications in a single unified step
    for pkg, metadata in pkg_metadata.items():
        process_package_changes(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
            backup_base=backup_base,
            changes=pkg_changes[pkg]
        )

    pkg_changes_with_actual_changes = [
            change for change in pkg_changes.values()
            if change.added_files or change.modified_files or change.deleted_files]
    
    if pkg_changes_with_actual_changes:
        logger.info("✨ Staging completed. Summary of changes:")
        for pkg_change in pkg_changes_with_actual_changes:
            logger.info(f"   Package '{pkg_change.package_name}': "
                        f"+{len(pkg_change.added_files)}, "
                        f"~{len(pkg_change.modified_files)}, "
                        f"-{len(pkg_change.deleted_files)}")
    else:
        logger.info("✨ Staging completed. No changes detected.")

    # Set state of packages to "staged" after successful staging
    for pkg in pkg_metadata.keys():
        state_registry.set_package_state(pkg, "staged")
    save_state_registry(state_file, state_registry)

    # Return only the packages that have actual changes
    return pkg_changes_with_actual_changes
