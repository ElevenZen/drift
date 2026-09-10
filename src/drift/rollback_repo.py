"""Primitive 8: Rollback Recovery.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Primitive Entry Point
    run_primitive_8_rollback_recovery(workspace_config, package_names, force, flags)
        1. Discover Target Packages & Sentinel Checks:
            workspace_config.get_installed_packages
            validate_rollback_packages [Layer 1] (state_registry.get_midway_packages safeguard)
        2. Classify Packages & Reset Git State:
            is_package_committed_in_install_head [Layer 1]
            reset_install_package_to_head [Layer 1] (git checkout & clean for committed packages)
        3. Redeploy Committed Packages:
            rollback_redeploy_committed_package [Layer 2]
                run_primitive_5_install_deployment (force=True, resolve_symlinks=True)
        4. Uninstall First-Time Packages:
            rollback_uninstalled_first_time_package [Layer 2]
                run_primitive_7_uninstall_packages (force=True)
                git clean -fd -- <pkg>
        5. Restore State Database:
            git checkout HEAD -- state.toml
            state_registry.set_package_state("installed") for redeployed packages
            state_registry.remove_package for uninstalled packages
            state_registry.save

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Git & State Inspection Helpers
        is_package_committed_in_install_head
        reset_install_package_to_head
        validate_rollback_packages
    Layer 2: Single-Package Rollback Actions
        rollback_redeploy_committed_package
        rollback_uninstalled_first_time_package
    Layer 3: Public Primitive Entry Point
        run_primitive_8_rollback_recovery
===============================================================================
"""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from .workspace_config import WorkspaceConfig
from .result_models import RollbackResult
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .install_repo import run_primitive_5_install_deployment
from .uninstall_repo import run_primitive_7_uninstall_packages
from .lifecycle_hooks import HookExecFlags

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Git & State Inspection Helpers
# =====================================================================

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


def validate_rollback_packages(
    state_registry: StateRegistry,
    discovered_packages: Sequence[str],
    force: bool = False,
) -> List[str]:
    """Validates that discovered packages are in a failed midway state, or returns all if force is True."""
    if force:
        return sorted(list(set(discovered_packages)))

    midway_pkgs = state_registry.get_midway_packages(discovered_packages)
    packages_to_rollback = {pkg for pkg, _ in midway_pkgs}
    packages_state_wrong = set(discovered_packages) - packages_to_rollback
    if len(packages_state_wrong) > 0:
        raise RuntimeError(
            "The following packages are not in a failed midway/conflict state ('staging' or 'deploying'): "
            f"[{','.join(sorted(packages_state_wrong))}]. "
            "Running 'rollback' now will bypass reverse synchronization and hard-reset "
            "all configuration files on your system, destroying any local drift. "
            "Use --force to override and rollback anyway."
        )
    return sorted(list(packages_to_rollback))


# =====================================================================
# Layer 2: Single-Package Rollback Actions
# =====================================================================

def rollback_redeploy_committed_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    flags: Optional[HookExecFlags] = None,
) -> None:
    """Executes full package redeployment fallback to restore system files for a single committed package."""
    install_res = run_primitive_5_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=[pkg],
        resolve_symlinks=True,
        force=True,
        flags=flags,
    )
    if install_res.status != "SUCCESS":
        raise RuntimeError(install_res.error_message or f"Rollback redeployment failed for package '{pkg}'.")


def rollback_uninstalled_first_time_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    flags: Optional[HookExecFlags] = None,
) -> None:
    """Cleans up host system files, restores overwritten backups, and removes directory for a first-time package that failed."""
    uninst_res = run_primitive_7_uninstall_packages(
        workspace_config=workspace_config,
        package_names=[pkg],
        force=True,
        flags=flags,
    )
    if uninst_res.status != "SUCCESS":
        raise RuntimeError(uninst_res.error_message or f"Rollback uninstallation of first-time package '{pkg}' failed.")

    # Clean untracked leftover directories in install/ if any remain
    subprocess.run(["git", "-C", str(workspace_config.install_path), "clean", "-fd", "--", pkg], capture_output=True)


# =====================================================================
# Layer 3: Public Primitive Entry Point
# =====================================================================

def run_primitive_8_rollback_recovery(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> RollbackResult:
    """Reverts failed midway deployments and restores system files to the last committed clean state (Primitive 8).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to rollback, or None for all target packages.
        force: If True, bypasses the failed midway conflict state safeguard ('staging' or 'deploying'),
            allowing a hard reset of packages to their last committed clean Git HEAD state even if they
            are currently in 'installed' state.
        flags: Optional HookExecFlags controlling hook execution options.

    Returns:
        RollbackResult containing details of the rollback operation.
    """
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    # 1. Discover target packages
    discovered = sorted(list(set(workspace_config.get_installed_packages(target_pkgs=package_names))))
    if not discovered:
        logger.info("✨ No active packages found to rollback.")
        return RollbackResult(
            command="rollback",
            status="SUCCESS",
            target_packages=list(package_names),
            restored_packages=[]
        )

    # 2. Check conflict states if force is False
    packages_to_rollback = validate_rollback_packages(
        state_registry=state_registry,
        discovered_packages=discovered,
        force=force,
    )
    if not packages_to_rollback:
        logger.info("✨ No packages in a failed midway/conflict state to rollback.")
        return RollbackResult(
            command="rollback",
            status="SUCCESS",
            target_packages=list(package_names) or list(discovered),
            restored_packages=[]
        )

    logger.info(f"Reverting local state database for packages: {packages_to_rollback}")

    install_base = workspace_config.install_path
    packages_to_redeploy: List[str] = []
    packages_to_uninstall: List[str] = []

    # 3. Classify packages into previously committed (redeployable) vs first-time (uninstallable)
    for pkg in sorted(packages_to_rollback):
        if is_package_committed_in_install_head(install_base, pkg):
            packages_to_redeploy.append(pkg)
            reset_install_package_to_head(install_base, pkg)
        else:
            packages_to_uninstall.append(pkg)

    # 4. Trigger full redeploy fallback for packages that were committed in HEAD (isolated per package)
    if packages_to_redeploy:
        logger.info(f"Executing Full Package Redeploy to restore system files for: {packages_to_redeploy}")
        for pkg in packages_to_redeploy:
            logger.info(f"Rollback redeployment for committed package '{pkg}'")
            rollback_redeploy_committed_package(workspace_config, pkg, flags=flags)

    # 5. Clean up host system files and directories for first-time packages that failed (isolated per package)
    if packages_to_uninstall:
        logger.info(f"Executing uninstallation rollback for first-time package(s): {packages_to_uninstall}")
        for pkg in packages_to_uninstall:
            logger.info(f"Rollback uninstallation for first-time package '{pkg}'")
            rollback_uninstalled_first_time_package(workspace_config, pkg, flags=flags)

    # 6. Restore the state registry entries
    # Revert state.toml file to HEAD commit
    subprocess.run(["git", "-C", str(install_base), "checkout", "HEAD", "--", "state.toml"], capture_output=True)

    # Reload registry after checkout to prevent dirty override
    reloaded_registry = load_state_registry(state_file)
    for pkg in packages_to_redeploy:
        reloaded_registry.set_package_state(pkg, "installed")
    for pkg in packages_to_uninstall:
        reloaded_registry.remove_package(pkg)
    reloaded_registry.save()

    if packages_to_uninstall:
        logger.info(f"🗑️ Cleanly uninstalled failed first-time package(s): {packages_to_uninstall}")
    if packages_to_redeploy:
        logger.info(f"✨ Restored previously committed clean state for: {packages_to_redeploy}")

    logger.info("✨ Rollback recovery complete.")
    return RollbackResult(
        command="rollback",
        status="SUCCESS",
        target_packages=list(package_names) or list(discovered),
        restored_packages=sorted(list(packages_to_rollback))
    )
