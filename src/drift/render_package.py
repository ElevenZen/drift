"""Renders packages and compiles templates using pathlib."""

import shutil
import logging
from pathlib import Path
from typing import List, Optional

from .constants import PACKAGE_CONFIG_FILE_NAME
from .workspace_config import WorkspaceConfig, RenderEngineConfig
from .package_config import (
    load_package_config_from_dir,
    PackageConfig,
)
from .dependency import find_engine_for_file
from .render_core import render_template_to_file

logger = logging.getLogger(__name__)


def clear_render_package_dir(workspace_config: WorkspaceConfig, package_name: str) -> None:
    """Clears the sandbox package directory inside the render folder to preserve the render/.git repository."""
    render_pkg_dir = workspace_config.render_path / package_name
    if render_pkg_dir.exists():
        if render_pkg_dir.is_dir():
            shutil.rmtree(render_pkg_dir)
        else:
            render_pkg_dir.unlink()


def is_package_config_file(file_path: Path, template_path: Optional[Path]) -> bool:
    """Checks if the given file path is the package config file or its template."""
    if not template_path:
        return False
    return file_path.resolve() == template_path.resolve()


def copy_static_package_config(render_pkg_dir: Path, pkg_config: PackageConfig) -> None:
    """Copies the package config file to the render package folder only if it is static."""
    src_path = pkg_config.config_template_path
    if not pkg_config.is_static():
        raise ValueError(f"Package config is not static: {pkg_config.name}")
    if not src_path or not src_path.is_file():
        raise FileNotFoundError(f"Package config file not found: {src_path}")
    dest_path = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, dest_path)
    logger.debug(f"Copied static package config from {src_path} to {dest_path}")


def render_or_copy_file(
    file_path: Path,
    package_dir: Path,
    render_pkg_dir: Path,
    workspace_config: WorkspaceConfig
) -> None:
    """Renders a single file using a matched engine, or copies it if no engine matches.

    Rendered files will have the engine suffix stripped in the output path.
    """
    relative_path = file_path.relative_to(package_dir)
    engines = list(workspace_config.render_engine_configs.values())
    engine: Optional[RenderEngineConfig] = find_engine_for_file(relative_path.as_posix(), engines)

    if engine:
        stripped_relative_path = engine.strip_suffix(relative_path.as_posix())
        dest_path = render_pkg_dir / stripped_relative_path
        logger.info(f"Rendering template '{relative_path}' using engine '{engine.name}' to '{dest_path}'")
        render_template_to_file(
            engine_config=engine,
            drift_root=workspace_config.drift_root,
            template_file_path=file_path,
            output_file_path=dest_path
        )
    else:
        dest_path = render_pkg_dir / relative_path
        logger.info(f"Copying static file '{relative_path}' to '{dest_path}'")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dest_path)


def render_package(workspace_config: WorkspaceConfig, package_dir: Path) -> None:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = package_dir.name

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

    render_pkg_dir = workspace_config.render_path / package_name

    if pkg_config.is_static():
        copy_static_package_config(render_pkg_dir, pkg_config)

    # Misspelled .driftignore warning and copy logic
    warned_misspelled = False
    misspelled_path = package_dir / ".driftignore"
    correct_path = package_dir / ".drift_ignore"
    if misspelled_path.is_file() and not correct_path.is_file():
        logger.warning(
            f"Package '{package_name}' contains a misspelled ignore file '.driftignore'. "
            "Please rename it to '.drift_ignore'."
        )
        warned_misspelled = True
        dest_correct = render_pkg_dir / ".drift_ignore"
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(misspelled_path, dest_correct)

    # 3. Recursively process all other files inside the package directory
    import os
    for root, _, files in os.walk(package_dir):
        for file in files:
            file_path = Path(root) / file

            # Skip if the file is the package config file or its template itself
            if is_package_config_file(file_path, pkg_config.config_template_path):
                continue

            if file == ".driftignore":
                if not warned_misspelled:
                    logger.warning(
                        f"Package '{package_name}' contains a misspelled ignore file '.driftignore'. "
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


def render_all_packages(workspace_config: WorkspaceConfig) -> None:
    """Renders all enabled packages discovered in the workspace config's source directory."""
    discovered = workspace_config.get_package_names_from_source_dir()
    for package_name in discovered:
        if workspace_config.is_package_enabled(package_name):
            package_dir = workspace_config.source_path / package_name
            logger.info(f"Rendering enabled package '{package_name}'...")
            render_package(workspace_config, package_dir)
        else:
            logger.debug(f"Skipping disabled package '{package_name}'.")


def run_primitive_2_render_packages(workspace_config: WorkspaceConfig, package_names: Optional[List[str]] = None) -> None:
    """Renders specific packages (if provided) or all enabled packages in the workspace."""
    if package_names:
        for pkg in package_names:
            package_dir = workspace_config.source_path / pkg
            if not package_dir.exists():
                raise FileNotFoundError(f"Package directory does not exist: {package_dir}")
            logger.info(f"Rendering package '{pkg}'...")
            render_package(workspace_config, package_dir)
    else:
        render_all_packages(workspace_config)


def run_primitive_3_commit_render_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    package_names: Optional[List[str]] = None
) -> None:
    """Stages and commits changes inside the render sandbox Git repository (Primitive 3).

    If package_names is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    import subprocess

    render_dir = workspace_config.render_path
    if not render_dir.exists():
        raise FileNotFoundError(f"Render directory does not exist: {render_dir}")

    # 1. Stage changes (scoped to package folders if provided, otherwise all changes)
    if package_names:
        add_cmd = ["git", "-C", str(render_dir), "add"]
        for pkg in package_names:
            add_cmd.append(f"{pkg}/")
    else:
        add_cmd = ["git", "-C", str(render_dir), "add", "-A"]

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

    # 2. Check if there are staged changes to commit (scoped to package folders if provided)
    if package_names:
        status_cmd = ["git", "-C", str(render_dir), "status", "--porcelain"]
        for pkg in package_names:
            status_cmd.append(f"{pkg}/")
    else:
        status_cmd = ["git", "-C", str(render_dir), "status", "--porcelain"]

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
            ["git", "-C", str(render_dir), "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(f"Committed render repo changes with message: '{commit_message}'")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to commit changes in render repo. Stderr: {e.stderr}")
        raise RuntimeError(f"Failed to commit changes in render repo: {e.stderr}") from e
