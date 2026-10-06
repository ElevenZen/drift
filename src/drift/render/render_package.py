"""Phase 3 Package Payload rendering, Merkle DAG digestion, and primitive orchestration.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Primitive Orchestration & Multi-Package Batch
    - run_primitive_2_render_packages(workspace_config, target_pkgs, options) -> RenderResult
        Coordinates multi-package template rendering with shared workspace RenderCache.
    - run_primitive_3_commit_render_repo(workspace_config, commit_message, target_pkgs) -> None
        Stages and commits changes inside the render sandbox Git repository.

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

import sys
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Tuple, Optional, Sequence, Union, TYPE_CHECKING

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
from .render_core import RenderError
from ..core.exceptions import ConfigError, RenderCollisionError, HookMissingError, is_logged, mark_logged
from ..hooks.lifecycle_hooks import trigger_pre_source_hook, HookExecFlags
from ..core.file_action import FileActionType
from ..core.result_models import PackageRenderResult, RenderResult
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


@dataclass
class RenderOptions:
    """Options controlling package rendering behavior.

    Attributes:
        no_cache: When True, bypasses cache and forces clean re-rendering.
        dry_run: When True, simulates render planning and digestion without modifying
            the filesystem or executing lifecycle hooks.
        flags: Optional HookExecFlags controlling hook execution options.
    """
    no_cache: bool = False
    dry_run: bool = False
    silent: bool = False
    render_dir_mask: Optional[Path] = None
    flags: Optional[HookExecFlags] = None

    def get_hook_flags(self, settings=None) -> HookExecFlags:
        """Derives HookExecFlags, assigning dry_run and no_cache from RenderOptions to HookExecFlags.

        When flags is not explicitly specified, defaults no_hooks to True in dry_run mode
        (zero mutation simulation), and False in normal mode.
        """
        if self.flags is None:
            base = HookExecFlags.resolve(None, settings=settings)
            default_no_hooks = True if self.dry_run else base.no_hooks
            return replace(base, no_cache=self.no_cache, dry_run=self.dry_run, no_hooks=default_no_hooks)
        base = HookExecFlags.resolve(self.flags, settings=settings)
        return replace(base, no_cache=self.no_cache, dry_run=self.dry_run)

    @classmethod
    def resolve(
        cls,
        options: Optional[Union["RenderOptions", HookExecFlags]] = None,
    ) -> "RenderOptions":
        """Resolves RenderOptions from either RenderOptions or HookExecFlags."""
        if isinstance(options, RenderOptions):
            return options
        if isinstance(options, HookExecFlags):
            return cls(
                no_cache=options.no_cache,
                dry_run=options.dry_run,
                flags=options,
            )
        return cls()


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

    # NOTE: env_node deliberately hashes pkg_config.env_resolve.effective_dict rather than
    # the entire ambient os.environ. Hashing os.environ would capture volatile session noise
    # (SHLVL, _, OLDPWD, SSH_AUTH_SOCK, TMUX_PANE, etc.), destroying Merkle cache invariance
    # and resulting in a 0% cache hit rate across terminal sessions. If templates require host
    # environment variables, users should declare them in [env.fallback] (e.g. USER = "${USER}")
    # so they are deterministically tracked in effective_dict.
    env_node = JsonNode(pkg_config.env_resolve.effective_dict)
    exp_ctx = ExpansionContext(
        package_name=pkg_name,
        enable_render=pkg_config.package.enable_render,
        env_node=env_node,
        render_engines=effective_engines,
        cache=workspace_config.render_cache,
        path_translation=translation_map,
        drift_root=workspace_config.drift_root,
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
        render_dir_mask=opts.render_dir_mask,
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
            render_dir_mask=scoped_opts.render_dir_mask,
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


# =====================================================================
# Layer 3: Primitive Orchestration & Multi-Package Batch
# =====================================================================

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
