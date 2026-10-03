"""Universal Dependency Graph (DAG) node definitions and polymorphic digestion.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Node Definitions & Polymorphic Digestion
    - Node base class (value, depends_on, hashes, own_hash, merkle_hash, digest)
    - Node specializations:
        - TextNode / JsonNode: Raw content & deterministic JSON text
        - FileNode: Base class for filesystem path nodes
        - UnknownFileNode: Placeholder node resolved during expansion
        - IndependentFileNode: Unmanaged leaf dependency asset
        - StaticFileNode: 1:1 copied static file (.digest copies file)
        - DirectoryNode: Directory synchronization target (.digest ensures dir)
        - CachedNode: Reused output from previous phase / run
        - EngineOutputFileNode: Template compiled by render engine (.digest renders)
        - PackageConfigNode: Rendered package configuration container
        - PackageHooksNode: Phase 2 hooks execution root container
        - PackagePayloadNode: Phase 3 payload execution root container
===============================================================================
"""

from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineConfig
    from .render_digester import DigestionContext

from .render_cache import NodeHashes


# =====================================================================
# Layer 1: Node Definitions & Invariants
# =====================================================================

@dataclass
class Node:
    """Universal dependency graph node with Merkle hash storage."""

    value: str
    depends_on: List["Node"] = field(default_factory=list)
    hashes: Optional[NodeHashes] = None

    @property
    def own_hash(self) -> Optional[str]:
        return self.hashes.own_hash if self.hashes else None

    @property
    def merkle_hash(self) -> Optional[str]:
        return self.hashes.merkle_hash if self.hashes else None

    def digest(self, context: "DigestionContext") -> None:
        """Default digestion for leaf/dependency nodes."""
        from .render_hasher import compute_merkle_node_hash

        compute_merkle_node_hash(self, context.drift_root)


@dataclass(init=False)
class TextNode(Node):
    """Raw text content node. Computes SHA-256 hash at construction."""

    def __init__(self, content: str):
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        super().__init__(value=content, depends_on=[], hashes=NodeHashes(own_hash=h, merkle_hash=h))


@dataclass(init=False)
class JsonNode(TextNode):
    """Deterministic JSON text node with sorted keys, retaining original data for rendering."""

    data: Any

    def __init__(self, data: Any):
        normalized_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
        super().__init__(normalized_json)
        self.data = data


@dataclass(init=False)
class FileNode(Node):
    """Base class for any node representing a filesystem path."""

    file_path: Path

    def __init__(self, file_path: Path, depends_on: Optional[List[Node]] = None):
        super().__init__(value=file_path.as_posix(), depends_on=depends_on or [])
        self.file_path = file_path


@dataclass(init=False)
class UnknownFileNode(FileNode):
    """Placeholder AST node whose concrete type is resolved during expansion."""

    pass


@dataclass(init=False)
class IndependentFileNode(FileNode):
    """Unmanaged source asset or template (leaf file node)."""

    pass


@dataclass(init=False)
class StaticFileNode(FileNode):
    """1:1 copied static file."""

    src_path: Path

    def __init__(self, output_path: Path, src_path: Path):
        super().__init__(output_path, depends_on=[IndependentFileNode(src_path)])
        self.src_path = src_path

    @property
    def output_path(self) -> Path:
        return self.file_path

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_file_disk, compute_merkle_node_hash

        if check_and_apply_cache(self, self.output_path, context):
            return

        src = context.drift_root / self.src_path
        dst = context.drift_root / self.output_path
        if not src.is_file():
            raise FileNotFoundError(f"Static source file not found: {self.src_path}")

        if not context.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        own_h = hash_file_disk(self.output_path, context.drift_root) or ""
        m_h = compute_merkle_node_hash(self, context.drift_root) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.output_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.output_path, self.hashes)


@dataclass(init=False)
class DirectoryNode(Node):
    """Empty directory synchronization target (.drift_keep)."""

    dir_path: Path

    def __init__(self, dir_path: Path):
        super().__init__(value=dir_path.as_posix(), depends_on=[])
        self.dir_path = dir_path

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_directory_disk, compute_merkle_node_hash

        if check_and_apply_cache(self, self.dir_path, context):
            return

        if not context.dry_run:
            (context.drift_root / self.dir_path).mkdir(parents=True, exist_ok=True)

        own_h = hash_directory_disk(self.dir_path, context.drift_root) or ""
        m_h = compute_merkle_node_hash(self, context.drift_root) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dir_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.dir_path, self.hashes)


@dataclass(init=False)
class CachedNode(FileNode):
    """Node representing an output already digested in the current run."""

    def __init__(
        self,
        output_path: Path,
        hashes: Optional[NodeHashes] = None,
    ):
        super().__init__(output_path, depends_on=[])
        self.hashes = hashes

    @property
    def output_path(self) -> Path:
        return self.file_path


@dataclass(init=False)
class EngineOutputFileNode(FileNode):
    """Template compiled by a render engine."""

    input_node: Optional[Node]
    template_node: Node
    env_node: JsonNode
    engine_node: JsonNode
    engine_config: Optional["RenderEngineConfig"]

    def __init__(
        self,
        output_path: Path,
        input_node: Optional[Node],
        template_node: Node,
        env_node: JsonNode,
        engine_node: JsonNode,
        engine_config: Optional["RenderEngineConfig"] = None,
    ):
        deps = [input_node, template_node, env_node, engine_node] if input_node else [template_node, env_node, engine_node]
        super().__init__(output_path, depends_on=deps)
        self.input_node = input_node
        self.template_node = template_node
        self.env_node = env_node
        self.engine_node = engine_node
        self.engine_config = engine_config

    @property
    def output_path(self) -> Path:
        return self.file_path

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_file_disk, compute_merkle_node_hash
        from .render_core import render_template_to_file

        if check_and_apply_cache(self, self.output_path, context):
            return

        if not isinstance(self.template_node, FileNode):
            raise TypeError(f"Template node must be a FileNode, got {type(self.template_node)}")

        tmpl = context.drift_root / self.template_node.file_path
        dst = context.drift_root / self.output_path
        in_file = (context.drift_root / self.input_node.file_path) if isinstance(self.input_node, FileNode) else None

        if self.engine_config is None:
            raise ValueError(f"EngineOutputFileNode for '{self.output_path}' lacks engine_config")

        if not context.dry_run:
            render_template_to_file(
                engine_config=self.engine_config,
                drift_root=context.drift_root,
                template_file_path=tmpl,
                output_file_path=dst,
                input_file_path=in_file,
            )

        own_h = hash_file_disk(self.output_path, context.drift_root) or ""
        m_h = compute_merkle_node_hash(self, context.drift_root) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.output_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.output_path, self.hashes)


@dataclass(init=False)
class PackageConfigNode(FileNode):
    """Rendered package configuration (drift_package.toml)."""

    def __init__(self, output_path: Path, sources: List[Node], env_node: JsonNode):
        super().__init__(output_path, depends_on=[*sources, env_node])

    @property
    def output_path(self) -> Path:
        return self.file_path

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import prune_obsolete_config_files
        from .render_lock import RenderBucket

        super().digest(context)
        if self.merkle_hash:
            context.active_hashes.add(self.merkle_hash)

        pruned = prune_obsolete_config_files(
            drift_root=context.drift_root,
            package_render_dir=context.package_render_dir,
            active_paths=context.result.active_paths,
            dry_run=context.dry_run,
        )
        context.result.pruned_paths.extend(pruned)
        context.lockfile.update_bucket_hashes(RenderBucket.CONFIG, context.active_hashes)
        if not context.dry_run:
            context.lockfile.save_to_dir(context.drift_root / context.package_render_dir)


@dataclass(init=False)
class PackageHooksNode(Node):
    """Root container for Phase 2 (Hooks)."""

    def __init__(self, pkg_name: str, hook_nodes: List[Node]):
        super().__init__(value=pkg_name, depends_on=hook_nodes)

    def digest(self, context: "DigestionContext") -> None:
        from ..core.constants import DRIFT_INTERNAL_DIR_NAME, DRIFT_INTERNAL_HOOKS_DIR_NAME
        from .render_digester import prune_obsolete_hooks
        from .render_lock import RenderBucket

        super().digest(context)
        if self.merkle_hash:
            context.active_hashes.add(self.merkle_hash)

        hooks_dir = context.package_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        pruned = prune_obsolete_hooks(
            drift_root=context.drift_root,
            hooks_dir=hooks_dir,
            active_paths=context.result.active_paths,
            dry_run=context.dry_run,
        )
        context.result.pruned_paths.extend(pruned)
        context.lockfile.update_bucket_hashes(RenderBucket.HOOKS, context.active_hashes)
        if not context.dry_run:
            context.lockfile.save_to_dir(context.drift_root / context.package_render_dir)


@dataclass(init=False)
class PackagePayloadNode(Node):
    """Root Merkle container for Phase 3 (Payload)."""

    def __init__(self, pkg_name: str, payload_nodes: List[Node]):
        super().__init__(value=pkg_name, depends_on=payload_nodes)

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import prune_obsolete_payload_files
        from .render_lock import RenderBucket

        super().digest(context)
        if self.merkle_hash:
            context.active_hashes.add(self.merkle_hash)

        pruned = prune_obsolete_payload_files(
            drift_root=context.drift_root,
            package_render_dir=context.package_render_dir,
            active_paths=context.result.active_paths,
            dry_run=context.dry_run,
        )
        context.result.pruned_paths.extend(pruned)
        context.lockfile.update_bucket_hashes(RenderBucket.PAYLOAD, context.active_hashes)
        if not context.dry_run:
            context.lockfile.save_to_dir(context.drift_root / context.package_render_dir)

