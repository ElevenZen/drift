import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
)
from drift.core.exceptions import ConfigError, RenderCollisionError, CyclicDependencyError
from drift.core.file_action import FileAction, FileActionType, format_action_line
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
from drift.render.render_digester import (
    DigestionContext,
    digest_render_dag,
    topological_sort_nodes,
)
from drift.render.render_expansion import (
    ExpansionContext,
    to_node_key,
    expand_unknown_path,
    translate_path,
    make_root_dependency_node,
)
from drift.render.render_lock import RenderLockfile, RenderBucket


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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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
                package_render_dir=tmp_root / "render/test_pkg",
                package_src_dir=tmp_root / "src/test_pkg",
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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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

        # Input node should be recursively expanded and translated to .drift/render/package/
        self.assertIsNotNone(root.input_node)
        self.assertIsInstance(root.input_node, EngineOutputFileNode)
        assert isinstance(root.input_node, EngineOutputFileNode)
        self.assertEqual(root.input_node.dst_path, Path("/workspace/render/test_pkg/.drift/render/package/data.json"))

    def test_workspace_engine_input_translation(self) -> None:
        """Validates that workspace engine inputs under config/ are translated to render/<package>/.drift/render/workspace/."""
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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
        )
        input_ctx = ctx.derive_engine_input_context()

        node = expand_unknown_path(Path("/workspace/config/mustache.envst.json"), input_ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(node.dst_path, Path("/workspace/render/test_pkg/.drift/render/workspace/mustache.json"))

    def test_engine_input_translation_rules_with_custom_package_render_dir_and_src_dir(self) -> None:
        """Validates that custom package_render_dir and package_src_dir in ExpansionContext
        translate workspace and package engine inputs to the sandbox render directory.
        """
        envsubst_engine = MagicMock()
        envsubst_engine.name = "envsubst"
        envsubst_engine.render_command = "envsubst"
        envsubst_engine.suffix = "envst"
        envsubst_engine.is_internal = False
        envsubst_engine.input_file = None
        envsubst_engine.strip_suffix.return_value = "/workspace/config/mustache.json"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.return_value = envsubst_engine

        custom_render_dir = Path("/tmp/sandbox/render/test_pkg")
        custom_src_dir = Path("/custom/source/test_pkg")

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            package_render_dir=custom_render_dir,
            package_src_dir=custom_src_dir,
        )
        input_ctx = ctx.derive_engine_input_context()
        self.assertEqual(input_ctx.package_render_dir, custom_render_dir)
        self.assertEqual(input_ctx.package_src_dir, custom_src_dir)

        rules = ctx.get_engine_input_translation_rules()
        self.assertIn(Path("/workspace/config"), rules)
        self.assertEqual(
            rules[Path("/workspace/config")],
            custom_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME / DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME,
        )
        self.assertIn(custom_src_dir, rules)
        self.assertEqual(
            rules[custom_src_dir],
            custom_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
        )

        node = expand_unknown_path(Path("/workspace/config/mustache.envst.json"), input_ctx)
        self.assertIsInstance(node, EngineOutputFileNode)
        assert isinstance(node, EngineOutputFileNode)
        self.assertEqual(
            node.dst_path,
            custom_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME / DRIFT_INTERNAL_WORKSPACE_INPUT_DIR_NAME / "mustache.json",
        )

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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
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

        with self.assertRaises(CyclicDependencyError) as cm:
            topological_sort_nodes(node_a)
        self.assertIn("Cyclic dependency detected in render graph", str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)

    def test_expand_tree_detects_direct_circular_engine_inputs(self) -> None:
        """Validates that direct 2-engine cycles (A -> B -> A) raise ValueError during tree expansion."""
        # Engine A has input handled by Engine B
        engine_a = MagicMock()
        engine_a.name = "engine_a"
        engine_a.suffix = "suf_a"
        engine_a.input_file = Path("/workspace/src/test_pkg/b.suf_b")
        engine_a.strip_suffix.return_value = "/workspace/src/test_pkg/output.txt"

        # Engine B has input handled by Engine A
        engine_b = MagicMock()
        engine_b.name = "engine_b"
        engine_b.suffix = "suf_b"
        engine_b.input_file = Path("/workspace/src/test_pkg/a.suf_a")
        engine_b.strip_suffix.return_value = "/workspace/src/test_pkg/b.txt"

        def find_engine(f_str: str) -> Any:
            if f_str.endswith(".suf_a"):
                return engine_a
            if f_str.endswith(".suf_b"):
                return engine_b
            return None

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.side_effect = find_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        with self.assertRaises(CyclicDependencyError) as cm:
            expand_unknown_path(Path("/workspace/src/test_pkg/main.suf_a"), ctx)
        self.assertIn("Cyclic dependency detected: render engine inputs form a cycle: engine_a -> engine_b -> engine_a", str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)

    def test_expand_tree_detects_self_referencing_engine_inputs(self) -> None:
        """Validates that self-referencing engine inputs (A -> A) raise ValueError during tree expansion."""
        engine_a = MagicMock()
        engine_a.name = "engine_a"
        engine_a.suffix = "suf_a"
        engine_a.input_file = Path("/workspace/src/test_pkg/self.suf_a")
        engine_a.strip_suffix.return_value = "/workspace/src/test_pkg/output.txt"

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.side_effect = lambda f: engine_a if f.endswith(".suf_a") else None

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        with self.assertRaises(CyclicDependencyError) as cm:
            expand_unknown_path(Path("/workspace/src/test_pkg/main.suf_a"), ctx)
        self.assertIn("Cyclic dependency detected: render engine inputs form a cycle: engine_a -> engine_a", str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)

    def test_expand_tree_detects_transitive_circular_engine_inputs(self) -> None:
        """Validates that multi-level engine cycles (A -> B -> C -> A) raise ValueError during tree expansion."""
        engine_a = MagicMock(name="engine_a", suffix="suf_a", input_file=Path("/workspace/src/test_pkg/b.suf_b"))
        engine_a.name = "engine_a"
        engine_a.strip_suffix.return_value = "/workspace/src/test_pkg/a.txt"

        engine_b = MagicMock(name="engine_b", suffix="suf_b", input_file=Path("/workspace/src/test_pkg/c.suf_c"))
        engine_b.name = "engine_b"
        engine_b.strip_suffix.return_value = "/workspace/src/test_pkg/b.txt"

        engine_c = MagicMock(name="engine_c", suffix="suf_c", input_file=Path("/workspace/src/test_pkg/a.suf_a"))
        engine_c.name = "engine_c"
        engine_c.strip_suffix.return_value = "/workspace/src/test_pkg/c.txt"

        def find_engine(f_str: str) -> Any:
            if f_str.endswith(".suf_a"):
                return engine_a
            if f_str.endswith(".suf_b"):
                return engine_b
            if f_str.endswith(".suf_c"):
                return engine_c
            return None

        mock_registry = MagicMock()
        mock_registry.find_engine_for_file.side_effect = find_engine

        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
            enable_render=True,
            env_node=JsonNode({}),
            render_engines=mock_registry,
            cache=self.render_cache,
            path_translation={Path("/workspace/src/test_pkg"): Path("/workspace/render/test_pkg")},
        )

        with self.assertRaises(CyclicDependencyError) as cm:
            expand_unknown_path(Path("/workspace/src/test_pkg/start.suf_a"), ctx)
        self.assertIn("Cyclic dependency detected: render engine inputs form a cycle: engine_a -> engine_b -> engine_c -> engine_a", str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)

    def test_expand_unknown_path_detects_cyclic_path_expansion(self) -> None:
        """Validates that a path recursively referencing itself during expansion raises CyclicDependencyError."""
        ctx = ExpansionContext(
            drift_root=Path("/workspace"),
            package_name="test_pkg",
            package_render_dir=Path("/workspace/render/test_pkg"),
            package_src_dir=Path("/workspace/src/test_pkg"),
            enable_render=False,
            env_node=JsonNode({}),
            render_engines=MagicMock(),
            cache=self.render_cache,
        )

        test_path = Path("/workspace/loop.txt")
        ctx.visiting_paths.add(to_node_key(test_path))

        with self.assertRaises(CyclicDependencyError) as cm:
            expand_unknown_path(test_path, ctx)
        self.assertIn("Cyclic dependency detected: path '/workspace/loop.txt' forms a cycle during expansion", str(cm.exception))
        self.assertIsInstance(cm.exception, ValueError)


class TestPackageConfigNodeResolutionAndEmptyGuard(unittest.TestCase):
    """Exhaustive tests for PackageConfigNode primary src_path resolution, two-pass digestion, and empty config guards."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.src_dir = self.root / "src"
        self.render_dir = self.root / "render"
        self.cache = RenderCache()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_src_path_resolution_with_static_package_config(self) -> None:
        """Verifies PackageConfigNode picks up static drift_package.toml directly when present."""
        pkg_dir = self.src_dir / "static_pkg"
        pkg_dir.mkdir(parents=True)
        static_cfg = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        static_cfg.write_text("[package]\nname = 'static_pkg'\n", encoding="utf-8")

        dst_path = self.render_dir / "static_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )
        self.assertEqual(node.src_path, static_cfg)

    def test_src_path_resolution_with_templated_dependency(self) -> None:
        """Verifies PackageConfigNode falls back to the first source dependency's src_path when static config is absent."""
        pkg_dir = self.src_dir / "tmpl_pkg"
        pkg_dir.mkdir(parents=True)
        tmpl_file = pkg_dir / "drift_package.envst.toml"
        tmpl_file.write_text("[package]\nname = 'tmpl_pkg'\n", encoding="utf-8")

        dst_path = self.render_dir / "tmpl_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        dep_node = IndependentFileNode(tmpl_file)
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )
        self.assertEqual(node.src_path, tmpl_file)

    def test_src_path_resolution_fallback_when_sources_empty(self) -> None:
        """Verifies PackageConfigNode gracefully handles absence of both static file and sources without crashing."""
        pkg_dir = self.src_dir / "empty_pkg"
        pkg_dir.mkdir(parents=True)
        dst_path = self.render_dir / "empty_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )
        self.assertIsNone(node.src_path)

    def test_repeated_digestion_templated_config_skip_identical_action(self) -> None:
        """Verifies two-pass digestion of a templated package config plans SKIP_IDENTICAL without ValueError."""
        pkg_name = "tmpl_pkg"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True)
        tmpl_file = pkg_dir / "drift_package.envst.toml"
        tmpl_file.write_text("[package]\nname = 'tmpl_pkg'\n", encoding="utf-8")

        pkg_render_dir = self.render_dir / pkg_name
        pkg_render_dir.mkdir(parents=True)
        dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        # Create intermediate rendered config produced by Phase 0/1 expansion
        intermediate_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / "render" / "package"
        intermediate_dir.mkdir(parents=True)
        intermediate_cfg = intermediate_dir / PACKAGE_CONFIG_FILE_NAME
        intermediate_cfg.write_text("[package]\nname = 'tmpl_pkg'\n", encoding="utf-8")

        dep_node = StaticFileNode(dst_path=intermediate_cfg, src_path=tmpl_file)

        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )
        self.assertEqual(node.src_path, tmpl_file)

        ctx1 = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )

        # Pass 1: initial digestion -> WRITE_CONFIG
        digest_render_dag(node, ctx1)
        self.assertTrue(dst_path.is_file())
        config_action1 = next(a for a in ctx1.result.actions if a.dst_path == dst_path)
        self.assertEqual(config_action1.action_type, FileActionType.WRITE_CONFIG)

        # Pass 2: repeat digestion -> SKIP_IDENTICAL without triggering ValueError on src_path
        ctx2 = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )
        digest_render_dag(node, ctx2)
        config_action2 = next(a for a in ctx2.result.actions if a.dst_path == dst_path)
        self.assertEqual(config_action2.action_type, FileActionType.SKIP_IDENTICAL)
        self.assertEqual(config_action2.src_path, tmpl_file)
        self.assertEqual(config_action2.dst_path, dst_path)

        # Verify formatting includes arrow from template to dst
        line = format_action_line(config_action2, drift_root=self.root)
        self.assertIn("⏭️ [SKIP_IDENTICAL]", line)
        self.assertIn(f"src/{pkg_name}/drift_package.envst.toml -> render/{pkg_name}/.drift/drift_package.toml", line)

        # Verify cache recorded source stat fingerprint for template
        cached_entry = self.cache.get(dst_path, src_path=tmpl_file)
        self.assertIsNotNone(cached_entry)

    def test_digestion_fallback_to_target_path_when_src_path_none(self) -> None:
        """Verifies that if src_path is None on an existing identical target, digestion falls back safely."""
        pkg_name = "synthetic_pkg"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True)
        pkg_render_dir = self.render_dir / pkg_name
        pkg_render_dir.mkdir(parents=True)
        dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        # Intermediate file has valid content
        intermediate_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / "render"
        intermediate_dir.mkdir(parents=True)
        intermediate_cfg = intermediate_dir / PACKAGE_CONFIG_FILE_NAME
        intermediate_cfg.write_text("[package]\nname = 'synthetic_pkg'\n", encoding="utf-8")

        # Create a PathNode with src_path=None
        class SyntheticPathNode(PathNode[Path, Optional[Path]]):
            def __init__(self, dst: Path):
                super().__init__(dst_path=dst, src_path=None)

        dep_node = SyntheticPathNode(intermediate_cfg)
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )
        self.assertIsNone(node.src_path)

        # Pre-seed destination with exact compiled output so already_matched is True
        from drift.utils.toml_utils import dump_toml
        from drift.config.package_loader import resolve_and_interpolate_package_config
        from drift.utils.toml_utils import parse_toml
        stitched, _ = resolve_and_interpolate_package_config(
            parse_toml(intermediate_cfg.read_text(encoding="utf-8")),
            package_name=pkg_name,
        )
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_text(dump_toml(stitched), encoding="utf-8")

        ctx = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )
        # Should NOT raise ValueError: FileActionType.SKIP_IDENTICAL must have src_path set
        digest_render_dag(node, ctx)
        action = next(a for a in ctx.result.actions if a.dst_path == dst_path)
        self.assertEqual(action.action_type, FileActionType.SKIP_IDENTICAL)
        self.assertEqual(action.src_path, dst_path)
        # Format line with src == dst formats as single path without redundant arrow
        line = format_action_line(action, drift_root=self.root)
        self.assertIn("⏭️ [SKIP_IDENTICAL]", line)
        self.assertNotIn("->", line)

    def test_empty_config_raises_config_error(self) -> None:
        """Verifies that an empty package configuration without hooks raises ConfigError."""
        pkg_name = "empty_cfg_pkg"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True)
        pkg_render_dir = self.render_dir / pkg_name
        pkg_render_dir.mkdir(parents=True)
        dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        # Empty intermediate config file (0 bytes)
        intermediate_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / "render"
        intermediate_dir.mkdir(parents=True)
        intermediate_cfg = intermediate_dir / PACKAGE_CONFIG_FILE_NAME
        intermediate_cfg.write_text("# only comments\n\n", encoding="utf-8")

        dep_node = StaticFileNode(dst_path=intermediate_cfg, src_path=intermediate_cfg)
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )

        ctx = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )

        with self.assertRaises(ConfigError) as cm:
            digest_render_dag(node, ctx)
        self.assertIn(f"Package configuration for '{pkg_name}' is empty", str(cm.exception))

    def test_empty_config_populated_by_dynamic_package_hook_succeeds(self) -> None:
        """Verifies that an initially empty config populated by a dynamic package hook passes validation."""
        pkg_name = "hook_cfg_pkg"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True)
        pkg_render_dir = self.render_dir / pkg_name
        pkg_render_dir.mkdir(parents=True)
        dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        # Empty intermediate config file
        intermediate_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / "render"
        intermediate_dir.mkdir(parents=True)
        intermediate_cfg = intermediate_dir / PACKAGE_CONFIG_FILE_NAME
        intermediate_cfg.write_text("", encoding="utf-8")

        # Dynamic Python package hook that injects [package] table
        hook_file = pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text(
            "def configure_package(context):\n"
            "    return {'package': {'name': 'hook_cfg_pkg'}}\n",
            encoding="utf-8",
        )

        dep_node = StaticFileNode(dst_path=intermediate_cfg, src_path=intermediate_cfg)
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )

        ctx = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )

        # Digestion must succeed because hook provided the configuration
        digest_render_dag(node, ctx)
        self.assertTrue(dst_path.is_file())
        self.assertIn("hook_cfg_pkg", dst_path.read_text(encoding="utf-8"))

    def test_empty_config_hook_returns_empty_raises_config_error(self) -> None:
        """Verifies that if a dynamic package hook runs but still returns an empty dict, ConfigError is raised."""
        pkg_name = "empty_hook_pkg"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True)
        pkg_render_dir = self.render_dir / pkg_name
        pkg_render_dir.mkdir(parents=True)
        dst_path = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME

        intermediate_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / "render"
        intermediate_dir.mkdir(parents=True)
        intermediate_cfg = intermediate_dir / PACKAGE_CONFIG_FILE_NAME
        intermediate_cfg.write_text("", encoding="utf-8")

        hook_file = pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text(
            "def configure_package(context):\n"
            "    return {}\n",
            encoding="utf-8",
        )

        dep_node = StaticFileNode(dst_path=intermediate_cfg, src_path=intermediate_cfg)
        node = PackageConfigNode(
            dst_path=dst_path,
            sources=[dep_node],
            env_node=JsonNode({}),
            package_dir=pkg_dir,
        )

        ctx = DigestionContext(
            drift_root=self.root,
            package_name=pkg_name,
            package_render_dir=pkg_render_dir,
            lockfile=RenderLockfile(),
            bucket=RenderBucket.CONFIG,
            cache=self.cache,
            silent=True,
        )

        with self.assertRaises(ConfigError) as cm:
            digest_render_dag(node, ctx)
        self.assertIn(f"Package configuration for '{pkg_name}' is empty", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

