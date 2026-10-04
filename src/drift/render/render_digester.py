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
    - format_render_action_line(action_type, dst_path, src_path, reason, drift_root) -> str
        Formats a single render DAG digestion action into a clean terminal line.
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
from typing import List, Optional, Set, Sequence, Dict

logger = logging.getLogger(__name__)

from ..core.constants import (
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DirMode,
)
from ..core.folder_diff import list_folder_paths
from ..utils.file_ops import prune_empty_parents
from .render_cache import NodeHashes, RenderCache
from .render_lock import RenderLockfile, RenderBucket
from .render_dag import Node


# =====================================================================
# Layer 1: Inspection & Predicates
# =====================================================================

def is_drift_internal_path(rel_path: Path, package_render_dir: Path) -> bool:
    """Checks if rel_path (relative to drift_root) is within package_render_dir/.drift/."""
    try:
        sub_rel = rel_path.relative_to(package_render_dir)
        return bool(sub_rel.parts and sub_rel.parts[0] == DRIFT_INTERNAL_DIR_NAME)
    except ValueError:
        return False


# =====================================================================
# Layer 2: Digestion Models & Cache Helpers
# =====================================================================

@dataclass
class DigestionResult:
    """Structured result container summarizing digestion execution."""

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
    package_render_dir: Path  # Path("render") / package_name (relative to drift_root)
    lockfile: RenderLockfile
    bucket: RenderBucket
    cache: RenderCache
    force: bool = False
    dry_run: bool = False

    # Result container mutated during traversal (all paths relative to drift_root)
    result: DigestionResult = field(default_factory=DigestionResult)
    # Active hashes set accumulated for lockfile synchronization
    active_hashes: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.result.updated_lockfile.get_all_hashes():
            self.result.updated_lockfile = self.lockfile

    def save_lockfile(self) -> None:
        """Saves updated lockfile to package render directory unless dry_run is set."""
        if not self.dry_run:
            self.lockfile.save_to_dir(self.drift_root / self.package_render_dir)



def _display_render_path(path: Optional[Path], drift_root: Optional[Path] = None) -> str:
    if path is None:
        return ""
    if drift_root is not None:
        try:
            return str(path.relative_to(drift_root))
        except ValueError:
            pass
    return str(path)


def format_render_action_line(
    action_type: str,
    dst_path: Path,
    src_path: Optional[Path] = None,
    reason: Optional[str] = None,
    drift_root: Optional[Path] = None,
) -> str:
    """Formats a single render DAG digestion action into a clean terminal line."""
    dst_str = _display_render_path(dst_path, drift_root)
    src_str = _display_render_path(src_path, drift_root) if src_path is not None else None
    reason_str = f" ({reason})" if reason else ""

    if action_type == "ENSURE_DIR":
        return f"    📁 [ENSURE_DIR]      {dst_str}"
    elif action_type == "COPY":
        return f"    📄 [COPY]            {src_str} -> {dst_str}"
    elif action_type == "RENDER":
        return f"    🧪 [RENDER]          {src_str} -> {dst_str}{reason_str}"
    elif action_type == "CONFIG":
        return f"    ⚙️ [CONFIG]          {dst_str}"
    elif action_type == "PRUNE":
        return f"    🗑️ [PRUNE]           {dst_str}"
    elif action_type == "SKIP_IDENTICAL":
        if src_str:
            return f"    ⏭️ [SKIP_IDENTICAL]  {src_str} -> {dst_str}{reason_str}"
        return f"    ⏭️ [SKIP_IDENTICAL]  {dst_str}{reason_str}"
    return f"    [{action_type}] {dst_str}{reason_str}"


def check_and_apply_cache(
    node: Node,
    target_path: Path,
    context: DigestionContext,
) -> bool:
    """Checks if the node is already cached in lockfile; if so, populates hashes and records as skipped.

    Returns True if cache hit (skipped), False if execution/generation is needed.
    """
    if context.force:
        return False

    cached = context.lockfile.check_lockfile_matches(context.bucket, node, context.drift_root)
    if cached is not None:
        node.hashes = cached
        context.result.skipped_paths.append(target_path)
        if cached.merkle_hash:
            context.active_hashes.add(cached.merkle_hash)
        if context.cache is not None:
            context.cache.set(target_path, cached, src_path=node.src_path)

        engine_name = getattr(getattr(node, "engine_config", None), "name", None)
        logger.debug(
            format_render_action_line(
                "SKIP_IDENTICAL",
                target_path,
                src_path=node.src_path,
                reason=engine_name,
                drift_root=context.drift_root,
            )
        )
        return True
    return False


# =====================================================================
# Layer 2: Scoped Pruning Subsystems
# =====================================================================

def prune_obsolete_config_files(
    drift_root: Path,
    package_render_dir: Path,
    active_paths: Sequence[Path],
    dry_run: bool = False,
) -> List[Path]:
    """Prunes unrendered config files (drift_package.toml, drift_package.local.toml) from .drift/ and .drift/render/."""
    render_internal = drift_root / package_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME
    candidates = [
        drift_root / package_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME,
        drift_root / package_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME,
        render_internal / PACKAGE_CONFIG_FILE_NAME,
        render_internal / PACKAGE_CONFIG_LOCAL_FILE_NAME,
        render_internal / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME / PACKAGE_CONFIG_FILE_NAME,
        render_internal / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME,
    ]
    active_set = set(active_paths)
    pruned: List[Path] = []

    for cand in candidates:
        if cand.is_file() and cand not in active_set:
            if not dry_run:
                cand.unlink()
            logger.info(format_render_action_line("PRUNE", cand, drift_root=drift_root))
            pruned.append(cand)

    return sorted(pruned)


def prune_obsolete_hooks(
    drift_root: Path,
    hooks_dir: Path,
    active_paths: Sequence[Path],
    dry_run: bool = False,
) -> List[Path]:
    """Removes obsolete hook scripts and empty directory placeholders from hooks_dir."""
    disk_hooks = drift_root / hooks_dir
    if not disk_hooks.is_dir():
        return []

    active_set = set(active_paths)
    candidates = list_folder_paths(disk_hooks, base_rel=drift_root / hooks_dir, dir_mode=DirMode.ONLY_EMPTY_DIR)
    pruned: List[Path] = []

    for cand in reversed(candidates):
        if cand not in active_set:
            disk_cand = drift_root / cand
            if disk_cand.is_file():
                if not dry_run:
                    disk_cand.unlink()
                    prune_empty_parents(disk_cand.parent, disk_hooks)
                logger.info(format_render_action_line("PRUNE", disk_cand, drift_root=drift_root))
                pruned.append(cand)
            elif disk_cand.is_dir():
                if not dry_run:
                    if not any(disk_cand.iterdir()):
                        disk_cand.rmdir()
                        prune_empty_parents(disk_cand.parent, disk_hooks)
                logger.info(format_render_action_line("PRUNE", disk_cand, drift_root=drift_root))
                pruned.append(cand)

    return sorted(pruned)


def prune_obsolete_payload_files(
    drift_root: Path,
    package_render_dir: Path,
    active_paths: Sequence[Path],
    dry_run: bool = False,
) -> List[Path]:
    """Removes obsolete payload files and empty directories, strictly shielding .drift/."""
    disk_pkg = drift_root / package_render_dir
    if not disk_pkg.is_dir():
        return []

    active_set = set(active_paths)
    candidates = list_folder_paths(disk_pkg, base_rel=drift_root / package_render_dir, dir_mode=DirMode.ONLY_EMPTY_DIR)
    pruned: List[Path] = []

    payload_candidates = [
        cand for cand in candidates
        if not is_drift_internal_path(cand, drift_root / package_render_dir)
    ]

    for cand in reversed(payload_candidates):
        if cand not in active_set:
            disk_cand = drift_root / cand
            if disk_cand.is_file():
                if not dry_run:
                    disk_cand.unlink()
                    prune_empty_parents(disk_cand.parent, disk_pkg)
                logger.info(format_render_action_line("PRUNE", disk_cand, drift_root=drift_root))
                pruned.append(cand)
            elif disk_cand.is_dir():
                if not dry_run:
                    if not any(disk_cand.iterdir()):
                        disk_cand.rmdir()
                        prune_empty_parents(disk_cand.parent, disk_pkg)
                logger.info(format_render_action_line("PRUNE", disk_cand, drift_root=drift_root))
                pruned.append(cand)

    return sorted(pruned)


# =====================================================================
# Layer 3: Top-Level Graph Digester & Orchestrator
# =====================================================================

def topological_sort_nodes(root_node: Node) -> List[Node]:
    """Topologically sorts a DAG using depth-first post-order traversal.

    Returns nodes in order from leaves (prerequisites) to root (dependent container).

    Raises:
        ValueError: If a cyclic dependency is detected.
    """
    visited: Dict[int, int] = {}  # id(node) -> state: 1=visiting, 2=visited
    result: List[Node] = []

    def dfs(n: Node) -> None:
        nid = id(n)
        if visited.get(nid, 0) == 1:
            raise ValueError(f"Cyclic dependency detected in render graph involving node: '{n.value}'")
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
