"""Primitive 9: Workspace Garbage Collection (Orphan Cleanup and Database Purge).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Public Primitive Entry Point
    run_primitive_9_purge_workspace_garbage(workspace_config, dry_run, flags)
        1. Orphan Package Uninstallation:
            run_primitive_7_uninstall_packages(package_names=(), force=True)
                (Uninstalls all installed packages disabled in workspace config)
        2. Database Purge (render/ and install/):
            purge_render_folders(workspace_config, dry_run) [Layer 2]
                filter_candidate_package_dirs [Layer 1]
                filter (is_zombie_package_dir OR disabled OR missing from src/)
                remove_purged_folders [Layer 1]
            purge_install_folders(workspace_config, dry_run) [Layer 2]
                filter_candidate_package_dirs [Layer 1]
                filter (is_zombie_package_dir OR (unregistered AND (disabled OR missing from src/)))
                remove_purged_folders [Layer 1]
        3. Scoped Database Git Commits:
            commit_repo_changes (scoped to purged folder names in render/ and install/)
        4. Return GcResult

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Functional Filtering & Deletion Helpers
        filter_candidate_package_dirs
        is_zombie_package_dir
        filter_zombie_package_dirs
        remove_purged_folders
    Layer 2: Database Purge Handlers
        purge_zombie_folders
        purge_render_folders
        purge_install_folders
    Layer 3: Public Primitive Entry Point
        run_primitive_9_purge_workspace_garbage
===============================================================================
"""

import logging
import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Iterator, Iterable

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry
from .uninstall_repo import run_primitive_7_uninstall_packages
from .constants import PACKAGE_CONFIG_FILE_NAME_LIST, CONFIG_DIR_NAME, FORBIDDEN_PACKAGE_NAMES
from .git_utils import commit_repo_changes
from .lifecycle_hooks import HookExecFlags
from .result_models import GcResult

logger = logging.getLogger(__name__)


def filter_candidate_package_dirs(base_path: Path, ignore_names: Iterable[str] = ()) -> Iterator[Path]:
    """Filters base_path entries for visible subdirectories not in ignore_names."""
    if not base_path.exists():
        return iter(())
    ignore_set = set(ignore_names)
    items = filter(lambda item: item.is_dir(), sorted(base_path.iterdir()))
    items = filter(lambda item: not item.name.startswith(".") and item.name not in ignore_set, items)
    return items


def is_zombie_package_dir(item: Path) -> bool:
    """Returns True if the directory lacks any valid package config file."""
    return not any((item / cfg_name).exists() for cfg_name in PACKAGE_CONFIG_FILE_NAME_LIST)


def filter_zombie_package_dirs(items: Iterable[Path]) -> Iterator[Path]:
    """Filters package directories that lack any valid package config file."""
    return filter(is_zombie_package_dir, items)


def remove_purged_folders(
    base_path: Path,
    folder_names: Sequence[str],
    db_name: str,
    dry_run: bool = False,
) -> None:
    """Logs and removes purged folder names from base_path unless dry_run is True."""
    if not folder_names:
        return
    if dry_run:
        logger.info(f"🔍 [DRY RUN] Would purge folder(s) from {db_name}: {', '.join(folder_names)}")
    else:
        logger.info(f"🗑️  [PURGE] Removing folder(s) from {db_name}: {', '.join(folder_names)}")
        for name in folder_names:
            shutil.rmtree(base_path / name)


def purge_zombie_folders(
        base_path: Path,
        ignore_names: Sequence[str],
        db_name: str,
        dry_run: bool) -> List[str]:
    """Purges directories in base_path that lack any valid package config file."""
    items = filter_candidate_package_dirs(base_path, ignore_names)
    zombie_items = filter_zombie_package_dirs(items)
    zombies = [item.name for item in zombie_items]
    remove_purged_folders(base_path, zombies, db_name, dry_run=dry_run)
    return zombies


def purge_render_folders(
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """
    Purges obsolete, disabled, or zombie folders from render/ database:
    1. Folders without any valid package config file (zombies).
    2. Package folders that are disabled in the workspace configuration.
    3. Package folders whose source directory in src/ has been removed.
    """
    render_path = workspace_config.render_path
    ignore_names = set(FORBIDDEN_PACKAGE_NAMES) | {CONFIG_DIR_NAME}
    items = filter_candidate_package_dirs(render_path, ignore_names)
    purged_items = filter(
        lambda item: (
            is_zombie_package_dir(item)
            or not workspace_config.is_package_enabled(item.name)
            or not (workspace_config.source_path / item.name).is_dir()
        ),
        items,
    )
    purged = [item.name for item in purged_items]
    remove_purged_folders(render_path, purged, "render database", dry_run=dry_run)
    return purged


def purge_install_folders(
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
) -> List[str]:
    """
    Purges obsolete or zombie folders from install/ database:
    1. Folders without any valid package config file (zombies).
    2. Folders not registered in state.toml and disabled in workspace or missing from src/.
    """
    install_path = workspace_config.install_path
    registry_path = install_path / "state.toml"
    registered_pkgs = set()
    if registry_path.exists():
        try:
            registry = load_state_registry(registry_path)
            registered_pkgs = set(registry.packages.keys())
        except Exception:
            pass

    ignore_names = set(FORBIDDEN_PACKAGE_NAMES)
    items = filter_candidate_package_dirs(install_path, ignore_names)
    purged_items = filter(
        lambda item: (
            is_zombie_package_dir(item)
            or (
                item.name not in registered_pkgs
                and (
                    not workspace_config.is_package_enabled(item.name)
                    or not (workspace_config.source_path / item.name).is_dir()
                )
            )
        ),
        items,
    )
    purged = [item.name for item in purged_items]
    remove_purged_folders(install_path, purged, "install database", dry_run=dry_run)
    return purged


def run_primitive_9_purge_workspace_garbage(
    workspace_config: WorkspaceConfig,
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> GcResult:
    """
    Identifies and removes garbage from the drift workspace databases.
    1. Uninstalls orphan packages (present in state but disabled in config).
    2. Removes package folders in render/ and install/ that are disabled, missing from source, or lack config.
    """
    # --- Part 1: Orphan Package Uninstallation ---
    # We let run_primitive_7_uninstall_packages handle orphan identification if package_names is empty
    uninstalled_orphans = run_primitive_7_uninstall_packages(
        workspace_config, 
        package_names=(), 
        force=True, 
        dry_run=dry_run,
        flags=flags,
    )
    if uninstalled_orphans.status != "SUCCESS":
        raise RuntimeError(uninstalled_orphans.error_message or "Garbage collection orphan uninstallation failed.")

    # --- Part 2: Database Folder Purge (Disabled packages & zombie folders) ---
    # Purge render/
    render_zombies = purge_render_folders(
        workspace_config, 
        dry_run=dry_run
    )
    
    # Purge install/
    install_zombies = purge_install_folders(
        workspace_config, 
        dry_run=dry_run
    )

    render_commit_msg: Optional[str] = None
    install_commit_msg: Optional[str] = None

    # --- Part 3: Commit Database Changes ---
    if not dry_run:
        if render_zombies:
            render_commit_msg = f"GC Purge: Removed folder(s) {', '.join(render_zombies)}"
            commit_repo_changes(
                workspace_config.render_path,
                render_commit_msg,
                target_pkgs=render_zombies,
                repo_name="render repo"
            )
        if install_zombies:
            install_commit_msg = f"GC Purge: Removed folder(s) {', '.join(install_zombies)}"
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
