"""Renders packages and compiles templates using pathlib."""

from __future__ import annotations

import sys
import shutil
import logging
from pathlib import Path
from typing import List, Tuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .workspace_config import WorkspaceConfig, RenderEngineConfig
    from .package_config import PackageConfig

from .constants import (
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_LEGACY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    INITIAL_ENV,
)
from .workspace_config import secrets_env_scope, WorkspaceConfig
from .package_config import load_package_config_from_source_dir
from .render_input import find_engine_for_file, render_input_templates
from .render_core import render_template_to_file, RenderError
from .exceptions import ConfigError
from .lifecycle_hooks import trigger_pre_source_lifecycle_hook
from .result_models import PackageRenderResult, RenderResult
from .file_utils import remove_file_or_dir, atomic_copy_file, translate_dot_prefixes

logger = logging.getLogger(__name__)


def clear_render_package_dir(workspace_config: WorkspaceConfig, package_name: str) -> None:
    """Clears the sandbox package directory inside the render folder to preserve the render/.git repository."""
    render_pkg_dir = workspace_config.render_path / package_name
    if render_pkg_dir.exists() or render_pkg_dir.is_symlink():
        remove_file_or_dir(render_pkg_dir)


def render_or_copy_file(
    file_path: Path,
    package_dir: Path,
    render_pkg_dir: Path,
    workspace_config: WorkspaceConfig,
    pkg_config: PackageConfig
) -> Tuple[str, bool]:
    """Renders a single file using a matched engine, or copies it if no engine matches or rendering is disabled.

    Rendered files will have the engine suffix stripped in the output path.
    Returns (relative_dest_path, is_rendered).
    """
    relative_path = file_path.relative_to(package_dir)

    # If the item is a directory (e.g. empty directory or symlink to directory in source), create it in render/ without copying or rendering
    if file_path.is_dir():
        dest_path = render_pkg_dir / relative_path
        logger.info(f"📁 Directory: {relative_path}")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")
        dest_path.mkdir(parents=True, exist_ok=True)
        return (relative_path.as_posix(), False)

    engines = list(workspace_config.render_engine_configs.values())
    engine: Optional[RenderEngineConfig] = None
    if pkg_config.enable_render:
        engine = find_engine_for_file(relative_path.as_posix(), engines)

    if engine:
        stripped_relative_path = engine.strip_suffix(relative_path.as_posix())
        translated_dest = translate_dot_prefixes(Path(stripped_relative_path))
        if translated_dest.name in DRIFT_IGNORE_FILE_NAME_LIST:
            raise ConfigError(
                f"Package '{package_dir.name}' cannot render template '{file_path.name}' to '{stripped_relative_path}'. "
                f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
            )
        dest_path = render_pkg_dir / stripped_relative_path
        logger.info(f"🎨 Rendering: {relative_path} ({engine.name})")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")
        render_template_to_file(
            engine_config=engine,
            drift_root=workspace_config.drift_root,
            template_file_path=file_path,
            output_file_path=dest_path
        )
        dest_rel = stripped_relative_path
        is_rendered = True
    else:
        translated_dest = translate_dot_prefixes(relative_path)
        if (translated_dest.name in DRIFT_IGNORE_FILE_NAME_LIST
                and relative_path.as_posix() not in DRIFT_IGNORE_FILE_NAME_LIST):
            raise ConfigError(
                f"Package '{package_dir.name}' cannot use '{file_path.name}' to represent '{DRIFT_IGNORE_FILE_NAME}'. "
                f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
            )
        dest_path = render_pkg_dir / relative_path
        logger.info(f"📄 Copying: {relative_path}")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(file_path, dest_path)
        dest_rel = relative_path.as_posix()
        is_rendered = False

    # Ensure hook permissions on POSIX for declared lifecycle hooks
    ensure_rendered_file_hook_permissions(
        file_path=file_path,
        dest_path=dest_path,
        dest_rel=dest_rel,
        relative_path=relative_path,
        pkg_config=pkg_config
    )

    return (dest_rel, is_rendered)


def ensure_rendered_file_hook_permissions(
    file_path: Path,
    dest_path: Path,
    dest_rel: str,
    relative_path: Path,
    pkg_config: PackageConfig
) -> None:
    """Ensures rendered or copied lifecycle hook files have executable permissions (0o755) on POSIX.

    Since template rendering and atomic copying already preserve source file mode,
    we only need to check if the file matches a configured lifecycle hook.
    """
    if sys.platform == "win32":
        return

    configured_hooks = pkg_config.hooks.get_configured_hook_paths()
    if dest_rel not in configured_hooks and relative_path.as_posix() not in configured_hooks:
        return

    try:
        if dest_path.exists() and dest_path.is_file():
            dest_mode = dest_path.stat().st_mode
            if not (dest_mode & 0o111):
                dest_path.chmod(dest_mode | 0o755)
        if file_path.exists() and file_path.is_file():
            src_mode = file_path.stat().st_mode
            if not (src_mode & 0o111):
                file_path.chmod(src_mode | 0o755)
    except Exception as e:
        logger.debug(f"Could not ensure executable permission for hook file '{dest_path}': {e}")


def handle_driftignore_file(package_dir: Path, render_pkg_dir: Path) -> None:
    """Handles warning and copying of drift ignore files."""
    package_name = package_dir.name
    misspelled_path = package_dir / DRIFT_IGNORE_LEGACY_FILE_NAME
    correct_path = package_dir / DRIFT_IGNORE_FILE_NAME

    if correct_path.exists():
        if correct_path.is_dir():
            raise ValueError(f"The path '{correct_path}' is a directory, but must be a file.")
        if misspelled_path.is_file():
            logger.warning(
                f"Both '{DRIFT_IGNORE_FILE_NAME}' and legacy '{DRIFT_IGNORE_LEGACY_FILE_NAME}' exist in package '{package_name}'. "
                f"The misspelled file '{DRIFT_IGNORE_LEGACY_FILE_NAME}' will be ignored; using '{DRIFT_IGNORE_FILE_NAME}'."
            )
        dest_correct = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(correct_path, dest_correct)
    elif misspelled_path.is_file():
        logger.warning(
            f"Package '{package_name}' contains a misspelled ignore file '{DRIFT_IGNORE_LEGACY_FILE_NAME}'. "
            f"Please rename it to '{DRIFT_IGNORE_FILE_NAME}'."
        )
        dest_correct = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(misspelled_path, dest_correct)


def render_package_files(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    pkg_config: PackageConfig,
    render_pkg_dir: Path,
    no_hooks: bool = False
) -> PackageRenderResult:
    """Renders package source files, copies static assets, and triggers lifecycle hooks."""
    from .ignore import DriftIgnore
    from .folder_diff import list_folder_paths
    package_name = package_dir.name

    # Trigger pre_source hook before reading / processing source files
    trigger_pre_source_lifecycle_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        pkg_config=pkg_config,
        load_envs=False,
        no_hooks=no_hooks
    )

    handle_driftignore_file(package_dir, render_pkg_dir)

    # 3. Recursively process all other files inside the package source directory to render
    # Proactively check for nested ignore files and trigger clean validation
    DriftIgnore.load_from_dir(package_dir)

    src_dir_to_render = pkg_config.get_source_directory_to_render(package_dir)
    if not src_dir_to_render.exists() or not src_dir_to_render.is_dir():
        raise FileNotFoundError(f"Package '{package_name}' source directory not found: '{src_dir_to_render}'")

    # Use list_folder_paths to walk every file in src_dir_to_render, resolving symlinks to directories
    all_files = list_folder_paths(src_dir_to_render, resolve_symlinks=True)

    rendered_files: List[str] = []
    copied_files: List[str] = []

    for file in all_files:
        file_path = src_dir_to_render / file

        # Skip if the file is the package config file or its template itself
        if pkg_config.is_package_config_file(file_path):
            continue

        # Skip any '.*' files (except .drift_ignore) in rendering process and print info
        if file.name.startswith(".") and file.name not in DRIFT_IGNORE_FILE_NAME_LIST:
            logger.info(f"ℹ️  [SKIP] Skipping hidden file '{file}' in package rendering. All hidden files must use the 'dot-' prefix in source templates.")
            continue

        if file.name in DRIFT_IGNORE_FILE_NAME_LIST:
            if file_path.parent != package_dir:
                raise ValueError(f"Ignore config '{DRIFT_IGNORE_FILE_NAME}' must be located at the root of the package directory.")
            continue

        dest_rel, was_rendered = render_or_copy_file(
            file_path=file_path,
            package_dir=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            workspace_config=workspace_config,
            pkg_config=pkg_config
        )
        if was_rendered:
            rendered_files.append(dest_rel)
        else:
            copied_files.append(dest_rel)

    # Trigger post_render hook
    pkg_config.hooks.trigger_post_render(render_dir=render_pkg_dir, no_hooks=no_hooks)
    logger.info(f"✨ Package '{package_name}' rendered successfully.")

    return PackageRenderResult(
        package=package_name,
        status="SUCCESS",
        rendered_files=rendered_files,
        copied_static_files=copied_files
    )


def render_package(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    no_hooks: bool = False
) -> PackageRenderResult:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = package_dir.name

    # Clear the target package render directory first to avoid sequence issues with template-rendered config files
    clear_render_package_dir(workspace_config, package_name)

    render_pkg_dir = workspace_config.render_path / package_name

    pkg_config = load_package_config_from_source_dir(
        package_dir=package_dir,
        workspace_config=workspace_config
    )

    # Pre-flight Requirements Check (declarative host facts + dynamic probe hook)
    is_satisfied, failure_reason = pkg_config.evaluate_requirements(workspace_config, no_hooks=no_hooks)
    if not is_satisfied:
        logger.info(f"ℹ️  [SKIP] Skipping package '{package_name}': {failure_reason}")
        return PackageRenderResult(
            package=package_name,
            status="SKIPPED",
            skip_reason=failure_reason
        )

    with pkg_config.package_envs(workspace_config):
        return render_package_files(
            workspace_config=workspace_config,
            package_dir=package_dir,
            pkg_config=pkg_config,
            render_pkg_dir=render_pkg_dir,
            no_hooks=no_hooks
        )


def run_primitive_2_render_packages(
    workspace_config: WorkspaceConfig,
    target_pkgs: Optional[List[str]] = None,
    no_hooks: bool = False
) -> RenderResult:
    """Renders specific packages (if provided) or all enabled packages in the workspace."""
    results: List[PackageRenderResult] = []
    errors: List[Tuple[str, str, Exception]] = []
    with secrets_env_scope(workspace_config.drift_root):
        # 1. Resolve and render engine input dependencies first (e.g. mustache.envst.json -> mustache.json)
        render_input_templates(
            engines=list(workspace_config.render_engine_configs.values()),
            drift_root=workspace_config.drift_root,
            workspace_config=workspace_config
        )

        # 2. Identify and render packages
        active_packages = workspace_config.get_source_packages(target_pkgs=target_pkgs)
        for package_name in active_packages:
            package_dir = workspace_config.source_path / package_name
            try:
                pkg_res = render_package(workspace_config, package_dir, no_hooks=no_hooks)
                results.append(pkg_res)
            except Exception as e:
                logger.debug(f"Render exception for package '{package_name}':", exc_info=True)
                if isinstance(e, FileNotFoundError):
                    err_msg = f"File not found: {e}"
                elif isinstance(e, RenderError):
                    err_msg = f"Render failed: {e}"
                else:
                    err_msg = f"Error: {e}"
                logger.error(f"❌ Failed to render package '{package_name}': {err_msg}")
                errors.append((package_name, err_msg, e))
                results.append(PackageRenderResult(
                    package=package_name,
                    status="FAILED",
                    error=err_msg
                ))

        if errors:
            failed_pkgs_str = ", ".join(f"'{pkg}' ({err})" for pkg, err, _ in errors)
            return RenderResult(
                status="FAILED",
                packages=results,
                error_package=errors[0][0],
                error_message=f"Template rendering failed for package(s): {failed_pkgs_str}"
            )

        return RenderResult(
            status="SUCCESS",
            packages=results
        )


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
