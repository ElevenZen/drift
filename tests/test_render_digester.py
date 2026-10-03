"""Targeted unit tests for graph digestion engine and scoped pruning."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

from drift.config.render_engine_config import RenderEngineConfig
from drift.core.constants import (
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    RENDER_LOCK_FILE_NAME,
)
from drift.render.render_cache import NodeHashes, StaticRenderCache, static_render_cache
from drift.render.render_lock import RenderLockfile, RenderBucket
from drift.render.render_dag import (
    FileNode,
    IndependentFileNode,
    StaticFileNode,
    DirectoryNode,
    EngineOutputFileNode,
    PackageConfigNode,
    PackageHooksNode,
    PackagePayloadNode,
    JsonNode,
)
from drift.render.render_digester import (
    is_drift_internal_path,
    DigestionResult,
    DigestionContext,
    check_and_apply_cache,
    prune_obsolete_config_files,
    prune_obsolete_hooks,
    prune_obsolete_payload_files,
    digest_render_dag,
)


class TestInspectionPredicates(unittest.TestCase):
    """Unit tests for inspection predicates."""

    def test_is_drift_internal_path(self) -> None:
        pkg_render = Path("render/my_pkg")

        # Internal files and folders
        self.assertTrue(is_drift_internal_path(Path("render/my_pkg/.drift"), pkg_render))
        self.assertTrue(is_drift_internal_path(Path("render/my_pkg/.drift/drift_package.toml"), pkg_render))
        self.assertTrue(is_drift_internal_path(Path("render/my_pkg/.drift/hooks/pre_install.sh"), pkg_render))
        self.assertTrue(is_drift_internal_path(Path("render/my_pkg/.drift/render/template.json"), pkg_render))

        # Payload files outside .drift/
        self.assertFalse(is_drift_internal_path(Path("render/my_pkg/app.conf"), pkg_render))
        self.assertFalse(is_drift_internal_path(Path("render/my_pkg/bin/tool"), pkg_render))
        self.assertFalse(is_drift_internal_path(Path("render/my_pkg/.config/app/settings"), pkg_render))

        # Completely unrelated package paths
        self.assertFalse(is_drift_internal_path(Path("render/other_pkg/.drift/drift_package.toml"), pkg_render))


class TestScopedPruning(unittest.TestCase):
    """Unit tests for scoped pruning subsystems."""

    def test_prune_obsolete_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            pkg_render = Path("render/pkg_a")
            internal_dir = drift_root / pkg_render / DRIFT_INTERNAL_DIR_NAME
            internal_dir.mkdir(parents=True)

            main_cfg = pkg_render / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
            local_cfg = pkg_render / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME

            (drift_root / main_cfg).write_text("name = 'pkg_a'")
            (drift_root / local_cfg).write_text("enabled = true")

            # Active set only includes main_cfg; local_cfg is obsolete
            active_paths = [main_cfg]

            # 1. Dry run
            dry_pruned = prune_obsolete_config_files(drift_root, pkg_render, active_paths, dry_run=True)
            self.assertEqual(dry_pruned, [local_cfg])
            self.assertTrue((drift_root / local_cfg).is_file())

            # 2. Live execution
            pruned = prune_obsolete_config_files(drift_root, pkg_render, active_paths, dry_run=False)
            self.assertEqual(pruned, [local_cfg])
            self.assertFalse((drift_root / local_cfg).exists())
            self.assertTrue((drift_root / main_cfg).is_file())

    def test_prune_obsolete_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            pkg_render = Path("render/pkg_b")
            hooks_dir = pkg_render / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
            disk_hooks = drift_root / hooks_dir

            active_hook = hooks_dir / "pre_sync.sh"
            obsolete_hook = hooks_dir / "sub" / "old_hook.sh"
            empty_folder = hooks_dir / "empty_dir"

            (disk_hooks / "sub").mkdir(parents=True)
            (disk_hooks / "empty_dir").mkdir(parents=True)
            (drift_root / active_hook).write_text("#!/bin/sh\necho active")
            (drift_root / obsolete_hook).write_text("#!/bin/sh\necho old")

            # Active paths only contain active_hook
            active_paths = [active_hook]

            # Dry run: files intact
            dry_pruned = prune_obsolete_hooks(drift_root, hooks_dir, active_paths, dry_run=True)
            self.assertIn(obsolete_hook, dry_pruned)
            self.assertTrue((drift_root / obsolete_hook).is_file())

            # Live run: obsolete hook and empty directories pruned
            pruned = prune_obsolete_hooks(drift_root, hooks_dir, active_paths, dry_run=False)
            self.assertIn(obsolete_hook, pruned)
            self.assertIn(empty_folder, pruned)
            self.assertFalse((drift_root / obsolete_hook).exists())
            self.assertFalse((disk_hooks / "sub").exists())
            self.assertFalse((disk_hooks / "empty_dir").exists())
            self.assertTrue((drift_root / active_hook).is_file())

    def test_prune_obsolete_payload_files_and_drift_shielding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            pkg_render = Path("render/pkg_c")
            disk_pkg = drift_root / pkg_render

            # Create internal .drift/ files that must NEVER be pruned
            internal_dir = disk_pkg / DRIFT_INTERNAL_DIR_NAME
            internal_dir.mkdir(parents=True)
            lock_path = internal_dir / RENDER_LOCK_FILE_NAME
            lock_path.write_text("{}")
            cfg_path = internal_dir / PACKAGE_CONFIG_FILE_NAME
            cfg_path.write_text("name = 'pkg_c'")

            # Create payload files
            active_file = pkg_render / "bin" / "active.sh"
            obsolete_file = pkg_render / "nested" / "obsolete.txt"
            empty_payload_dir = pkg_render / "empty_dir"

            (disk_pkg / "bin").mkdir(parents=True)
            (disk_pkg / "nested").mkdir(parents=True)
            empty_payload_dir_disk = drift_root / empty_payload_dir
            empty_payload_dir_disk.mkdir(parents=True)

            (drift_root / active_file).write_text("active")
            (drift_root / obsolete_file).write_text("obsolete")

            active_paths = [active_file]

            # Prune payload files
            pruned = prune_obsolete_payload_files(drift_root, pkg_render, active_paths, dry_run=False)
            self.assertIn(obsolete_file, pruned)
            self.assertIn(empty_payload_dir, pruned)

            # Obsolete file and its empty parent dir deleted
            self.assertFalse((drift_root / obsolete_file).exists())
            self.assertFalse((disk_pkg / "nested").exists())
            self.assertFalse(empty_payload_dir_disk.exists())

            # Active file preserved
            self.assertTrue((drift_root / active_file).is_file())

            # .drift/ hierarchy strictly shielded!
            self.assertTrue(lock_path.is_file())
            self.assertTrue(cfg_path.is_file())
            self.assertTrue(internal_dir.is_dir())


class TestDigestionModelsAndCache(unittest.TestCase):
    """Unit tests for DigestionResult, DigestionContext, and check_and_apply_cache."""

    def setUp(self) -> None:
        static_render_cache.clear()

    def tearDown(self) -> None:
        static_render_cache.clear()

    def test_digestion_result_properties(self) -> None:
        res = DigestionResult(
            rendered_paths=[Path("a"), Path("b")],
            skipped_paths=[Path("c")],
            pruned_paths=[Path("d"), Path("e"), Path("f")],
        )
        self.assertEqual(res.active_paths, [Path("a"), Path("b"), Path("c")])
        self.assertEqual(res.rendered_count, 2)
        self.assertEqual(res.skipped_count, 1)
        self.assertEqual(res.pruned_count, 3)

    def test_check_and_apply_cache_hit_and_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            p = Path("render/pkg/foo.txt")
            (drift_root / p).parent.mkdir(parents=True)
            (drift_root / p).write_text("content")

            node = StaticFileNode(output_path=p, src_path=Path("src/pkg/foo.txt"))
            indep = node.depends_on[0]
            self.assertIsInstance(indep, FileNode)
            assert isinstance(indep, FileNode)
            indep_path = drift_root / indep.file_path
            indep_path.parent.mkdir(parents=True, exist_ok=True)
            indep_path.write_text("source content")
            from drift.render.render_hasher import compute_merkle_node_hash
            compute_merkle_node_hash(indep, drift_root)

            lock = RenderLockfile()
            ctx = DigestionContext(
                drift_root=drift_root,
                package_name="pkg",
                package_render_dir=Path("render/pkg"),
                lockfile=lock,
                bucket=RenderBucket.PAYLOAD,
                cache=static_render_cache,
            )

            # 1. Miss when not in lockfile
            self.assertFalse(check_and_apply_cache(node, p, ctx))
            self.assertEqual(ctx.result.skipped_count, 0)

            # Compute actual hash and populate lockfile
            from drift.render.render_hasher import hash_file_disk, hash_text
            own_h = hash_file_disk(p, drift_root)
            self.assertIsNotNone(own_h)
            expected_m = hash_text(f"StaticFileNode:{own_h}:{indep.merkle_hash}")
            lock.update_payload_hashes([expected_m])

            # 2. Hit when in lockfile
            hit = check_and_apply_cache(node, p, ctx)
            self.assertTrue(hit)
            self.assertEqual(ctx.result.skipped_count, 1)
            self.assertEqual(ctx.result.skipped_paths, [p])
            self.assertIn(expected_m, ctx.active_hashes)
            self.assertEqual(node.merkle_hash, expected_m)
            self.assertTrue(static_render_cache.contains(p))

            # 3. Force flag bypasses cache
            ctx_force = DigestionContext(
                drift_root=drift_root,
                package_name="pkg",
                package_render_dir=Path("render/pkg"),
                lockfile=lock,
                bucket=RenderBucket.PAYLOAD,
                force=True,
            )
            self.assertFalse(check_and_apply_cache(node, p, ctx_force))


class TestDigestRenderDAG(unittest.TestCase):
    """End-to-end integration unit tests for digest_render_dag."""

    def setUp(self) -> None:
        static_render_cache.clear()

    def tearDown(self) -> None:
        static_render_cache.clear()

    def _setup_workspace(self, root: Path, pkg_name: str = "demo_pkg") -> Dict[str, Path]:
        src_dir = root / "src" / pkg_name
        src_dir.mkdir(parents=True)
        render_pkg_dir = root / "render" / pkg_name
        render_pkg_dir.mkdir(parents=True)

        # Source static file
        static_src = src_dir / "static.txt"
        static_src.write_text("static-content-v1")

        # Source template
        tmpl_src = src_dir / "app.conf.tmpl"
        tmpl_src.write_text("KEY=VALUE")

        # Keep placeholder
        keep_src = src_dir / "empty_dir" / ".drift_keep"
        keep_src.parent.mkdir(parents=True)
        keep_src.write_text("")

        return {
            "src_dir": src_dir,
            "render_pkg_dir": render_pkg_dir,
            "static_src": static_src,
            "tmpl_src": tmpl_src,
            "keep_src": keep_src,
        }

    def test_fresh_render_generates_outputs_and_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)

            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            # Construct DAG
            static_out = pkg_render_dir / "static.txt"
            static_node = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")

            engine_cfg = RenderEngineConfig(
                name="test_engine",
                suffix=".tmpl",
                render_command="internal",
            )
            tmpl_out = pkg_render_dir / "app.conf"
            tmpl_node = EngineOutputFileNode(
                output_path=tmpl_out,
                input_node=None,
                template_node=IndependentFileNode(Path("src") / pkg_name / "app.conf.tmpl"),
                env_node=JsonNode({}),
                engine_node=JsonNode({"name": "test_engine"}),
                engine_config=engine_cfg,
            )

            dir_out = pkg_render_dir / "empty_dir"
            dir_node = DirectoryNode(dir_path=dir_out)

            payload_root = PackagePayloadNode(pkg_name, [static_node, tmpl_node, dir_node])

            lockfile = RenderLockfile()
            ctx = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=lockfile,
                bucket=RenderBucket.PAYLOAD,
                cache=static_render_cache,
            )

            # Execute digestion
            result = digest_render_dag(payload_root, ctx)

            # Verifications
            self.assertEqual(result.rendered_count, 3)
            self.assertEqual(result.skipped_count, 0)
            self.assertEqual(result.pruned_count, 0)

            # Files created on disk
            self.assertTrue((drift_root / static_out).is_file())
            self.assertEqual((drift_root / static_out).read_text(), "static-content-v1")

            self.assertTrue((drift_root / tmpl_out).is_file())
            self.assertEqual((drift_root / tmpl_out).read_text(), "KEY=VALUE")

            self.assertTrue((drift_root / dir_out).is_dir())

            # Lockfile created on disk
            lock_disk = drift_root / pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME
            self.assertTrue(lock_disk.is_file())

            reloaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            self.assertEqual(len(reloaded_lock.payload_hashes), 4)  # 3 children + 1 payload root container

    def test_incremental_zero_diff_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            static_out = pkg_render_dir / "static.txt"
            static_node = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            payload_root = PackagePayloadNode(pkg_name, [static_node])

            # 1. First run
            lock1 = RenderLockfile()
            ctx1 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=lock1,
                bucket=RenderBucket.PAYLOAD,
            )
            digest_render_dag(payload_root, ctx1)
            mtime_initial = (drift_root / static_out).stat().st_mtime_ns

            # 2. Second run with unchanged files
            loaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            static_node2 = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            payload_root2 = PackagePayloadNode(pkg_name, [static_node2])

            ctx2 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=loaded_lock,
                bucket=RenderBucket.PAYLOAD,
            )
            result2 = digest_render_dag(payload_root2, ctx2)

            self.assertEqual(result2.rendered_count, 0)
            self.assertEqual(result2.skipped_count, 1)
            self.assertEqual(result2.skipped_paths, [static_out])

            # Verify file was NOT touched on disk
            mtime_after = (drift_root / static_out).stat().st_mtime_ns
            self.assertEqual(mtime_initial, mtime_after)

    def test_partial_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            static_out = pkg_render_dir / "static.txt"
            static_node = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")

            engine_cfg = RenderEngineConfig(
                name="test_engine",
                suffix=".tmpl",
                render_command="internal",
            )
            tmpl_out = pkg_render_dir / "app.conf"
            tmpl_node = EngineOutputFileNode(
                output_path=tmpl_out,
                input_node=None,
                template_node=IndependentFileNode(Path("src") / pkg_name / "app.conf.tmpl"),
                env_node=JsonNode({}),
                engine_node=JsonNode({"name": "test_engine"}),
                engine_config=engine_cfg,
            )

            # Run 1
            payload_root = PackagePayloadNode(pkg_name, [static_node, tmpl_node])
            ctx1 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=RenderLockfile(),
                bucket=RenderBucket.PAYLOAD,
            )
            digest_render_dag(payload_root, ctx1)

            # Modify template only
            ws["tmpl_src"].write_text("KEY=MODIFIED_VALUE")

            # Run 2
            loaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            static_node2 = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            tmpl_node2 = EngineOutputFileNode(
                output_path=tmpl_out,
                input_node=None,
                template_node=IndependentFileNode(Path("src") / pkg_name / "app.conf.tmpl"),
                env_node=JsonNode({}),
                engine_node=JsonNode({"name": "test_engine"}),
                engine_config=engine_cfg,
            )
            payload_root2 = PackagePayloadNode(pkg_name, [static_node2, tmpl_node2])

            ctx2 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=loaded_lock,
                bucket=RenderBucket.PAYLOAD,
            )
            result2 = digest_render_dag(payload_root2, ctx2)

            self.assertEqual(result2.rendered_count, 1)
            self.assertEqual(result2.skipped_count, 1)
            self.assertEqual(result2.rendered_paths, [tmpl_out])
            self.assertEqual(result2.skipped_paths, [static_out])
            self.assertEqual((drift_root / tmpl_out).read_text(), "KEY=MODIFIED_VALUE")

    def test_missing_file_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            static_out = pkg_render_dir / "static.txt"
            static_node = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            payload_root = PackagePayloadNode(pkg_name, [static_node])

            # Run 1
            ctx1 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=RenderLockfile(),
                bucket=RenderBucket.PAYLOAD,
            )
            digest_render_dag(payload_root, ctx1)
            self.assertTrue((drift_root / static_out).is_file())

            # Delete file on disk (corrupted or deleted externally)
            (drift_root / static_out).unlink()
            self.assertFalse((drift_root / static_out).exists())

            # Run 2: Lockfile still contains hash, but disk file is missing
            loaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            static_node2 = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            payload_root2 = PackagePayloadNode(pkg_name, [static_node2])

            ctx2 = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=loaded_lock,
                bucket=RenderBucket.PAYLOAD,
            )
            result2 = digest_render_dag(payload_root2, ctx2)

            # Recovered by re-rendering
            self.assertEqual(result2.rendered_count, 1)
            self.assertEqual(result2.skipped_count, 0)
            self.assertTrue((drift_root / static_out).is_file())

    def test_dry_run_leaves_disk_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            static_out = pkg_render_dir / "static.txt"
            static_node = StaticFileNode(output_path=static_out, src_path=Path("src") / pkg_name / "static.txt")
            payload_root = PackagePayloadNode(pkg_name, [static_node])

            ctx = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=RenderLockfile(),
                bucket=RenderBucket.PAYLOAD,
                dry_run=True,
            )
            result = digest_render_dag(payload_root, ctx)

            # Recorded in result as rendered
            self.assertEqual(result.rendered_count, 1)
            # But file and lockfile NOT created on disk
            self.assertFalse((drift_root / static_out).exists())
    def test_package_config_node_digestion(self) -> None:
        """Validates PackageConfigNode digestion, pruning of obsolete config, and lockfile bucket update."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name

            # Pre-create rendered drift_package.toml and obsolete drift_package.local.toml
            internal_dir = drift_root / pkg_render_dir / DRIFT_INTERNAL_DIR_NAME
            internal_dir.mkdir(parents=True)
            cfg_out = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
            (drift_root / cfg_out).write_text("name = 'demo_pkg'")
            obsolete_local = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_LOCAL_FILE_NAME
            (drift_root / obsolete_local).write_text("debug = true")

            # Source file
            src_cfg = Path("src") / pkg_name / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
            (drift_root / src_cfg).parent.mkdir(parents=True)
            (drift_root / src_cfg).write_text("name = 'demo_pkg'")

            cfg_node = PackageConfigNode(
                output_path=cfg_out,
                sources=[IndependentFileNode(src_cfg)],
                env_node=JsonNode({"USER": "tester"}),
            )

            # Record cfg_out in rendered_paths so it is treated as active
            ctx = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=RenderLockfile(),
                bucket=RenderBucket.CONFIG,
            )
            ctx.result.rendered_paths.append(cfg_out)

            digest_render_dag(cfg_node, ctx)

            # Obsolete local config was pruned
            self.assertFalse((drift_root / obsolete_local).exists())
            self.assertIn(obsolete_local, ctx.result.pruned_paths)

            # Lockfile saved with config_hashes
            reloaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            self.assertEqual(len(reloaded_lock.config_hashes), 1)
            self.assertIn(cfg_node.merkle_hash, reloaded_lock.config_hashes)

    def test_package_hooks_node_digestion(self) -> None:
        """Validates PackageHooksNode digestion, pruning of obsolete hooks, and lockfile bucket update."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            drift_root = Path(tmp_dir)
            ws = self._setup_workspace(drift_root)
            pkg_name = "demo_pkg"
            pkg_render_dir = Path("render") / pkg_name
            hooks_dir = pkg_render_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME

            # Source hook
            src_hook = Path("src") / pkg_name / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "pre_sync.sh"
            (drift_root / src_hook).parent.mkdir(parents=True)
            (drift_root / src_hook).write_text("#!/bin/sh\necho sync")

            # Destination hook
            hook_out = hooks_dir / "pre_sync.sh"
            hook_node = StaticFileNode(output_path=hook_out, src_path=src_hook)

            # Create an obsolete hook on disk that should be pruned
            disk_hooks = drift_root / hooks_dir
            disk_hooks.mkdir(parents=True)
            obsolete_hook = hooks_dir / "old_hook.sh"
            (drift_root / obsolete_hook).write_text("#!/bin/sh\necho old")

            hooks_root = PackageHooksNode(pkg_name, [hook_node])

            ctx = DigestionContext(
                drift_root=drift_root,
                package_name=pkg_name,
                package_render_dir=pkg_render_dir,
                lockfile=RenderLockfile(),
                bucket=RenderBucket.HOOKS,
            )

            digest_render_dag(hooks_root, ctx)

            # Active hook generated, obsolete hook pruned
            self.assertTrue((drift_root / hook_out).is_file())
            self.assertFalse((drift_root / obsolete_hook).exists())
            self.assertIn(obsolete_hook, ctx.result.pruned_paths)

            # Lockfile saved with hook_hashes
            reloaded_lock = RenderLockfile.load_from_dir(drift_root / pkg_render_dir)
            self.assertIn(hook_node.merkle_hash, reloaded_lock.hook_hashes)
            self.assertIn(hooks_root.merkle_hash, reloaded_lock.hook_hashes)


if __name__ == "__main__":
    unittest.main()
