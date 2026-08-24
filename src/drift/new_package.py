import logging
from pathlib import Path
from typing import Optional
from .workspace_config import WorkspaceConfig
from .constants import PACKAGE_CONFIG_FILE_NAME, PACKAGE_CONFIG_FILE_NAME_LIST

logger = logging.getLogger(__name__)

DEFAULT_PACKAGE_CONFIG_TEMPLATE = """# src/{package_name}/{config_filename}
[package]
name = "{package_name}"
install_method = "{install_method}"  # Options: "stow" (symlink) or "copy" (physical)
{target_directory_line}

# Lifecycle Hooks (Optional)
# pre_source   = ""
# pre_install  = ""
# post_install = ""
# pre_update   = ""
# post_update  = ""
# post_render  = ""
# hook_timeout = 120

# Advanced Flags
# sudo = false
# fully_controlled_dirs = []  # Sync deletions inside these directories
# enable_render = true
# enable_install = true
"""

def get_default_package_config_template() -> str:
    """Gets the default drift_package.toml template string."""
    return DEFAULT_PACKAGE_CONFIG_TEMPLATE


def run_primitive_10_create_new_package(
    workspace_config: WorkspaceConfig,
    package_name: str,
    force: bool = False,
    target_directory: Optional[str] = None,
    install_method: Optional[str] = None
) -> Path:
    """Scaffolds a new package directory and a default package configuration file."""
    package_dir = workspace_config.source_path / package_name
    
    package_dir.mkdir(parents=True, exist_ok=True)
    existing_info = workspace_config.find_source_file_for_rendered_names(package_dir, PACKAGE_CONFIG_FILE_NAME_LIST)
    if existing_info and not force:
        raise FileExistsError(
            f"Configuration file already exists: {existing_info.path}. "
            "Use --force to overwrite."
        )

    final_config_name = PACKAGE_CONFIG_FILE_NAME
    config_file = package_dir / final_config_name
    
    if target_directory is None:
        target_directory_line = '# target_directory = "~"   # Destination for this package'
    else:
        target_directory_line = f'target_directory = "{target_directory}"   # Destination for this package'

    final_install_method: str = install_method or workspace_config.default_install_method
    if final_install_method not in ("stow", "copy"):
        raise ValueError(f"install_method must be 'stow' or 'copy', got '{final_install_method}'")

    config_content = get_default_package_config_template().format(
        package_name=package_name,
        config_filename=final_config_name,
        target_directory_line=target_directory_line,
        install_method=final_install_method
    )
    config_file.write_text(config_content, encoding="utf-8")

    logger.info(f"✨ Package '{package_name}' created successfully!")
    logger.info(f"📝 Generated {final_config_name} at {config_file}")
    
    return package_dir
