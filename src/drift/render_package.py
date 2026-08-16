import os
import shutil
import logging
from typing import List, Optional

from .constants import PACKAGE_CONFIG_FILE_NAME
from .workspace_config import WorkspaceConfig
from .package_config import (
    load_package_config_from_dir,
    PackageConfig,
)
from .dependency import find_engine_for_file, strip_engine_suffix
from .render_core import render_template_to_file

logger = logging.getLogger(__name__)


def clear_render_package_dir(workspace_config: WorkspaceConfig, package_name: str) -> None:
    """Clears the sandbox package directory inside the render folder to preserve the render/.git repository."""
    render_pkg_dir = os.path.join(
        workspace_config.drift_root_path,
        workspace_config.render_directory,
        package_name
    )
    if os.path.exists(render_pkg_dir):
        if os.path.isdir(render_pkg_dir):
            shutil.rmtree(render_pkg_dir)
        else:
            os.remove(render_pkg_dir)


def is_package_config_file(file_path: str, template_path: Optional[str]) -> bool:
    """Checks if the given file path is the package config file or its template."""
    if not template_path:
        return False
    return os.path.abspath(file_path) == os.path.abspath(template_path)


def copy_static_package_config(render_pkg_dir: str, pkg_config: PackageConfig) -> None:
    """Copies the package config file to the render package folder if it is static."""
    src_path = pkg_config.config_template_path
    if not src_path or not os.path.isfile(src_path):
        raise FileNotFoundError(f"Package config file not found: {src_path}")
    dest_path = os.path.join(render_pkg_dir, PACKAGE_CONFIG_FILE_NAME)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copy2(src_path, dest_path)
    logger.debug(f"Copied static package config from {src_path} to {dest_path}")


def render_or_copy_file(
    file_path: str,
    package_dir: str,
    render_pkg_dir: str,
    workspace_config: WorkspaceConfig
) -> None:
    """Renders a single file using a matched engine, or copies it if no engine matches."""
    relative_path = os.path.relpath(file_path, package_dir)
    engines = list(workspace_config.render_engine_configs.values())
    engine = find_engine_for_file(relative_path, engines)

    if engine:
        stripped_relative_path = strip_engine_suffix(relative_path, engine.suffix)
        dest_path = os.path.join(render_pkg_dir, stripped_relative_path)
        logger.info(f"Rendering template '{relative_path}' using engine '{engine.name}' to '{dest_path}'")
        render_template_to_file(
            engine_config=engine,
            drift_root=workspace_config.drift_root_path,
            template_file_path=file_path,
            output_file_path=dest_path
        )
    else:
        dest_path = os.path.join(render_pkg_dir, relative_path)
        logger.info(f"Copying static file '{relative_path}' to '{dest_path}'")
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(file_path, dest_path)


def render_package(workspace_config: WorkspaceConfig, package_dir: str) -> None:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = os.path.basename(os.path.normpath(package_dir))

    # Clear the target package render directory first to avoid sequence issues with template-rendered config files
    clear_render_package_dir(workspace_config, package_name)

    # Load package config from directory
    # (which automatically renders package.envst.toml if it is a template)
    pkg_config = load_package_config_from_dir(
        package_dir=package_dir,
        package_name=package_name,
        workspace_config=workspace_config
    )

    if not pkg_config.enable_render:
        logger.info(f"Rendering is disabled for package '{package_name}'. Skipping.")
        return

    render_pkg_dir = os.path.join(
        workspace_config.drift_root_path,
        workspace_config.render_directory,
        package_name
    )

    if pkg_config.is_static():
        copy_static_package_config(render_pkg_dir, pkg_config)

    # Misspelled .driftignore warning and copy logic
    warned_misspelled = False
    misspelled_path = os.path.join(package_dir, ".driftignore")
    correct_path = os.path.join(package_dir, ".drift_ignore")
    if os.path.isfile(misspelled_path) and not os.path.isfile(correct_path):
        logger.warning(
            f"⚠️ Warning: Package '{package_name}' contains a misspelled ignore file '.driftignore'. "
            "Please rename it to '.drift_ignore'."
        )
        warned_misspelled = True
        dest_correct = os.path.join(render_pkg_dir, ".drift_ignore")
        os.makedirs(os.path.dirname(dest_correct), exist_ok=True)
        shutil.copy2(misspelled_path, dest_correct)

    # 3. Recursively process all other files inside the package directory
    for root, _, files in os.walk(package_dir):
        for file in files:
            file_path = os.path.join(root, file)

            # Skip if the file is the package config file or its template itself
            if is_package_config_file(file_path, pkg_config.config_template_path):
                continue

            if file == ".driftignore":
                if not warned_misspelled:
                    logger.warning(
                        f"⚠️ Warning: Package '{package_name}' contains a misspelled ignore file '.driftignore'. "
                        "Please rename it to '.drift_ignore'."
                    )
                    warned_misspelled = True
                continue

            render_or_copy_file(
                file_path=file_path,
                package_dir=package_dir,
                render_pkg_dir=render_pkg_dir,
                workspace_config=workspace_config
            )


def get_discovered_packages(workspace_config: WorkspaceConfig) -> List[str]:
    """Finds all potential package subdirectory names within the source directory."""
    source_dir = os.path.join(workspace_config.drift_root_path, workspace_config.source_directory)
    if not os.path.exists(source_dir) or not os.path.isdir(source_dir):
        return []

    packages = []
    for entry in os.listdir(source_dir):
        entry_path = os.path.join(source_dir, entry)
        if os.path.isdir(entry_path):
            packages.append(entry)
    return sorted(packages)


def is_package_enabled(workspace_config: WorkspaceConfig, package_name: str) -> bool:
    """Checks if a package is enabled based on WorkspaceConfig packages list or packages_enable_default."""
    if package_name in workspace_config.packages_enable:
        return workspace_config.packages_enable[package_name]
    return workspace_config.packages_enable_default


def render_all_packages(workspace_config: WorkspaceConfig) -> None:
    """Renders all enabled packages discovered in the workspace config's source directory."""
    discovered = get_discovered_packages(workspace_config)
    for package_name in discovered:
        if is_package_enabled(workspace_config, package_name):
            package_dir = os.path.join(
                workspace_config.drift_root_path,
                workspace_config.source_directory,
                package_name
            )
            logger.info(f"Rendering enabled package '{package_name}'...")
            render_package(workspace_config, package_dir)
        else:
            logger.debug(f"Skipping disabled package '{package_name}'.")


def commit_render_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    package_name: Optional[str] = None
) -> None:
    """Stages and commits changes inside the render sandbox Git repository (Primitive 3).

    If package_name is specified, only that package's subdirectory is staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    import subprocess

    render_dir = os.path.join(workspace_config.drift_root_path, workspace_config.render_directory)
    if not os.path.exists(render_dir):
        raise FileNotFoundError(f"Render directory does not exist: {render_dir}")

    # 1. Stage changes (scoped to package folder if provided, otherwise all changes)
    if package_name:
        add_cmd = ["git", "-C", render_dir, "add", f"{package_name}/"]
    else:
        add_cmd = ["git", "-C", render_dir, "add", "-A"]

    try:
        subprocess.run(
            add_cmd,
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to stage changes in render repo. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to stage changes in render repo: {e.stderr}") from e

    # 2. Check if there are staged changes to commit (scoped to package folder if provided)
    if package_name:
        status_cmd = ["git", "-C", render_dir, "status", "--porcelain", f"{package_name}/"]
    else:
        status_cmd = ["git", "-C", render_dir, "status", "--porcelain"]

    try:
        status_res = subprocess.run(
            status_cmd,
            capture_output=True,
            text=True,
            check=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check git status in render repo. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to check git status in render repo: {e.stderr}") from e

    if not status_res.stdout.strip():
        logger.info("Nothing to commit, render repository is clean.")
        return

    # 3. Perform git commit with the given commit message
    try:
        subprocess.run(
            ["git", "-C", render_dir, "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(f"Committed render repo changes with message: '{commit_message}'")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit changes in render repo. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to commit changes in render repo: {e.stderr}") from e
