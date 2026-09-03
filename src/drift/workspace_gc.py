"""Primitive 9: Workspace Garbage Collection (Orphan Cleanup and Database Purge)."""

import logging
import shutil
from pathlib import Path
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry
from .uninstall_repo import run_primitive_7_uninstall_packages
from .constants import PACKAGE_CONFIG_FILE_NAME_LIST, CONFIG_DIR_NAME
from .git_utils import commit_repo_changes
from .result_models import GcResult

logger = logging.getLogger(__name__)

def purge_zombie_folders(
        base_path: Path,
        ignore_names: List[str],
        db_name: str,
        dry_run: bool) -> List[str]:
    if not base_path.exists():
        return []
        
    zombies = []
    for item in base_path.iterdir():
        if not item.is_dir():
            continue
        if item.name.startswith(".") or item.name in ignore_names:
            continue
        
        # Check if it has any valid package config file
        has_config = any((item / cfg_name).exists() for cfg_name in PACKAGE_CONFIG_FILE_NAME_LIST)
        if not has_config:
            zombies.append(item.name)
    
    if zombies:
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would purge zombie folder(s) from {db_name}: {', '.join(zombies)}")
        else:
            logger.info(f"🗑️  [PURGE] Removing zombie folder(s) from {db_name}: {', '.join(zombies)}")
            for z in zombies:
                shutil.rmtree(base_path / z)
    return zombies


def run_primitive_9_purge_workspace_garbage(
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
    no_hooks: bool = False
) -> GcResult:
    """
    Identifies and removes garbage from the drift workspace databases.
    1. Uninstalls orphan packages (present in state but disabled in config).
    2. Removes package folders in render/ and install/ that lack a package config file.
    """
    # --- Part 1: Orphan Package Uninstallation ---
    # We let run_primitive_7_uninstall_packages handle orphan identification if package_names is None
    uninstalled_orphans = run_primitive_7_uninstall_packages(
        workspace_config, 
        package_names=None, 
        force=True, 
        dry_run=dry_run,
        no_hooks=no_hooks
    )
    if uninstalled_orphans.status != "SUCCESS":
        raise RuntimeError(uninstalled_orphans.error_message or "Garbage collection orphan uninstallation failed.")

    # --- Part 2: Database Folder Purge (Zombie folders without config) ---
    # Purge render/
    render_zombies = purge_zombie_folders(
        workspace_config.render_path, 
        ignore_names=[CONFIG_DIR_NAME], 
        db_name="render database",
        dry_run=dry_run
    )
    
    # Purge install/
    install_zombies = purge_zombie_folders(
        workspace_config.install_path, 
        ignore_names=[], 
        db_name="install database",
        dry_run=dry_run
    )

    render_commit_msg: Optional[str] = None
    install_commit_msg: Optional[str] = None

    # --- Part 3: Commit Database Changes ---
    if not dry_run:
        if render_zombies:
            render_commit_msg = f"GC Purge: Removed zombie folder(s) {', '.join(render_zombies)}"
            commit_repo_changes(
                workspace_config.render_path,
                render_commit_msg,
                target_pkgs=render_zombies,
                repo_name="render repo"
            )
        if install_zombies:
            install_commit_msg = f"GC Purge: Removed zombie folder(s) {', '.join(install_zombies)}"
            commit_repo_changes(
                workspace_config.install_path,
                install_commit_msg,
                target_pkgs=install_zombies,
                repo_name="install repo"
            )
    
    if not uninstalled_orphans and not render_zombies and not install_zombies:
        logger.info("✨ Workspace is clean. No garbage detected.")

    return GcResult(
        command="gc",
        status="SUCCESS",
        dry_run=dry_run,
        uninstalled_orphans=list(uninstalled_orphans),
        purged_render_zombies=render_zombies,
        purged_install_zombies=install_zombies,
        render_commit_message=render_commit_msg,
        install_commit_message=install_commit_msg
    )
