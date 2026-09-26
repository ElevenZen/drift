"""Renders packages and compiles templates using pathlib."""

from __future__ import annotations

import sys
import shutil
import logging
from dataclasses import replace
from pathlib import Path
from typing import List, Tuple, Optional, Sequence, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig
    from ..config.package_config import PackageConfig
    from ..config.render_engine_config import RenderEngineConfig, RenderEngineRegistry

from ..core.constants import (
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_LEGACY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
)
from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from .render_input import render_input_templates
from .render_core import render_template_to_file, RenderError
from ..core.exceptions import ConfigError, RenderCollisionError, HookMissingError, is_logged, mark_logged, is_drift_error
from ..hooks.lifecycle_hooks import trigger_pre_source_hook, HookExecFlags
from ..core.result_models import PackageRenderResult, RenderResult
from ..utils.file_ops import remove, copy_file
from ..utils.path_utils import encode_dot_prefix, is_relative_to

logger = logging.getLogger(__name__)


def clear_render_package_dir(workspace_config: WorkspaceConfig, package_name: str) -> None:
    """Clears the sandbox package directory inside the render folder to preserve the render/.git repository."""
    render_pkg_dir = workspace_config.render_path / package_name
    if render_pkg_dir.exists() or render_pkg_dir.is_symlink():
        remove(render_pkg_dir)


def _validate_not_driftignore_target(
    file_path: Path,
    target_rel_path: str,
    package_name: str,
    is_template: bool = False,
) -> None:
    """Validates that a template or pseudo-dot file is not used to dynamically generate .driftignore."""
    target_name = Path(target_rel_path).name
    translated_name = encode_dot_prefix(Path(target_name)).name
    if translated_name in DRIFT_IGNORE_FILE_NAME_LIST:
        if is_template:
            raise ConfigError(
                f"Package '{package_name}' cannot render template '{file_path.name}' to '{target_rel_path}'. "
                f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
            )
        # check if the any file not in package root is being used to represent .driftignore
        if target_rel_path not in DRIFT_IGNORE_FILE_NAME_LIST:
            raise ConfigError(
                f"Package '{package_name}' cannot use '{file_path.name}' to represent '{DRIFT_IGNORE_FILE_NAME}'. "
                f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
            )


def ensure_rendered_file_hook_permissions(
    src_path: Path,
    dest_path: Path,
    rel_path: Path,
    pkg_config: PackageConfig,
) -> None:
    """Ensures rendered or copied lifecycle hook files have executable permissions (0o755) on POSIX.

    Since template rendering and atomic copying already preserve source file mode,
    we only need to check if the file matches a configured lifecycle hook.
    """
    if sys.platform == "win32":
        return

    rel_posix = rel_path.as_posix()
    hook_rel_posix = (Path(DRIFT_HOOKS_DIR_NAME) / rel_path).as_posix()
    if rel_posix not in pkg_config.hooks.configured_relative_paths and hook_rel_posix not in pkg_config.hooks.configured_relative_paths:
        return

    try:
        if dest_path.exists() and dest_path.is_file():
            dest_mode = dest_path.stat().st_mode
            if not (dest_mode & 0o111):
                dest_path.chmod(dest_mode | 0o755)
        if src_path.exists() and src_path.is_file():
            src_mode = src_path.stat().st_mode
            if not (src_mode & 0o111):
                src_path.chmod(src_mode | 0o755)
    except Exception as e:
        logger.debug(f"Could not ensure executable permission for hook file '{dest_path}': {e}")


def render_or_copy_file(
    rel_path: Path,
    src_dir: Path,
    dest_dir: Path,
    drift_root: Path,
    pkg_config: PackageConfig,
    render_engines: RenderEngineRegistry,
) -> Tuple[str, bool]:
    """Renders a single file using a matched engine, or copies it if no engine matches or rendering is disabled.

    Rendered files will have the engine suffix stripped in the output path.
    Returns (relative_dest_path, is_rendered).
    """
    file_path = src_dir / rel_path

    # If the item is a directory (e.g. empty directory or symlink to directory in source), create it in dest_dir without copying or rendering
    if file_path.is_dir():
        dest_path = dest_dir / rel_path
        logger.info(f"📁 Directory: {rel_path}")
        logger.debug(f"   -> {dest_path.relative_to(drift_root)}")
        dest_path.mkdir(parents=True, exist_ok=True)
        return (rel_path.as_posix(), False)

    engine: Optional[RenderEngineConfig] = None
    if pkg_config.package.enable_render:
        engine = render_engines.find_engine_for_file(rel_path.as_posix())

    if engine:
        stripped_relative_path = engine.strip_suffix(rel_path.as_posix())
        dest_path = dest_dir / stripped_relative_path
        logger.info(f"🎨 Rendering: {rel_path} ({engine.name})")
        logger.debug(f"   -> {dest_path.relative_to(drift_root)}")
        render_template_to_file(
            engine_config=engine,
            drift_root=drift_root,
            template_file_path=file_path,
            output_file_path=dest_path
        )
        dest_rel = stripped_relative_path
        is_rendered = True
    else:
        dest_path = dest_dir / rel_path
        logger.info(f"📄 Copying: {rel_path}")
        logger.debug(f"   -> {dest_path.relative_to(drift_root)}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        copy_file(file_path, dest_path)
        dest_rel = rel_path.as_posix()
        is_rendered = False

    return (dest_rel, is_rendered)


def render_package_file_entry(
    rel_path: Path,
    src_dir: Path,
    dest_dir: Path,
    drift_root: Path,
    pkg_config: PackageConfig,
    render_engines: RenderEngineRegistry,
    skip_drift_hooks: bool = False,
) -> Optional[Tuple[Path, bool]]:
    """Renders or copies a package file entry.

    Filters out package config files, .drift_ignore, unmapped hidden files,
    and optionally drift_hooks/ when rendering the payload pass.
    Returns (output_subpath, was_rendered) where output_subpath is relative to dest_dir,
    or None if the file should be skipped.
    """
    file_path = src_dir / rel_path

    # 1. Skip if the file is the package config file or its template itself
    if pkg_config.is_package_config_file(file_path):
        return None

    # 2. Skip any '.*' files (except .drift_ignore) in rendering process and print info
    if rel_path.name.startswith(".") and rel_path.name not in DRIFT_IGNORE_FILE_NAME_LIST:
        logger.info(
            f"ℹ️  [SKIP] Skipping hidden file '{rel_path}' in package rendering. "
            "All hidden files must use the 'dot-' prefix in source templates."
        )
        return None

    # 3. Handle root .drift_ignore (already handled by handle_driftignore_file) or reject nested ignore files
    if rel_path.name in DRIFT_IGNORE_FILE_NAME_LIST:
        return None

    # 4. Skip drift_hooks/ if requested (e.g. during payload rendering pass)
    # This flag is for allowing 'drift_hooks/drift_hooks' directory.
    if skip_drift_hooks and is_relative_to(rel_path, Path(DRIFT_HOOKS_DIR_NAME)):
        return None

    dest_rel_str, was_rendered = render_or_copy_file(
        rel_path=rel_path,
        src_dir=src_dir,
        dest_dir=dest_dir,
        drift_root=drift_root,
        pkg_config=pkg_config,
        render_engines=render_engines,
    )

    _validate_not_driftignore_target(
        file_path=file_path,
        target_rel_path=dest_rel_str,
        package_name=pkg_config.name,
        is_template=was_rendered,
    )

    # Ensure hook permissions on POSIX for declared lifecycle hooks
    ensure_rendered_file_hook_permissions(
        src_path=file_path,
        dest_path=dest_dir / dest_rel_str,
        rel_path=Path(dest_rel_str),
        pkg_config=pkg_config,
    )

    return Path(dest_rel_str), was_rendered


def render_subfolder_entries(
    src_dir: Path,
    dest_dir: Path,
    drift_root: Path,
    pkg_config: PackageConfig,
    render_engines: RenderEngineRegistry,
    written_destinations: Optional[Dict[str, Path]] = None,
    rendered_files: Optional[List[str]] = None,
    copied_files: Optional[List[str]] = None,
    dest_prefix: Path = Path("."),
    skip_drift_hooks: bool = False,
) -> None:
    """Renders or copies all files in src_dir into dest_dir, tracking collisions and file manifests."""
    from ..core.folder_diff import list_folder_paths

    written = written_destinations if written_destinations is not None else {}
    files = list_folder_paths(src_dir, resolve_symlinks=True)
    for file in files:
        res = render_package_file_entry(
            rel_path=file,
            src_dir=src_dir,
            dest_dir=dest_dir,
            drift_root=drift_root,
            pkg_config=pkg_config,
            render_engines=render_engines,
            skip_drift_hooks=skip_drift_hooks,
        )
        if res is None:
            continue

        output_subpath, was_rendered = res
        dest_key = (dest_prefix / output_subpath).as_posix() if dest_prefix != Path(".") else output_subpath.as_posix()
        if dest_key in written:
            prev_file = written[dest_key]
            raise RenderCollisionError(
                f"Multiple source files in package '{pkg_config.name}' render to the same destination path '{dest_key}': "
                f"'{prev_file}' and '{src_dir / file}'."
            )
        written[dest_key] = src_dir / file
        if rendered_files is not None and was_rendered:
            rendered_files.append(dest_key)
        elif copied_files is not None and not was_rendered:
            copied_files.append(dest_key)


def handle_driftignore_file(package_dir: Path, render_pkg_dir: Path) -> None:
    """Handles warning and copying of drift ignore files into .drift/ control plane."""
    package_name = package_dir.name
    misspelled_path = package_dir / DRIFT_IGNORE_LEGACY_FILE_NAME
    correct_path = package_dir / DRIFT_IGNORE_FILE_NAME
    dest_correct = render_pkg_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_IGNORE_FILE_NAME

    if correct_path.exists():
        if correct_path.is_dir():
            raise ValueError(f"The path '{correct_path}' is a directory, but must be a file.")
        if misspelled_path.is_file():
            logger.warning(
                f"Both '{DRIFT_IGNORE_FILE_NAME}' and legacy '{DRIFT_IGNORE_LEGACY_FILE_NAME}' exist in package '{package_name}'. "
                f"The misspelled file '{DRIFT_IGNORE_LEGACY_FILE_NAME}' will be ignored; using '{DRIFT_IGNORE_FILE_NAME}'."
            )
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        copy_file(correct_path, dest_correct)
    elif misspelled_path.is_file():
        logger.warning(
            f"Package '{package_name}' contains a misspelled ignore file '{DRIFT_IGNORE_LEGACY_FILE_NAME}'. "
            f"Please rename it to '{DRIFT_IGNORE_FILE_NAME}'."
        )
        dest_correct.parent.mkdir(parents=True, exist_ok=True)
        copy_file(misspelled_path, dest_correct)


def render_package_files(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    pkg_config: PackageConfig,
    render_pkg_dir: Path,
    hook_flags: HookExecFlags,
    render_engines: RenderEngineRegistry,
) -> PackageRenderResult:
    """
    Renders package source files, copies static assets, and triggers lifecycle hooks.
    Package-Level Env should be loaded by the caller of this function.
    """
    from ..core.ignore import DriftIgnore
    package_name = package_dir.name
    logger.info(f"📦 Rendering package '{package_name}'...")

    # Trigger pre_source hook before reading / processing source files
    trigger_pre_source_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        flags=hook_flags,
        pkg_config_override=pkg_config,
        engines_override=render_engines,
    )

    handle_driftignore_file(package_dir, render_pkg_dir)

    # 3. Recursively process all other files inside the package source directory to render
    # Proactively check for nested ignore files and trigger clean validation
    DriftIgnore.load_from_dir(package_dir, is_source=True)

    src_dir_to_render = pkg_config.get_source_directory_to_render(package_dir)
    if not src_dir_to_render.exists() or not src_dir_to_render.is_dir():
        raise FileNotFoundError(f"Package '{package_name}' source directory not found: '{src_dir_to_render}'")

    rendered_files: List[str] = []
    copied_files: List[str] = []
    written_destinations: Dict[str, Path] = {}

    # Pass 1: Render deployable payload from src_dir_to_render into render_pkg_dir
    render_subfolder_entries(
        src_dir=src_dir_to_render,
        dest_dir=render_pkg_dir,
        drift_root=workspace_config.drift_root,
        pkg_config=pkg_config,
        render_engines=render_engines,
        written_destinations=written_destinations,
        rendered_files=rendered_files,
        copied_files=copied_files,
        skip_drift_hooks=True,
    )

    # Pass 2: Render control plane lifecycle hooks from package_dir/drift_hooks into render_pkg_dir/.drift/hooks
    hooks_src_dir = package_dir / DRIFT_HOOKS_DIR_NAME
    if hooks_src_dir.is_dir():
        hook_dest_dir = render_pkg_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        hook_dest_prefix = Path(DRIFT_INTERNAL_DIR_NAME) / DRIFT_INTERNAL_HOOKS_DIR_NAME
        render_subfolder_entries(
            src_dir=hooks_src_dir,
            dest_dir=hook_dest_dir,
            drift_root=workspace_config.drift_root,
            pkg_config=pkg_config,
            render_engines=render_engines,
            written_destinations=written_destinations,
            rendered_files=rendered_files,
            copied_files=copied_files,
            dest_prefix=hook_dest_prefix,
            skip_drift_hooks=False,
        )

    # Trigger post_render hook
    pkg_config.hooks.trigger_post_render(
        flags=hook_flags,
    )
    logger.info(f"✨ Package '{package_name}' rendered successfully.")

    return PackageRenderResult(
        package=package_name,
        status="SUCCESS",
        rendered_files=rendered_files,
        copied_static_files=copied_files
    )


def prepare_package_render_engines(
    workspace_config: WorkspaceConfig,
    pkg_config: PackageConfig,
    render_pkg_dir: Path,
) -> RenderEngineRegistry:
    """
    Overlays package-level render engine configurations onto the workspace registry
    and renders any input file templates into the package's internal sandbox (.drift/render/).
    """
    effective_engines = pkg_config.package_render_engines(workspace_config)
    render_input_templates(
        engines=effective_engines,
        drift_root=workspace_config.drift_root,
        output_dir=render_pkg_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME,
    )
    return effective_engines


def render_package(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    flags: Optional[HookExecFlags] = None,
) -> PackageRenderResult:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = package_dir.name
    hook_flags = HookExecFlags.resolve(flags, settings=workspace_config.settings)

    # Clear the target package render directory first to avoid sequence issues with template-rendered config files
    clear_render_package_dir(workspace_config, package_name)

    render_pkg_dir = workspace_config.render_path / package_name

    pkg_config = PackageConfig.from_source_dir(
        package_dir=package_dir,
        workspace_config=workspace_config
    )

    scoped_flags = replace(hook_flags, load_envs=False)

    with pkg_config.package_envs():
        # Pre-flight Requirements Check (declarative host facts + dynamic probe hook)
        is_satisfied, failure_reason = pkg_config.evaluate_requirements(
            workspace_config, flags=scoped_flags
        )
        if not is_satisfied:
            logger.info(f"ℹ️  [SKIP] Skipping package '{package_name}': {failure_reason}")
            return PackageRenderResult(
                package=package_name,
                status="SKIPPED",
                skip_reason=failure_reason
            )

        # Stage 2 & 2.5: Merge package render engines and render intermediate input templates
        effective_engines = prepare_package_render_engines(
            workspace_config=workspace_config,
            pkg_config=pkg_config,
            render_pkg_dir=render_pkg_dir,
        )

        return render_package_files(
            workspace_config=workspace_config,
            pkg_config=pkg_config,
            package_dir=package_dir,
            render_pkg_dir=render_pkg_dir,
            hook_flags=scoped_flags,
            render_engines=effective_engines,
        )


def run_primitive_2_render_packages(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    flags: Optional[HookExecFlags] = None,
) -> RenderResult:
    """Renders specific packages (if provided) or all enabled packages in the workspace."""
    hook_flags = HookExecFlags.resolve(flags, settings=workspace_config.settings)
    results: List[PackageRenderResult] = []
    errors: List[Tuple[str, str, Exception]] = []
    # 1. Resolve and render engine input dependencies first (e.g. mustache.envst.json -> mustache.json)
    render_input_templates(
        engines=workspace_config.render_engine_configs,
        drift_root=workspace_config.drift_root,
        output_dir=workspace_config.render_path / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME,
    )

    # 2. Identify and render packages
    active_packages = workspace_config.filter_source_packages_by_target(target_packages=target_pkgs or None)
    for package_name in active_packages:
        package_dir = workspace_config.source_path / package_name
        try:
            pkg_res = render_package(
                workspace_config, package_dir, flags=hook_flags
            )
            results.append(pkg_res)
        except Exception as e:
            logger.debug(f"Render exception for package '{package_name}':", exc_info=True)
            if isinstance(e, HookMissingError):
                err_msg = f"Hook missing: {e}"
            elif isinstance(e, FileNotFoundError):
                err_msg = f"File not found: {e}"
            elif isinstance(e, RenderCollisionError):
                err_msg = f"Render collision: {e}"
            elif isinstance(e, RenderError):
                err_msg = f"Render failed: {e}"
            elif isinstance(e, ConfigError):
                err_msg = f"Config error: {e}"
            else:
                err_msg = f"Error: {e}"
            if is_logged(e):
                logger.error(f"❌ Failed to render package '{package_name}'.")
            else:
                logger.error(f"❌ Failed to render package '{package_name}': {err_msg}")
                mark_logged(e)
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
    target_pkgs: Sequence[str] = ()
) -> None:
    """Stages and commits changes inside the render sandbox Git repository (Primitive 3).

    If target_pkgs is specified, only those packages' subdirectories are staged and committed.
    If there are no changes to commit, it returns gracefully without raising an error.
    """
    from ..utils.git_utils import commit_repo_changes

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
