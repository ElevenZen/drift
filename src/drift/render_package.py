"""Renders packages and compiles templates using pathlib."""

import shutil
import logging
from pathlib import Path
from typing import List, Optional

from .constants import DRIFT_IGNORE_FILE_NAME, PACKAGE_CONFIG_FILE_NAME, INITIAL_ENV
from .workspace_config import WorkspaceConfig, RenderEngineConfig, parse_secrets_env, load_env_settings, unload_env_settings
from .package_config import (
    load_package_config_from_source_dir,
    PackageConfig,
)
from .render_input import find_engine_for_file, render_input_templates
from .render_core import render_template_to_file

logger = logging.getLogger(__name__)


def trigger_post_render_hook(pkg_config: PackageConfig, workspace_config: WorkspaceConfig) -> None:
    """Trigger the post_render hook if it exists."""
    from .lifecycle_hooks import trigger_package_lifecycle_hook
    pkg = pkg_config.name
    render_pkg_dir = workspace_config.render_path / pkg
    
    if not render_pkg_dir.exists():
        return
        
    trigger_package_lifecycle_hook(
        pkg=pkg,
        hook_name="post_render",
        metadata=pkg_config,
        hook_dir=render_pkg_dir,
        cwd=render_pkg_dir
    )


def clear_render_package_dir(workspace_config: WorkspaceConfig, package_name: str) -> None:
    """Clears the sandbox package directory inside the render folder to preserve the render/.git repository."""
    render_pkg_dir = workspace_config.render_path / package_name
    if render_pkg_dir.exists():
        if render_pkg_dir.is_dir():
            shutil.rmtree(render_pkg_dir)
        else:
            render_pkg_dir.unlink()


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
        logger.info(f"🎨 Rendering: {relative_path} ({engine.name})")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")
        render_template_to_file(
            engine_config=engine,
            drift_root=workspace_config.drift_root,
            template_file_path=file_path,
            output_file_path=dest_path
        )
    else:
        dest_path = render_pkg_dir / relative_path
        logger.info(f"📄 Copying: {relative_path}")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, dest_path)



def prepare_package_config(package_dir: Path, package_name: str, workspace_config: WorkspaceConfig, render_pkg_dir: Path) -> Optional[PackageConfig]:
    """Loads package config and checks if rendering is enabled.

    Since load_package_config_from_source_dir always renders/writes the config file to render_pkg_dir,
    we no longer need to check is_static() or copy it manually here.
    """
    pkg_config = load_package_config_from_source_dir(
        package_dir=package_dir,
        package_name=package_name,
        workspace_config=workspace_config
    )

    if not pkg_config.enable_render:
        logger.info(f"Rendering is disabled for package '{package_name}'. Skipping.")
        return None

    return pkg_config


def handle_driftignore_file(package_dir: Path, render_pkg_dir: Path, package_name: str) -> None:
    """Handles warning and copying of drift ignore files."""
    misspelled_path = package_dir / ".driftignore"
    correct_path = package_dir / DRIFT_IGNORE_FILE_NAME

    if correct_path.exists():
        if correct_path.is_dir():
            raise ValueError(f"The path '{correct_path}' is a directory, but must be a file.")
        if misspelled_path.is_file():
            logger.warning(
                f"Both '{DRIFT_IGNORE_FILE_NAME}' and legacy '.driftignore' exist in package '{package_name}'. "
                f"The misspelled file '.driftignore' will be ignored; using '{DRIFT_IGNORE_FILE_NAME}'."
            )
        dest_correct = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(correct_path, dest_correct)
    elif misspelled_path.is_file():
        logger.warning(
            f"Package '{package_name}' contains a misspelled ignore file '.driftignore'. "
            "Please rename it to '.drift_ignore'."
        )
        dest_correct = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(misspelled_path, dest_correct)


def render_package(workspace_config: WorkspaceConfig, package_dir: Path) -> None:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = package_dir.name

    # Clear the target package render directory first to avoid sequence issues with template-rendered config files
    clear_render_package_dir(workspace_config, package_name)

    render_pkg_dir = workspace_config.render_path / package_name

    pkg_config = prepare_package_config(package_dir, package_name, workspace_config, render_pkg_dir)
    if not pkg_config:
        return

    handle_driftignore_file(package_dir, render_pkg_dir, package_name)

    # 3. Recursively process all other files inside the package directory
    from .ignore import DriftIgnore
    # Proactively check for nested ignore files and trigger clean validation
    DriftIgnore.load_from_dir(package_dir)

    from .folder_diff import compare_folders
    # Use compare_folders to walk every file in package_dir, resolving symlinks to directories
    diff = compare_folders(package_dir, render_pkg_dir, resolve_symlinks=True, src_only=True)

    for file in diff.added:
        file_path = package_dir / file

        # Skip if the file is the package config file or its template itself
        if pkg_config.is_package_config_file(file_path):
            continue

        # Skip any '.*' files (except .drift_ignore) in rendering process and print info
        if file.name.startswith(".") and file.name not in [".drift_ignore", ".driftignore"]:
            logger.info(f"ℹ️  [SKIP] Skipping hidden file '{file}' in package rendering. All hidden files must use the 'dot-' prefix in source templates.")
            continue

        if file == Path(".driftignore"):
            continue

        if ((file.name == ".driftignore" or file.name == ".drift_ignore")
                and file_path.parent != package_dir):
            raise ValueError("Ignore config '.drift_ignore' must be located at the root of the package directory.")

        render_or_copy_file(
            file_path=file_path,
            package_dir=package_dir,
            render_pkg_dir=render_pkg_dir,
            workspace_config=workspace_config
        )

    # Trigger post_render hook
    trigger_post_render_hook(pkg_config, workspace_config)
    logger.info(f"✨ Package '{package_name}' rendered successfully.")


def run_primitive_2_render_packages(
        workspace_config: WorkspaceConfig,
        target_pkgs: Optional[List[str]] = None) -> None:
    """Renders specific packages (if provided) or all enabled packages in the workspace."""
    # Parse the secrets from secrets.env file and load them, keeping track of original values to restore them on exit/failure.
    secrets = parse_secrets_env(workspace_config.drift_root)
    saved_envs = load_env_settings(secrets, overwrite=True, env_keep=INITIAL_ENV)
    try:
        # 1. Resolve and render engine input dependencies first (e.g. mustache.envst.json -> mustache.json)
        render_input_templates(
            engines=list(workspace_config.render_engine_configs.values()),
            drift_root=workspace_config.drift_root,
            workspace_config=workspace_config
        )

        # 2. Identify and render packages
        candidates = workspace_config.get_package_names_from_source_dir()
        active_packages = workspace_config.get_packages(
                candidates, target_pkgs, custom_dir=workspace_config.source_path)
        for package_name in active_packages:
            package_dir = workspace_config.source_path / package_name
            render_package(workspace_config, package_dir)
    finally:
        unload_env_settings(saved_envs)


def run_primitive_3_commit_render_repo(
    workspace_config: WorkspaceConfig,
    commit_message: str,
    target_pkgs: Optional[List[str]] = None
) -> None:
    """Stages and commits changes inside the render sandbox Git repository (Primitive 3).

    If target_pkgs is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    from .git_utils import commit_repo_changes

    render_dir = workspace_config.render_path
    
    committed = commit_repo_changes(
        repo_path=render_dir,
        commit_message=commit_message,
        target_pkgs=target_pkgs,
        repo_name="render repo"
    )
    
    if committed:
        logger.info(f"✨ Committed render repo changes with message: '{commit_message}'")
    else:
        logger.info("Nothing to commit, render repository is clean.")
