"""Phase 3 Package Payload rendering and Merkle DAG digestion.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: Single-Package Pipeline Orchestration
    - render_package(workspace_config, package_dir, options) -> PackageRenderResult
        Orchestrates Phase 1 (Config) -> Phase 2 (Hooks) -> Requirements -> Phase 3 (Payload).
    - render_package_files(workspace_config, package_dir, pkg_config, render_pkg_dir, options, render_engines) -> PackageRenderResult
        Digests Phase 3 Merkle DAG under pkg_config.package_envs(), updates lockfile, triggers post_render.
    - handle_driftignore_file(package_dir, render_pkg_dir, dry_run) -> None
        Synchronizes root .drift_ignore into .drift/ control plane.

Layer 1: DAG Construction & Candidate Filtering
    - build_phase3_payload_dag(workspace_config, pkg_config, package_dir, effective_engines) -> PackagePayloadNode
        Discovers payload candidates, excludes control plane files, and expands AST Merkle DAG.
===============================================================================
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.workspace_config import WorkspaceConfig
    from ..config.package_config import PackageConfig
    from ..config.render_engine_config import RenderEngineRegistry

from ..core.constants import (
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_LEGACY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_HOOKS_DIR_NAME,
    DirMode,
)
from ..config.package_config import PackageConfig
from ..hooks.lifecycle_hooks import trigger_pre_source_hook
from ..core.file_action import FileActionType
from ..models import PackageRenderResult, RenderOptions
from ..utils.file_ops import copy_file
from ..utils.path_utils import is_relative_to, to_relative_posix
from ..core.folder_diff import list_folder_paths
from .render_dag import (
    Node,
    PathNode,
    JsonNode,
    UnknownPathNode,
    EngineOutputFileNode,
    PackagePayloadNode,
)
from .render_expansion import (
    ExpansionContext,
    expand_node_dependencies,
)
from .render_digester import (
    DigestionContext,
    digest_render_dag,
    is_drift_internal_path,
)
from .render_lock import RenderLockfile, RenderBucket

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: DAG Construction & Candidate Filtering
# =====================================================================

def build_phase3_payload_dag(
    workspace_config: WorkspaceConfig,
    pkg_config: PackageConfig,
    package_dir: Path,
    effective_engines: RenderEngineRegistry,
) -> PackagePayloadNode:
    """Constructs and expands the Phase 3 AST Merkle DAG for package payload dotfiles."""
    pkg_name = pkg_config.name
    src_dir_to_render = pkg_config.get_source_directory_to_render(package_dir)
    if not src_dir_to_render.exists() or not src_dir_to_render.is_dir():
        raise FileNotFoundError(f"Package '{pkg_name}' source directory not found: '{src_dir_to_render}'")

    render_pkg_dir = workspace_config.render_path / pkg_name
    translation_map = {src_dir_to_render: render_pkg_dir}

    all_items = list_folder_paths(
        src_dir=src_dir_to_render,
        base_rel=src_dir_to_render,
        resolve_symlinks=True,
        dir_mode=DirMode.ONLY_EMPTY_DIR,
    )

    initial_nodes: list[Node] = []
    for item in all_items:
        rel = item.relative_to(src_dir_to_render)

        # 1. Skip if the file is the package config file or its template
        if pkg_config.is_package_config_file(item):
            continue

        # 2. Skip any '.*' files (except .drift_ignore) in rendering process and print info
        if rel.name.startswith(".") and rel.name not in DRIFT_IGNORE_FILE_NAME_LIST:
            logger.info(
                f"ℹ️  [SKIP] Skipping hidden file '{rel}' in rendering. "
                "All hidden files to be deployed must use the 'dot-' prefix in source templates."
            )
            continue

        # 3. Skip root .drift_ignore (already handled by handle_driftignore_file)
        if rel.name in DRIFT_IGNORE_FILE_NAME_LIST:
            continue

        # 4. Skip drift_hooks/ (handled in Phase 2)
        if is_relative_to(rel, Path(DRIFT_HOOKS_DIR_NAME)):
            continue

        # 5. Skip .drift/ internal directory if present
        if is_relative_to(rel, Path(DRIFT_INTERNAL_DIR_NAME)):
            continue

        initial_nodes.append(UnknownPathNode(item))

    payload_root = PackagePayloadNode(pkg_name=pkg_name, payload_nodes=initial_nodes)

    # NOTE: env_node deliberately hashes pkg_config.env_resolve.impact.restricted_env() rather than
    # the entire ambient os.environ. Hashing os.environ would capture volatile session noise
    # (SHLVL, _, OLDPWD, SSH_AUTH_SOCK, TMUX_PANE, etc.), destroying Merkle cache invariance
    # and resulting in a 0% cache hit rate across terminal sessions. If templates require host
    # environment variables, users should declare them in [env.fallback] (e.g. USER = "${USER}")
    # so they are deterministically tracked in restricted_env.
    env_node = JsonNode(pkg_config.env_resolve.impact.restricted_env())
    exp_ctx = ExpansionContext(
        package_name=pkg_name,
        enable_render=pkg_config.package.enable_render,
        env_node=env_node,
        render_engines=effective_engines,
        cache=workspace_config.render_cache,
        path_translation=translation_map,
        drift_root=workspace_config.drift_root,
        package_render_dir=render_pkg_dir,
        package_src_dir=package_dir,
    )
    expand_node_dependencies(payload_root, exp_ctx)
    return payload_root


# =====================================================================
# Layer 2: Single-Package Pipeline Orchestration
# =====================================================================

def handle_driftignore_file(
    package_dir: Path,
    render_pkg_dir: Path,
    dry_run: bool = False,
) -> None:
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
        if not dry_run:
            dest_correct.parent.mkdir(parents=True, exist_ok=True)
            copy_file(correct_path, dest_correct)
    elif misspelled_path.is_file():
        logger.warning(
            f"Package '{package_name}' contains a misspelled ignore file '{DRIFT_IGNORE_LEGACY_FILE_NAME}'. "
            f"Please rename it to '{DRIFT_IGNORE_FILE_NAME}'."
        )
        if not dry_run:
            dest_correct.parent.mkdir(parents=True, exist_ok=True)
            copy_file(misspelled_path, dest_correct)
    elif dest_correct.is_file():
        if not dry_run:
            dest_correct.unlink()


def render_package_files(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    pkg_config: PackageConfig,
    render_pkg_dir: Path,
    options: Optional[RenderOptions] = None,
    render_engines: Optional[RenderEngineRegistry] = None,
) -> PackageRenderResult:
    """Renders package source files, copies static assets, and triggers lifecycle hooks via Merkle DAG digestion."""
    from ..core.ignore import DriftIgnore
    package_name = package_dir.name
    logger.info(f"📦 Rendering package '{package_name}'...")

    opts = RenderOptions.resolve(options)
    resolved_hook_flags = opts.get_hook_flags(settings=workspace_config.settings)
    effective_engines = (
        render_engines
        if render_engines is not None
        else pkg_config.package_render_engines(workspace_config)
    )

    # 1. Trigger pre_source hook before reading / processing source files
    if resolved_hook_flags.dry_run and resolved_hook_flags.no_hooks:
        if getattr(pkg_config.hooks, "pre_source", None):
            logger.info(
                f"🪝  [DRY-RUN] Skipped pre_source hook for '{package_name}' (zero-mutation mode). "
                f"Note: Dynamically generated templates will not appear in this plan. "
                f"Pass '--with-hooks' to execute pre-flight hooks."
            )
    trigger_pre_source_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        flags=resolved_hook_flags,
        pkg_config_override=pkg_config,
        engines_override=effective_engines,
    )

    # 2. Control plane ignore metadata
    handle_driftignore_file(package_dir, render_pkg_dir, dry_run=opts.dry_run)

    # 3. Proactively check for nested ignore files and trigger clean validation
    DriftIgnore.load_from_dir(package_dir, is_source=True)

    # 4. Phase 3 AST Merkle DAG construction & expansion
    payload_root = build_phase3_payload_dag(
        workspace_config=workspace_config,
        pkg_config=pkg_config,
        package_dir=package_dir,
        effective_engines=effective_engines,
    )

    # 5. Digestion & scoped pruning (strictly shielding .drift/)
    lockfile = RenderLockfile.load_from_dir(render_pkg_dir)
    ctx = DigestionContext(
        drift_root=workspace_config.drift_root,
        package_name=package_name,
        package_render_dir=render_pkg_dir,
        lockfile=lockfile,
        bucket=RenderBucket.PAYLOAD,
        cache=workspace_config.render_cache,
        force=opts.no_cache,
        dry_run=opts.dry_run,
        silent=opts.silent,
    )

    digest_render_dag(payload_root, ctx)

    # 6. Gather rendered vs copied files for PackageRenderResult
    rendered_files = [
        to_relative_posix(a.dst_path, render_pkg_dir)
        for a in ctx.result.actions
        if a.action_type == FileActionType.RENDER_ITEM and a.dst_path is not None
    ]
    copied_files = [
        to_relative_posix(a.dst_path, render_pkg_dir)
        for a in ctx.result.actions
        if a.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY)
        and a.dst_path is not None
        and not is_drift_internal_path(a.dst_path, render_pkg_dir)
    ]

    # 7. Trigger post_render hook
    pkg_config.hooks.trigger_post_render(
        flags=resolved_hook_flags,
    )
    if opts.silent:
        logger.debug(f"✨ Package '{package_name}' rendered successfully.")
    else:
        logger.info(f"✨ Package '{package_name}' rendered successfully.")

    status = (
        "UP_TO_DATE"
        if (ctx.result.rendered_count == 0 and ctx.result.pruned_count == 0 and ctx.result.skipped_count > 0)
        else "SUCCESS"
    )

    return PackageRenderResult(
        package=package_name,
        status=status,
        actions=ctx.result.actions,
        rendered_files=rendered_files,
        copied_static_files=copied_files,
    )


def render_package(
    workspace_config: WorkspaceConfig,
    package_dir: Path,
    options: Optional[RenderOptions] = None,
) -> PackageRenderResult:
    """Renders all templates and copies static files in a package folder into the render directory."""
    package_name = package_dir.name
    opts = RenderOptions.resolve(options)
    hook_flags = opts.get_hook_flags(settings=workspace_config.settings)

    render_pkg_dir = workspace_config.render_path / package_name

    pkg_config = PackageConfig.from_source_dir(
        package_dir=package_dir,
        workspace_config=workspace_config,
        dry_run=opts.dry_run,
        silent=opts.silent,
    )

    scoped_flags = replace(hook_flags, load_envs=False)
    scoped_opts = replace(opts, flags=scoped_flags)

    with pkg_config.package_envs():
        # Stage 2: Merge package render engines
        effective_engines = pkg_config.package_render_engines(workspace_config)

        # Phase 2: Render lifecycle hooks into render/<pkg>/.drift/hooks/
        from .render_hooks import render_hooks
        render_hooks(
            workspace_config=workspace_config,
            pkg_config=pkg_config,
            engines_override=effective_engines,
            dry_run=scoped_opts.dry_run,
            silent=scoped_opts.silent,
        )

        # Pre-flight Requirements Check (declarative host facts + dynamic probe hook)
        is_satisfied, failure_reason = pkg_config.evaluate_requirements(
            workspace_config, flags=scoped_flags
        )
        if not is_satisfied:
            if opts.silent:
                logger.debug(f"ℹ️  [SKIP] Skipping package '{package_name}': {failure_reason}")
            else:
                logger.info(f"ℹ️  [SKIP] Skipping package '{package_name}': {failure_reason}")
            return PackageRenderResult(
                package=package_name,
                status="SKIPPED",
                skip_reason=failure_reason,
            )

        return render_package_files(
            workspace_config=workspace_config,
            pkg_config=pkg_config,
            package_dir=package_dir,
            render_pkg_dir=render_pkg_dir,
            options=scoped_opts,
            render_engines=effective_engines,
        )
