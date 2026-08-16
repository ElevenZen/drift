import os
from typing import Optional

from ..constants import CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME
from ..workspace_config import load_workspace_config
from ..render_package import render_package, render_all_packages
from ..init_repo import init_drift_workspace


def execute_init(drift_root: str, force: bool = False) -> None:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    init_drift_workspace(drift_root, force=force)


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
