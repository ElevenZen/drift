"""Primitive 4: Sandbox Staging (render/ -> install/ state database).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Primitive Entry Point
    run_primitive_4_stage_render_to_install(workspace_config, target_pkgs, force)
        1. Package Discovery & Config Validation:
            workspace_config.get_rendered_packages
            load_package_config_from_render_dir
        2. Transaction Safety & Sentinel Checks:
            state_registry.get_midway_packages
            ensure_install_pkg_dir_clean [Layer 1]
        3. Diff & Change Classification:
            compute_package_stage_diff(pkg, install_base, render_base) [Layer 2]
                compare_folders (deployable diff with DriftIgnore)
                compare_folders (physical diff without DriftIgnore)
        4. Staging Transaction Execution (if packages have physical changes):
            stage_modified_packages(packages_to_stage, pkg_metadata, ...) [Layer 4]
                check_sudo_privilege (if sudo required)
                state_registry.set_package_state("staging") & save
                apply_package_stage_changes(pkg, ...) [Layer 3]
                    backup_and_delete_one_file (deletions with backup)
                    atomic_copy_file / copy_file_mode_with_sudo (additions & modifications)
                    copy_ignore_and_config_files(render_dir, install_dir, ...) [Layer 1]
                state_registry.set_package_state("staged") & save
        5. Return Summary Map of Changed Packages

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Pre-flight Verification & File Operations
        ensure_install_pkg_dir_clean
        copy_ignore_and_config_files
    Layer 2: Diff Computation & Classification
        compute_package_stage_diff
    Layer 3: Single Package Physical Staging
        apply_package_stage_changes
    Layer 4: Staging Transaction & State Management
        stage_modified_packages
    Layer 5: Public Primitive Entry Point
        run_primitive_4_stage_render_to_install
===============================================================================
"""

import datetime
import shutil
import logging
import shlex
from pathlib import Path
from typing import List, Union, Optional, Sequence, Tuple, Dict, Mapping
from dataclasses import dataclass, field

from .constants import (
    PACKAGE_CONFIG_FILE_NAME,
    MANAGED_CONFIG_FILES,
    DRIFT_IGNORE_FILE_NAME,
    STOW_LOCAL_IGNORE_FILE_NAME,
)
from .workspace_config import WorkspaceConfig
from .package_config import (
    load_package_config_rendered,
    load_package_config_from_render_dir,
    PackageConfig,
)
from .file_utils import (
    file_contents_differ,
    backup_and_delete_one_file,
    remove_file_or_dir,
    atomic_copy_file,
    copy_file_mode_with_sudo,
)
from .folder_diff import compare_folders, FolderDiff
from .ignore import DriftIgnore
from .git_utils import has_uncommitted_modifications
from .state_registry import load_state_registry, save_state_registry, StateRegistry
from .exceptions import DriftDetectedError

logger = logging.getLogger(__name__)


@dataclass
class PackageStageChanges:
    """Represents comprehensive staging changes for a single package."""
    package_name: str
    deployable_changes: FolderDiff = field(default_factory=FolderDiff)
    physical_changes: FolderDiff = field(default_factory=FolderDiff)

    def __init__(
        self,
        package_name: str,
        deployable_changes: Optional[FolderDiff] = None,
        physical_changes: Optional[FolderDiff] = None,
    ) -> None:
        self.package_name = package_name
        self.deployable_changes = deployable_changes if deployable_changes is not None else FolderDiff()
        self.physical_changes = physical_changes if physical_changes is not None else self.deployable_changes

    @property
    def has_changes(self) -> bool:
        """Returns True if any physical file (payload, config, hooks) was added, modified, or deleted."""
        d = self.physical_changes
        return bool(d.added or d.modified or d.deleted)

    @property
    def has_deployable_changes(self) -> bool:
        """Returns True if any deployable payload file was added, modified, or deleted."""
        d = self.deployable_changes
        return bool(d.added or d.modified or d.deleted)

    @property
    def has_non_deployable_changes(self) -> bool:
        """Returns True if any metadata, hook, or non-deployable file was added, modified, or deleted."""
        p = self.physical_changes
        d = self.deployable_changes
        return (
            len(p.added) != len(d.added)
            or len(p.modified) != len(d.modified)
            or len(p.deleted) != len(d.deleted)
        )

    @property
    def non_deployable_changes(self) -> FolderDiff:
        """Returns physical changes that are not deployable payload files (e.g. metadata, config, hooks, ignored files)."""
        dep_added = set(self.deployable_changes.added)
        dep_modified = set(self.deployable_changes.modified)
        dep_deleted = set(self.deployable_changes.deleted)

        return FolderDiff(
            added=[p for p in self.physical_changes.added if p not in dep_added],
            modified=[p for p in self.physical_changes.modified if p not in dep_modified],
            deleted=[p for p in self.physical_changes.deleted if p not in dep_deleted],
        )



# =====================================================================
# Layer 1: Pre-flight Verification & File Operations
# =====================================================================

def ensure_install_pkg_dir_clean(install_base: Path, pkg: str) -> None:
    """Verifies that the package directory in install/ has no uncommitted local git changes."""
    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
        return
    if has_uncommitted_modifications(install_base, install_pkg_dir):
        raise DriftDetectedError(
            f"Package '{pkg}' in install directory has uncommitted local modifications. "
            "Please commit or stash your changes before staging, or use --force flag to bypass this check."
        )


def copy_ignore_and_config_files(
    render_pkg_dir: Path,
    install_pkg_dir: Path,
    ignore_handler: DriftIgnore,
) -> None:
    """Copies ignore and package config files from render/ to install/ and sets up Stow ignores."""
    # 1. Copy the physical .drift_ignore file to install/pkg dir if it was rendered in render/
    render_ignore = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
    if render_ignore.is_file():
        install_ignore = install_pkg_dir / DRIFT_IGNORE_FILE_NAME
        install_pkg_dir.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(render_ignore, install_ignore)

    # 2. Create physical .stow-local-ignore
    ignore_handler.create_stow_ignore_file(install_pkg_dir)

    # 3. Copy the drift_package.toml to install/pkg dir, this file must exist or an Error will be raised.
    render_config = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    if not render_config.is_file():
        raise FileNotFoundError(f"Missing required '{PACKAGE_CONFIG_FILE_NAME}' in render sandbox of package.")

    install_config = install_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    install_pkg_dir.mkdir(parents=True, exist_ok=True)
    atomic_copy_file(render_config, install_config)


# =====================================================================
# Layer 2: Diff Computation & Classification
# =====================================================================

def compute_package_stage_diff(
    pkg: str,
    install_base: Path,
    render_base: Path,
) -> Tuple[PackageStageChanges, DriftIgnore]:
    """Computes deployable and physical file changes between render/ and install/ for a single package.

    Returns:
        Tuple of (stage_changes, ignore_handler):
        - stage_changes: PackageStageChanges containing both deployable and physical changes.
        - ignore_handler: The DriftIgnore instance for the package.
    """
    install_pkg_dir = install_base / pkg
    render_pkg_dir = render_base / pkg

    if not render_pkg_dir.exists():
        raise RuntimeError(f"Render sandbox directory for package '{pkg}' does not exist. Please render first.")

    ignore_handler = DriftIgnore.load_from_dir(render_pkg_dir)

    # 1. Compute deployable changes with ignore_handler for the function output
    deploy_diff = compare_folders(
        src_dir=render_pkg_dir,
        dst_dir=install_pkg_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=False,
    )

    # 2. Compute all physical file changes without ignore_handler to stage everything into install/
    all_diff = compare_folders(
        src_dir=render_pkg_dir,
        dst_dir=install_pkg_dir,
        ignore_handler=None,
        resolve_symlinks=False,
    )
    # Exclude MANAGED_CONFIG_FILES from deleted list as they are managed/generated in install/
    all_diff.deleted = [p for p in all_diff.deleted if p.name not in MANAGED_CONFIG_FILES]

    stage_changes = PackageStageChanges(
        package_name=pkg,
        deployable_changes=deploy_diff,
        physical_changes=all_diff,
    )

    return stage_changes, ignore_handler


# =====================================================================
# Layer 3: Single Package Physical Staging
# =====================================================================

def apply_package_stage_changes(
    pkg: str,
    install_base: Path,
    render_base: Path,
    backup_base: Path,
    stage_changes: PackageStageChanges,
    ignore_handler: DriftIgnore,
) -> None:
    """Applies calculated physical deletions, additions, and modifications for a single package into install/."""
    install_pkg_dir = install_base / pkg
    render_pkg_dir = render_base / pkg
    backup_dir = backup_base / pkg / "deleted_files"
    all_diff = stage_changes.physical_changes

    # A. Process Deletions (clear obsolete paths and handle multi-level type changes first)
    for rel_file in all_diff.deleted:
        if rel_file.name in MANAGED_CONFIG_FILES:
            continue
        install_file = install_pkg_dir / rel_file
        if not install_file.exists() and not install_file.is_symlink():
            continue

        # No symlink should exist in deleted files,
        # but if they do, remove them without backup.
        if install_file.is_symlink():
            logger.warning(f"⚠️  [BUG] Unexpected symlink found for deletion: {pkg}/{rel_file}. Removing without backup.")
            remove_file_or_dir(install_file)
            continue

        # If directory exists in deleted list, it means it's an empty directory that should be removed. Remove it without backup.
        if install_file.is_dir():
            if any(install_file.iterdir()):
                logger.warning(f"⚠️  [BUG] Non-empty directory found for deletion: {pkg}/{rel_file}. Removing without backup.")
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
        atomic_copy_file(src, dst)

    # C. Process Modifications
    for rel_file in all_diff.modified:
        src = render_pkg_dir / rel_file
        dst = install_pkg_dir / rel_file
        if src.is_dir() and not src.is_symlink():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        logger.info(f"🔄 Modifying: {pkg}/{rel_file}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if all_diff.is_mode_only_change(rel_file, render_pkg_dir, install_pkg_dir):
            copy_file_mode_with_sudo(src, dst, sudo=False)
        else:
            atomic_copy_file(src, dst)

    # Copy ignore and config files (handles .stow-local-ignore and drift_package.toml)
    copy_ignore_and_config_files(
        render_pkg_dir=render_pkg_dir,
        install_pkg_dir=install_pkg_dir,
        ignore_handler=ignore_handler,
    )


# =====================================================================
# Layer 4: Staging Transaction & State Management
# =====================================================================

def stage_modified_packages(
    packages_to_stage: Mapping[str, Tuple[PackageStageChanges, DriftIgnore]],
    pkg_metadata: Mapping[str, PackageConfig],
    install_base: Path,
    render_base: Path,
    backup_base: Path,
    state_registry: StateRegistry,
) -> None:
    """Applies physical changes and manages staging state transitions for packages with changes."""
    if not packages_to_stage:
        return

    # 1. Check sudo privilege ONLY if any package with actual changes requires sudo
    needs_sudo = any(pkg_metadata[pkg].sudo for pkg in packages_to_stage.keys())
    if needs_sudo:
        from .file_utils import check_sudo_privilege
        check_sudo_privilege(True)

    # 2. Set state of packages with changes to "staging" before staging to prevent partial staging issues
    for pkg in packages_to_stage.keys():
        metadata = pkg_metadata[pkg]
        state_registry.set_package_state(pkg, "staging", install_method=metadata.install_method)
    state_registry.save()

    # 3. Apply stage changes to install/ directory for each package with changes
    for pkg, (changes, ignore_handler) in packages_to_stage.items():
        apply_package_stage_changes(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
            backup_base=backup_base,
            stage_changes=changes,
            ignore_handler=ignore_handler,
        )

    # 4. Set state of packages with changes to "staged" after successful staging
    for pkg in packages_to_stage.keys():
        state_registry.set_package_state(pkg, "staged")
    state_registry.save()


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def run_primitive_4_stage_render_to_install(
    workspace_config: WorkspaceConfig,
    target_pkgs: Union[str, Sequence[str]] = (),
    force: bool = False,
) -> Dict[str, PackageStageChanges]:
    """Reconciles the sandbox render/ folder into the install/ database (Primitive 4).

    Args:
        workspace_config: The workspace configuration instance.
        target_pkgs: Specific package name(s) to stage, or empty sequence for all active packages.
        force: If True, bypasses checks for midway failed package states ('staging' or 'deploying')
            and ignores uncommitted local modifications in the install/ directory.
            Note: Does NOT bypass 'enable_install = false' package configurations.

    Returns:
        A dictionary mapping package name to PackageStageChanges objects for all packages with changes.
    """
    if isinstance(target_pkgs, str):
        target_pkgs_seq: Sequence[str] = [target_pkgs]
    else:
        target_pkgs_seq = target_pkgs if target_pkgs else ()

    # Load active packages from render directory
    active_packages = workspace_config.get_rendered_packages(target_pkgs=target_pkgs_seq)

    # If active_packages is empty, we should just return empty dict and not proceed further.
    if not active_packages:
        logger.info("No active packages selected for staging. Skipping.")
        return {}

    render_base = workspace_config.render_path
    install_base = workspace_config.install_path
    backup_base = workspace_config.backup_path

    # 1. First find all active packages to process and load their metadata from RENDER directory
    # Filter out packages that are not enabled for installation/deployment.
    pkg_metadata = {}
    for pkg in active_packages:
        metadata = load_package_config_from_render_dir(render_base, pkg)
        if not metadata.enable_install:
            continue
        # Verify hook files exist and are regular files in render/ sandbox
        metadata.hooks.check_hook_files(render_base / pkg)
        pkg_metadata[pkg] = metadata

    # Check if active_packages is empty after filtering by enable_install, and if so, raise an error
    if not pkg_metadata:
        raise RuntimeError("No active packages are enabled for installation/deployment.")

    # 2. First verify package states from state registry before checking uncommitted changes.
    # If a package is in 'staging' or 'deploying' state, a previous operation failed midway
    # (which naturally causes uncommitted changes in install/), so we must report the mid-fail state first.
    state_file = install_base / "state.toml"
    state_registry = load_state_registry(state_file)
    if not force:
        midway_pkgs = state_registry.get_midway_packages(list(pkg_metadata.keys()))
        if midway_pkgs:
            pkg_names = [pkg for pkg, _ in midway_pkgs]
            pkg_cmd_str = shlex.join(pkg_names)
            details = ", ".join(f"'{p}' ({st})" for p, st in midway_pkgs)
            raise RuntimeError(
                f"Safety Abort: Package(s) in midway transaction state: {details}, "
                f"indicating a previous operation failed midway. "
                f"Please run 'drift rollback {pkg_cmd_str}' to restore a clean state before retrying."
            )

        # Check every package folder in install/ if it has uncommitted local modifications.
        # If so and the force flag is not present, raise a DriftDetectedError.
        for pkg in pkg_metadata.keys():
            ensure_install_pkg_dir_clean(install_base, pkg)

    logger.info(f"🔍 Staging {len(pkg_metadata)} packages: {', '.join(pkg_metadata.keys())}")

    # 3. Compute stage diffs and deployable changes for all packages
    computed_diffs = {}
    for pkg in pkg_metadata.keys():
        stage_changes, ignore_handler = compute_package_stage_diff(
            pkg=pkg,
            install_base=install_base,
            render_base=render_base,
        )
        computed_diffs[pkg] = (stage_changes, ignore_handler)

    # Identify packages that have physical stage changes
    packages_to_stage = {
        pkg: (changes, ignore_handler)
        for pkg, (changes, ignore_handler) in computed_diffs.items()
        if changes.has_changes
    }

    # 4. Apply physical changes and state transitions for modified packages
    if packages_to_stage:
        stage_modified_packages(
            packages_to_stage=packages_to_stage,
            pkg_metadata=pkg_metadata,
            install_base=install_base,
            render_base=render_base,
            backup_base=backup_base,
            state_registry=state_registry,
        )

    # 5. Prepare a dictionary of packages that had actual changes for return value
    changed_package_map = {
        pkg: changes for pkg, (changes, _) in computed_diffs.items()
        if changes.has_changes
    }

    # 6. Prepare summary of changes for logging
    if changed_package_map:
        logger.info("✨ Staging completed. Summary of changes:")
        for pkg_change in changed_package_map.values():
            dep = pkg_change.deployable_changes
            non_dep = pkg_change.non_deployable_changes
            logger.info(f"   Package '{pkg_change.package_name}': "
                        f"+{len(dep.added)}, "
                        f"~{len(dep.modified)}, "
                        f"-{len(dep.deleted)}")
            if pkg_change.has_non_deployable_changes:
                logger.info(f"     (metadata/hooks: "
                            f"+{len(non_dep.added)}, "
                            f"~{len(non_dep.modified)}, "
                            f"-{len(non_dep.deleted)})")
    else:
        logger.info("✨ Staging completed. No changes detected.")

    return changed_package_map
