"""Primitive 8: Rollback Recovery."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry, save_state_registry
from .install_repo import run_primitive_5_install_deployment
from .uninstall_repo import (
    get_uninstall_metadata,
    remove_deployed_files,
    restore_backups,
    clean_up_package_directories,
)

logger = logging.getLogger(__name__)


def is_package_committed_in_install_head(install_base: Path, pkg: str) -> bool:
    """Checks if the package exists in the HEAD commit of the install repository."""
    res = subprocess.run(
        ["git", "-C", str(install_base), "cat-file", "-e", f"HEAD:{pkg}"],
        capture_output=True
    )
    return res.returncode == 0


def reset_install_package_to_head(install_base: Path, pkg: str) -> None:
    """Resets a package directory inside the install state repository to the HEAD commit."""
    # Revert modifications & deletions in the package directory
    subprocess.run(["git", "-C", str(install_base), "checkout", "HEAD", "--", pkg], capture_output=True)
    # Clean untracked files & directories inside the package directory
    subprocess.run(["git", "-C", str(install_base), "clean", "-fd", "--", pkg], capture_output=True)


def rollback_uninstalled_first_time_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    deployed_files: List[Path]
) -> None:
    """Cleans up host system files, restores overwritten backups, and removes directory for a first-time package that failed."""
    target_dir, sudo = get_uninstall_metadata(workspace_config, pkg)

    # 1. Remove deployed files from target host
    if deployed_files:
        remove_deployed_files(pkg, deployed_files, target_dir, sudo)

    # 2. Restore any original host files that were backed up during collision guard
    restore_backups(workspace_config, pkg, target_dir, sudo)

    # 3. Clean up install/ and backup/ package directories
    clean_up_package_directories(workspace_config, pkg)
    subprocess.run(["git", "-C", str(workspace_config.install_path), "clean", "-fd", "--", pkg], capture_output=True)


def run_primitive_8_rollback_recovery(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> List[str]:
    """Reverts failed midway deployments and restores system files to the last committed clean state."""
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    # 1. Discover target packages
    discovered = workspace_config.get_discovered_packages(
        custom_dir=workspace_config.source_path,
        target_pkgs=package_names
    )

    if not discovered:
        logger.info("✨ No active packages found to rollback.")
        return []

    # 2. Check conflict states if force is False
    if not force:
        packages_state_wrong = [ pkg for pkg in discovered
                                if state_registry.get_package_state(pkg) not in ["staging", "deploying"]]
        if packages_state_wrong:
            raise RuntimeError(
                    "The following packages are not in a failed midway/conflict state ('staging' or 'deploying'): "
                    f"[{','.join(packages_state_wrong)}]. "
                    "Running 'rollback' now will bypass reverse synchronization and hard-reset "
                    "all configuration files on your system, destroying any local drift. "
                    "Use --force to override and rollback anyway.")

    packages_to_rollback = discovered
    logger.info(f"Reverting local state database for packages: {packages_to_rollback}")

    install_base = workspace_config.install_path
    packages_to_redeploy: List[str] = []
    packages_to_uninstall: List[str] = []

    # 3. Classify packages into previously committed (redeployable) vs first-time (uninstallable)
    for pkg in packages_to_rollback:
        if is_package_committed_in_install_head(install_base, pkg):
            packages_to_redeploy.append(pkg)
            reset_install_package_to_head(install_base, pkg)
        else:
            packages_to_uninstall.append(pkg)
            # Retrieve deployed files recorded in state.toml before physical delivery
            deployed_files = state_registry.get_package_deployed_files(pkg)
            rollback_uninstalled_first_time_package(workspace_config, pkg, deployed_files)

    # Revert state.toml file to HEAD commit
    subprocess.run(["git", "-C", str(install_base), "checkout", "HEAD", "--", "state.toml"], capture_output=True)

    # 4. Trigger full redeploy fallback for packages that were committed in HEAD
    if packages_to_redeploy:
        logger.info(f"Executing Full Package Redeploy to restore system files for: {packages_to_redeploy}")
        run_primitive_5_install_deployment(
            workspace_config=workspace_config,
            packages_to_redeploy=packages_to_redeploy,
            resolve_symlinks=True,
            force=True,
            no_hooks=no_hooks
        )

    # 5. Restore the state registry entries
    # Reload registry after checkout to prevent dirty override
    reloaded_registry = load_state_registry(state_file)
    for pkg in packages_to_redeploy:
        reloaded_registry.set_package_state(pkg, "installed")
    for pkg in packages_to_uninstall:
        reloaded_registry.remove_package(pkg)
    save_state_registry(state_file, reloaded_registry)

    if packages_to_uninstall:
        logger.info(f"🗑️ Cleanly uninstalled failed first-time package(s): {packages_to_uninstall}")
    if packages_to_redeploy:
        logger.info(f"✨ Restored previously committed clean state for: {packages_to_redeploy}")

    logger.info("✨ Rollback recovery complete.")
    return packages_to_rollback
