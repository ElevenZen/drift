"""Package Pre-flight Validation Guards & Assertion Helpers.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pre-flight Assertion Guards:
    - assert_packages_hooks_exist(pkg_metadata, base_dir, is_source, hook_names)
        * Inspects lifecycle hook files across all target packages via check_missing_hooks.
        * Aggregates all missing or non-file hooks across the batch.
        * Raises HookMissingError(packages=[...]) if any failures are detected.

    - assert_install_pkg_dirs_clean(install_base, packages)
        * Inspects install/ package directories for uncommitted git modifications via has_uncommitted_modifications.
        * Aggregates all dirty package directories across the batch.
        * Raises DriftDetectedError(packages=[...]) if any unclean directories are detected.

    - assert_packages_not_in_midway_state(packages, state_registry)
        * Inspects package states in state.toml to ensure none are in midway transaction state ('staging' or 'installing').
        * Aggregates all midway packages across the batch.
        * Raises MidwayTransactionError(packages=[...]) if any are detected.

    - assert_packages_install_dirs_exist(install_base, packages)
        * Inspects install/ repository base directory to ensure each package directory exists.
        * Aggregates all missing directories across the batch.
        * Raises PackageInstallDirMissingError(packages=[...]) if any packages are unstaged.

    - assert_packages_target_dirs_valid(pkg_metadata, workspace_config)
        * Validates that destination target directories are absolute paths and not within or equal to drift_root.
        * Aggregates all non-absolute or collision violations across the batch.
        * Raises ConfigError(packages=[...]) if non-absolute, or InstallCollisionError(packages=[...]) if inside drift_root.

    - assert_packages_target_dirs_writable(pkg_metadata, workspace_config)
        * Validates that host destination target directories are writable before applying changes.
        * Aggregates all permission failures across the batch via try-except.
        * Raises TargetPermissionError(packages=[...]) if any target directory is not writable.

    - assert_no_cross_package_conflicts(workspace_config, discovered_packages, pkg_metadata_map, state_registry)
        * Audits destination paths for cross-package collisions before executing deployments.
        * Aggregates all intra-batch and inter-package conflicts across the workspace.
        * Raises CrossPackageCollisionError(packages=[...], conflicts=...) if any collisions are detected.

-------------------------------------------------------------------------------
Layers:
    Layer 1: Package-level Pre-flight Assertion Primitives
        assert_packages_hooks_exist
        assert_install_pkg_dirs_clean
        assert_packages_not_in_midway_state
        assert_packages_install_dirs_exist
        assert_packages_target_dirs_valid
        assert_packages_target_dirs_writable
        assert_no_cross_package_conflicts
==============================================================================="""

import shlex
import collections
import itertools
from pathlib import Path
from typing import Union, Iterable, Sequence, Mapping, Dict, List, Tuple, Optional

from ..config.package_config import PackageConfig
from ..config.workspace_config import WorkspaceConfig
from ..core.ignore import DriftIgnore
from ..core.state_registry import StateRegistry
from ..core.exceptions import (
    ConfigError,
    CrossPackageCollisionError,
    DriftDetectedError,
    HookMissingError,
    InstallCollisionError,
    MidwayTransactionError,
    PackageInstallDirMissingError,
    TargetPermissionError,
)
from ..utils.git_utils import has_uncommitted_modifications
from ..utils.path_utils import is_relative_to, resolve_target_path
from ..utils.file_ops import assert_writable


def assert_packages_hooks_exist(
    pkg_metadata: Mapping[str, PackageConfig],
    base_dir: Path,
    is_source: bool = False,
    hook_names: Sequence[str] = (),
) -> None:
    """Validates that configured lifecycle hook files exist and are regular files for all packages.

    Collects all hook assertion failures across all packages before raising.

    Args:
        pkg_metadata: Mapping from package name to PackageConfig.
        base_dir: Base directory containing package folders (e.g. render/, install/, or src/).
        is_source: True if validating source directory (src/), False if rendered/staged (render/ or install/).
        hook_names: Specific hook names to check, or empty sequence for all lifecycle hooks.

    Raises:
        HookMissingError: If one or more hook files are missing or invalid across the packages.
    """
    hook_failures: Dict[str, List[Tuple[str, Path, str]]] = {
        pkg: missing
        for pkg, metadata in pkg_metadata.items()
        if (missing := metadata.hooks.check_missing_hooks(base_dir / pkg, is_source=is_source, hook_names=hook_names))
    }
    if hook_failures:
        lines = [
            f"❌ Lifecycle hook file validation failed for {len(hook_failures)} package(s):"
        ]
        for pkg, failures in sorted(hook_failures.items()):
            for _, _, reason in failures:
                lines.append(f"  • {reason}")
        raise HookMissingError(
            "\n".join(lines),
            packages=sorted(hook_failures.keys()),
        )


def assert_install_pkg_dirs_clean(
    install_base: Path,
    packages: Union[str, Iterable[str]],
) -> None:
    """Verifies that none of the target package directories in install/ have uncommitted local git changes.

    Collects all unclean package directories across the batch before raising.

    Args:
        install_base: Path to the install/ repository base directory.
        packages: Single package name or iterable of package names to check.

    Raises:
        DriftDetectedError: If one or more package directories in install/ have uncommitted modifications.
    """
    pkg_list = [packages] if isinstance(packages, str) else list(packages)
    dirty_pkgs = [
        pkg
        for pkg in sorted(pkg_list)
        if (install_base / pkg).is_dir() and has_uncommitted_modifications(install_base, install_base / pkg)
    ]
    if dirty_pkgs:
        details = ", ".join(f"'{pkg}'" for pkg in dirty_pkgs)
        raise DriftDetectedError(
            f"Safety Abort: Package(s) {details} in install directory has uncommitted local modifications.\n"
            f"Please commit or stash your changes before staging, or use --force flag to bypass this check.",
            packages=dirty_pkgs,
        )


def assert_packages_not_in_midway_state(
    packages: Iterable[str],
    state_registry: StateRegistry,
) -> None:
    """Verifies that none of the packages are in a midway transaction state ('staging' or 'installing').

    Collects all midway packages across the batch before raising.

    Args:
        packages: Iterable of package names to check.
        state_registry: StateRegistry instance.

    Raises:
        MidwayTransactionError: If one or more packages are in a midway transaction state.
    """
    midway_pkgs = state_registry.get_midway_packages(target_packages=packages)
    if midway_pkgs:
        pkg_names = [pkg for pkg, _ in midway_pkgs]
        pkg_cmd_str = shlex.join(pkg_names)
        details = ", ".join(f"'{pkg}' ({state})" for pkg, state in midway_pkgs)
        raise MidwayTransactionError(
            f"Safety Abort: Package(s) in midway transaction state: {details}, "
            f"indicating a previous operation failed midway. "
            f"Please run 'drift rollback {pkg_cmd_str}' to restore a clean state before retrying.",
            packages=pkg_names,
        )


def assert_packages_install_dirs_exist(
    install_base: Path,
    packages: Iterable[str],
) -> None:
    """Verifies that the staged install directory exists for each package.

    Collects all missing package install directories before raising.

    Args:
        install_base: Path to the install/ repository base directory.
        packages: Iterable of package names to verify.

    Raises:
        PackageInstallDirMissingError: If one or more packages lack an install/ directory.
    """
    missing_pkgs = [
        pkg for pkg in sorted(packages)
        if not (install_base / pkg).is_dir()
    ]
    if missing_pkgs:
        missing_dirs = [f"'{install_base / pkg}'" for pkg in missing_pkgs]
        raise PackageInstallDirMissingError(
            f"Package installation directory does not exist for {len(missing_pkgs)} package(s): {', '.join(missing_dirs)}. "
            f"Please run 'drift stage' to stage the package(s) before deploying.",
            packages=missing_pkgs,
        )


def _inspect_package_target_dir_validity(
    package_name: str,
    metadata: PackageConfig,
    workspace_config: WorkspaceConfig,
) -> Optional[Tuple[str, str, str]]:
    """Validates target directory path for a single package, returning (package, error_kind, message) on violation."""
    if not metadata.package.enable_install:
        return None
    target_dir = metadata.get_target_directory(workspace_config)
    abs_drift_root = workspace_config.drift_root.absolute()
    if not target_dir.is_absolute():
        return (
            package_name,
            "non_absolute",
            f"Package '{package_name}': Target directory '{target_dir}' must be absolute.",
        )
    if target_dir == abs_drift_root or is_relative_to(target_dir, abs_drift_root):
        return (
            package_name,
            "collision",
            f"The target directory written in config '{target_dir}' "
            f"cannot be inside or equal to the drift workspace root '{abs_drift_root}'.",
        )
    return None


def assert_packages_target_dirs_valid(
    pkg_metadata: Mapping[str, PackageConfig],
    workspace_config: WorkspaceConfig,
) -> None:
    """Validates that destination target directories for all enabled packages are absolute and outside drift_root.

    Collects all target directory assertion failures across the batch before raising.

    Args:
        pkg_metadata: Mapping from package name to PackageConfig.
        workspace_config: The workspace configuration instance.

    Raises:
        ConfigError: If a target directory is not an absolute path.
        InstallCollisionError: If a target directory resolves inside or equal to drift_root.
    """
    validations = [
        res
        for pkg, metadata in sorted(pkg_metadata.items())
        if (res := _inspect_package_target_dir_validity(pkg, metadata, workspace_config)) is not None
    ]
    non_absolute = [(pkg, msg) for pkg, kind, msg in validations if kind == "non_absolute"]
    collisions = [(pkg, msg) for pkg, kind, msg in validations if kind == "collision"]

    if non_absolute:
        raise ConfigError(
            "Invalid target directory configuration:\n  • " + "\n  • ".join(msg for _, msg in non_absolute),
            packages=[pkg for pkg, _ in non_absolute],
        )
    if collisions:
        raise InstallCollisionError(
            "Safety Abort: " + "; ".join(msg for _, msg in collisions),
            packages=[pkg for pkg, _ in collisions],
        )


def _check_package_target_writable(
    package_name: str,
    metadata: PackageConfig,
    workspace_config: WorkspaceConfig,
) -> Optional[Tuple[str, Path, str]]:
    """Inspects writability for a single package target directory, returning failure details if any."""
    if not metadata.package.enable_install:
        return None
    target_dir = metadata.get_target_directory(workspace_config)
    try:
        assert_writable(target_dir, metadata.package.sudo)
        return None
    except (PermissionError, OSError) as e:
        return (package_name, target_dir, str(e))


def assert_packages_target_dirs_writable(
    pkg_metadata: Mapping[str, PackageConfig],
    workspace_config: WorkspaceConfig,
) -> None:
    """Validates that destination target directories for all enabled packages are writable on the host system.

    Collects all permission assertion failures across the batch before raising.

    Args:
        pkg_metadata: Mapping from package name to PackageConfig.
        workspace_config: The workspace configuration instance.

    Raises:
        TargetPermissionError: If any target directory cannot be written to.
    """
    failures = [
        failure
        for pkg, metadata in sorted(pkg_metadata.items())
        if (failure := _check_package_target_writable(pkg, metadata, workspace_config)) is not None
    ]
    if failures:
        lines = [
            f"❌ Target directory permission check failed for {len(failures)} package(s):"
        ]
        for pkg, t_dir, err_msg in failures:
            lines.append(f"  • Package '{pkg}' (target: '{t_dir}'): {err_msg}")
        raise TargetPermissionError(
            "\n".join(lines),
            packages=[pkg for pkg, _, _ in failures],
        )


def _gather_package_destination_targets(
    workspace_config: WorkspaceConfig,
    pkg: str,
    metadata: PackageConfig,
) -> List[Tuple[Path, Path]]:
    """Gathers (relative_source_file, absolute_host_target) for all deployable files in a package."""
    if not metadata.package.enable_install:
        return []
    install_pkg_dir = workspace_config.install_path / pkg
    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir, is_source=False)
    deployable_files = ignore_handler.filter_deployable_files(install_pkg_dir)
    target_dir = metadata.get_target_directory(workspace_config)

    return [
        (rel_file, resolve_target_path(rel_file, target_dir))
        for rel_file in deployable_files
    ]


def assert_no_cross_package_conflicts(
    workspace_config: WorkspaceConfig,
    discovered_packages: Iterable[str],
    pkg_metadata_map: Mapping[str, PackageConfig],
    state_registry: StateRegistry,
) -> None:
    """Audits destination paths for cross-package collisions before executing deployments.

    Validates:
    1. Intra-batch conflicts: Two or more packages in current deployment batch claiming identical host paths.
    2. Inter-package conflicts: A package in current batch claiming a host path already owned
       by a different installed package recorded in state.toml (outside the current batch).

    Collects all conflicting destination targets across the workspace and reports them together.

    Raises:
        CrossPackageCollisionError: When one or more cross-package collisions are detected.
    """
    discovered_set = set(discovered_packages)

    # 1. Gather destination claims from current batch packages
    batch_claims: List[Tuple[Path, Tuple[str, str]]] = [
        (dst_path, (pkg, "batch"))
        for pkg in discovered_set
        if (metadata := pkg_metadata_map.get(pkg)) and metadata.package.enable_install
        for _, dst_path in _gather_package_destination_targets(workspace_config, pkg, metadata)
    ]

    # 2. Gather destination claims from installed packages outside the current batch
    external_installed_ownership = state_registry.build_destination_ownership_map(
        exclude_packages=discovered_set
    )
    installed_claims: List[Tuple[Path, Tuple[str, str]]] = [
        (dst_path, (owner, "installed"))
        for dst_path, owner in external_installed_ownership.items()
    ]

    # 3. Group and aggregate claims by destination path
    claims_by_path: Dict[Path, List[Tuple[str, str]]] = collections.defaultdict(list)
    for dst_path, claim in itertools.chain(batch_claims, installed_claims):
        claims_by_path[dst_path].append(claim)

    # 4. Filter for paths with multiple competing claims involving the current batch
    conflicts = {
        dst: claims
        for dst, claims in claims_by_path.items()
        if len(claims) > 1 and any(src == "batch" for _, src in claims)
    }

    if not conflicts:
        return

    # 5. Format comprehensive diagnostic report for all collisions
    conflict_lines = [
        f"❌ Cross-package destination conflicts detected ({len(conflicts)} collision(s)):",
    ]
    for dst in sorted(conflicts.keys(), key=lambda p: str(p)):
        claims = conflicts[dst]
        batch_claimants = sorted(set(pkg for pkg, src in claims if src == "batch"))
        installed_owners = sorted(set(pkg for pkg, src in claims if src == "installed"))

        if len(batch_claimants) > 1:
            conflict_lines.append(
                f"  • '{dst}': Intra-batch collision between packages {batch_claimants}"
            )
        elif installed_owners:
            conflict_lines.append(
                f"  • '{dst}': Package '{batch_claimants[0]}' (current batch) collides with '{installed_owners[0]}' (already installed)"
            )

    conflicting_packages = sorted(set(
        pkg for claims in conflicts.values() for pkg, _ in claims
    ))

    raise CrossPackageCollisionError(
        "\n".join(conflict_lines),
        packages=conflicting_packages,
        conflicts=conflicts,
    )
