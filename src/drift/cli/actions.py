import os
from typing import Optional

from ..workspace_config import load_workspace_config
from ..render_package import render_package, render_all_packages


def execute_render(drift_root: str, package_name: Optional[str] = None) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    config_path = os.path.join(drift_root, "config", "drift.toml")
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
