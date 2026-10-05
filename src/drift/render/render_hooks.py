"""Phase 2 Lifecycle Hooks rendering, permission enforcement, and Merkle DAG digestion.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Pipeline Orchestration & Permissions
    - render_hooks(workspace_config, pkg_config, ...) -> DigestionResult
    - ensure_configured_hook_permissions(workspace_config, pkg_config) -> None

Layer 1: DAG Construction & Path Translation
    - build_phase2_hooks_dag(workspace_config, pkg_config, ...) -> PackageHooksNode
===============================================================================
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig
    from ..config.package_config import PackageConfig
    from ..config.render_engine_config import RenderEngineRegistry

from ..core.constants import (
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DirMode,
)
from ..core.folder_diff import list_folder_paths
from ..utils.path_utils import to_relative_path
from .render_dag import (
    Node,
    JsonNode,
    DirectoryNode,
    UnknownPathNode,
    PackageHooksNode,
)
from .render_digester import (
    DigestionContext,
    DigestionResult,
    digest_render_dag,
)
from .render_expansion import (
    ExpansionContext,
    expand_node_dependencies,
    translate_path,
)
from .render_lock import RenderLockfile, RenderBucket

logger = logging.getLogger(__name__)


def build_phase2_hooks_dag(
    workspace_config: "WorkspaceConfig",
    pkg_config: "PackageConfig",
    engines_override: Optional["RenderEngineRegistry"] = None,
) -> PackageHooksNode:
    """Constructs and expands the Phase 2 AST Merkle DAG for all files and directories in drift_hooks/."""
    pkg_name = pkg_config.name
    hooks_src_dir = workspace_config.source_path / pkg_name / DRIFT_HOOKS_DIR_NAME

    if not hooks_src_dir.is_dir():
        return PackageHooksNode(pkg_name=pkg_name, hook_nodes=[])

    effective_engines = (
        engines_override
        if engines_override is not None
        else pkg_config.package_render_engines(workspace_config)
    )

    src_prefix = workspace_config.source_path / pkg_name / DRIFT_HOOKS_DIR_NAME
    dst_prefix = workspace_config.render_path / pkg_name / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
    translation_map = {src_prefix: dst_prefix}

    all_items = list_folder_paths(
        src_dir=hooks_src_dir,
        base_rel=src_prefix,
        dir_mode=DirMode.ONLY_EMPTY_DIR,
    )

    initial_nodes: list[Node] = []
    for item in all_items:
        initial_nodes.append(UnknownPathNode(item))

    hooks_root = PackageHooksNode(pkg_name=pkg_name, hook_nodes=initial_nodes)

    env_node = JsonNode(pkg_config.env_resolve.effective_dict)
    exp_ctx = ExpansionContext(
        package_name=pkg_name,
        enable_render=True,
        env_node=env_node,
        render_engines=effective_engines,
        cache=workspace_config.render_cache,
        path_translation=translation_map,
        drift_root=workspace_config.drift_root,
    )
    expand_node_dependencies(hooks_root, exp_ctx)

    return hooks_root


def ensure_configured_hook_permissions(
    workspace_config: "WorkspaceConfig",
    pkg_config: "PackageConfig",
    engines_override: Optional["RenderEngineRegistry"] = None,
    dry_run: bool = False,
) -> None:
    """Ensures configured lifecycle hook files have executable permissions (0o755) on POSIX.

    Operates strictly as a post-process on the hook files referenced in pkg_config.hooks,
    updating both the source file in src/ (whether static or template) and the rendered file in render/.drift/hooks/.
    When dry_run is True, permissions modification is skipped.
    """
    if dry_run or sys.platform == "win32":
        return

    src_pkg_dir = workspace_config.source_path / pkg_config.name
    render_pkg_dir = workspace_config.render_path / pkg_config.name
    hook_dest_dir = render_pkg_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME

    engines = (
        engines_override
        if engines_override is not None
        else pkg_config.package_render_engines(workspace_config)
    )

    for rel_hook_str in pkg_config.hooks.configured_relative_paths:
        rel_hook = Path(rel_hook_str)
        candidate_dirs = [ src_pkg_dir / rel_hook.parent, ]
        for src_dir in candidate_dirs:
            if not src_dir.is_dir():
                continue
            match = engines.find_source_file_for_rendered_names(src_dir, [rel_hook.name])
            if not match:
                continue
            try:
                src_mode = match.path.stat().st_mode
                if not (src_mode & 0o111):
                    match.path.chmod(src_mode | 0o755)
            except Exception as e:
                logger.debug(f"Could not chmod source hook file '{match.path}': {e}")

        # Check rendered file in render/.drift/hooks/
        dest_path = hook_dest_dir / to_relative_path(rel_hook, Path(DRIFT_HOOKS_DIR_NAME))
        if dest_path.is_file():
            try:
                dest_mode = dest_path.stat().st_mode
                if not (dest_mode & 0o111):
                    dest_path.chmod(dest_mode | 0o755)
            except Exception as e:
                logger.debug(f"Could not chmod rendered hook file '{dest_path}': {e}")


def render_hooks(
    workspace_config: "WorkspaceConfig",
    pkg_config: "PackageConfig",
    engines_override: Optional["RenderEngineRegistry"] = None,
    dry_run: bool = False,
) -> DigestionResult:
    """Renders all lifecycle hooks in src/<pkg>/drift_hooks into render/<pkg>/.drift/hooks.

    Digests the Phase 2 Merkle DAG under pkg_config.package_envs(), prunes obsolete hook files,
    updates lockfile.hook_hashes, and ensures executable permissions on all configured hooks.
    """
    hooks_root = build_phase2_hooks_dag(
        workspace_config=workspace_config,
        pkg_config=pkg_config,
        engines_override=engines_override,
    )

    pkg_render_dir = workspace_config.render_path / pkg_config.name
    lockfile = RenderLockfile.load_from_dir(pkg_render_dir)

    ctx = DigestionContext(
        drift_root=workspace_config.drift_root,
        package_name=pkg_config.name,
        package_render_dir=pkg_render_dir,
        lockfile=lockfile,
        bucket=RenderBucket.HOOKS,
        cache=workspace_config.render_cache,
        dry_run=dry_run,
    )

    with pkg_config.package_envs():
        digest_render_dag(hooks_root, ctx)

    ensure_configured_hook_permissions(
        workspace_config=workspace_config,
        pkg_config=pkg_config,
        engines_override=engines_override,
        dry_run=dry_run,
    )
    return ctx.result
