"""Targeted unit tests for render DAG models, tree expansion, and topological sorting."""

import unittest
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

from drift.core.exceptions import RenderCollisionError
from drift.render.render_cache import NodeHashes, RenderCache
from drift.render.render_dag import (
    Node,
    PathNode,
    TextNode,
    JsonNode,
    FileNode,
    UnknownPathNode,
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
    expand_unknown_path,
    translate_path,
    make_root_dependency_node,
)
from drift.render.render_digester import topological_sort_nodes


class TestRenderDAG(unittest.TestCase):
    def setUp(self) -> None:
        self.render_cache = RenderCache()

    def tearDown(self) -> None:
        pass

    def test_node_attributes_and_defaults(self) -> None:
        """Validates that base Node and FileNode properly store values and default to None for hashes."""
        base_node = Node(value="test_val")
        self.assertEqual(base_node.value, "test_val")
        self.assertEqual(base_node.depends_on, [])
        self.assertIsNone(base_node.own_hash)
        self.assertIsNone(base_node.merkle_hash)

        file_node = FileNode(Path("foo/bar.txt"))
        self.assertEqual(file_node.value, "foo/bar.txt")
        self.assertEqual(file_node.dst_path, Path("foo/bar.txt"))
        self.assertIsNone(file_node.src_path)
        self.assertIsNone(file_node.own_hash)
        self.assertIsNone(file_node.merkle_hash)
        self.assertIsInstance(file_node, PathNode)

        path_node = PathNode(dst_path=Path("out/bar.txt"), src_path=Path("src/bar.txt"))
        self.assertEqual(path_node.value, "out/bar.txt")
        self.assertEqual(path_node.dst_path, Path("out/bar.txt"))
        self.assertEqual(path_node.src_path, Path("src/bar.txt"))
        self.assertIsInstance(path_node, Node)

    def test_unified_path_accessors(self) -> None:
        """Validates that compatible node types provide unified src_path and dst_path properties."""
        # 1. Base Node
        base = Node(value="base")
        self.assertFalse(hasattr(base, "src_path"))
        self.assertFalse(hasattr(base, "dst_path"))

        # 2. IndependentFileNode
        indep = IndependentFileNode(Path("src/pkg/leaf.txt"))
        self.assertEqual(indep.src_path, Path("src/pkg/leaf.txt"))
        self.assertEqual(indep.dst_path, Path("src/pkg/leaf.txt"))
        self.assertIsInstance(indep, PathNode)
        self.assertIsInstance(indep, FileNode)

        # 3. StaticFileNode
        static = StaticFileNode(dst_path=Path("render/pkg/file.txt"), src_path=Path("src/pkg/file.txt"))
        self.assertEqual(static.src_path, Path("src/pkg/file.txt"))
        self.assertEqual(static.dst_path, Path("render/pkg/file.txt"))
        self.assertIsInstance(static, PathNode)
        self.assertIsInstance(static, FileNode)

        # 4. DirectoryNode
        dir_node = DirectoryNode(dst_path=Path("render/pkg/empty"), src_path=Path("src/pkg/empty"))
        self.assertEqual(dir_node.dst_path, Path("render/pkg/empty"))
        self.assertEqual(dir_node.src_path, Path("src/pkg/empty"))
        self.assertIsInstance(dir_node, PathNode)
        self.assertNotIsInstance(dir_node, FileNode)

        # 5. CachedNode
        cached = CachedNode(dst_path=Path("render/pkg/out.txt"), src_path=Path("src/pkg/in.txt"))
        self.assertEqual(cached.src_path, Path("src/pkg/in.txt"))
        self.assertEqual(cached.dst_path, Path("render/pkg/out.txt"))
        self.assertIsInstance(cached, PathNode)
        self.assertIsInstance(cached, FileNode)

        # 6. EngineOutputFileNode
        engine_node = EngineOutputFileNode(
            dst_path=Path("render/pkg/out.json"),
            input_node=None,
            template_node=indep,
            env_node=JsonNode({}),
            engine_node=JsonNode({}),
        )
        self.assertEqual(engine_node.src_path, Path("src/pkg/leaf.txt"))
        self.assertEqual(engine_node.dst_path, Path("render/pkg/out.json"))
        self.assertIsInstance(engine_node, PathNode)
        self.assertIsInstance(engine_node, FileNode)

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

    def test_render_cache(self) -> None:
        """Validates RenderCache set, get, contains, and clear."""
        cache = RenderCache()
        p = Path("render/pkg/.zshrc")
        self.assertFalse(cache.contains(p))
        self.assertIsNone(cache.get(p))

        cache.set_existence_check_enabled(False)  # Disable existence check for testing
        cache.register(p, own_hash="hash1", merkle_hash="hash2")
        self.assertTrue(cache.contains(p))
        self.assertEqual(cache.get(p), NodeHashes(own_hash="hash1", merkle_hash="hash2"))
        self.assertEqual(len(cache), 1)

        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertFalse(cache.contains(p))

    def test_render_cache_stat_fingerprinting(self) -> None:
        """Validates that RenderCache invalidates when output disappears or source file mtime/size changes."""
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            source_file = tmp_root / "source.txt"
            source_file.write_text("initial source")
            output_file = tmp_root / "output.txt"
            output_file.write_text("initial output")

            cache = RenderCache()
            hashes = NodeHashes(own_hash="own1", merkle_hash="merkle1")

            # 1. Register with src_path
            cache.set(output_file, hashes, src_path=source_file)
            self.assertTrue(cache.contains(output_file, src_path=source_file))
            self.assertEqual(cache.get(output_file, src_path=source_file), hashes)

            # 2. Cache hit is stable when files unchanged
            self.assertEqual(cache.get(output_file, src_path=source_file), hashes)

            # 3. Output file deletion triggers cache eviction
            output_file.unlink()
            self.assertFalse(cache.contains(output_file, src_path=source_file))
            self.assertIsNone(cache.get(output_file, src_path=source_file))
            self.assertEqual(len(cache), 0)

            # Re-create output file and re-cache
            output_file.write_text("re-created output")
            cache.set(output_file, hashes, src_path=source_file)
            self.assertTrue(cache.contains(output_file, src_path=source_file))

            # 4. Source file modification (mtime / size change) triggers cache eviction
            time.sleep(0.01)  # Ensure nanosecond timestamp advance
            source_file.write_text("modified source content with different size")
            self.assertFalse(cache.contains(output_file, src_path=source_file))
            self.assertIsNone(cache.get(output_file, src_path=source_file))
            self.assertEqual(len(cache), 0)

    def test_cached_node_restoration(self) -> None:
        """Validates CachedNode restores both own_hash and merkle_hash."""
        c = CachedNode(dst_path=Path("app.conf"),
                       src_path=Path("app.envst.conf"),
                       hashes=NodeHashes(own_hash="h_own", merkle_hash="h_merkle"))
        self.assertEqual(c.dst_path, Path("app.conf"))
        self.assertEqual(c.src_path, Path("app.envst.conf"))
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
        """Validates that candidate files inside pkg_source_dir become UnknownPathNode
        and files outside become IndependentFileNode with absolute paths.
        """
        drift_root = Path("/workspace")
        pkg_source_dir = Path("/workspace/src/test_pkg")

        # File inside package source directory
        inside_file = Path("/workspace/src/test_pkg/scripts/deploy.sh")
        inside_node = make_root_dependency_node(inside_file, pkg_source_dir, drift_root)
        self.assertIsInstance(inside_node, UnknownPathNode)
        assert isinstance(inside_node, UnknownPathNode)
        self.assertEqual(inside_node.src_path, Path("/workspace/src/test_pkg/scripts/deploy.sh"))
        self.assertIsNone(inside_node.dst_path)

        # File outside package source directory (e.g. global config or external asset)
        outside_file = Path("/workspace/config/shared.json")
        outside_node = make_root_dependency_node(outside_file, pkg_source_dir, drift_root)
        self.assertIsInstance(outside_node, IndependentFileNode)
        assert isinstance(outside_node, IndependentFileNode)
        self.assertEqual(outside_node.dst_path, outside_file.resolve())
        self.assertEqual(outside_node.src_path, outside_file.resolve())

    def test_expand_unknown_file_static(self) -> None:
        """Validates expansion of a file without an engine into a StaticFileNode with path translation."""
        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = None

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"FOO": "bar"}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        node = expand_unknown_path(Path("/workspace/src/test_pkg/scripts/run.sh"), ctx)
        self.assertIsInstance(node, StaticFileNode)
        assert isinstance(node, StaticFileNode)
        self.assertEqual(node.dst_path, Path("/workspace/render/test_pkg/scripts/run.sh"))
        self.assertEqual(node.src_path, Path("/workspace/src/test_pkg/scripts/run.sh"))
        self.assertEqual(len(node.depends_on), 1)
        self.assertIsInstance(node.depends_on[0], IndependentFileNode)
        assert isinstance(node.depends_on[0], IndependentFileNode)
        self.assertEqual(node.depends_on[0].dst_path, Path("/workspace/src/test_pkg/scripts/run.sh"))
        self.assertEqual(node.depends_on[0].src_path, Path("/workspace/src/test_pkg/scripts/run.sh"))

    def test_expand_unknown_dir(self) -> None:
        """Validates expansion of a directory into DirectoryNode with translated dst_path and original src_path."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            src_dir = tmp_root / "src/test_pkg/my_dir"
            src_dir.mkdir(parents=True)
            render_dir = tmp_root / "render/test_pkg/my_dir"

            ctx = ExpansionContext(
                drift_root=tmp_root,
                package_name="test_pkg",
                enable_render=True,
                env_node=JsonNode({}),
                render_engines=MagicMock(),
                cache=self.render_cache,
                path_translation={tmp_root / "src/test_pkg": tmp_root / "render/test_pkg"},
            )

            node = expand_unknown_path(src_dir, ctx)
            self.assertIsInstance(node, DirectoryNode)
            assert isinstance(node, DirectoryNode)
            self.assertEqual(node.dst_path, render_dir)
            self.assertEqual(node.src_path, src_dir)

    def test_expand_unknown_file_template(self) -> None:
        """Validates expansion of an engine template into EngineOutputFileNode with path translation."""
        mock_engine = MagicMock()
        mock_engine.name = "envsubst"
        mock_engine.suffix = "envst"
        mock_engine.render_command = "envsubst < %s"
        mock_engine.is_internal = False
        mock_engine.input_file = None
        mock_engine.strip_suffix.return_value = "/workspace/src/test_pkg/config.toml"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = mock_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"PORT": "8080"}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        node = expand_unknown_path(Path("/workspace/src/test_pkg/config.toml.envst"), ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(node.dst_path, Path("/workspace/render/test_pkg/config.toml"))
        self.assertEqual(node.src_path, Path("/workspace/src/test_pkg/config.toml.envst"))
        self.assertIsNone(node.input_node)
        assert isinstance(node.template_node, FileNode)
        self.assertEqual(node.template_node.dst_path, Path("/workspace/src/test_pkg/config.toml.envst"))
        self.assertEqual(node.template_node.src_path, Path("/workspace/src/test_pkg/config.toml.envst"))
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
        mustache_engine.input_file = Path("/workspace/src/test_pkg/data.json.envst")
        mustache_engine.strip_suffix.return_value = "/workspace/src/test_pkg/rendered.html"

        # Engine 2: envsubst compiles data.json.envst -> data.json
        envsubst_engine = MagicMock()
        envsubst_engine.name = "envsubst"
        envsubst_engine.suffix = "envst"
        envsubst_engine.render_command = "envsubst < %s"
        envsubst_engine.is_internal = False
        envsubst_engine.input_file = None
        envsubst_engine.strip_suffix.return_value = "/workspace/src/test_pkg/data.json"

        def find_engine(f_str: str) -> Any:
            if f_str.endswith(".mustache"):
                return mustache_engine
            if f_str.endswith(".envst"):
                return envsubst_engine
            return None

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.side_effect = find_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        root = expand_unknown_path(Path("/workspace/src/test_pkg/template.html.mustache"), ctx)
        self.assertIsInstance(root, EngineOutputFileNode)
        assert isinstance(root, EngineOutputFileNode)
        # Main payload output path
        self.assertEqual(root.dst_path, Path("/workspace/render/test_pkg/rendered.html"))

        # Input node should be recursively expanded and translated to .drift/render/
        self.assertIsNotNone(root.input_node)
        self.assertIsInstance(root.input_node, EngineOutputFileNode)
        assert isinstance(root.input_node, EngineOutputFileNode)
        self.assertEqual(root.input_node.dst_path, Path("/workspace/render/test_pkg/.drift/render/data.json"))

    def test_workspace_engine_input_translation(self) -> None:
        """Validates that workspace engine inputs under config/ are translated to render/.drift/render/."""
        envsubst_engine = MagicMock()
        envsubst_engine.name = "envsubst"
        envsubst_engine.suffix = "envst"
        envsubst_engine.render_command = "envsubst < %s"
        envsubst_engine.is_internal = False
        envsubst_engine.input_file = None
        envsubst_engine.strip_suffix.return_value = "/workspace/config/mustache.json"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = envsubst_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
        )
        input_ctx = ctx.derive_engine_input_context()

        node = expand_unknown_path(Path("/workspace/config/mustache.envst.json"), input_ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(node.dst_path, Path("/workspace/render/.drift/render/mustache.json"))

    def test_render_cache_produces_cached_node(self) -> None:
        """Validates that a path pre-populated in RenderCache emits a CachedNode."""
        target_output = Path("/workspace/render/test_pkg/bin/tool")
        self.render_cache.set_existence_check_enabled(False)
        self.render_cache.register(target_output, own_hash="own123", merkle_hash="merkle456")

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = None

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        node = expand_unknown_path(Path("/workspace/src/test_pkg/bin/tool"), ctx)
        self.assertIsInstance(node, CachedNode)
        assert isinstance(node, CachedNode)
        self.assertEqual(node.dst_path, target_output)
        self.assertEqual(node.own_hash, "own123")
        self.assertEqual(node.merkle_hash, "merkle456")
        mock_registry.find_engine_for_file.assert_called_once_with("/workspace/src/test_pkg/bin/tool")

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
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({"KEY": "val"}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        n1 = expand_unknown_path(Path("/workspace/src/test_pkg/file1.envst"), ctx)
        n2 = expand_unknown_path(Path("/workspace/src/test_pkg/file2.envst"), ctx)

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
        mock_engine.strip_suffix.return_value = "/workspace/src/test_pkg/config.toml"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = mock_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        expand_unknown_path(Path("/workspace/src/test_pkg/config.toml.envst"), ctx)
        with self.assertRaises(RenderCollisionError) as cm:
            expand_unknown_path(Path("/workspace/src/test_pkg/config.toml.custom.envst"), ctx)
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
