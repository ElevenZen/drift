"""Targeted unit tests for render DAG models, tree expansion, and topological sorting."""

import unittest
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

from drift.core.exceptions import RenderCollisionError
from drift.render.render_cache import NodeHashes, StaticRenderCache, static_render_cache
from drift.render.render_dag import (
    Node,
    TextNode,
    JsonNode,
    FileNode,
    UnknownFileNode,
    IndependentFileNode,
    StaticFileNode,
    DirectoryNode,
    CachedNode,
    EngineOutputFileNode,
    PackageConfigNode,
    PackageHooksNode,
    PackagePayloadNode,
)
from drift.render.render_expansion import (
    ExpansionContext,
    to_node_key,
    expand_unknown_file,
    create_node_for_file,
    translate_path,
    make_root_dependency_node,
)
from drift.render.render_digester import topological_sort_nodes


class TestRenderDAG(unittest.TestCase):
    def setUp(self) -> None:
        static_render_cache.clear()

    def tearDown(self) -> None:
        static_render_cache.clear()

    def test_node_attributes_and_defaults(self) -> None:
        """Validates that base Node and FileNode properly store values and default to None for hashes."""
        base_node = Node(value="test_val")
        self.assertEqual(base_node.value, "test_val")
        self.assertEqual(base_node.depends_on, [])
        self.assertIsNone(base_node.own_hash)
        self.assertIsNone(base_node.merkle_hash)

        file_node = FileNode(Path("foo/bar.txt"))
        self.assertEqual(file_node.value, "foo/bar.txt")
        self.assertEqual(file_node.file_path, Path("foo/bar.txt"))
        self.assertIsNone(file_node.own_hash)
        self.assertIsNone(file_node.merkle_hash)

    def test_package_payload_node(self) -> None:
        """Validates PackagePayloadNode container properties."""
        p_node = PackagePayloadNode("my_pkg", [])
        self.assertEqual(p_node.value, "my_pkg")
        self.assertEqual(p_node.depends_on, [])

    def test_text_node_and_json_node_hash_determinism(self) -> None:
        """Validates TextNode and JsonNode constructor hashing, key sorting, and data retention."""
        t1 = TextNode("hello world")
        self.assertIsNotNone(t1.own_hash)
        self.assertEqual(t1.own_hash, t1.merkle_hash)
        self.assertEqual(t1.value, "hello world")

        # JsonNode should sort keys deterministically regardless of input insertion order
        data1 = {"b": 2, "a": 1, "nested": {"z": 26, "y": 25}}
        data2 = {"a": 1, "nested": {"y": 25, "z": 26}, "b": 2}
        j1 = JsonNode(data1)
        j2 = JsonNode(data2)
        self.assertEqual(j1.value, j2.value)
        self.assertEqual(j1.merkle_hash, j2.merkle_hash)
        self.assertEqual(j1.data, data1)
        self.assertEqual(j2.data, data2)

    def test_static_render_cache(self) -> None:
        """Validates StaticRenderCache set, get, contains, and clear."""
        cache = StaticRenderCache()
        p = Path("render/pkg/.zshrc")
        self.assertFalse(cache.contains(p))
        self.assertIsNone(cache.get(p))

        cache.register(p, own_hash="hash1", merkle_hash="hash2")
        self.assertTrue(cache.contains(p))
        self.assertEqual(cache.get(p), NodeHashes(own_hash="hash1", merkle_hash="hash2"))
        self.assertEqual(len(cache), 1)

        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertFalse(cache.contains(p))

    def test_cached_node_restoration(self) -> None:
        """Validates CachedNode restores both own_hash and merkle_hash."""
        c = CachedNode(Path("app.conf"), hashes=NodeHashes(own_hash="h_own", merkle_hash="h_merkle"))
        self.assertEqual(c.file_path, Path("app.conf"))
        self.assertEqual(c.own_hash, "h_own")
        self.assertEqual(c.merkle_hash, "h_merkle")
        self.assertEqual(c.depends_on, [])

    def test_translate_path(self) -> None:
        """Validates path translation with exact, prefix, and longest-prefix rules."""
        translation_map = {
            Path("src/pkg/drift_package.toml"): Path("render/pkg/.drift/drift_package.toml"),
            Path("src/pkg/drift_hooks"): Path("render/pkg/.drift/hooks"),
            Path("src/pkg"): Path("render/pkg"),
        }

        # Exact match
        self.assertEqual(
            translate_path(Path("src/pkg/drift_package.toml"), translation_map),
            Path("render/pkg/.drift/drift_package.toml"),
        )

        # Longest prefix match: drift_hooks takes precedence over src/pkg
        self.assertEqual(
            translate_path(Path("src/pkg/drift_hooks/pre_render.sh"), translation_map),
            Path("render/pkg/.drift/hooks/pre_render.sh"),
        )

        # General prefix match
        self.assertEqual(
            translate_path(Path("src/pkg/configs/app.toml"), translation_map),
            Path("render/pkg/configs/app.toml"),
        )

        # Unmatched path returns itself
        self.assertEqual(
            translate_path(Path("external/file.txt"), translation_map),
            Path("external/file.txt"),
        )

    def test_make_root_dependency_node(self) -> None:
        """Validates that candidate files inside pkg_source_dir become UnknownFileNode
        and files outside become IndependentFileNode with absolute paths.
        """
        drift_root = Path("/workspace")
        pkg_source_dir = Path("/workspace/src/test_pkg")

        # File inside package source directory
        inside_file = Path("/workspace/src/test_pkg/scripts/deploy.sh")
        inside_node = make_root_dependency_node(inside_file, pkg_source_dir, drift_root)
        self.assertIsInstance(inside_node, UnknownFileNode)
        assert isinstance(inside_node, UnknownFileNode)
        self.assertEqual(inside_node.file_path, Path("src/test_pkg/scripts/deploy.sh"))

        # File outside package source directory (e.g. global config or external asset)
        outside_file = Path("/workspace/config/shared.json")
        outside_node = make_root_dependency_node(outside_file, pkg_source_dir, drift_root)
        self.assertIsInstance(outside_node, IndependentFileNode)
        assert isinstance(outside_node, IndependentFileNode)
        self.assertEqual(outside_node.file_path, outside_file.resolve())

    def test_expand_unknown_file_static(self) -> None:
        """Validates expansion of a file without an engine into a StaticFileNode with path translation."""
        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = None

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"FOO": "bar"}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        node = expand_unknown_file(Path("src/test_pkg/scripts/run.sh"), ctx)
        self.assertIsInstance(node, StaticFileNode)
        assert isinstance(node, StaticFileNode)
        self.assertEqual(node.file_path, Path("render/test_pkg/scripts/run.sh"))
        self.assertEqual(node.src_path, Path("src/test_pkg/scripts/run.sh"))
        self.assertEqual(len(node.depends_on), 1)
        self.assertIsInstance(node.depends_on[0], IndependentFileNode)
        assert isinstance(node.depends_on[0], IndependentFileNode)
        self.assertEqual(node.depends_on[0].file_path, Path("src/test_pkg/scripts/run.sh"))

    def test_expand_unknown_file_template(self) -> None:
        """Validates expansion of an engine template into EngineOutputFileNode with path translation."""
        mock_engine = MagicMock()
        mock_engine.name = "envsubst"
        mock_engine.suffix = "envst"
        mock_engine.render_command = "envsubst < %s"
        mock_engine.is_internal = False
        mock_engine.input_file = None
        mock_engine.strip_suffix.return_value = "src/test_pkg/config.toml"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = mock_engine

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"PORT": "8080"}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        node = expand_unknown_file(Path("src/test_pkg/config.toml.envst"), ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(node.file_path, Path("render/test_pkg/config.toml"))
        self.assertIsNone(node.input_node)
        assert isinstance(node.template_node, FileNode)
        self.assertEqual(node.template_node.file_path, Path("src/test_pkg/config.toml.envst"))
        self.assertEqual(node.env_node, ctx.env_node)
        self.assertEqual(node.engine_node.data["name"], "envsubst")
        self.assertEqual(node.engine_config, mock_engine)

    def test_expand_unknown_file_recursive_engine_input(self) -> None:
        """Validates recursive expansion when a template engine declares an input_file template,
        translating package and workspace input paths into .drift/render/ destinations.
        """
        # Engine 1: Mustache takes an input file 'src/test_pkg/data.json.envst'
        mustache_engine = MagicMock()
        mustache_engine.name = "mustache"
        mustache_engine.suffix = "mustache"
        mustache_engine.render_command = "mustache %i %s"
        mustache_engine.is_internal = False
        mustache_engine.input_file = Path("src/test_pkg/data.json.envst")
        mustache_engine.strip_suffix.return_value = "src/test_pkg/rendered.html"

        # Engine 2: envsubst compiles data.json.envst -> data.json
        envsubst_engine = MagicMock()
        envsubst_engine.name = "envsubst"
        envsubst_engine.suffix = "envst"
        envsubst_engine.render_command = "envsubst < %s"
        envsubst_engine.is_internal = False
        envsubst_engine.input_file = None
        envsubst_engine.strip_suffix.return_value = "src/test_pkg/data.json"

        def find_engine(f_str: str) -> Any:
            if f_str.endswith(".mustache"):
                return mustache_engine
            if f_str.endswith(".envst"):
                return envsubst_engine
            return None

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.side_effect = find_engine

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        root = expand_unknown_file(Path("src/test_pkg/template.html.mustache"), ctx)
        self.assertIsInstance(root, EngineOutputFileNode)
        assert isinstance(root, EngineOutputFileNode)
        # Main payload output path
        self.assertEqual(root.file_path, Path("render/test_pkg/rendered.html"))

        # Input node should be recursively expanded and translated to .drift/render/
        self.assertIsNotNone(root.input_node)
        self.assertIsInstance(root.input_node, EngineOutputFileNode)
        assert isinstance(root.input_node, EngineOutputFileNode)
        self.assertEqual(root.input_node.file_path, Path("render/test_pkg/.drift/render/data.json"))

    def test_workspace_engine_input_translation(self) -> None:
        """Validates that workspace engine inputs under config/ are translated to render/.drift/render/."""
        envsubst_engine = MagicMock()
        envsubst_engine.name = "envsubst"
        envsubst_engine.suffix = "envst"
        envsubst_engine.render_command = "envsubst < %s"
        envsubst_engine.is_internal = False
        envsubst_engine.input_file = None
        envsubst_engine.strip_suffix.return_value = "config/mustache.json"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = envsubst_engine

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
        )
        input_ctx = ctx.derive_engine_input_context()

        node = expand_unknown_file(Path("config/mustache.envst.json"), input_ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(node.file_path, Path("render/.drift/render/mustache.json"))

    def test_static_cache_produces_cached_node(self) -> None:
        """Validates that a path pre-populated in static_render_cache emits a CachedNode."""
        target_output = Path("render/test_pkg/bin/tool")
        static_render_cache.register(target_output, own_hash="own123", merkle_hash="merkle456")

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = None

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        node = expand_unknown_file(Path("src/test_pkg/bin/tool"), ctx)
        self.assertIsInstance(node, CachedNode)
        assert isinstance(node, CachedNode)
        self.assertEqual(node.file_path, target_output)
        self.assertEqual(node.own_hash, "own123")
        self.assertEqual(node.merkle_hash, "merkle456")
        mock_registry.find_engine_for_file.assert_called_once_with("src/test_pkg/bin/tool")

    def test_node_refs_shares_instances(self) -> None:
        """Validates that multiple files sharing the same engine reuse the exact same JsonNode."""
        mock_engine = MagicMock()
        mock_engine.name = "envsubst"
        mock_engine.suffix = "envst"
        mock_engine.render_command = "envsubst < %s"
        mock_engine.is_internal = False
        mock_engine.input_file = None
        mock_engine.strip_suffix.side_effect = lambda f: f.replace(".envst", "")

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = mock_engine

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"KEY": "val"}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        n1 = expand_unknown_file(Path("src/test_pkg/file1.envst"), ctx)
        n2 = expand_unknown_file(Path("src/test_pkg/file2.envst"), ctx)

        self.assertIsInstance(n1, EngineOutputFileNode)
        self.assertIsInstance(n2, EngineOutputFileNode)
        assert isinstance(n1, EngineOutputFileNode)
        assert isinstance(n2, EngineOutputFileNode)
        # Shared instances in memory
        self.assertIs(n1.env_node, n2.env_node)
        self.assertIs(n1.engine_node, n2.engine_node)

    def test_render_collision_detection(self) -> None:
        """Validates that two source files resolving to the same output path raise RenderCollisionError."""
        mock_engine = MagicMock()
        mock_engine.name = "envsubst"
        mock_engine.suffix = "envst"
        mock_engine.render_command = "envsubst < %s"
        mock_engine.is_internal = False
        mock_engine.input_file = None
        mock_engine.strip_suffix.return_value = "src/test_pkg/config.toml"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = mock_engine

        ctx = ExpansionContext(
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            path_translation={Path("src/test_pkg"): Path("render/test_pkg")},
        )

        expand_unknown_file(Path("src/test_pkg/config.toml.envst"), ctx)
        with self.assertRaises(RenderCollisionError) as cm:
            expand_unknown_file(Path("src/test_pkg/config.toml.custom.envst"), ctx)
        self.assertIn("Multiple source files in package 'test_pkg'", str(cm.exception))

    def test_topological_sort_order(self) -> None:
        """Validates topological sorting puts all dependencies strictly before their dependents."""
        leaf1 = Node(value="leaf1")
        leaf2 = Node(value="leaf2")
        mid1 = Node(value="mid1", depends_on=[leaf1, leaf2])
        mid2 = Node(value="mid2", depends_on=[leaf2])
        root = Node(value="root", depends_on=[mid1, mid2])

        sorted_nodes = topological_sort_nodes(root)
        indices = {n.value: idx for idx, n in enumerate(sorted_nodes)}

        self.assertLess(indices["leaf1"], indices["mid1"])
        self.assertLess(indices["leaf2"], indices["mid1"])
        self.assertLess(indices["leaf2"], indices["mid2"])
        self.assertLess(indices["mid1"], indices["root"])
        self.assertLess(indices["mid2"], indices["root"])
        self.assertEqual(sorted_nodes[-1], root)

    def test_topological_sort_cycle_detection(self) -> None:
        """Validates that circular dependencies raise ValueError."""
        node_a = Node(value="node_a")
        node_b = Node(value="node_b")
        node_c = Node(value="node_c")

        node_a.depends_on.append(node_b)
        node_b.depends_on.append(node_c)
        node_c.depends_on.append(node_a)  # Cycle!

        with self.assertRaises(ValueError) as cm:
            topological_sort_nodes(node_a)
        self.assertIn("Cyclic dependency detected in render graph", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
