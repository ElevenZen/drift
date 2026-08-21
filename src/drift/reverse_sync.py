"""Primitive 1 Reverse Sync operation (System -> install/ state database)."""

import logging
from pathlib import Path
from typing import List, Optional, Union

from .workspace_config import WorkspaceConfig
from .constants import MANAGED_CONFIG_FILES
from .ignore import DriftIgnore
from .file_utils import (
    is_relative_to,
    resolve_system_target,
    translate_dot_prefixes_reverse,
    remove_file_or_dir,
)
from .folder_diff import compare_folders
from .sync_ops import reverse_sync_file_or_dir
from .install_repo import load_config_for_install

logger = logging.getLogger(__name__)


def reverse_sync_package(pkg: str, install_base: Path, workspace_config: WorkspaceConfig) -> None:
    """Performs the reverse sync process for a single package."""
    install_pkg_dir = install_base / pkg
    try:
        metadata = load_config_for_install(install_base, pkg)
    except Exception as e:
        logger.warning(f"Skipping package '{pkg}' during reverse sync: {e}")
        return

    if not metadata.enable_install:
        logger.info(f"Reverse sync is disabled for package '{pkg}' (enable_install = false). Skipping.")
        return

    target_dir_path = metadata.get_target_directory(workspace_config)
    assert target_dir_path.is_absolute(), f"Target directory '{target_dir_path}' for package '{pkg}' must be an absolute path."
    if not target_dir_path.exists():
        logger.warning(f"Target directory '{target_dir_path}' for package '{pkg}' does not exist. Skipping reverse sync.")
        return

    # Load ignore patterns
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

    # Use compare_folders to detect all changes (modifications and wild files in FCD)
    # We compare System (src) against Repo (dst) in "reverse" translation mode.
    # This identifies:
    # - added: files on system NOT in repo (potential wild files for FCD)
    # - modified: files on system that differ from repo (including symlink vs physical mismatches)
    # - deleted: files in repo NOT on system (tracked files deleted manually)
    diff = compare_folders(
        src_dir=target_dir_path,
        dst_dir=install_pkg_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=True,
        translate_mode="reverse"
    )

    # 1. Handle system deletions (deleted in system but exist in repo)
    for rel in diff.deleted:
        # diff returns rel relative to src_dir (system). 
        # But for deletions, we need to know what it corresponds to in repo.
        repo_rel = translate_dot_prefixes_reverse(rel)
        repo_file = install_pkg_dir / repo_rel
        
        # We only sync back deletions for files that are actually in the repo
        if repo_file.exists():
            logger.info(f"System Deletion: '{target_dir_path / rel}' is missing. Deleting counterpart '{repo_file}' from install/...")
            remove_file_or_dir(repo_file)

    # 2. Handle system modifications
    for rel in diff.modified:
        system_file = target_dir_path / rel
        repo_file = install_pkg_dir / translate_dot_prefixes_reverse(rel)
        reverse_sync_file_or_dir(system_file, repo_file, ignore_handler=ignore_handler)

    # 3. Handle wild files in FCD and promoted tracked files (added on system but not in repo)
    # We sync these if:
    # a) They are within a Fully-Controlled Directory (FCD).
    # b) They are part of a tracked path that changed type (e.g. file became a directory).
    for rel in diff.added:
        if rel.name in MANAGED_CONFIG_FILES:
            continue

        is_to_sync = False
        # Case a: Check if rel is inside any FCD
        for fcd_rel in metadata.fully_controlled_dirs:
            if is_relative_to(rel, fcd_rel):
                is_to_sync = True
                break
        
        if not is_to_sync:
            # Case b: Check if any parent (or the path itself) was previously tracked
            # but is now reported as 'deleted' (meaning it's missing or changed type on system).
            for parent in [rel] + list(rel.parents):
                if parent in diff.deleted:
                    is_to_sync = True
                    break
        
        if is_to_sync:
            system_file = target_dir_path / rel
            repo_file = install_pkg_dir / translate_dot_prefixes_reverse(rel)
            reverse_sync_file_or_dir(system_file, repo_file, ignore_handler=ignore_handler)


def run_primitive_1_reverse_sync(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None
) -> None:
    """Unconditionally pulls configuration state from host system back to the install/ repository (Primitive 1)."""
    install_base = workspace_config.install_path
    if not install_base.exists():
        logger.warning(f"Install state database directory '{install_base}' does not exist. Skipping reverse sync.")
        return

    # Determine packages to process
    discovered_packages = workspace_config.get_discovered_packages(
        custom_dir=install_base,
        target_pkgs=package_names,
    )

    for pkg in discovered_packages:
        reverse_sync_package(pkg, install_base, workspace_config)
