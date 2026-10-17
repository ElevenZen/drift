"""Primitives 2 & 3: Workspace Package Rendering and Render Repository Commit.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Primitive Orchestration & Multi-Package Batch
    - run_primitive_2_render_packages(workspace_config, target_pkgs, options) -> RenderResult
        Coordinates multi-package template rendering via render_package() with shared workspace RenderCache.
    - run_primitive_3_commit_render_repo(workspace_config, commit_message, target_pkgs) -> None
        Stages and commits changes inside the render sandbox Git repository.
===============================================================================
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig

from ..core.exceptions import (
    ConfigError,
    HookMissingError,
    RenderCollisionError,
    is_logged,
    mark_logged,
)
from ..models import PackageRenderResult, RenderOptions, RenderResult
from ..render.render_core import RenderError
from ..render.render_package import render_package

logger = logging.getLogger(__name__)


def run_primitive_2_render_packages(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    options: Optional[RenderOptions] = None,
) -> RenderResult:
    """Renders specific packages (if provided) or all enabled packages in the workspace."""
    opts = RenderOptions.resolve(options)

    results: List[PackageRenderResult] = []
    errors: List[Tuple[str, str, Exception]] = []

    # 1. Identify and render packages
    active_packages = workspace_config.filter_source_packages_by_target(target_packages=target_pkgs or None)
    for package_name in active_packages:
        package_dir = workspace_config.source_path / package_name
        try:
            pkg_res = render_package(
                workspace_config, package_dir, options=opts
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
                error=err_msg,
            ))

    if errors:
        failed_pkgs_str = ", ".join(f"'{pkg}' ({err})" for pkg, err, _ in errors)
        return RenderResult(
            status="FAILED",
            packages=results,
            error_package=errors[0][0],
            error_message=f"Template rendering failed for package(s): {failed_pkgs_str}",
            dry_run=opts.dry_run,
        )

    return RenderResult(
        status="SUCCESS",
        packages=results,
        dry_run=opts.dry_run,
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
        repo_name="render repo",
    )

    if committed:
        logger.info(f"✨ Committed render repo changes with message: '{commit_message}'")
    else:
        logger.info("Nothing to commit, render repository is clean.")
