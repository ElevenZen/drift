import os
import logging
from typing import Optional

from ..constants import CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME
from ..workspace_config import load_workspace_config
from ..render_package import render_package, render_all_packages
from ..init_repo import init_drift_workspace
from ..check_repo import get_drift_root

logger = logging.getLogger(__name__)

# Disable unused import warning for get_drift_root, as it may be used in CLI backends.
_ = get_drift_root


def execute_init(drift_root: str, force: bool = False, no_git_root: bool = False) -> None:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    init_drift_workspace(drift_root, force=force, no_git_root=no_git_root)


def execute_render(drift_root: str, package_name: Optional[str] = None) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    config_path = os.path.join(drift_root, CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME)
    workspace_config = load_workspace_config(config_path)

    if package_name:
        package_dir = os.path.join(
            workspace_config.drift_root_path,
            workspace_config.source_directory,
            package_name
        )
        if not os.path.exists(package_dir):
            raise FileNotFoundError(f"Package directory does not exist: {package_dir}")

        render_package(workspace_config, package_dir)
    else:
        render_all_packages(workspace_config)


def execute_stage(drift_root: str, package_name: Optional[str] = None, force: bool = False) -> None:
    """Core function to execute staging from render to install, shared by both CLI backends."""
    from ..stage_repo import run_primitive_4_stage_render_to_install

    config_path = os.path.join(drift_root, CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME)
    workspace_config = load_workspace_config(config_path)

    changes = run_primitive_4_stage_render_to_install(workspace_config, target_pkg=package_name, force=force)
    if not changes:
        logger.info("No changes staged. All files are up-to-date.")
    else:
        for pkg_change in changes:
            logger.info(f"Package '{pkg_change.package_name}' staged changes:")
            for file in pkg_change.added_files:
                logger.info(f"  [+] {file}")
            for file in pkg_change.modified_files:
                logger.info(f"  [*] {file}")
            for file in pkg_change.deleted_files:
                logger.info(f"  [-] {file}")


def execute_render_commit(drift_root: str, message: str, package_name: Optional[str] = None) -> None:
    """Core function to execute committing render repository changes, shared by both CLI backends."""
    from ..render_package import commit_render_repo

    config_path = os.path.join(drift_root, CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME)
    workspace_config = load_workspace_config(config_path)

    commit_render_repo(workspace_config, commit_message=message, package_name=package_name)
