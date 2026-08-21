"""Primitive 8: Rollback Recovery."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry, save_state_registry
from .install_repo import run_primitive_5_install_deployment

logger = logging.getLogger(__name__)


def reset_install_package_to_head(install_base: Path, pkg: str) -> None:
    """Resets a package directory inside the install state repository to the HEAD commit."""
    # Revert modifications & deletions in the package directory
    subprocess.run(["git", "-C", str(install_base), "checkout", "HEAD", "--", pkg], capture_output=True)
    # Clean untracked files & directories inside the package directory
    subprocess.run(["git", "-C", str(install_base), "clean", "-fd", "--", pkg], capture_output=True)


def run_primitive_8_rollback_recovery(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    force: bool = False
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

    # 3. For each target package, reset the install/ repository folder to HEAD
    install_base = workspace_config.install_path
    for pkg in packages_to_rollback:
        reset_install_package_to_head(install_base, pkg)

    # Revert state.toml file
    subprocess.run(["git", "-C", str(install_base), "checkout", "HEAD", "--", "state.toml"], capture_output=True)

    # 4. Trigger full redeploy fallback to restore physical/symlinked system files to target state
    logger.info(f"Executing Full Package Redeploy to restore system files for: {packages_to_rollback}")
    run_primitive_5_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=packages_to_rollback,
        resolve_symlinks=True,
        force=True
    )

    # 5. Restore the state registry entries back to 'installed'
    # Reload registry after checkout to prevent dirty override
    reloaded_registry = load_state_registry(state_file)
    for pkg in packages_to_rollback:
        reloaded_registry.set_package_state(pkg, "installed")
    save_state_registry(state_file, reloaded_registry)

    logger.info("✨ Rollback recovery complete. Clean committed state restored.")
    return packages_to_rollback
