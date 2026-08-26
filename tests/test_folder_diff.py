"""Comprehensive unit tests for compare_folders in drift.folder_diff."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from drift.constants import set_test_mode
from drift.folder_diff import compare_folders, list_folder_paths, find_links_pointing_into, FolderDiff
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

    def test_ignored_file_present_in_dst_is_not_marked_deleted(self) -> None:
        (self.src / "normal.txt").write_text("normal", encoding="utf-8")
        (self.dst / "normal.txt").write_text("normal", encoding="utf-8")

        # File is ignored in src, but exists in dst
        (self.src / "ignored.tmp").write_text("temp", encoding="utf-8")
        (self.dst / "ignored.tmp").write_text("temp", encoding="utf-8")

        ignore = DriftIgnore([r"\.tmp$"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)

        self.assertEqual(diff.matches, [Path("normal.txt")])
        self.assertEqual(diff.deleted, [])
        self.assertEqual(diff.added, [])

    def test_ignored_directory_does_not_delete_dst_contents(self) -> None:
        ignore_src = self.src / "build"
        ignore_dst = self.dst / "build"
        ignore_src.mkdir()
        ignore_dst.mkdir()

        (ignore_src / "output.bin").write_text("bin", encoding="utf-8")
        (ignore_dst / "output.bin").write_text("bin", encoding="utf-8")
        (ignore_dst / "other.bin").write_text("other", encoding="utf-8")

        ignore = DriftIgnore([r"^/build"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore)

        self.assertEqual(diff.added, [])
        self.assertEqual(diff.deleted, [])

    def test_reverse_translation_with_ignore_handler(self) -> None:
        # In reverse translation, src is host system (.file), ignore patterns use repo format (dot-file)
        (self.src / ".private").write_text("private", encoding="utf-8")
        (self.dst / "dot-private").write_text("private", encoding="utf-8")

        ignore = DriftIgnore(["^dot-private$"])
        diff = compare_folders(self.src, self.dst, ignore_handler=ignore, translate_mode="reverse")

        self.assertEqual(diff.added, [])
        self.assertEqual(diff.deleted, [])
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

    def test_resolve_symlinks_false_raw_matching(self) -> None:
        target = self.base / "target.txt"
        target.write_text("content", encoding="utf-8")

        os.symlink(target, self.src / "link.txt")
        os.symlink(target, self.dst / "link.txt")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.matches, [Path("link.txt")])
        self.assertEqual(diff.modified, [])

    def test_resolve_symlinks_false_symlink_vs_regular_file(self) -> None:
        target = self.base / "target.txt"
        target.write_text("content", encoding="utf-8")

        os.symlink(target, self.src / "item.txt")
        (self.dst / "item.txt").write_text("content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.modified, [Path("item.txt")])

    def test_resolve_symlinks_false_src_file_vs_dst_symlink(self) -> None:
        target = self.base / "target.txt"
        target.write_text("content", encoding="utf-8")

        (self.src / "item.txt").write_text("content", encoding="utf-8")
        os.symlink(target, self.dst / "item.txt")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.modified, [Path("item.txt")])

    def test_resolve_symlinks_false_src_symlink_vs_dst_dir(self) -> None:
        target = self.base / "target.txt"
        target.write_text("content", encoding="utf-8")

        os.symlink(target, self.src / "item")
        dst_dir = self.dst / "item"
        dst_dir.mkdir()
        (dst_dir / "child.txt").write_text("child content", encoding="utf-8")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.added, [Path("item")])
        self.assertEqual(diff.deleted, [Path("item/child.txt")])
        self.assertEqual(diff.modified, [])

    def test_resolve_symlinks_true_symlink_dir_vs_symlink_dir(self) -> None:
        real1 = self.base / "real_dir1"
        real1.mkdir()
        (real1 / "f.txt").write_text("same", encoding="utf-8")
        real2 = self.base / "real_dir2"
        real2.mkdir()
        (real2 / "f.txt").write_text("same", encoding="utf-8")

        os.symlink(real1, self.src / "dir_link")
        os.symlink(real2, self.dst / "dir_link")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=True)
        self.assertEqual(diff.matches, [Path("dir_link/f.txt")])
        self.assertEqual(diff.modified, [])

    def test_find_links_pointing_into(self) -> None:
        """Verifies find_links_pointing_into finds symlinks whose targets lie inside target_dir."""
        target_root = self.base / "drift_target"
        target_root.mkdir()
        drift_file = target_root / "internal.txt"
        drift_file.write_text("internal", encoding="utf-8")

        outside_dir = self.base / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "external.txt"
        outside_file.write_text("external", encoding="utf-8")

        search_dir = self.base / "search_area"
        search_dir.mkdir()

        # 1. Symlink pointing into target_root
        link_inside = search_dir / "link_inside.txt"
        link_inside.symlink_to(drift_file)

        # 2. Symlink pointing outside
        link_outside = search_dir / "link_outside.txt"
        link_outside.symlink_to(outside_file)

        # 3. Regular file
        normal_file = search_dir / "normal.txt"
        normal_file.write_text("normal", encoding="utf-8")

        # 4. Nested directory with symlink inside
        nested = search_dir / "sub" / "deep"
        nested.mkdir(parents=True)
        nested_link_inside = nested / "deep_link.txt"
        nested_link_inside.symlink_to(drift_file)

        found = find_links_pointing_into(search_dir, target_root)
        self.assertEqual(set(found), {link_inside, nested_link_inside})

        # Test single file search_path
        self.assertEqual(find_links_pointing_into(link_inside, target_root), [link_inside])
        self.assertEqual(find_links_pointing_into(link_outside, target_root), [])

    def test_find_links_pointing_into_follow_symlinks(self) -> None:
        """Verifies follow_symlinks=True traverses into symlinked directories."""
        target_root = self.base / "drift_target"
        target_root.mkdir()
        drift_file = target_root / "internal.txt"
        drift_file.write_text("internal", encoding="utf-8")

        real_sub = self.base / "real_sub"
        real_sub.mkdir()
        (real_sub / "nested_in_symdir.txt").symlink_to(drift_file)

        search_dir = self.base / "search_dir"
        search_dir.mkdir()
        sym_dir = search_dir / "sym_dir"
        sym_dir.symlink_to(real_sub)

        # follow_symlinks=False does not traverse inside sym_dir
        self.assertEqual(find_links_pointing_into(search_dir, target_root, follow_symlinks=False), [])

        # follow_symlinks=True traverses inside sym_dir
        found = find_links_pointing_into(search_dir, target_root, follow_symlinks=True)
        self.assertEqual(found, [sym_dir / "nested_in_symdir.txt"])


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

    def test_src_dir_dst_symlink_mismatch_resolve_symlinks_false(self) -> None:
        self.src.mkdir(parents=True, exist_ok=True)
        self.dst.mkdir(parents=True, exist_ok=True)

        # src has directory 'item', dst has symlink 'item' (pointing to external file or dir)
        item_dir = self.src / "item"
        item_dir.mkdir()
        (item_dir / "child.txt").write_text("child content", encoding="utf-8")

        external_target = self.base / "external_target.txt"
        external_target.write_text("external", encoding="utf-8")
        os.symlink(external_target, self.dst / "item")

        diff = compare_folders(self.src, self.dst, resolve_symlinks=False)
        self.assertEqual(diff.deleted, [Path("item")])
        self.assertEqual(diff.added, [Path("item/child.txt")])
        self.assertEqual(diff.modified, [])

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
        # 'dot-' alone as a folder segment is preserved as a literal directory name 'dot-'
        special_src = self.src / "dot-"
        special_src.mkdir()
        (special_src / "dot-bashrc").write_text("content", encoding="utf-8")
        
        special_dst = self.dst / "dot-"
        special_dst.mkdir()
        (special_dst / ".bashrc").write_text("content", encoding="utf-8")

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


class TestListFolderPaths(unittest.TestCase):
    """Tests for list_folder_paths helper function."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name).resolve()
        self.root = self.base / "pkg_root"
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_folder_paths_basic(self) -> None:
        (self.root / "file1.txt").write_text("1", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file2.txt").write_text("2", encoding="utf-8")
        (self.root / "empty_dir").mkdir()

        paths = list_folder_paths(self.root)
        self.assertEqual(paths, [Path("empty_dir"), Path("file1.txt"), Path("sub/file2.txt")])

    def test_list_folder_paths_with_base_rel(self) -> None:
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file.txt").write_text("content", encoding="utf-8")

        paths = list_folder_paths(self.root / "sub", base_rel=Path("sub"))
        self.assertEqual(paths, [Path("sub/file.txt")])

    def test_list_folder_paths_with_ignore_handler(self) -> None:
        (self.root / "keep.txt").write_text("keep", encoding="utf-8")
        (self.root / "ignore_me.bak").write_text("ignored", encoding="utf-8")

        ignore = DriftIgnore([r"\.bak$"])
        paths = list_folder_paths(self.root, ignore_handler=ignore)
        self.assertEqual(paths, [Path("keep.txt")])

    def test_list_folder_paths_single_file(self) -> None:
        f = self.root / "single.txt"
        f.write_text("hello", encoding="utf-8")
        paths = list_folder_paths(f, base_rel=Path("single.txt"))
        self.assertEqual(paths, [Path("single.txt")])

    def test_list_folder_paths_symlinked_dir_resolved(self) -> None:
        target_dir = self.base / "target_dir"
        target_dir.mkdir()
        (target_dir / "child.txt").write_text("target child", encoding="utf-8")

        link_dir = self.root / "link_dir"
        link_dir.symlink_to(target_dir)

        # resolve_symlinks=True follows symlink to directory
        paths_resolved = list_folder_paths(link_dir, base_rel=Path("link_dir"), resolve_symlinks=True)
        self.assertEqual(paths_resolved, [Path("link_dir/child.txt")])

        # resolve_symlinks=False treats it as a single symlink entry
        paths_unresolved = list_folder_paths(link_dir, base_rel=Path("link_dir"), resolve_symlinks=False)
        self.assertEqual(paths_unresolved, [Path("link_dir")])


if __name__ == "__main__":
    unittest.main()
