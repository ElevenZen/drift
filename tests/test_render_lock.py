"""Targeted unit tests for Merkle hashing and lockfile persistence."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from drift.core.constants import DRIFT_INTERNAL_DIR_NAME, RENDER_LOCK_FILE_NAME
from drift.render.render_dag import (
    TextNode,
    JsonNode,
    IndependentFileNode,
    StaticFileNode,
    DirectoryNode,
    PackagePayloadNode,
    PackageHooksNode,
    CachedNode,
)
from drift.render.render_hasher import (
    hash_bytes,
    hash_text,
    hash_file_disk,
    hash_directory_disk,
    compute_node_own_hash,
    compute_merkle_node_hash,
    format_hash_log,
)
from drift.render.render_lock import RenderLockfile


class TestRenderHasher(unittest.TestCase):
    """Unit tests for low-level disk hashing and Merkle DAG hashing."""

    def test_format_hash_log(self) -> None:
        """Validates that format_hash_log cleanly truncates hash strings to 10 chars."""
        full_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(format_hash_log(full_hash), "e3b0c44298")
        self.assertEqual(len(format_hash_log(full_hash)), 10)
        self.assertEqual(format_hash_log(full_hash, length=8), "e3b0c442")

        # None and empty
        self.assertEqual(format_hash_log(None), "none")
        self.assertEqual(format_hash_log(""), "none")

        # Colon-separated dependency hash string
        dep_str = f"{full_hash}:{full_hash}"
        self.assertEqual(format_hash_log(dep_str), "e3b0c44298:e3b0c44298")

        # Collections
        self.assertEqual(format_hash_log([full_hash, full_hash]), "[e3b0c44298, e3b0c44298]")

    def test_hash_bytes_and_text(self) -> None:
        """Validates basic SHA-256 computation over bytes and strings."""
        data = b"hello drift incremental render"
        text = "hello drift incremental render"
        self.assertEqual(hash_bytes(data), hash_text(text))
        self.assertEqual(len(hash_text(text)), 64)

    def test_hash_file_disk_missing(self) -> None:
        """Validates that nonexistent files or directories return None."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            missing = tmp_path / "does_not_exist.txt"
            self.assertIsNone(hash_file_disk(missing))

            # Directory passed to hash_file_disk should return None
            sub_dir = tmp_path / "subdir"
            sub_dir.mkdir()
            self.assertIsNone(hash_file_disk(sub_dir))

    def test_hash_file_disk_existing_and_permissions(self) -> None:
        """Validates that file content, path relativity, and POSIX permissions affect file hash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            file_path = tmp_path / "sample.txt"
            file_path.write_bytes(b"sample content")

            # Basic hash
            h1 = hash_file_disk(file_path)
            self.assertIsNotNone(h1)
            assert h1 is not None

            # Same file and mode gives identical hash
            h2 = hash_file_disk(file_path)
            self.assertEqual(h1, h2)

            # Changing content changes hash
            file_path.write_bytes(b"modified content")
            h3 = hash_file_disk(file_path)
            self.assertNotEqual(h1, h3)

            # Changing permissions (e.g. chmod 755 vs 644) changes hash
            file_path.chmod(0o644)
            h_644 = hash_file_disk(file_path)
            file_path.chmod(0o755)
            h_755 = hash_file_disk(file_path)
            self.assertNotEqual(h_644, h_755)

    def test_hash_directory_disk(self) -> None:
        """Validates directory presence and hashing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            dir_path = tmp_path / "test_dir"

            # Missing directory returns None
            self.assertIsNone(hash_directory_disk(dir_path))

            # Existing directory returns hash
            dir_path.mkdir()
            h_dir = hash_directory_disk(dir_path)
            self.assertIsNotNone(h_dir)

            # File passed to directory hasher returns None
            f_path = tmp_path / "file.txt"
            f_path.write_text("not a dir")
            self.assertIsNone(hash_directory_disk(f_path))

    def test_compute_node_own_hash(self) -> None:
        """Validates own_hash computation across different node specializations."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # 1. TextNode and JsonNode already have own_hash populated
            t_node = TextNode("custom text")
            self.assertEqual(compute_node_own_hash(t_node), t_node.own_hash)

            j_node = JsonNode({"port": 8080})
            self.assertEqual(compute_node_own_hash(j_node), j_node.own_hash)

            # 2. IndependentFileNode hashes source file on disk
            src_file = tmp_path / "source.txt"
            indep = IndependentFileNode(src_file)
            # Before creation -> None
            self.assertIsNone(compute_node_own_hash(indep))
            self.assertIsNone(indep.own_hash)

            # After creation -> valid hash
            src_file.write_text("source data")
            h_src = compute_node_own_hash(indep)
            self.assertIsNotNone(h_src)
            self.assertEqual(h_src, hash_file_disk(src_file))

            # 3. StaticFileNode (FileNode output target)
            dest_file = tmp_path / "rendered.txt"
            static_node = StaticFileNode(dest_file, src_file)
            # Rendered target does not exist yet -> None
            self.assertIsNone(compute_node_own_hash(static_node))

            # Once rendered target exists -> computed
            dest_file.write_text("rendered data")
            h_dest = compute_node_own_hash(static_node)
            self.assertIsNotNone(h_dest)
            self.assertEqual(h_dest, hash_file_disk(dest_file))

            # 4. DirectoryNode
            empty_dir = tmp_path / "empty_dir"
            dir_node = DirectoryNode(empty_dir)
            self.assertIsNone(compute_node_own_hash(dir_node))
            empty_dir.mkdir()
            h_dir = compute_node_own_hash(dir_node)
            self.assertIsNotNone(h_dir)
            self.assertEqual(h_dir, hash_directory_disk(empty_dir))

            # 5. PackagePayloadNode and PackageHooksNode
            pkg_node = PackagePayloadNode("pkg_foo", [])
            h_pkg = compute_node_own_hash(pkg_node)
            self.assertEqual(h_pkg, hash_text("PackagePayloadNode:pkg_foo"))

            hooks_node = PackageHooksNode("pkg_foo", [])
            h_hooks = compute_node_own_hash(hooks_node)
            self.assertEqual(h_hooks, hash_text("PackageHooksNode:pkg_foo"))

    def test_compute_merkle_node_hash_leaf_and_composite(self) -> None:
        """Validates Merkle hash computation from leaf prerequisites to parent containers."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            src_path = tmp_path / "source.txt"
            src_path.write_text("hello source")
            dest_path = tmp_path / "dest.txt"
            dest_path.write_text("hello dest")

            indep_node = IndependentFileNode(src_path)
            static_node = StaticFileNode(dest_path, src_path)
            static_node.depends_on = [indep_node]

            # If dependency's merkle_hash is not yet computed -> returns None
            self.assertIsNone(compute_merkle_node_hash(static_node))

            # Compute dependency's merkle hash first (topological post-order)
            m_indep = compute_merkle_node_hash(indep_node)
            self.assertIsNotNone(m_indep)
            self.assertEqual(indep_node.merkle_hash, m_indep)

            # Now static_node can compute its merkle hash
            m_static = compute_merkle_node_hash(static_node)
            self.assertIsNotNone(m_static)
            self.assertEqual(static_node.merkle_hash, m_static)

            # Verify deterministic Merkle formula
            expected = hash_text(f"StaticFileNode:{static_node.own_hash}:{indep_node.merkle_hash}")
            self.assertEqual(m_static, expected)

    def test_merkle_propagation_on_change(self) -> None:
        """Validates that modifying a leaf dependency cascades into a new parent Merkle hash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            f_path = tmp_path / "file.txt"
            f_path.write_text("v1")

            indep = IndependentFileNode(f_path)
            m1 = compute_merkle_node_hash(indep)
            self.assertIsNotNone(m1)

            # Modify file on disk and reset node hashes
            f_path.write_text("v2")
            indep.hashes = None

            m2 = compute_merkle_node_hash(indep)
            self.assertIsNotNone(m2)
            self.assertNotEqual(m1, m2)


class TestRenderLockfile(unittest.TestCase):
    """Unit tests for RenderLockfile loading, atomic persistence, and bucket isolation."""

    def test_load_from_dir_missing(self) -> None:
        """Validates loading from a directory with no lockfile returns an empty lockfile."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock = RenderLockfile.load_from_dir(Path(tmp_dir))
            self.assertEqual(lock.config_hashes, set())
            self.assertEqual(lock.hook_hashes, set())
            self.assertEqual(lock.payload_hashes, set())
            self.assertFalse(lock.contains_hash("abc"))
            self.assertEqual(lock.get_all_hashes(), set())

    def test_load_from_dir_corrupted(self) -> None:
        """Validates loading from malformed or corrupted JSON gracefully returns an empty lockfile."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            internal_dir = tmp_path / DRIFT_INTERNAL_DIR_NAME
            internal_dir.mkdir()
            lock_file = internal_dir / RENDER_LOCK_FILE_NAME
            lock_file.write_text("{ this is malformed json", encoding="utf-8")

            lock = RenderLockfile.load_from_dir(tmp_path)
            self.assertEqual(lock.config_hashes, set())
            self.assertEqual(lock.hook_hashes, set())
            self.assertEqual(lock.payload_hashes, set())

    def test_save_and_reload(self) -> None:
        """Validates saving to disk atomically with sorted keys, and reloading correctly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            lock = RenderLockfile(
                config_hashes={"c_beta", "c_alpha"},
                hook_hashes={"h_1"},
                payload_hashes={"p_zeta", "p_alpha"},
            )
            lock.save_to_dir(tmp_path)

            lock_path = tmp_path / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME
            self.assertTrue(lock_path.is_file())

            # Verify deterministic sorting in JSON
            content = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(content["config_hashes"], ["c_alpha", "c_beta"])
            self.assertEqual(content["hook_hashes"], ["h_1"])
            self.assertEqual(content["payload_hashes"], ["p_alpha", "p_zeta"])

            # Reload and check invariants
            reloaded = RenderLockfile.load_from_dir(tmp_path)
            self.assertEqual(reloaded.config_hashes, {"c_alpha", "c_beta"})
            self.assertEqual(reloaded.hook_hashes, {"h_1"})
            self.assertEqual(reloaded.payload_hashes, {"p_alpha", "p_zeta"})
            self.assertTrue(reloaded.contains_hash("c_alpha"))
            self.assertTrue(reloaded.contains_hash("h_1"))
            self.assertTrue(reloaded.contains_hash("p_zeta"))
            self.assertFalse(reloaded.contains_hash("nonexistent"))
            self.assertEqual(
                reloaded.get_all_hashes(),
                {"c_alpha", "c_beta", "h_1", "p_alpha", "p_zeta"},
            )

    def test_bucket_isolation_and_updates(self) -> None:
        """Validates that updating one bucket does not overwrite or clobber other buckets."""
        lock = RenderLockfile(
            config_hashes={"cfg_1"},
            hook_hashes={"hook_1"},
            payload_hashes={"pay_1"},
        )

        lock.update_payload_hashes(["pay_2", "pay_3"])
        self.assertEqual(lock.config_hashes, {"cfg_1"})
        self.assertEqual(lock.hook_hashes, {"hook_1"})
        self.assertEqual(lock.payload_hashes, {"pay_2", "pay_3"})

        lock.update_config_hashes(["cfg_new"])
        self.assertEqual(lock.config_hashes, {"cfg_new"})
        self.assertEqual(lock.hook_hashes, {"hook_1"})
        self.assertEqual(lock.payload_hashes, {"pay_2", "pay_3"})

        lock.update_hook_hashes(["hook_new"])
        self.assertEqual(lock.config_hashes, {"cfg_new"})
        self.assertEqual(lock.hook_hashes, {"hook_new"})
        self.assertEqual(lock.payload_hashes, {"pay_2", "pay_3"})
