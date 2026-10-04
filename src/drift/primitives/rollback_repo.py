"""Primitive 8: Rollback Recovery.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Primitive Entry Point
    run_primitive_8_rollback_recovery(workspace_config, package_names, force, flags)
        1. Discover Target Packages & Sentinel Checks:
            workspace_config.filter_install_packages_by_target
            validate_rollback_packages [Layer 1] (state_registry.get_midway_packages safeguard)
        2. Classify Packages & Reset Git State:
            is_package_committed_in_install_head [Layer 1]
            reset_install_package_to_head [Layer 1] (git checkout & clean for committed packages)
        3. Unified Dependency Resolution:
            resolve_package_uninstall_order (reverse topological order across all rollback targets)
        4. Execute Rollback One-by-One in Reverse Order:
            If committed package: rollback_reinstall_committed_package [Layer 2]
                run_primitive_5_install (force=True, resolve_symlinks=True)
            If first-time package: rollback_uninstalled_first_time_package [Layer 2]
                run_primitive_7_uninstall_packages (force=True)
                git clean -fd -- <pkg>
        5. Restore State Database:
            git checkout HEAD -- state.toml
            state_registry.set_package_state("installed") for reinstalled packages
            state_registry.remove_package for uninstalled packages
            state_registry.save

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Git & State Inspection Helpers
        is_package_committed_in_install_head
        reset_install_package_to_head
        validate_rollback_packages
    Layer 2: Single-Package Rollback Actions
        rollback_reinstall_committed_package
        rollback_uninstalled_first_time_package
    Layer 3: Public Primitive Entry Point
        run_primitive_8_rollback_recovery
===============================================================================
"""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig, PackageSectionConfig
from ..core.result_models import RollbackResult
from ..core.state_registry import load_state_registry, StateRegistry
from .install_repo import run_primitive_5_install, InstallConfig
from .uninstall_repo import run_primitive_7_uninstall_packages, UninstallConfig
from .package_assertions import resolve_package_uninstall_order
from ..hooks.lifecycle_hooks import HookExecFlags
from ..utils.config_utils import partition

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Git & State Inspection Helpers
# =====================================================================

def is_package_committed_in_install_head(install_base: Path, pkg: str) -> bool:
    """Checks if the package exists in the HEAD commit of the install repository."""
    # check=False is intentional: git cat-file returns exit code 1 if the path is absent in HEAD.
    res = subprocess.run(
        ["git", "-C", str(install_base), "cat-file", "-e", f"HEAD:{pkg}"],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def reset_install_package_to_head(install_base: Path, pkg: str) -> None:
    """Resets a package directory inside the install state repository to the HEAD commit."""
    # Revert modifications & deletions in the package directory
    res_checkout = subprocess.run(
        ["git", "-C", str(install_base), "checkout", "HEAD", "--", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_checkout.returncode != 0:
        logger.warning(f"git checkout HEAD failed for '{pkg}': {res_checkout.stderr.strip()}")

    # Clean untracked files & directories inside the package directory
    res_clean = subprocess.run(
        ["git", "-C", str(install_base), "clean", "-fd", "--", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_clean.returncode != 0:
        logger.warning(f"git clean failed for '{pkg}': {res_clean.stderr.strip()}")


def validate_rollback_packages(
    state_registry: StateRegistry,
    discovered_packages: Sequence[str],
    force: bool = False,
) -> List[str]:
    """Validates that discovered packages are in a failed midway state, or returns all if force is True."""
    if force:
        return sorted(set(discovered_packages))

    eligible_pkgs = state_registry.get_rollback_eligible_packages(discovered_packages)
    packages_to_rollback = {pkg for pkg, _ in eligible_pkgs}
    valid_pkgs, packages_state_wrong = partition(
        lambda pkg: pkg in packages_to_rollback, discovered_packages
    )
    if packages_state_wrong:
        raise RuntimeError(
            "The following packages are not in a failed midway/conflict state ('staging', 'staged', or 'installing'): "
            f"[{','.join(sorted(set(packages_state_wrong)))}]. "
            "Running 'rollback' now will bypass reverse synchronization and hard-reset "
            "all configuration files on your system, destroying any local drift. "
            "Use --force to override and rollback anyway."
        )
    return sorted(set(valid_pkgs))


# =====================================================================
# Layer 2: Single-Package Rollback Actions
# =====================================================================

def rollback_reinstall_committed_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    flags: Optional[HookExecFlags] = None,
) -> None:
    """Executes full package reinstallation fallback to restore system files for a single committed package."""
    logger.info(f"Rollback reinstallation for committed package '{pkg}'")
    install_res = run_primitive_5_install(
        workspace_config=workspace_config,
        target_pkgs=[pkg],
        config=InstallConfig(
            resolve_symlinks=True,
            force=True,
            reinstall=True,
            flags=flags,
        ),
    )
    if install_res.status != "SUCCESS":
        raise RuntimeError(install_res.error_message or f"Rollback reinstallation failed for package '{pkg}'.")


def rollback_uninstalled_first_time_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    flags: Optional[HookExecFlags] = None,
) -> None:
    """Cleans up host system files, restores overwritten backups, and removes directory for a first-time package that failed."""
    logger.info(f"Rollback uninstallation for first-time package '{pkg}'")
    uninst_res = run_primitive_7_uninstall_packages(
        workspace_config=workspace_config,
        package_names=[pkg],
        config=UninstallConfig(force=True, flags=flags),
    )
    if uninst_res.status != "SUCCESS":
        raise RuntimeError(uninst_res.error_message or f"Rollback uninstallation of first-time package '{pkg}' failed.")

    # Clean untracked leftover directories in install/ if any remain
    res_clean = subprocess.run(
        ["git", "-C", str(workspace_config.install_path), "clean", "-fd", "--", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_clean.returncode != 0:
        logger.debug(f"git clean for package '{pkg}' exited with code {res_clean.returncode}: {res_clean.stderr.strip()}")


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
        force: If True, bypasses the failed midway conflict state safeguard ('staging', 'staged', or 'installing'),
            allowing a hard reset of packages to their last committed clean Git HEAD state even if they
            are currently in 'installed' state.
        flags: Optional HookExecFlags controlling hook execution options.

    Returns:
        RollbackResult containing details of the rollback operation.
    """
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    # 1. Discover target packages
    discovered = sorted(
        set(
            workspace_config.filter_install_packages_by_target(
                target_packages=package_names or None,
                missing_ok=True,
            )
        )
    )
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

    logger.info(f"Reverting local state database for packages: {packages_to_rollback}")

    install_base = workspace_config.install_path
    # 3. Classify packages into previously committed (reinstallable) vs first-time (uninstallable)
    packages_to_reinstall, packages_to_uninstall = partition(
        lambda pkg: is_package_committed_in_install_head(install_base, pkg),
        packages_to_rollback,
    )
    for pkg in packages_to_reinstall:
        reset_install_package_to_head(install_base, pkg)

    # 4. Resolve unified reverse topological order across all packages being rolled back
    rollback_metadata = {
        pkg: (
            PackageConfig.from_install_dir(install_base / pkg, workspace_config)
            if (install_base / pkg).is_dir()
            else PackageConfig(PackageSectionConfig(name=pkg))
        )
        for pkg in packages_to_rollback
    }
    rollback_deps = {pkg: meta.package.dependencies for pkg, meta in rollback_metadata.items()}
    ordered_rollback = resolve_package_uninstall_order(rollback_deps)

    # 5. Dispatch rollback actions one-by-one in reverse topological order
    reinstall_set = set(packages_to_reinstall)
    for pkg in ordered_rollback:
        if pkg in reinstall_set:
            logger.info(f"Executing Full Package Reinstall to restore system files for: {pkg}")
            rollback_reinstall_committed_package(workspace_config, pkg, flags=flags)
        else:
            logger.info(f"Executing uninstallation rollback for first-time package: {pkg}")
            rollback_uninstalled_first_time_package(workspace_config, pkg, flags=flags)

    # 6. Restore the state registry entries
    # Revert state.toml file to HEAD commit
    res_st = subprocess.run(
        ["git", "-C", str(install_base), "checkout", "HEAD", "--", "state.toml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_st.returncode != 0:
        logger.warning(f"Failed to revert state.toml to HEAD: {res_st.stderr.strip()}")

    # Reload registry after checkout to prevent dirty override
    reloaded_registry = load_state_registry(state_file)
    for pkg in packages_to_reinstall:
        reloaded_registry.set_package_state(pkg, "installed")
    for pkg in packages_to_uninstall:
        reloaded_registry.remove_package(pkg)
    reloaded_registry.save()

    if packages_to_uninstall:
        logger.info(f"🗑️ Cleanly uninstalled failed first-time package(s): {packages_to_uninstall}")
    if packages_to_reinstall:
        logger.info(f"✨ Restored previously committed clean state for: {packages_to_reinstall}")

    logger.info("✨ Rollback recovery complete.")
    return RollbackResult(
        command="rollback",
        status="SUCCESS",
        target_packages=list(package_names) or discovered,
        restored_packages=packages_to_rollback
    )
