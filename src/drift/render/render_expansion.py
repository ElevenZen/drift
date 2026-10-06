"""AST expansion, path translation, and render collision detection.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 2: AST Expansion & Collision Detection
    - expand_node_dependencies(root, ctx) -> None
        Expands all UnknownPathNode dependencies of a root node into concrete AST nodes.
    - expand_unknown_file(file_path, ctx) -> Node
        Recursively resolves UnknownPathNode into concrete Node hierarchy.
    - create_node_for_file(file_path, ctx) -> Node
        Constructs concrete Node based on cache, engine matching, and path translation.
    - assert_no_render_collisions(dst_path, src_path, collision_map, pkg_name)
        Validates collision-free 1:1 or N:1 mappings to destination paths.

Layer 1: Path Translation & Root Discovery
    - resolve_engine_input_path(input_file, drift_root) -> Path
        Converts canonical absolute input_file into a relative path from drift_root.
    - translate_path(path, translation_map) -> Path
        Prefix-based translation of source paths to render destination paths.
    - make_root_dependency_node(src_path, pkg_source_dir, drift_root) -> Node
        Wraps candidate file into UnknownPathNode (managed) or IndependentFileNode (external).
    - to_node_key(path) -> str
        Canonical string key representation for node deduplication.
    - ExpansionContext: State container holding engine registry, translation rules, and node cache.
===============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Optional, Callable, cast, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderEngineRegistry, RenderEngineConfig

from ..core.constants import (
    CONFIG_DIR_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
)
from ..core.exceptions import RenderCollisionError, ConfigError, CyclicDependencyError
from ..utils.path_utils import is_relative_to, encode_dot_prefix, to_relative_posix
from .render_cache import RenderCache
from .render_dag import (
    Node,
    JsonNode,
    UnknownPathNode,
    IndependentFileNode,
    StaticFileNode,
    DirectoryNode,
    CachedNode,
    EngineOutputFileNode,
)


def to_node_key(path: Path) -> str:
    """Converts a Path into a canonical string key for node_refs."""
    return path.as_posix()


@dataclass
class ExpansionContext:
    """State container for AST expansion, node deduplication, and collision detection."""

    drift_root: Path
    package_name: str
    enable_render: bool
    env_node: JsonNode
    render_engines: "RenderEngineRegistry"
    cache: RenderCache
    path_translation: Dict[Path, Path] = field(default_factory=dict)
    node_refs: Dict[str, Node] = field(default_factory=dict)
    collision_map: Dict[Path, Path] = field(default_factory=dict)
    visiting_paths: Set[str] = field(default_factory=set)
    visiting_engines: List[str] = field(default_factory=list)

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
            config/<rel_path> -> render/<package_name>/.drift/render/workspace/<rel_path>
        Package engine inputs:
            src/<package_name>/<rel_path> -> render/<package_name>/.drift/render/package/<rel_path>
        """
        pkg_render_dir = self.drift_root / "render" / self.package_name
        render_internal = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME
        return {
            self.drift_root / CONFIG_DIR_NAME: render_internal / DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME,
            self.drift_root / "src" / self.package_name: render_internal / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
        }

    def derive_engine_input_context(self) -> "ExpansionContext":
        """Creates an ExpansionContext specialized for expanding engine input files,
        using engine input translation rules while sharing node_refs, collision_map,
        and circularity detection state.
        """
        return ExpansionContext(
            package_name=self.package_name,
            enable_render=self.enable_render,
            env_node=self.env_node,
            render_engines=self.render_engines,
            cache=self.cache,
            path_translation=self.get_engine_input_translation_rules(),
            node_refs=self.node_refs,
            collision_map=self.collision_map,
            visiting_paths=self.visiting_paths,
            visiting_engines=self.visiting_engines,
            drift_root=self.drift_root,
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
    src_path: Path,
    pkg_source_dir: Path,
    drift_root: Path,
) -> Node:
    """Wraps a candidate file into an initial AST dependency for root nodes.

    Files located inside pkg_source_dir become UnknownPathNode with paths relative to drift_root.
    Files located outside pkg_source_dir are wrapped in IndependentFileNode with absolute paths,
    ensuring unmanaged external files take part only as leaf dependencies and are not rendered.
    """
    abs_file = (drift_root / src_path).resolve()
    abs_pkg_source = pkg_source_dir.resolve()
    if is_relative_to(abs_file, abs_pkg_source):
        return UnknownPathNode(abs_file)
    return IndependentFileNode(abs_file)


def assert_no_render_collisions(
    dst_path: Path,
    src_path: Path,
    collision_map: Dict[Path, Path],
    package_name: str,
) -> None:
    """Validates that two different source files do not compile to the same output destination.

    Raises:
        RenderCollisionError: If dst_path is already mapped to a different input file.
    """
    if dst_path in collision_map and collision_map[dst_path] != src_path:
        prev_file = collision_map[dst_path]
        raise RenderCollisionError(
            f"Multiple source files in package '{package_name}' render to the same destination path '{dst_path.as_posix()}': "
            f"'{prev_file}' and '{src_path}'."
        )


def assert_not_driftignore_template_target(
    dst_path: Path,
    file_path: Path,
    package_name: str,
    drift_root: Path,
) -> None:
    """Validates that a template does not dynamically target .drift_ignore.

    Raises:
        ConfigError: If dst_path decodes or matches .drift_ignore.
    """
    target_name = encode_dot_prefix(Path(dst_path.name)).name
    if target_name in DRIFT_IGNORE_FILE_NAME_LIST:
        pkg_render_dir = drift_root / f"render/{package_name}"
        target_rel = to_relative_posix(dst_path, pkg_render_dir)
        raise ConfigError(
            f"Package '{package_name}' cannot render template '{file_path.name}' to '{target_rel}'. "
            f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
        )


def create_node_for_file(file_path: Path, ctx: ExpansionContext) -> Node:
    """Creates a concrete Node for a file path based on running cache, engine matching,
    path translation, and collision detection.
    """
    # 1. Determine stripped path by checking if file is handled by a render engine
    engine = ctx.render_engines.find_engine_for_file(file_path.as_posix()) if ctx.enable_render else None
    stripped_path = Path(engine.strip_suffix(file_path.as_posix())) if engine else file_path

    # 2. Translate stripped path to target destination path via path_translation
    dst_path = translate_path(stripped_path, ctx.path_translation)

    # 3. Check process-level running cache for the translated dst_path
    cached_hashes = ctx.cache.get(dst_path, src_path=file_path)
    if cached_hashes is not None:
        return CachedNode(dst_path, src_path=file_path, hashes=cached_hashes)

    # 4. If the file is not handled by an engine and is not translated into a new output path,
    # it represents an untranslated external or leaf file dependency.
    if engine is None and dst_path == file_path:
        return IndependentFileNode(file_path)

    # 5. Early collision check on dst_path
    assert_no_render_collisions(dst_path, file_path, ctx.collision_map, ctx.package_name)
    ctx.collision_map[dst_path] = file_path

    if engine:
        assert_not_driftignore_template_target(dst_path, file_path, ctx.package_name, ctx.drift_root)
        # Recursively expand input template if engine declares an input_file
        input_node: Optional[Node] = None
        if engine.input_file is not None:
            if engine.name in ctx.visiting_engines:
                cycle_str = " -> ".join(ctx.visiting_engines + [engine.name])
                raise CyclicDependencyError(
                    f"Cyclic dependency detected: render engine inputs form a cycle: {cycle_str}."
                )
            ctx.visiting_engines.append(engine.name)
            try:
                input_ctx = ctx.derive_engine_input_context()
                input_node = expand_unknown_path(engine.input_file, input_ctx)
            finally:
                ctx.visiting_engines.pop()
        template_node = IndependentFileNode(file_path)
        engine_node = ctx.get_engine_node(engine)

        return EngineOutputFileNode(
            dst_path=dst_path,
            input_node=input_node,
            template_node=template_node,
            env_node=ctx.env_node,
            engine_node=engine_node,
            engine_config=engine,
        )
    else:
        return StaticFileNode(dst_path=dst_path, src_path=file_path)


def create_node_for_dir(dir_path: Path, ctx: ExpansionContext) -> Node:
    dst_path = translate_path(dir_path, ctx.path_translation)
    cached_hashes = ctx.cache.get(dst_path, src_path=dir_path)
    if cached_hashes is not None:
        return CachedNode(dst_path, src_path=dir_path, hashes=cached_hashes)
    assert_no_render_collisions(dst_path, dir_path, ctx.collision_map, ctx.package_name)
    ctx.collision_map[dst_path] = dir_path
    return DirectoryNode(dst_path=dst_path, src_path=dir_path)


def expand_unknown_path(path: Path, ctx: ExpansionContext) -> Node:
    """Expands an UnknownPathNode into its concrete cached, template, or static node,
    reusing existing references from node_refs via get_or_register_path_node.
    """
    key = to_node_key(path)
    if key in ctx.node_refs:
        return ctx.node_refs[key]
    if key in ctx.visiting_paths:
        raise CyclicDependencyError(
            f"Cyclic dependency detected: path '{path.as_posix()}' forms a cycle during expansion."
        )
    ctx.visiting_paths.add(key)
    try:
        return ctx.get_or_register_path_node(
            path,
            lambda: create_node_for_dir(path, ctx) if path.is_dir() else create_node_for_file(path, ctx),
        )
    finally:
        ctx.visiting_paths.discard(key)


def expand_node_dependencies(root: Node, ctx: ExpansionContext) -> None:
    """Expands all UnknownPathNode dependencies of a root node into concrete AST nodes."""
    expanded: list[Node] = []
    for dep in root.depends_on:
        if isinstance(dep, UnknownPathNode):
            expanded.append(expand_unknown_path(dep.src_path, ctx))
        else:
            expanded.append(dep)
    root.depends_on = expanded

