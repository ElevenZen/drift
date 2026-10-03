"""AST expansion, path translation, and render collision detection.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: AST Expansion & Collision Detection
    - expand_unknown_file(file_path, ctx) -> Node
        Recursively resolves UnknownFileNode into concrete Node hierarchy.
    - create_node_for_file(file_path, ctx) -> Node
        Constructs concrete Node based on cache, engine matching, and path translation.
    - assert_no_render_collisions(output_path, input_path, collision_map, pkg_name)
        Validates collision-free 1:1 or N:1 mappings to destination paths.

Layer 1: Path Translation & Root Discovery
    - translate_path(path, translation_map) -> Path
        Prefix-based translation of source paths to render destination paths.
    - make_root_dependency_node(file_path, pkg_source_dir, drift_root) -> Node
        Wraps candidate file into UnknownFileNode (managed) or IndependentFileNode (external).
    - to_node_key(path) -> str
        Canonical string key representation for node deduplication.
    - ExpansionContext: State container holding engine registry, translation rules, and node cache.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Callable, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineRegistry, RenderEngineConfig

from ..core.exceptions import RenderCollisionError
from .render_cache import static_render_cache
from .render_dag import (
    Node,
    JsonNode,
    UnknownFileNode,
    IndependentFileNode,
    StaticFileNode,
    CachedNode,
    EngineOutputFileNode,
)


def to_node_key(path: Path) -> str:
    """Converts a Path into a canonical string key for node_refs."""
    return path.as_posix()


@dataclass
class ExpansionContext:
    """State container for AST expansion, node deduplication, and collision detection."""

    package_name: str
    enable_render: bool
    env_node: JsonNode
    render_engines: "RenderEngineRegistry"
    path_translation: Dict[Path, Path] = field(default_factory=dict)
    node_refs: Dict[str, Node] = field(default_factory=dict)
    collision_map: Dict[Path, Path] = field(default_factory=dict)

    def get_or_register_node(self, key: str, factory: Callable[[], Node]) -> Node:
        """Retrieves existing node by canonical key or creates and registers it."""
        if key not in self.node_refs:
            self.node_refs[key] = factory()
        return self.node_refs[key]

    def get_or_register_path_node(self, path: Path, factory: Callable[[], Node]) -> Node:
        """Retrieves or registers a node for a filesystem path using its canonical key."""
        return self.get_or_register_node(to_node_key(path), factory)

    def get_engine_node(self, engine: "RenderEngineConfig") -> JsonNode:
        """Retrieves or creates a deterministic JsonNode for an engine configuration."""
        key = f"engine:{engine.name}"
        return cast(
            JsonNode,
            self.get_or_register_node(
                key,
                lambda: JsonNode({
                    "name": engine.name,
                    "render_command": engine.render_command,
                    "suffix": engine.suffix,
                    "is_internal": engine.is_internal,
                }),
            ),
        )

    def get_engine_input_translation_rules(self) -> Dict[Path, Path]:
        """Generates path translation rules for engine input files.

        Workspace engine inputs:
            config/<rel_path> -> render/.drift/render/<rel_path>
        Package engine inputs:
            src/<package_name>/<rel_path> -> render/<package_name>/.drift/render/<rel_path>
        """
        return {
            Path("config"): Path("render/.drift/render"),
            Path(f"src/{self.package_name}"): Path(f"render/{self.package_name}/.drift/render"),
        }

    def derive_engine_input_context(self) -> "ExpansionContext":
        """Creates an ExpansionContext specialized for expanding engine input files,
        using engine input translation rules while sharing node_refs and collision_map.
        """
        return ExpansionContext(
            package_name=self.package_name,
            enable_render=self.enable_render,
            env_node=self.env_node,
            render_engines=self.render_engines,
            path_translation=self.get_engine_input_translation_rules(),
            node_refs=self.node_refs,
            collision_map=self.collision_map,
        )


def translate_path(path: Path, translation_map: Dict[Path, Path]) -> Path:
    """Translates a source path to its destination path using longest matching prefix rule.

    If path matches or is relative to any key in translation_map, transforms it to be relative
    to the corresponding destination value. If no key matches, returns the original path unchanged.
    """
    if path in translation_map:
        return translation_map[path]

    sorted_rules = sorted(
        translation_map.items(),
        key=lambda kv: len(kv[0].parts),
        reverse=True,
    )
    for src_prefix, dst_prefix in sorted_rules:
        try:
            rel = path.relative_to(src_prefix)
            return dst_prefix / rel
        except ValueError:
            continue

    return path


def make_root_dependency_node(
    file_path: Path,
    pkg_source_dir: Path,
    drift_root: Path,
) -> Node:
    """Wraps a candidate file into an initial AST dependency for root nodes.

    Files located inside pkg_source_dir become UnknownFileNode with paths relative to drift_root.
    Files located outside pkg_source_dir are wrapped in IndependentFileNode with absolute paths,
    ensuring unmanaged external files take part only as leaf dependencies and are not rendered.
    """
    abs_file = file_path if file_path.is_absolute() else (drift_root / file_path).resolve()
    abs_pkg_source = pkg_source_dir.resolve()

    try:
        abs_file.relative_to(abs_pkg_source)
        rel_to_drift = abs_file.relative_to(drift_root.resolve())
        return UnknownFileNode(rel_to_drift)
    except ValueError:
        return IndependentFileNode(abs_file)


def assert_no_render_collisions(
    output_path: Path,
    input_path: Path,
    collision_map: Dict[Path, Path],
    package_name: str,
) -> None:
    """Validates that two different source files do not compile to the same output destination.

    Raises:
        RenderCollisionError: If output_path is already mapped to a different input file.
    """
    if output_path in collision_map and collision_map[output_path] != input_path:
        prev_file = collision_map[output_path]
        raise RenderCollisionError(
            f"Multiple source files in package '{package_name}' render to the same destination path '{output_path.as_posix()}': "
            f"'{prev_file}' and '{input_path}'."
        )


def create_node_for_file(file_path: Path, ctx: ExpansionContext) -> Node:
    """Creates a concrete Node for a file path based on running cache, engine matching,
    path translation, and collision detection.
    """
    # 1. Determine stripped path by checking if file is handled by a render engine
    engine = ctx.render_engines.find_engine_for_file(file_path.as_posix()) if ctx.enable_render else None
    stripped_path = Path(engine.strip_suffix(file_path.as_posix())) if engine else file_path

    # 2. Translate stripped path to target destination path via path_translation
    output_path = translate_path(stripped_path, ctx.path_translation)

    # 3. Check static process-level running cache for the translated output_path
    cached_hashes = static_render_cache.get(output_path)
    if cached_hashes is not None:
        return CachedNode(output_path, hashes=cached_hashes)

    # 4. Early collision check on output_path
    assert_no_render_collisions(output_path, file_path, ctx.collision_map, ctx.package_name)
    ctx.collision_map[output_path] = file_path

    if engine:
        # Recursively expand input template if engine declares an input_file
        input_node: Optional[Node] = None
        if engine.input_file is not None:
            input_ctx = ctx.derive_engine_input_context()
            input_node = expand_unknown_file(engine.input_file, input_ctx)
        template_node = IndependentFileNode(file_path)
        engine_node = ctx.get_engine_node(engine)

        return EngineOutputFileNode(
            output_path=output_path,
            input_node=input_node,
            template_node=template_node,
            env_node=ctx.env_node,
            engine_node=engine_node,
            engine_config=engine,
        )
    else:
        return StaticFileNode(output_path=output_path, src_path=file_path)


def expand_unknown_file(file_path: Path, ctx: ExpansionContext) -> Node:
    """Expands an UnknownFileNode into its concrete cached, template, or static node,
    reusing existing references from node_refs via get_or_register_path_node.
    """
    return ctx.get_or_register_path_node(
        file_path,
        lambda: create_node_for_file(file_path, ctx),
    )
