"""Core action implementations for drift CLI backend triggers using pathlib."""

import logging
from pathlib import Path
from typing import Optional, List

from ..constants import CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME
from ..workspace_config import load_workspace_config
from ..render_package import render_package, render_all_packages
from ..init_repo import init_drift_workspace
from ..check_repo import get_drift_root

logger = logging.getLogger(__name__)

# Disable unused import warning for get_drift_root, as it may be used in CLI backends.
_ = get_drift_root


def execute_init(drift_root: Path, force: bool = False, no_git_root: bool = False) -> None:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    init_drift_workspace(drift_root, force=force, no_git_root=no_git_root)


def execute_render(drift_root: Path, package_name: Optional[str] = None) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    config_path = drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    workspace_config = load_workspace_config(config_path)

    if package_name:
        package_dir = workspace_config.source_path / package_name
        if not package_dir.exists():
            raise FileNotFoundError(f"Package directory does not exist: {package_dir}")

        render_package(workspace_config, package_dir)
    else:
        render_all_packages(workspace_config)


def execute_stage(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False) -> None:
    """Core function to execute staging from render to install, shared by both CLI backends."""
    from ..stage_repo import run_primitive_4_stage_render_to_install

    config_path = drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    workspace_config = load_workspace_config(config_path)

    # Convert single string to a list for robustness
    if isinstance(package_names, str):
        package_names = [package_names]

    changes = run_primitive_4_stage_render_to_install(workspace_config, target_pkgs=package_names, force=force)
    if not changes:
        logger.info("No changes staged. All files are up-to-date.")
    else:
        for pkg_change in changes:
            logger.info(f"Package '{pkg_change.package_name}' staged changes:")
            for file in pkg_change.added_files:
                logger.info(f"  [+] {file.as_posix()}")
            for file in pkg_change.modified_files:
                logger.info(f"  [*] {file.as_posix()}")
            for file in pkg_change.deleted_files:
                logger.info(f"  [-] {file.as_posix()}")


def execute_apply(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False) -> None:
    """Core function to execute state application (apply), shared by both CLI backends."""
    from ..install_repo import run_primitive_5_install_deployment

    config_path = drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    workspace_config = load_workspace_config(config_path)

    # Convert single string to a list for robustness
    if isinstance(package_names, str):
        package_names = [package_names]

    # Determine packages to redeploy
    if package_names:
        packages_to_redeploy = []
        discovered = workspace_config.get_package_names_from_install_dir()
        for pkg in package_names:
            if pkg in discovered or force:
                packages_to_redeploy.append(pkg)
            else:
                raise ValueError(f"Target package '{pkg}' was not discovered in install directory '{workspace_config.install_directory}'. "
                                 f"Use --force to force {pkg} deployment.")
    else:
        # Redeploy all active packages currently inside install/ State Database
        packages_to_redeploy = workspace_config.get_package_names_from_install_dir()

    run_primitive_5_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=packages_to_redeploy,
        resolve_symlinks=True,
        force=force,
        package_changes=None
    )


def execute_render_commit(drift_root: Path, message: str, package_name: Optional[str] = None) -> None:
    """Core function to execute committing render repository changes, shared by both CLI backends."""
    from ..render_package import commit_render_repo

    config_path = drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    workspace_config = load_workspace_config(config_path)

    commit_render_repo(workspace_config, commit_message=message, package_name=package_name)
