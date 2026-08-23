"""Comprehensive unit tests for compare_folders in drift.folder_diff."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from drift.constants import set_test_mode
from drift.folder_diff import compare_folders, FolderDiff
from drift.ignore import DriftIgnore


class TestFolderDiffBasic(unittest.TestCase):
    """Tests for standard directory comparisons without translation or special flags."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_folders(self) -> None:
        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(diff.matches, [])
        self.assertEqual(diff.internal_symlinks, [])

    def test_identical_files_in_root_and_subdirs(self) -> None:
        (self.src / "file1.txt").write_text("hello", encoding="utf-8")
        (self.dst / "file1.txt").write_text("hello", encoding="utf-8")

        sub_src = self.src / "sub" / "nested"
        sub_dst = self.dst / "sub" / "nested"
        sub_src.mkdir(parents=True, exist_ok=True)
        sub_dst.mkdir(parents=True, exist_ok=True)

        (sub_src / "file2.txt").write_text("world", encoding="utf-8")
        (sub_dst / "file2.txt").write_text("world", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(sorted(diff.matches), [Path("file1.txt"), Path("sub/nested/file2.txt")])

    def test_modified_files(self) -> None:
        (self.src / "file.txt").write_text("version 1", encoding="utf-8")
        (self.dst / "file.txt").write_text("version 2", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.modified, [Path("file.txt")])
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(diff.matches, [])

    def test_added_files(self) -> None:
        (self.src / "new.txt").write_text("new content", encoding="utf-8")
        (self.src / "sub").mkdir()
        (self.src / "sub" / "sub_new.txt").write_text("sub new", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(sorted(diff.added), [Path("new.txt"), Path("sub/sub_new.txt")])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(diff.matches, [])

    def test_deleted_files(self) -> None:
        (self.dst / "orphan.txt").write_text("orphan content", encoding="utf-8")
        (self.dst / "sub").mkdir()
        (self.dst / "sub" / "sub_orphan.txt").write_text("sub orphan", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(sorted(diff.deleted), [Path("orphan.txt"), Path("sub/sub_orphan.txt")])
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.matches, [])

    def test_mixed_complex_tree(self) -> None:
        # Matches
        (self.src / "same.txt").write_text("same", encoding="utf-8")
        (self.dst / "same.txt").write_text("same", encoding="utf-8")

        # Modified
        (self.src / "mod.txt").write_text("src_mod", encoding="utf-8")
        (self.dst / "mod.txt").write_text("dst_mod", encoding="utf-8")

        # Added
        (self.src / "add.txt").write_text("add", encoding="utf-8")

        # Deleted
        (self.dst / "del.txt").write_text("del", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.matches, [Path("same.txt")])
        self.assertEqual(diff.modified, [Path("mod.txt")])
        self.assertEqual(diff.added, [Path("add.txt")])
        self.assertEqual(diff.deleted, [Path("del.txt")])


class TestFolderDiffSingleFile(unittest.TestCase):
    """Tests for single-file compare_folders invocations (Path("") return value)."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_single_file_identical(self) -> None:
        f1 = self.base / "file1.txt"
        f2 = self.base / "file2.txt"
        f1.write_text("content", encoding="utf-8")
        f2.write_text("content", encoding="utf-8")

        diff = compare_folders(f1, f2)
        self.assertEqual(diff.matches, [Path("")])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.deleted, [])

    def test_single_file_modified(self) -> None:
        f1 = self.base / "file1.txt"
        f2 = self.base / "file2.txt"
        f1.write_text("content 1", encoding="utf-8")
        f2.write_text("content 2", encoding="utf-8")

        diff = compare_folders(f1, f2)
        self.assertEqual(diff.modified, [Path("")])
        self.assertEqual(diff.matches, [])

    def test_single_file_src_only_added(self) -> None:
        f1 = self.base / "file1.txt"
        f2 = self.base / "file_nonexistent.txt"
        f1.write_text("content", encoding="utf-8")

        diff = compare_folders(f1, f2)
        self.assertEqual(diff.added, [Path("")])
        self.assertEqual(diff.matches, [])
        self.assertEqual(diff.modified, [])

    def test_single_file_dst_only_deleted(self) -> None:
        f1 = self.base / "file_nonexistent.txt"
        f2 = self.base / "file2.txt"
        f2.write_text("content", encoding="utf-8")

        diff = compare_folders(f1, f2)
        self.assertEqual(diff.deleted, [Path("")])
        self.assertEqual(diff.added, [])


class TestFolderDiffTranslationForward(unittest.TestCase):
    """Tests for translate_mode='forward' (dot- prefix in src -> leading dot in dst)."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_forward_translation_matches(self) -> None:
        (self.src / "dot-bashrc").write_text("bashrc content", encoding="utf-8")
        (self.dst / ".bashrc").write_text("bashrc content", encoding="utf-8")

        sub_src = self.src / "dot-config" / "app"
        sub_dst = self.dst / ".config" / "app"
        sub_src.mkdir(parents=True, exist_ok=True)
        sub_dst.mkdir(parents=True, exist_ok=True)
        (sub_src / "dot-settings.json").write_text("{}", encoding="utf-8")
        (sub_dst / ".settings.json").write_text("{}", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="forward")
        self.assertEqual(
            sorted(diff.matches),
            [Path("dot-bashrc"), Path("dot-config/app/dot-settings.json")]
        )
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])

    def test_forward_translation_added(self) -> None:
        (self.src / "dot-profile").write_text("profile content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="forward")
        self.assertEqual(diff.added, [Path("dot-profile")])
        self.assertEqual(diff.deleted, [])

    def test_forward_translation_deleted(self) -> None:
        (self.dst / ".vimrc").write_text("vimrc content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="forward")
        self.assertEqual(diff.deleted, [Path("dot-vimrc")])
        self.assertEqual(diff.added, [])

    def test_forward_translation_modified(self) -> None:
        (self.src / "dot-gitconfig").write_text("user=Alice", encoding="utf-8")
        (self.dst / ".gitconfig").write_text("user=Bob", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="forward")
        self.assertEqual(diff.modified, [Path("dot-gitconfig")])
        self.assertEqual(diff.matches, [])


class TestFolderDiffTranslationReverse(unittest.TestCase):
    """Tests for translate_mode='reverse' (leading dot in src -> dot- prefix in dst)."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reverse_translation_matches(self) -> None:
        (self.src / ".bashrc").write_text("bashrc content", encoding="utf-8")
        (self.dst / "dot-bashrc").write_text("bashrc content", encoding="utf-8")

        sub_src = self.src / ".config" / "app"
        sub_dst = self.dst / "dot-config" / "app"
        sub_src.mkdir(parents=True, exist_ok=True)
        sub_dst.mkdir(parents=True, exist_ok=True)
        (sub_src / ".settings.json").write_text("{}", encoding="utf-8")
        (sub_dst / "dot-settings.json").write_text("{}", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="reverse")
        self.assertEqual(
            sorted(diff.matches),
            [Path(".bashrc"), Path(".config/app/.settings.json")]
        )
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])

    def test_reverse_translation_added(self) -> None:
        (self.src / ".profile").write_text("profile content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="reverse")
        self.assertEqual(diff.added, [Path(".profile")])

    def test_reverse_translation_deleted(self) -> None:
        (self.dst / "dot-vimrc").write_text("vimrc content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="reverse")
        self.assertEqual(diff.deleted, [Path(".vimrc")])

    def test_reverse_translation_modified(self) -> None:
        (self.src / ".gitconfig").write_text("user=Alice", encoding="utf-8")
        (self.dst / "dot-gitconfig").write_text("user=Bob", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="reverse")
        self.assertEqual(diff.modified, [Path(".gitconfig")])


class TestFolderDiffSrcOnly(unittest.TestCase):
    """Tests for src_only=True flag (skipping deletion detection from dst)."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_src_only_ignores_dst_extras(self) -> None:
        (self.src / "common.txt").write_text("hello", encoding="utf-8")
        (self.dst / "common.txt").write_text("hello", encoding="utf-8")

        # Extra file in dst
        (self.dst / "extra_dst.txt").write_text("extra", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, src_only=True)
        self.assertEqual(diff.matches, [Path("common.txt")])
        self.assertEqual(diff.deleted, [])  # NOT marked as deleted
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.modified, [])

    def test_src_only_still_detects_additions_and_modifications(self) -> None:
        (self.src / "mod.txt").write_text("v1", encoding="utf-8")
        (self.dst / "mod.txt").write_text("v2", encoding="utf-8")
        (self.src / "new.txt").write_text("new", encoding="utf-8")
        (self.dst / "dst_extra.txt").write_text("extra", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, src_only=True)
        self.assertEqual(diff.modified, [Path("mod.txt")])
        self.assertEqual(diff.added, [Path("new.txt")])
        self.assertEqual(diff.deleted, [])


class TestFolderDiffIgnoreHandler(unittest.TestCase):
    """Tests for DriftIgnore integration with compare_folders."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ignored_file_missing_in_dst_is_completely_skipped(self) -> None:
        (self.src / "normal.txt").write_text("normal", encoding="utf-8")
        (self.src / "secret.key").write_text("secret", encoding="utf-8")

        ignore = DriftIgnore([r"\.key$"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)

        self.assertEqual(diff.added, [Path("normal.txt")])
        self.assertNotIn(Path("secret.key"), diff.added)

    def test_ignored_file_present_in_dst_is_marked_deleted(self) -> None:
        (self.src / "normal.txt").write_text("normal", encoding="utf-8")
        (self.dst / "normal.txt").write_text("normal", encoding="utf-8")

        # File is ignored in src, but exists in dst (should be cleaned up from dst)
        (self.src / "ignored.tmp").write_text("temp", encoding="utf-8")
        (self.dst / "ignored.tmp").write_text("temp", encoding="utf-8")

        ignore = DriftIgnore([r"\.tmp$"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)

        self.assertEqual(diff.matches, [Path("normal.txt")])
        self.assertEqual(diff.deleted, [Path("ignored.tmp")])
        self.assertEqual(diff.added, [])

    def test_ignored_directory_deletes_dst_contents(self) -> None:
        ignore_src = self.src / "build"
        ignore_dst = self.dst / "build"
        ignore_src.mkdir()
        ignore_dst.mkdir()

        (ignore_src / "output.bin").write_text("bin", encoding="utf-8")
        (ignore_dst / "output.bin").write_text("bin", encoding="utf-8")
        (ignore_dst / "other.bin").write_text("other", encoding="utf-8")

        ignore = DriftIgnore([r"^/build"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)

        self.assertEqual(sorted(diff.deleted), [Path("build/other.bin"), Path("build/output.bin")])

    def test_reverse_translation_with_ignore_handler(self) -> None:
        # In reverse translation, src is host system (.file), ignore patterns use repo format (dot-file)
        (self.src / ".private").write_text("private", encoding="utf-8")
        (self.dst / "dot-private").write_text("private", encoding="utf-8")

        ignore = DriftIgnore(["^dot-private$"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore, translate_mode="reverse")

        self.assertEqual(diff.deleted, [Path(".private")])
        self.assertEqual(diff.matches, [])


class TestFolderDiffSymlinks(unittest.TestCase):
    """Tests for symlink handling with resolve_symlinks=True and False."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_symlinks_true_file_symlink_matching(self) -> None:
        target = self.base / "real_file.txt"
        target.write_text("target content", encoding="utf-8")

        # Symlink in src pointing to target
        os.symlink(target, self.src / "link.txt")
        (self.dst / "link.txt").write_text("target content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.matches, [Path("link.txt")])
        self.assertEqual(diff.modified, [])

    def test_resolve_symlinks_true_file_symlink_modified(self) -> None:
        target = self.base / "real_file.txt"
        target.write_text("version 1", encoding="utf-8")

        os.symlink(target, self.src / "link.txt")
        (self.dst / "link.txt").write_text("version 2", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.modified, [Path("link.txt")])

    def test_resolve_symlinks_true_directory_symlink_recursive(self) -> None:
        real_dir = self.base / "real_dir"
        real_dir.mkdir()
        (real_dir / "inner.txt").write_text("inner", encoding="utf-8")

        os.symlink(real_dir, self.src / "sym_dir")
        (self.dst / "sym_dir").mkdir()
        (self.dst / "sym_dir" / "inner.txt").write_text("inner", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.matches, [Path("sym_dir/inner.txt")])

    def test_resolve_symlinks_true_broken_symlink(self) -> None:
        broken_target = self.base / "nonexistent.txt"
        os.symlink(broken_target, self.src / "broken.txt")
        os.symlink(broken_target, self.dst / "broken.txt")

        # Both point to same broken link -> matching link targets
        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.modified, [])

        # Dst has different broken target
        os.unlink(self.dst / "broken.txt")
        os.symlink(self.base / "other_nonexistent.txt", self.dst / "broken.txt")
        diff2 = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff2.modified, [Path("broken.txt")])

    def test_resolve_symlinks_false_raw_comparison(self) -> None:
        t1 = self.base / "target1.txt"
        t2 = self.base / "target2.txt"
        t1.write_text("content", encoding="utf-8")
        t2.write_text("content", encoding="utf-8")

        os.symlink(t1, self.src / "link.txt")
        os.symlink(t2, self.dst / "link.txt")

        # With resolve_symlinks=False, raw link targets differ
        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.modified, [Path("link.txt")])

    def test_resolve_symlinks_false_symlink_vs_regular_file(self) -> None:
        target = self.base / "target.txt"
        target.write_text("content", encoding="utf-8")

        os.symlink(target, self.src / "item.txt")
        (self.dst / "item.txt").write_text("content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.modified, [Path("item.txt")])


class TestFolderDiffInternalSymlinksSafety(unittest.TestCase):
    """Tests for drift_root safety detection and internal_symlinks tracking."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.drift_root = self.base / "drift_workspace"
        self.src = self.drift_root / "install" / "pkg_a"
        self.dst = self.base / "system_target"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_valid_stow_link_pointing_to_src_is_not_flagged(self) -> None:
        src_file = self.src / "file.txt"
        src_file.write_text("content", encoding="utf-8")

        # Valid stow symlink in system target pointing directly to src_file
        os.symlink(src_file, self.dst / "file.txt")

        diff = compare_folders(self.src, self.dst, drift_root=self.drift_root)
        self.assertEqual(diff.internal_symlinks, [])

    def test_rogue_internal_symlink_pointing_into_drift_root_is_flagged(self) -> None:
        src_file = self.src / "file.txt"
        src_file.write_text("content", encoding="utf-8")

        other_internal_file = self.drift_root / "config" / "drift.toml"
        other_internal_file.parent.mkdir(parents=True, exist_ok=True)
        other_internal_file.write_text("config", encoding="utf-8")

        # Rogue symlink in system target pointing to a different file in drift_root
        os.symlink(other_internal_file, self.dst / "file.txt")

        diff = compare_folders(self.src, self.dst, drift_root=self.drift_root)
        self.assertEqual(diff.internal_symlinks, [Path("file.txt")])

    def test_external_symlink_is_not_flagged(self) -> None:
        src_file = self.src / "file.txt"
        src_file.write_text("content", encoding="utf-8")

        external_target = self.base / "outside_target.txt"
        external_target.write_text("external", encoding="utf-8")

        os.symlink(external_target, self.dst / "file.txt")

        diff = compare_folders(self.src, self.dst, drift_root=self.drift_root)
        self.assertEqual(diff.internal_symlinks, [])


class TestFolderDiffTypeMismatchesAndRoots(unittest.TestCase):
    """Tests for type mismatches (dir vs file) and non-existent roots."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_src_dir_dst_file_mismatch(self) -> None:
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

        # src has directory 'item', dst has file 'item'
        item_dir = self.src / "item"
        item_dir.mkdir()
        (item_dir / "child.txt").write_text("child", encoding="utf-8")
        (self.dst / "item").write_text("regular file", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.deleted, [Path("item")])
        self.assertEqual(diff.added, [Path("item/child.txt")])

    def test_src_file_dst_dir_mismatch(self) -> None:
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

        # src has file 'item', dst has directory 'item'
        (self.src / "item").write_text("regular file", encoding="utf-8")
        item_dir = self.dst / "item"
        item_dir.mkdir()
        (item_dir / "child.txt").write_text("child", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.added, [Path("item")])
        self.assertEqual(diff.deleted, [Path("item/child.txt")])

    def test_nonexistent_src_root(self) -> None:
        self.dst.mkdir(parents=True, exist_ok=True)
        (self.dst / "existing.txt").write_text("content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.deleted, [Path("existing.txt")])
        self.assertEqual(diff.added, [])

    def test_nonexistent_dst_root(self) -> None:
        self.src.mkdir(parents=True, exist_ok=True)
        (self.src / "new.txt").write_text("content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.added, [Path("new.txt")])
        self.assertEqual(diff.deleted, [])

    def test_both_roots_nonexistent(self) -> None:
        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.matches, [])


class TestFolderDiffSpecialEdgeCases(unittest.TestCase):
    """Tests for edge cases: empty directories, special dot- segment folding, circular symlinks."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.src = self.base / "src"
        self.dst = self.base / "dst"
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_directory_added_without_ignore_handler(self) -> None:
        # An empty directory in src (with no ignore handler) is tracked in added
        empty_dir = self.src / "empty_folder"
        empty_dir.mkdir()

        diff = compare_folders(self.src, self.dst, ignore_handler=None)
        self.assertEqual(diff.added, [Path("empty_folder")])

    def test_empty_directory_deleted_without_ignore_handler(self) -> None:
        # An empty directory in dst (with no ignore handler) is tracked in deleted
        empty_dir = self.dst / "empty_folder"
        empty_dir.mkdir()

        diff = compare_folders(self.src, self.dst, ignore_handler=None)
        self.assertEqual(diff.deleted, [Path("empty_folder")])

    def test_empty_directory_with_ignore_handler_is_ignored(self) -> None:
        # When an ignore handler is provided, empty directory recursion does not emit empty folder path
        empty_src = self.src / "empty_folder"
        empty_src.mkdir()
        ignore = DriftIgnore([])

        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)
        self.assertEqual(diff.added, [])

    def test_forward_translation_isolated_dot_dash_folder(self) -> None:
        # In drift, 'dot-' alone as a folder segment translates to root or same folder
        # e.g. src/dot-/file.txt translates to dst/.file.txt or dst/file.txt depending on dot- prefix
        special_src = self.src / "dot-"
        special_src.mkdir()
        (special_src / "dot-bashrc").write_text("content", encoding="utf-8")
        (self.dst / ".bashrc").write_text("content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, translate_mode="forward")
        self.assertEqual(diff.matches, [Path("dot-/dot-bashrc")])

    def test_broken_symlink_in_add_children_as_added(self) -> None:
        # Non-existent dst root, src has a broken symlink
        nonexistent_target = self.base / "ghost.txt"
        os.symlink(nonexistent_target, self.src / "broken_link.txt")
        shutil.rmtree(self.dst)

        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.added, [Path("broken_link.txt")])

    def test_broken_symlink_in_add_children_as_deleted(self) -> None:
        # Non-existent src root, dst has a broken symlink
        nonexistent_target = self.base / "ghost.txt"
        os.symlink(nonexistent_target, self.dst / "broken_link.txt")
        shutil.rmtree(self.src)

        diff = compare_folders(self.src, self.dst)
        self.assertEqual(diff.deleted, [Path("broken_link.txt")])

    def test_symlink_target_cycle_handled_gracefully(self) -> None:
        # Circular symlinks (link1 -> link2 -> link1)
        l1 = self.src / "cycle1"
        l2 = self.src / "cycle2"
        os.symlink(l2, l1)
        os.symlink(l1, l2)

        # compare_folders should catch the exception and mark as broken/modified without infinite loop crash
        (self.dst / "cycle1").write_text("regular", encoding="utf-8")
        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertIn(Path("cycle1"), diff.modified)


if __name__ == "__main__":
    unittest.main()
