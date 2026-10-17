"""Primitive 8: Rollback Recovery.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Primitive Entry Point
    run_primitive_8_rollback_recovery(workspace_config, package_names, force, flags)
        1. Discover Target Packages & Sentinel Checks:
            workspace_config.filter_install_packages_by_target
            validate_rollback_packages [Layer 1] (state_registry.get_rollback_eligible_packages safeguard)
        2. Classify Packages & Reset Committed Package Staging Directories:
            is_package_committed_in_install_head [Layer 1]
            reset_install_package_to_head [Layer 1] (git checkout & clean for committed packages)
        3. Unified Dependency Resolution:
            resolve_rollback_order [Layer 1]
                resolve_package_uninstall_order (reverse topological order, with fallback to input order on error)
        4. Execute 3-Step Rollback One-by-One in Reverse Topological Order:
            If committed package: rollback_reinstall_committed_package [Layer 2]
                (Requires install/<pkg> already reset to HEAD via reset_install_package_to_head)
                - Step 1: rollback_package_wal (undo WAL file actions & restore stashed backups)
                - Step 2: restore_package_state_from_head
                - Step 3: run_primitive_5_install (reinstall=False, run_hooks_when_unchanged=True, force=True, raise_on_error=False)
            If first-time package: rollback_uninstalled_first_time_package [Layer 2]
                - Step 1: rollback_package_wal (undo WAL file actions & restore stashed backups)
                - Step 2: clean_first_time_install_package & remove_package_state_entry
                - Step 3: skipped (zero hooks executed)
        5. Commit History:
            run_primitive_6_commit_install_repo (commits restored state and cleaned files)

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Git & State Inspection Helpers
        is_package_committed_in_install_head
        reset_install_package_to_head
        clean_first_time_install_package
        load_head_state_registry
        restore_package_state_from_head
        remove_package_state_entry
        resolve_rollback_order
        validate_rollback_packages
    Layer 2: Single-Package Rollback Actions
        rollback_reinstall_committed_package
        rollback_uninstalled_first_time_package
    Layer 3: Public Primitive Entry Point
        run_primitive_8_rollback_recovery
===============================================================================
"""

from dataclasses import replace
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig, PackageDependencies
from ..core.file_action import clear_package_wal, rollback_package_wal
from ..core.state_registry import (
    StateRegistry,
    load_state_registry,
    parse_state_registry_content,
)
from ..hooks.lifecycle_hooks import HookExecFlags
from ..models import RollbackResult
from ..utils.config_utils import partition
from ..utils.file_ops import remove_tree
from .install_repo import InstallOptions, run_primitive_5_install, run_primitive_6_commit_install_repo
from .package_assertions import resolve_package_uninstall_order

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


def clean_first_time_install_package(install_base: Path, pkg: str) -> None:
    """Removes an uncommitted first-time package directory from the install repository."""
    pkg_dir = install_base / pkg
    if pkg_dir.exists() or pkg_dir.is_symlink():
        remove_tree(pkg_dir)
    res_clean = subprocess.run(
        ["git", "-C", str(install_base), "clean", "-fd", "--", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_clean.returncode != 0:
        logger.debug(
            f"git clean for package '{pkg}' exited with code {res_clean.returncode}: {res_clean.stderr.strip()}"
        )


def load_head_state_registry(install_base: Path) -> StateRegistry:
    """Loads the committed HEAD version of state.toml from the install repository."""
    state_file = install_base / "state.toml"
    res = subprocess.run(
        ["git", "-C", str(install_base), "show", "HEAD:state.toml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return StateRegistry({}, state_file=state_file)
    return parse_state_registry_content(res.stdout, state_file=state_file)


def restore_package_state_from_head(install_base: Path, pkg: str) -> None:
    """Restores a single committed package's entry in state.toml from HEAD with state='installed'."""
    state_file = install_base / "state.toml"
    registry = load_state_registry(state_file)
    head_registry = load_head_state_registry(install_base)
    head_state = head_registry.packages.get(pkg)
    if head_state is not None:
        registry.packages[pkg] = replace(head_state, state="installed")
    else:
        registry.set_package_state(pkg, "installed")
    registry.save()


def remove_package_state_entry(install_base: Path, pkg: str) -> None:
    """Removes a first-time package's entry from state.toml."""
    state_file = install_base / "state.toml"
    registry = load_state_registry(state_file)
    registry.remove_package(pkg)
    registry.save()


def _resolve_package_sudo(workspace_config: WorkspaceConfig, pkg: str) -> bool:
    """Resolves whether a package requires sudo from its install config or fallback to state registry."""
    install_pkg_dir = workspace_config.install_path / pkg
    if install_pkg_dir.is_dir():
        try:
            meta = PackageConfig.from_install_dir(install_pkg_dir, workspace_config)
            return meta.package.sudo
        except Exception:
            pass
    registry = load_state_registry(workspace_config.install_path / "state.toml")
    pkg_state = registry.packages.get(pkg)
    return pkg_state.sudo if pkg_state is not None else False


def _load_package_dependencies_safe(workspace_config: WorkspaceConfig, pkg: str) -> PackageDependencies:
    """Safely loads a package's dependencies from install/<pkg>, falling back to [] on error."""
    install_pkg_dir = workspace_config.install_path / pkg
    if not install_pkg_dir.is_dir():
        return PackageDependencies()
    try:
        meta = PackageConfig.from_install_dir(install_pkg_dir, workspace_config)
        return meta.package.dependencies
    except Exception as exc:
        logger.warning(f"Failed to load package config for '{pkg}' during rollback: {exc}. Assuming no dependencies.")
        return PackageDependencies()


def resolve_rollback_order(
    workspace_config: WorkspaceConfig,
    packages_to_rollback: Sequence[str],
) -> List[str]:
    """Resolves reverse topological rollback order, falling back to input order if dependency resolution fails."""
    rollback_deps = {
        pkg: _load_package_dependencies_safe(workspace_config, pkg)
        for pkg in packages_to_rollback
    }
    try:
        return resolve_package_uninstall_order(rollback_deps)
    except Exception as exc:
        logger.warning(
            f"Failed to resolve dependency order during rollback ({exc}); "
            "falling back to default package order."
        )
        return list(packages_to_rollback)


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
    """Executes 3-step WAL rollback and incremental reinstall (reinstall=False, run_hooks_when_unchanged=True) for a committed package.

    Precondition:
        The package staging directory `install/<pkg>` must already be reset to the `HEAD` commit
        (via `reset_install_package_to_head`) before calling this function.
    """
    logger.info(f"Rollback reinstallation for committed package '{pkg}'")
    sudo = _resolve_package_sudo(workspace_config, pkg)
    pkg_backup_root = workspace_config.backup_path / pkg

    # Step 1: Undo WAL file actions and restore stashed backups
    rollback_package_wal(pkg_backup_root, sudo=sudo)

    # Step 2: Restore package state entry from HEAD
    restore_package_state_from_head(workspace_config.install_path, pkg)

    # Step 3: Incremental reinstall (reinstall=False, run_hooks_when_unchanged=True) with hook errors ignored by default
    rollback_flags = HookExecFlags.resolve(
        flags,
        fallback=HookExecFlags(raise_on_error=False),
    )
    install_res = run_primitive_5_install(
        workspace_config=workspace_config,
        target_pkgs=[pkg],
        options=InstallOptions(
            resolve_symlinks=True,
            force=True,
            reinstall=False,
            run_hooks_when_unchanged=True,
            no_deps=True,
            flags=rollback_flags,
        ),
    )
    clear_package_wal(pkg_backup_root, sudo=sudo)
    if install_res.status != "SUCCESS":
        raise RuntimeError(install_res.error_message or f"Rollback reinstallation failed for package '{pkg}'.")


def rollback_uninstalled_first_time_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    flags: Optional[HookExecFlags] = None,
) -> None:
    """Reverses WAL file actions, restores backups, and removes install state for a failed first-time package (no hooks)."""
    logger.info(f"Rollback uninstallation for first-time package '{pkg}'")
    sudo = _resolve_package_sudo(workspace_config, pkg)
    pkg_backup_root = workspace_config.backup_path / pkg

    # Step 1: Undo WAL file actions and restore stashed backups
    rollback_package_wal(pkg_backup_root, sudo=sudo)

    # Step 2: Clean install/<pkg> and remove from state.toml (Step 3 skipped, zero hooks run)
    clean_first_time_install_package(workspace_config.install_path, pkg)
    remove_package_state_entry(workspace_config.install_path, pkg)


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
    ordered_rollback = resolve_rollback_order(workspace_config, packages_to_rollback)

    # 5. Dispatch rollback actions one-by-one in reverse topological order
    reinstall_set = set(packages_to_reinstall)
    for pkg in ordered_rollback:
        if pkg in reinstall_set:
            logger.info(f"Executing WAL rollback and incremental reinstall for: {pkg}")
            rollback_reinstall_committed_package(workspace_config, pkg, flags=flags)
        else:
            logger.info(f"Executing WAL rollback for first-time package: {pkg}")
            rollback_uninstalled_first_time_package(workspace_config, pkg, flags=flags)

    # 6. Commit state database and file restorations in install repo
    if packages_to_rollback:
        pkg_word = "packages" if len(packages_to_rollback) > 1 else "package"
        commit_msg = f"Rollback: Restored clean state for {pkg_word} {', '.join(packages_to_rollback)}"
        run_primitive_6_commit_install_repo(workspace_config, commit_msg, packages_to_rollback)

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

