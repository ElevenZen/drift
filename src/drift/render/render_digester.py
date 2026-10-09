"""Graph digestion engine, in-place file generation, and scoped pruning.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Top-Level Graph Digester & Orchestrator
    - topological_sort_nodes(root_node) -> List[Node]
        Topologically sorts a DAG using depth-first post-order traversal with cycle detection.
    - digest_render_dag(root, context) -> DigestionResult
        Coordinates topological evaluation across node.digest(context).

Layer 2: Scoped Pruning Subsystems & Cache Helper
    - check_and_apply_cache(node, target_rel_path, context) -> bool
        Checks lockfile match, populates hashes, and updates skipped list on hit.
    - prune_obsolete_config_files(drift_root, package_render_dir, active_paths, dry_run) -> List[Path]
        Removes unrendered drift_package[.local].toml files from .drift/.
    - prune_obsolete_hooks(drift_root, hooks_dir, active_paths, dry_run) -> List[Path]
        Removes obsolete hook scripts from .drift/hooks/.
    - prune_obsolete_payload_files(drift_root, package_render_dir, active_paths, dry_run) -> List[Path]
        Removes obsolete payload files and empty directories using list_folder_paths(ONLY_EMPTY_DIR),
        strictly shielding .drift/.

Layer 1: Inspection & Predicates
    - is_drift_internal_path(rel_path, package_render_dir) -> bool
        Predicate protecting .drift/ from payload pruning.
===============================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set, Sequence, Dict, Union

logger = logging.getLogger(__name__)

from ..core.constants import (
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DirMode,
)
from ..core.exceptions import CyclicDependencyError
from ..core.file_action import FileAction, FileActionType, format_action_line
from ..core.folder_diff import list_folder_paths
from ..utils.file_ops import prune_empty_parents
from ..utils.path_utils import to_relative_path
from .render_cache import NodeHashes, RenderCache
from .render_lock import RenderLockfile, RenderBucket
from .render_dag import Node


# =====================================================================
# Layer 1: Inspection & Predicates
# =====================================================================

def is_drift_internal_path(rel_path: Path, package_render_dir: Path) -> bool:
    """Checks if rel_path (relative to package_render_dir or absolute) is within package_render_dir/.drift/."""
    sub_rel = to_relative_path(rel_path, package_render_dir)
    return bool(sub_rel.parts and sub_rel.parts[0] == DRIFT_INTERNAL_DIR_NAME)


# =====================================================================
# Layer 2: Digestion Models & Cache Helpers
# =====================================================================

@dataclass
class DigestionResult:
    """Structured result container summarizing digestion execution."""

    actions: List[FileAction] = field(default_factory=list)
    rendered_paths: List[Path] = field(default_factory=list)
    skipped_paths: List[Path] = field(default_factory=list)
    pruned_paths: List[Path] = field(default_factory=list)
    updated_lockfile: RenderLockfile = field(default_factory=RenderLockfile)

    @property
    def active_paths(self) -> List[Path]:
        """All currently active files and empty directories (rendered + skipped)."""
        return self.rendered_paths + self.skipped_paths

    @property
    def rendered_count(self) -> int:
        return len(self.rendered_paths)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_paths)

    @property
    def pruned_count(self) -> int:
        return len(self.pruned_paths)


@dataclass
class DigestionContext:
    """Execution context passed through Node.digest() calls."""

    drift_root: Path
    package_name: str
    # relative path to package render directory (e.g., "src/my_package/.drift/render/") or absolute path for external render directories
    package_render_dir: Path
    lockfile: RenderLockfile
    bucket: RenderBucket
    cache: RenderCache
    force: bool = False
    dry_run: bool = False
    silent: bool = False

    # Result container mutated during traversal (all paths relative to drift_root)
    result: DigestionResult = field(default_factory=DigestionResult)
    # Active hashes set accumulated for lockfile synchronization
    active_hashes: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.result.updated_lockfile.get_all_hashes():
            self.result.updated_lockfile = self.lockfile

    @property
    def absolute_package_render_dir(self) -> Path:
        """Returns absolute path to the package render directory."""
        if self.package_render_dir.is_absolute():
            return self.package_render_dir
        return self.drift_root / self.package_render_dir

    def hash_file(self, file_path: Path) -> Optional[str]:
        """Computes file hash on disk, using package-relative path if within package render directory."""
        from .render_hasher import hash_file_disk
        return hash_file_disk(file_path, package_render_dir=self.absolute_package_render_dir)

    def hash_directory(self, dir_path: Path) -> Optional[str]:
        """Computes directory hash on disk, using package-relative path if within package render directory."""
        from .render_hasher import hash_directory_disk
        return hash_directory_disk(dir_path, package_render_dir=self.absolute_package_render_dir)

    def log_action(self, action: FileAction) -> None:
        """Logs action line using logger.debug when silent=True, or logger.info otherwise."""
        line = format_action_line(action, drift_root=self.drift_root)
        if self.silent:
            logger.debug(line)
        else:
            logger.info(line)

    def save_lockfile(self) -> None:
        """Saves updated lockfile to package render directory unless dry_run is set."""
        if not self.dry_run:
            self.lockfile.save_to_dir(self.absolute_package_render_dir)



def check_and_apply_cache(
    node: Node,
    target_path: Path,
    context: DigestionContext,
) -> bool:
    """Checks if the node is already cached in lockfile; if so, populates hashes and records as skipped.

    Returns True if cache hit (skipped), False if execution/generation is needed.
    """
    if context.force:
        logger.debug(f"[Cache] Force bypass (context.force=True) for '{target_path}'.")
        return False

    cached = context.lockfile.check_lockfile_matches(
        context.bucket,
        node,
        context.drift_root,
        package_render_dir=context.absolute_package_render_dir,
    )
    if cached is not None:
        node.hashes = cached
        context.result.skipped_paths.append(target_path)
        if cached.merkle_hash:
            context.active_hashes.add(cached.merkle_hash)
        src_path = getattr(node, "src_path", None)
        if context.cache is not None:
            context.cache.set(target_path, cached, src_path=src_path)

        engine_name = getattr(getattr(node, "engine_config", None), "name", None)
        action = FileAction(
            action_type=FileActionType.SKIP_IDENTICAL,
            src_path=src_path or target_path,
            dst_path=target_path,
            reason=engine_name,
        )
        context.result.actions.append(action)
        from .render_hasher import format_hash_log
        logger.debug(
            format_action_line(
                action,
                drift_root=context.drift_root,
            )
        )
        logger.debug(f"[Cache] Cache HIT for '{target_path}': merkle_hash={format_hash_log(cached.merkle_hash)}.")
        return True

    logger.debug(f"[Cache] Cache MISS for '{target_path}' (bucket={context.bucket.value}).")
    return False


# =====================================================================
# Layer 2: Scoped Pruning Subsystems
# =====================================================================

def prune_obsolete_config_files(context: DigestionContext) -> List[Path]:
    """Prunes unrendered config files (drift_package.toml, drift_package.local.toml) from .drift/ and .drift/render/."""
    package_render_dir = context.absolute_package_render_dir
    render_internal = package_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME
    candidates = [
        package_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME,
        package_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME,
        render_internal / PACKAGE_CONFIG_FILE_NAME,
        render_internal / PACKAGE_CONFIG_LOCAL_FILE_NAME,
        render_internal / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME / PACKAGE_CONFIG_FILE_NAME,
        render_internal / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME,
    ]
    active_set = set(context.result.active_paths)
    pruned: List[Path] = []

    for cand in candidates:
        if cand.is_file() and cand not in active_set:
            if not context.dry_run:
                cand.unlink()
            action = FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=cand)
            context.log_action(action)
            pruned.append(cand)

    return sorted(pruned)


def prune_obsolete_hooks(context: DigestionContext) -> List[Path]:
    """Removes obsolete hook scripts and empty directory placeholders from .drift/hooks/."""
    hooks_dir = context.absolute_package_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
    if not hooks_dir.is_dir():
        return []

    active_set = set(context.result.active_paths)
    candidates = list_folder_paths(hooks_dir, base_rel=hooks_dir, dir_mode=DirMode.ONLY_EMPTY_DIR)
    pruned: List[Path] = []

    for cand in reversed(candidates):
        if cand not in active_set:
            disk_cand = cand
            if disk_cand.is_file():
                if not context.dry_run:
                    disk_cand.unlink()
                    prune_empty_parents(disk_cand.parent, hooks_dir)
                action = FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=disk_cand)
                context.log_action(action)
                pruned.append(cand)
            elif disk_cand.is_dir():
                if not context.dry_run:
                    if not any(disk_cand.iterdir()):
                        disk_cand.rmdir()
                        prune_empty_parents(disk_cand.parent, hooks_dir)
                action = FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=disk_cand)
                context.log_action(action)
                pruned.append(cand)

    return sorted(pruned)


def prune_obsolete_payload_files(context: DigestionContext) -> List[Path]:
    """Removes obsolete payload files and empty directories, strictly shielding .drift/."""
    disk_pkg = context.absolute_package_render_dir
    if not disk_pkg.is_dir():
        return []

    active_set = set(context.result.active_paths)
    candidates = list_folder_paths(disk_pkg, base_rel=disk_pkg, dir_mode=DirMode.ONLY_EMPTY_DIR)
    pruned: List[Path] = []

    payload_candidates = [
        cand for cand in candidates
        if not is_drift_internal_path(cand, disk_pkg)
    ]

    for cand in reversed(payload_candidates):
        if cand not in active_set:
            disk_cand = cand
            if disk_cand.is_file():
                if not context.dry_run:
                    disk_cand.unlink()
                    prune_empty_parents(disk_cand.parent, disk_pkg)
                action = FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=disk_cand)
                context.log_action(action)
                pruned.append(cand)
            elif disk_cand.is_dir():
                if not context.dry_run:
                    if not any(disk_cand.iterdir()):
                        disk_cand.rmdir()
                        prune_empty_parents(disk_cand.parent, disk_pkg)
                action = FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=disk_cand)
                context.log_action(action)
                pruned.append(cand)

    return sorted(pruned)


# =====================================================================
# Layer 3: Top-Level Graph Digester & Orchestrator
# =====================================================================

def topological_sort_nodes(root_node: Node) -> List[Node]:
    """Topologically sorts a DAG using depth-first post-order traversal.

    Returns nodes in order from leaves (prerequisites) to root (dependent container).

    Raises:
        CyclicDependencyError: If a cyclic dependency is detected.
    """
    visited: Dict[int, int] = {}  # id(node) -> state: 1=visiting, 2=visited
    result: List[Node] = []

    def dfs(n: Node) -> None:
        nid = id(n)
        if visited.get(nid, 0) == 1:
            raise CyclicDependencyError(f"Cyclic dependency detected in render graph involving node: '{n.value}'")
        if visited.get(nid, 0) == 2:
            return

        visited[nid] = 1
        for dep in n.depends_on:
            dfs(dep)
        visited[nid] = 2
        result.append(n)

    dfs(root_node)
    return result


def digest_render_dag(root: Node, context: DigestionContext) -> DigestionResult:
    """Topologically walks the AST and executes polymorphic digestion across all nodes."""
    nodes = topological_sort_nodes(root)
    for node in nodes:
        node.digest(context)

    return context.result
