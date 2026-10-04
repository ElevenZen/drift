"""Universal Dependency Graph (DAG) node definitions and polymorphic digestion.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 1: Node Definitions & Polymorphic Digestion
    - Node base class (value, depends_on, hashes, own_hash, merkle_hash, digest)
    - PathNode: Base class for filesystem path nodes (dst_path, src_path)
    - Node specializations:
        - TextNode / JsonNode: Raw content & deterministic JSON text
        - FileNode: Base class for file path nodes
        - DirectoryNode: Directory synchronization target (.digest ensures dir)
        - UnknownPathNode: Placeholder node resolved during expansion
        - IndependentFileNode: Unmanaged leaf dependency asset
        - StaticFileNode: 1:1 copied static file (.digest copies file)
        - CachedNode: Reused output from previous phase / run
        - EngineOutputFileNode: Template compiled by render engine (.digest renders)
        - PackageConfigNode: Rendered package configuration container
        - PackageHooksNode: Phase 2 hooks execution root container
        - PackagePayloadNode: Phase 3 payload execution root container
===============================================================================
"""

from __future__ import annotations

import json
import logging
import hashlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineConfig
    from ..config.workspace_config import WorkspaceConfig
    from ..config.package_config import PackageConfig
    from .render_digester import DigestionContext

from ..core.constants import PACKAGE_CONFIG_FILE_NAME
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

        compute_merkle_node_hash(self)


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
class PathNode(Node):
    """Base class for any node representing a filesystem path (file or directory)."""

    dst_path: Optional[Path] = None
    src_path: Optional[Path] = None

    def __init__(
        self,
        dst_path: Optional[Path] = None,
        src_path: Optional[Path] = None,
        depends_on: Optional[List[Node]] = None,
        hashes: Optional[NodeHashes] = None,
    ):
        primary_path = dst_path if dst_path is not None else src_path
        super().__init__(
            value=primary_path.as_posix() if primary_path is not None else "",
            depends_on=depends_on or [],
            hashes=hashes,
        )
        self.dst_path = dst_path
        self.src_path = src_path


@dataclass(init=False)
class FileNode(PathNode):
    """Base class for any node representing a file path."""

    def __init__(
        self,
        dst_path: Path,
        src_path: Optional[Path] = None,
        depends_on: Optional[List[Node]] = None,
        hashes: Optional[NodeHashes] = None,
    ):
        super().__init__(dst_path=dst_path, src_path=src_path, depends_on=depends_on, hashes=hashes)


@dataclass(init=False)
class UnknownPathNode(PathNode):
    """Placeholder AST node whose concrete type is resolved during expansion."""

    src_path: Path

    def __init__(self, src_path: Path):
        super().__init__(src_path=src_path)


@dataclass(init=False)
class IndependentFileNode(FileNode):
    """Unmanaged source asset or template (leaf file node)."""

    def __init__(self, src_path: Path):
        super().__init__(dst_path=src_path, src_path=src_path)


@dataclass(init=False)
class StaticFileNode(FileNode):
    """1:1 copied static file."""

    def __init__(self, dst_path: Path, src_path: Path):
        super().__init__(dst_path=dst_path, src_path=src_path, depends_on=[IndependentFileNode(src_path)])

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache, format_render_action_line
        from .render_hasher import hash_file_disk, compute_merkle_node_hash

        if check_and_apply_cache(self, self.dst_path, context):
            return

        if not self.src_path or not self.src_path.is_file():
            raise FileNotFoundError(f"Static source file not found: {self.src_path}")

        logger.info(
            format_render_action_line(
                "COPY",
                self.dst_path,
                src_path=self.src_path,
                drift_root=context.drift_root,
            )
        )

        if not context.dry_run:
            self.dst_path.parent.mkdir(parents=True, exist_ok=True)
            if self.src_path.resolve() != self.dst_path.resolve():
                shutil.copy2(self.src_path, self.dst_path)

        own_h = hash_file_disk(self.dst_path) or ""
        m_h = compute_merkle_node_hash(self) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path)


@dataclass(init=False)
class DirectoryNode(PathNode):
    """Empty directory synchronization target (.drift_keep)."""

    def __init__(self, dst_path: Path, src_path: Optional[Path] = None):
        super().__init__(dst_path=dst_path, src_path=src_path)

    def digest(self, context: "DigestionContext") -> None:
        from ..core.constants import DRIFT_KEEP_FILE_NAME
        from .render_digester import check_and_apply_cache, format_render_action_line
        from .render_hasher import hash_directory_disk, compute_merkle_node_hash

        keep_file = self.dst_path / DRIFT_KEEP_FILE_NAME
        if check_and_apply_cache(self, self.dst_path, context):
            context.result.skipped_paths.append(keep_file)
            return

        dir_existed = self.dst_path.is_dir()
        if not context.dry_run:
            self.dst_path.mkdir(parents=True, exist_ok=True)
            keep_file.touch()

        action_line = format_render_action_line(
                "ENSURE_DIR",
                self.dst_path,
                drift_root=context.drift_root,
            )

        if dir_existed:
            logger.debug(action_line)
        else:
            logger.info(action_line)

        own_h = hash_directory_disk(self.dst_path) or ""
        m_h = compute_merkle_node_hash(self) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.result.rendered_paths.append(keep_file)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path or self.dst_path)


@dataclass(init=False)
class CachedNode(FileNode):
    """Node representing an output already digested in the current run."""

    def __init__(
        self,
        dst_path: Path,
        src_path: Path,
        hashes: Optional[NodeHashes] = None,
    ):
        super().__init__(dst_path=dst_path, src_path=src_path, hashes=hashes)

    def digest(self, context: "DigestionContext") -> None:
        from ..core.constants import DRIFT_KEEP_FILE_NAME
        from .render_digester import format_render_action_line

        context.result.skipped_paths.append(self.dst_path)
        keep_file = self.dst_path / DRIFT_KEEP_FILE_NAME
        if keep_file.is_file():
            context.result.skipped_paths.append(keep_file)
        if self.hashes and self.hashes.merkle_hash:
            context.active_hashes.add(self.hashes.merkle_hash)
        logger.debug(
            format_render_action_line(
                "SKIP_IDENTICAL",
                self.dst_path,
                src_path=self.src_path,
                drift_root=context.drift_root,
            )
        )


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
        dst_path: Path,
        input_node: Optional[Node],
        template_node: Node,
        env_node: JsonNode,
        engine_node: JsonNode,
        engine_config: Optional["RenderEngineConfig"] = None,
    ):
        deps = [input_node, template_node, env_node, engine_node] if input_node else [template_node, env_node, engine_node]
        super().__init__(dst_path=dst_path, src_path=template_node.src_path, depends_on=deps)
        self.input_node = input_node
        self.template_node = template_node
        self.env_node = env_node
        self.engine_node = engine_node
        self.engine_config = engine_config

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache, format_render_action_line
        from .render_hasher import hash_file_disk, compute_merkle_node_hash
        from .render_core import render_template_to_file

        if check_and_apply_cache(self, self.dst_path, context):
            return

        tmpl = self.src_path
        if tmpl is None or not tmpl.is_file():
            raise FileNotFoundError(f"Template source file not found: {tmpl}")

        dst = self.dst_path
        in_file = self.input_node.dst_path if isinstance(self.input_node, PathNode) else None

        if self.engine_config is None:
            raise ValueError(f"EngineOutputFileNode for '{self.dst_path}' lacks engine_config")

        logger.info(
            format_render_action_line(
                "RENDER",
                dst,
                src_path=tmpl,
                reason=self.engine_config.name,
                drift_root=context.drift_root,
            )
        )

        if not context.dry_run:
            render_template_to_file(
                engine_config=self.engine_config,
                drift_root=context.drift_root,
                template_file_path=tmpl,
                output_file_path=dst,
                input_file_path=in_file,
            )

        own_h = hash_file_disk(self.dst_path) or ""
        m_h = compute_merkle_node_hash(self) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=tmpl)


@dataclass(init=False)
class PackageConfigNode(FileNode):
    """Root container for Phase 1: Package configuration compilation and variable stitching."""

    package_dir: Path
    workspace_config: Optional["WorkspaceConfig"]
    package_config: Optional["PackageConfig"]
    source_files: List[Path]

    def __init__(
        self,
        dst_path: Path,
        sources: List[Node],
        env_node: JsonNode,
        package_dir: Path,
        workspace_config: Optional["WorkspaceConfig"] = None,
    ):
        source_file = package_dir / PACKAGE_CONFIG_FILE_NAME
        src_path = source_file if source_file.exists() else None
        super().__init__(dst_path=dst_path, src_path=src_path, depends_on=[*sources, env_node])
        self.package_dir = package_dir
        self.workspace_config = workspace_config
        self.package_config = None
        self.source_files = []

    def digest(self, context: "DigestionContext") -> None:
        from ..config.package_config import PackageConfig
        from ..config.package_loader import resolve_and_interpolate_package_config
        from ..hooks.package_hook import apply_package_hook
        from ..utils.toml_utils import parse_toml, merge_toml, dump_toml
        from .render_cache import NodeHashes
        from .render_digester import prune_obsolete_config_files, format_render_action_line
        from .render_hasher import hash_file_disk, compute_merkle_node_hash
        from .render_lock import RenderBucket
        
        # 1. Parse and merge TOMLs from resolved dependencies
        combined_dict: dict = {}
        source_files: list[Path] = []

        for dep in self.depends_on:
            if isinstance(dep, PathNode):
                if dep.src_path is not None:
                    source_files.append(dep.src_path)
                if dep.dst_path.is_file():
                    combined_dict = merge_toml(combined_dict, parse_toml(dep.dst_path.read_text(encoding="utf-8")))
                else:
                    raise FileNotFoundError(f"Dependency output file not found: {dep.dst_path}")

        # 2. Dynamic Python package hook
        combined_dict, hook_path = apply_package_hook(
            self.package_dir,
            combined_dict,
            self.workspace_config,
        )
        if hook_path:
            source_files.append(hook_path)

        # 3. Variable stitching & interpolation
        stitched_dict, env_res = resolve_and_interpolate_package_config(
            combined_dict,
            package_name=context.package_name,
            workspace_config=self.workspace_config,
        )

        # 4. Write final stitched TOML to render/<pkg>/.drift/drift_package.toml
        target_path = self.dst_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            format_render_action_line(
                "CONFIG",
                target_path,
                drift_root=context.drift_root,
            )
        )
        if not context.dry_run:
            target_path.write_text(dump_toml(stitched_dict), encoding="utf-8")

        # 5. Merkle hash calculation
        own_h = hash_file_disk(self.dst_path) or ""
        m_h = compute_merkle_node_hash(self) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path)

        # 6. Prune obsolete configs
        pruned = prune_obsolete_config_files(
            drift_root=context.drift_root,
            package_render_dir=context.package_render_dir,
            active_paths=context.result.active_paths,
            dry_run=context.dry_run,
        )
        context.result.pruned_paths.extend(pruned)

        # 7. Update lockfile bucket
        context.lockfile.update_bucket_hashes(RenderBucket.CONFIG, context.active_hashes)
        context.save_lockfile()

        # 8. Model construction
        self.source_files = source_files
        self.package_config = PackageConfig.from_dict(
            stitched_dict,
            package_name=context.package_name,
            source_files=source_files,
            base_dir=self.package_dir,
            workspace_config=self.workspace_config,
        )


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
        context.save_lockfile()


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
        context.save_lockfile()

