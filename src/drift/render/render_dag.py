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
from typing import List, Optional, Any, Generic, TypeVar, TYPE_CHECKING

_DstT = TypeVar("_DstT", bound=Optional[Path])
_SrcT = TypeVar("_SrcT", bound=Optional[Path])

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineConfig
    from ..config.workspace_config import WorkspaceConfig
    from ..config.package_config import PackageConfig
    from .render_digester import DigestionContext

from ..core.constants import PACKAGE_CONFIG_FILE_NAME
from ..core.file_action import FileAction, FileActionType, format_action_line
from .render_cache import NodeHashes


def _log_digested_action(action: FileAction, context: "DigestionContext") -> None:
    """Logs action line using logger.debug when context.silent=True, or logger.info otherwise."""
    line = format_action_line(action, drift_root=context.drift_root)
    if context.silent:
        logger.debug(line)
    else:
        logger.info(line)


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

        compute_merkle_node_hash(self, package_render_dir=context.absolute_package_render_dir)


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
class PathNode(Node, Generic[_DstT, _SrcT]):
    """Base class for any node representing a filesystem path (file or directory)."""

    dst_path: _DstT
    src_path: _SrcT

    def __init__(
        self,
        dst_path: _DstT = None,  # type: ignore[assignment]
        src_path: _SrcT = None,  # type: ignore[assignment]
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
class FileNode(PathNode[Path, _SrcT], Generic[_SrcT]):
    """Base class for any node representing a file path."""

    def __init__(
        self,
        dst_path: Path,
        src_path: _SrcT = None,  # type: ignore[assignment]
        depends_on: Optional[List[Node]] = None,
        hashes: Optional[NodeHashes] = None,
    ):
        super().__init__(dst_path=dst_path, src_path=src_path, depends_on=depends_on, hashes=hashes)


@dataclass(init=False)
class UnknownPathNode(PathNode[Optional[Path], Path]):
    """Placeholder AST node whose concrete type is resolved during expansion."""

    def __init__(self, src_path: Path):
        super().__init__(dst_path=None, src_path=src_path)


@dataclass(init=False)
class IndependentFileNode(FileNode[Path]):
    """Unmanaged source asset or template (leaf file node)."""

    def __init__(self, src_path: Path):
        super().__init__(dst_path=src_path, src_path=src_path)


@dataclass(init=False)
class StaticFileNode(FileNode[Path]):
    """1:1 copied static file."""

    def __init__(self, dst_path: Path, src_path: Path):
        super().__init__(dst_path=dst_path, src_path=src_path, depends_on=[IndependentFileNode(src_path)])

    def digest(self, context: "DigestionContext") -> None:
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_file_disk, compute_merkle_node_hash

        if check_and_apply_cache(self, self.dst_path, context):
            return

        if not self.src_path.is_file():
            raise FileNotFoundError(f"Static source file not found: {self.src_path}")

        action_type = (
            FileActionType.UPDATE_COPY
            if self.dst_path.is_file()
            else FileActionType.CREATE_COPY
        )
        action = FileAction(
            action_type=action_type,
            src_path=self.src_path,
            dst_path=self.dst_path,
        )
        context.result.actions.append(action)
        _log_digested_action(action, context)

        if not context.dry_run:
            self.dst_path.parent.mkdir(parents=True, exist_ok=True)
            if self.src_path.resolve() != self.dst_path.resolve():
                shutil.copy2(self.src_path, self.dst_path)

        own_h = context.hash_file(self.dst_path) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=None)
        m_h = compute_merkle_node_hash(self, package_render_dir=context.absolute_package_render_dir) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        from .render_hasher import format_hash_log
        logger.debug(
            f"[Digest] StaticFileNode '{self.dst_path}': own_hash={format_hash_log(own_h)}, merkle_hash={format_hash_log(m_h)}."
        )
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path)


@dataclass(init=False)
class DirectoryNode(PathNode[Path, Optional[Path]]):
    """Empty directory synchronization target (.drift_keep)."""

    def __init__(self, dst_path: Path, src_path: Optional[Path] = None):
        super().__init__(dst_path=dst_path, src_path=src_path)

    def digest(self, context: "DigestionContext") -> None:
        from ..core.constants import DRIFT_KEEP_FILE_NAME
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_directory_disk, compute_merkle_node_hash, format_hash_log

        keep_file = self.dst_path / DRIFT_KEEP_FILE_NAME
        if check_and_apply_cache(self, self.dst_path, context):
            context.result.skipped_paths.append(keep_file)
            return

        dir_existed = self.dst_path.is_dir()
        if not context.dry_run:
            self.dst_path.mkdir(parents=True, exist_ok=True)
            keep_file.touch()

        action = FileAction(
            action_type=FileActionType.ENSURE_DIR,
            src_path=self.src_path,
            dst_path=self.dst_path,
        )
        context.result.actions.append(action)
        _log_digested_action(action, context)

        own_h = context.hash_directory(self.dst_path) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=None)
        m_h = compute_merkle_node_hash(self, package_render_dir=context.absolute_package_render_dir) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.result.rendered_paths.append(keep_file)
        context.active_hashes.add(m_h)
        logger.debug(
            f"[Digest] DirectoryNode '{self.dst_path}': own_hash={format_hash_log(own_h)}, merkle_hash={format_hash_log(m_h)}."
        )
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path or self.dst_path)


@dataclass(init=False)
class CachedNode(FileNode[Path]):
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

        context.result.skipped_paths.append(self.dst_path)
        keep_file = self.dst_path / DRIFT_KEEP_FILE_NAME
        if keep_file.is_file():
            context.result.skipped_paths.append(keep_file)
        if self.hashes and self.hashes.merkle_hash:
            context.active_hashes.add(self.hashes.merkle_hash)
        action = FileAction(
            action_type=FileActionType.SKIP_IDENTICAL,
            src_path=self.src_path,
            dst_path=self.dst_path,
        )
        context.result.actions.append(action)
        logger.debug(format_action_line(action, drift_root=context.drift_root))


@dataclass(init=False)
class EngineOutputFileNode(FileNode[Path]):
    """Template compiled by a render engine."""

    input_node: Optional[Node]
    template_node: PathNode[Any, Path]
    env_node: JsonNode
    engine_node: JsonNode
    engine_config: Optional["RenderEngineConfig"]

    def __init__(
        self,
        dst_path: Path,
        input_node: Optional[Node],
        template_node: PathNode[Any, Path],
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
        from .render_digester import check_and_apply_cache
        from .render_hasher import hash_file_disk, compute_merkle_node_hash
        from .render_core import render_template_to_file

        if check_and_apply_cache(self, self.dst_path, context):
            return

        tmpl = self.src_path
        if not tmpl.is_file():
            raise FileNotFoundError(f"Template source file not found: {tmpl}")

        dst = self.dst_path
        in_file = self.input_node.dst_path if isinstance(self.input_node, PathNode) and self.input_node.dst_path is not None else None

        if self.engine_config is None:
            raise ValueError(f"EngineOutputFileNode for '{self.dst_path}' lacks engine_config")

        action = FileAction(
            action_type=FileActionType.RENDER_ITEM,
            src_path=tmpl,
            dst_path=dst,
            reason=self.engine_config.name,
        )
        context.result.actions.append(action)
        _log_digested_action(action, context)

        if not context.dry_run:
            render_template_to_file(
                engine_config=self.engine_config,
                drift_root=context.drift_root,
                template_file_path=tmpl,
                output_file_path=dst,
                input_file_path=in_file,
            )

        own_h = context.hash_file(self.dst_path) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=None)
        m_h = compute_merkle_node_hash(self, package_render_dir=context.absolute_package_render_dir) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        from .render_hasher import format_hash_log
        logger.debug(
            f"[Digest] EngineOutputFileNode '{self.dst_path}': own_hash={format_hash_log(own_h)}, merkle_hash={format_hash_log(m_h)} (engine={self.engine_config.name if self.engine_config else 'none'})."
        )
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=tmpl)


@dataclass(init=False)
class PackageConfigNode(FileNode[Optional[Path]]):
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
        # Determine primary source for hashing:
        # if static config file exists, use it;
        # otherwise, use first available source node with a src_path.
        primary_src = source_file if source_file.exists() else next(
            (getattr(s, "src_path", None) for s in sources if getattr(s, "src_path", None) is not None),
            None,
        )
        super().__init__(dst_path=dst_path, src_path=primary_src, depends_on=[*sources, env_node])
        self.package_dir = package_dir
        self.workspace_config = workspace_config
        self.package_config = None
        self.source_files = []

    def digest(self, context: "DigestionContext") -> None:
        from ..config.package_config import PackageConfig
        from ..config.package_loader import resolve_and_interpolate_package_config
        from ..core.exceptions import ConfigError
        from ..hooks.package_hook import apply_package_hook
        from ..utils.toml_utils import parse_toml, merge_toml, dump_toml
        from .render_cache import NodeHashes
        from .render_digester import prune_obsolete_config_files
        from .render_hasher import hash_file_disk, compute_merkle_node_hash, format_hash_log
        from .render_lock import RenderBucket
        
        # 1. Parse and merge TOMLs from resolved dependencies
        combined_dict: dict = {}
        source_files: list[Path] = []

        for dep in self.depends_on:
            if isinstance(dep, PathNode):
                if dep.src_path is not None:
                    source_files.append(dep.src_path)
                if dep.dst_path is not None and dep.dst_path.is_file():
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

        if not combined_dict:
            raise ConfigError(
                f"Package configuration for '{self.package_dir.name}' is empty. "
                f"At least one configuration file or package hook in '{self.package_dir}' must provide valid configuration."
            )

        # 3. Variable stitching & interpolation
        stitched_dict, env_res = resolve_and_interpolate_package_config(
            combined_dict,
            package_name=context.package_name,
            workspace_config=self.workspace_config,
        )

        # 4. Write final stitched TOML to render/<pkg>/.drift/drift_package.toml
        target_path = self.dst_path
        new_content = dump_toml(stitched_dict)
        already_matched = False
        if target_path.is_file():
            try:
                already_matched = (target_path.read_text(encoding="utf-8") == new_content)
            except Exception:
                already_matched = False

        action_type = (
            FileActionType.SKIP_IDENTICAL if already_matched else FileActionType.WRITE_CONFIG
        )
        action = FileAction(
            action_type=action_type,
            src_path=self.src_path or target_path,
            dst_path=target_path,
        )
        context.result.actions.append(action)
        _log_digested_action(action, context)

        if not context.dry_run and not already_matched:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(new_content, encoding="utf-8")

        # 5. Merkle hash calculation
        own_h = context.hash_file(self.dst_path) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=None)
        m_h = compute_merkle_node_hash(self, package_render_dir=context.absolute_package_render_dir) or ""
        self.hashes = NodeHashes(own_hash=own_h, merkle_hash=m_h)
        if already_matched:
            context.result.skipped_paths.append(self.dst_path)
        else:
            context.result.rendered_paths.append(self.dst_path)
        context.active_hashes.add(m_h)
        logger.debug(
            f"[Digest] PackageConfigNode '{self.dst_path}': own_hash={format_hash_log(own_h)}, merkle_hash={format_hash_log(m_h)}."
        )
        if context.cache is not None:
            context.cache.set(self.dst_path, self.hashes, src_path=self.src_path)

        # 6. Prune obsolete configs
        pruned = prune_obsolete_config_files(context)
        context.result.pruned_paths.extend(pruned)
        for p in pruned:
            context.result.actions.append(
                FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=p)
            )

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
        from .render_digester import prune_obsolete_hooks
        from .render_lock import RenderBucket

        super().digest(context)
        if self.merkle_hash:
            context.active_hashes.add(self.merkle_hash)

        pruned = prune_obsolete_hooks(context)
        context.result.pruned_paths.extend(pruned)
        context.result.actions.extend(
            FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=p)
            for p in pruned
        )
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

        pruned = prune_obsolete_payload_files(context)
        context.result.pruned_paths.extend(pruned)
        context.result.actions.extend(
            FileAction(action_type=FileActionType.DELETE_ITEM, dst_path=p)
            for p in pruned
        )
        context.lockfile.update_bucket_hashes(RenderBucket.PAYLOAD, context.active_hashes)
        context.save_lockfile()

