"""Tests for file_utils operations."""

import os
import sys
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock


from drift.core.constants import set_test_mode
from drift.utils.path_utils import (
    is_relative_to,
    to_relative_path,
    to_relative_posix,
    encode_dot_prefix,
    decode_dot_prefix,
    relative_path_between,
)
from drift.utils.file_inspect import (
    tree_files,
    file_hash,
    contents_differ,
    is_concrete_dir,
    is_internal_lock_file,
    check_is_trackable,
    check_can_symlink,
)

CAN_SYMLINK = check_can_symlink()
from drift.utils.file_ops import (
    prune_empty_parents,
    remove_with_parents,
    copy_tree,
    move_tree,
    assert_writable,
    ensure_dir,
    remove_tree,
    remove_file_or_empty_dir,
    create_symlink,
    copy_file,
    copy_symlink,
)
from drift.core.file_action import (
    backup_item,
)


class TestFileUtils(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_is_relative_to(self) -> None:
        path = self.root / "subdir" / "file.txt"
        self.assertTrue(is_relative_to(path, self.root))
        self.assertTrue(is_relative_to(path, path))
        self.assertFalse(is_relative_to(self.root, path))

        # Unrelated paths
        unrelated = Path("/different/hierarchy/file.txt")
        self.assertFalse(is_relative_to(path, unrelated))

        # Cross-drive simulation (where relative_to raises ValueError)
        mock_path = MagicMock(spec=Path)
        mock_path.relative_to.side_effect = ValueError("Different drives: C: vs D:")
        self.assertFalse(is_relative_to(mock_path, self.root))

    def test_to_relative_path(self) -> None:
        """Verifies to_relative_path returns relative Path when relative, and self as Path fallback otherwise."""
        child = self.root / "subdir" / "file.txt"
        self.assertEqual(to_relative_path(child, self.root), Path("subdir/file.txt"))
        self.assertEqual(to_relative_path(self.root, self.root), Path("."))

        # Unrelated path fallback
        unrelated = Path("/outside/file.txt")
        self.assertEqual(to_relative_path(unrelated, self.root), unrelated)
        self.assertEqual(to_relative_path(self.root, unrelated), self.root)

    def test_to_relative_posix(self) -> None:
        """Verifies to_relative_posix returns relative POSIX string when relative, and as_posix fallback otherwise."""
        child = self.root / "subdir" / "file.txt"
        self.assertEqual(to_relative_posix(child, self.root), "subdir/file.txt")
        self.assertEqual(to_relative_posix(self.root, self.root), ".")

        # Unrelated path fallback
        unrelated = Path("/outside/file.txt")
        self.assertEqual(to_relative_posix(unrelated, self.root), "/outside/file.txt")
        self.assertEqual(to_relative_posix(self.root, unrelated), self.root.as_posix())

    def test_is_concrete_dir(self) -> None:
        """Verifies is_concrete_dir returns True only for real directories and False for files, symlinks, and missing paths."""
        real_dir = self.root / "real_dir"
        real_dir.mkdir()
        real_file = self.root / "real_file.txt"
        real_file.write_text("hello")
        missing_path = self.root / "does_not_exist"

        self.assertTrue(is_concrete_dir(real_dir))
        self.assertFalse(is_concrete_dir(real_file))
        self.assertFalse(is_concrete_dir(missing_path))

        if CAN_SYMLINK:
            dir_symlink = self.root / "dir_symlink"
            dir_symlink.symlink_to(real_dir)
            file_symlink = self.root / "file_symlink"
            file_symlink.symlink_to(real_file)
            broken_symlink = self.root / "broken_symlink"
            broken_symlink.symlink_to(self.root / "nonexistent")

            self.assertFalse(is_concrete_dir(dir_symlink))
            self.assertFalse(is_concrete_dir(file_symlink))
            self.assertFalse(is_concrete_dir(broken_symlink))

    def test_check_is_trackable(self) -> None:
        """Verifies check_is_trackable filters out concrete directories, temp files, and internal lockfiles."""
        sub_dir = self.root / "sub_dir"
        sub_dir.mkdir()
        valid_file = sub_dir / "valid.txt"
        valid_file.write_text("ok")
        temp_file = sub_dir / ".DS_Store"
        temp_file.write_text("junk")

        # User file named render_lock.json outside .drift/
        user_lock_file = sub_dir / "render_lock.json"
        user_lock_file.write_text("{}")

        # Internal Drift render lockfile inside .drift/
        internal_drift_dir = sub_dir / ".drift"
        internal_drift_dir.mkdir()
        internal_lock_file = internal_drift_dir / "render_lock.json"
        internal_lock_file.write_text("{}")

        # Other files in .drift/ (must remain valid diff candidates)
        pkg_toml_file = internal_drift_dir / "drift_package.toml"
        pkg_toml_file.write_text("[package]\nname = 'test'\n")

        # 1. Direct path checks
        self.assertTrue(check_is_trackable(valid_file))
        if CAN_SYMLINK:
            symlink_to_file = sub_dir / "link_to_file"
            symlink_to_file.symlink_to(valid_file)
            symlink_to_dir = sub_dir / "link_to_dir"
            symlink_to_dir.symlink_to(self.root)
            self.assertTrue(check_is_trackable(symlink_to_file))
            self.assertTrue(check_is_trackable(symlink_to_dir))          # symlinks are inspectable candidates
        self.assertFalse(check_is_trackable(sub_dir))                # concrete directory rejected
        self.assertFalse(check_is_trackable(temp_file))              # editor/OS temp file rejected

        # Internal lockfile rejected, but user lockfile and control plane configs accepted
        self.assertTrue(is_internal_lock_file(internal_lock_file))
        self.assertFalse(check_is_trackable(internal_lock_file))     # internal lockfile in .drift/ rejected
        self.assertFalse(is_internal_lock_file(user_lock_file))
        self.assertTrue(check_is_trackable(user_lock_file))          # user file named render_lock.json outside .drift/ accepted
        self.assertFalse(is_internal_lock_file(pkg_toml_file))
        self.assertTrue(check_is_trackable(pkg_toml_file))           # drift_package.toml in .drift/ accepted

        # 2. Checks with base_dir resolution
        self.assertTrue(check_is_trackable(Path("sub_dir/valid.txt"), base_dir=self.root))
        self.assertFalse(check_is_trackable(Path("sub_dir"), base_dir=self.root))
        self.assertFalse(check_is_trackable(Path("sub_dir/.DS_Store"), base_dir=self.root))
        self.assertFalse(check_is_trackable(Path("sub_dir/.drift/render_lock.json"), base_dir=self.root))
        self.assertTrue(check_is_trackable(Path("sub_dir/render_lock.json"), base_dir=self.root))

    def test_translate_dot_prefixes(self) -> None:
        """Verifies encode_dot_prefix converts 'dot-' to leading '.', skips 'dot-'/'dot-.',
        and only translates segments that start with 'dot-'.
        """
        self.assertEqual(encode_dot_prefix(Path("dot-bashrc")), Path(".bashrc"))
        self.assertEqual(encode_dot_prefix(Path("dot-config/nvim/init.lua")), Path(".config/nvim/init.lua"))
        self.assertEqual(encode_dot_prefix(Path("dot-config/dot-vimrc")), Path(".config/.vimrc"))
        self.assertEqual(encode_dot_prefix(Path("normal_dir/file.txt")), Path("normal_dir/file.txt"))

        # Segments 'dot-' and 'dot-.' are preserved (not translated to '.' or '..')
        self.assertEqual(encode_dot_prefix(Path("dot-")), Path("dot-"))
        self.assertEqual(encode_dot_prefix(Path("dot-.")), Path("dot-."))
        self.assertEqual(encode_dot_prefix(Path("config/dot-/file.txt")), Path("config/dot-/file.txt"))
        self.assertEqual(encode_dot_prefix(Path("config/dot-./file.txt")), Path("config/dot-./file.txt"))

        # Segments starting with 'dot-'
        self.assertEqual(encode_dot_prefix(Path("dot--foo")), Path(".-foo"))
        self.assertEqual(encode_dot_prefix(Path("dot-.bar")), Path("..bar"))

    def test_translate_dot_prefixes_reverse(self) -> None:
        """Verifies decode_dot_prefix converts leading '.' to 'dot-',
        and never translates '.' or '..' segments.
        """
        self.assertEqual(decode_dot_prefix(Path(".bashrc")), Path("dot-bashrc"))
        self.assertEqual(decode_dot_prefix(Path(".config/nvim/init.lua")), Path("dot-config/nvim/init.lua"))
        self.assertEqual(decode_dot_prefix(Path(".config/.vimrc")), Path("dot-config/dot-vimrc"))
        self.assertEqual(decode_dot_prefix(Path("normal_dir/file.txt")), Path("normal_dir/file.txt"))

        # '.' and '..' segments are preserved and never converted to 'dot-'
        self.assertEqual(decode_dot_prefix(Path(".")), Path("."))
        self.assertEqual(decode_dot_prefix(Path("..")), Path(".."))
        self.assertEqual(decode_dot_prefix(Path("config/./file.txt")), Path("config/file.txt"))

        # Other leading dot names
        self.assertEqual(decode_dot_prefix(Path("..bar")), Path("dot-.bar"))
        self.assertEqual(decode_dot_prefix(Path(".-foo")), Path("dot--foo"))

    def test_tree_relative_files(self) -> None:
        subdir = self.root / "subdir"
        subdir.mkdir()
        file1 = subdir / "file1.txt"
        file1.touch()
        subsubdir = subdir / "nested"
        subsubdir.mkdir()
        file2 = subsubdir / "file2.txt"
        file2.touch()

        files = tree_files(subdir)
        self.assertEqual(files, [Path("file1.txt"), Path("nested/file2.txt")])

        # Test non-existent dir
        self.assertEqual(tree_files(self.root / "nonexistent"), [])

    def test_get_relative_path(self) -> None:
        dir1 = self.root / "a" / "b" / "c"
        dir2 = self.root / "a" / "d" / "e"

        # Resolve them so resolve() can work
        dir1.mkdir(parents=True, exist_ok=True)
        dir2.mkdir(parents=True, exist_ok=True)

        rel = relative_path_between(dir1, dir2)
        self.assertEqual(rel, Path("../../d/e"))

    def test_compute_file_hash_and_differ(self) -> None:
        file1 = self.root / "file1.txt"
        file2 = self.root / "file2.txt"
        file3 = self.root / "file3.txt"

        file1.write_text("hello", encoding="utf-8")
        file2.write_text("hello", encoding="utf-8")
        file3.write_text("world", encoding="utf-8")

        hash1 = file_hash(file1)
        hash2 = file_hash(file2)
        hash3 = file_hash(file3)

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash3)

        self.assertFalse(contents_differ(file1, file2))
        self.assertTrue(contents_differ(file1, file3))

        # Edge cases:
        # 1. Both files do not exist
        non_existent1 = self.root / "non_existent1.txt"
        non_existent2 = self.root / "non_existent2.txt"
        self.assertFalse(contents_differ(non_existent1, non_existent2))

        # 2. One file exists, the other doesn't
        self.assertTrue(contents_differ(file1, non_existent1))
        self.assertTrue(contents_differ(non_existent1, file1))

        # 3. Same resolved path
        self.assertFalse(contents_differ(file1, file1))

        # 4. Non-file path (e.g., directory) raises ValueError
        dir_path = self.root / "some_directory"
        dir_path.mkdir()
        with self.assertRaises(ValueError):
            contents_differ(file1, dir_path)

        # 5. Different file sizes
        file_large = self.root / "large.txt"
        file_large.write_text("hello world long text", encoding="utf-8")
        self.assertTrue(contents_differ(file1, file_large))

    def test_rmdir_parents(self) -> None:
        nested = self.root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        file_path = nested / "file.txt"
        file_path.touch()

        # It won't remove since it's not empty
        prune_empty_parents(nested, self.root)
        self.assertTrue(nested.exists())

        # Delete the file
        file_path.unlink()

        # Run prune_empty_parents
        prune_empty_parents(nested, self.root)

        # nested "a/b/c" and "a/b" and "a" should be cleaned up
        self.assertFalse((self.root / "a").exists())

    def test_delete_one_file(self) -> None:
        file_path = self.root / "a" / "b" / "file.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("original content", encoding="utf-8")

        remove_with_parents(file_path, limit_dir=self.root)

        self.assertFalse(file_path.exists())
        self.assertFalse((self.root / "a").exists())  # Empty parent cleaned up
        self.assertTrue(self.root.exists())

    def test_assert_writable(self) -> None:
        writable_dir = self.root / "writable"
        writable_dir.mkdir()
        
        # Should complete gracefully
        assert_writable(writable_dir, sudo=False)
        assert_writable(writable_dir, sudo=True)

        # Non-existent dir resolves closest parent
        assert_writable(writable_dir / "nonexistent" / "subdir", sudo=False)

    def test_ensure_dir(self) -> None:
        path = self.root / "new_dir"
        ensure_dir(path, sudo=False)
        self.assertTrue(path.is_dir())

    @unittest.skipIf(sys.platform == "win32", "Sudo is POSIX-only")
    @patch("subprocess.run")
    def test_ensure_dir_with_sudo(self, mock_run) -> None:
        path = self.root / "sudo_dir"
        ensure_dir(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "mkdir", "-p", path.as_posix()], check=True, capture_output=True, shell=False)

    @unittest.skipIf(sys.platform == "win32", "Sudo is POSIX-only")
    @patch("subprocess.run")
    def test_remove_tree_file_or_dir_with_sudo(self, mock_run) -> None:
        path = self.root / "file_to_remove"
        path.touch()
        remove_tree(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "rm", "-f", path.as_posix()], check=True, capture_output=True, shell=False)

    @unittest.skipIf(sys.platform == "win32", "Sudo is POSIX-only")
    @patch("subprocess.run")
    def test_create_symlink_manually_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_link"
        create_symlink(src, dst, sudo=True)
        # Verify run was called with sudo ln -s
        mock_run.assert_any_call(["sudo", "ln", "-s", src.as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

    @unittest.skipIf(sys.platform == "win32", "Sudo is POSIX-only")
    @patch("subprocess.run")
    def test_atomic_copy_file_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"

        # 1. Pipeline success (mktemp -> cp -> mv)
        mock_proc = MagicMock()
        mock_proc.stdout = (self.root / ".tmp_dst_file_123456").as_posix()
        mock_run.return_value = mock_proc

        copy_file(src, dst, sudo=True)
        mock_run.assert_any_call(
            ["sudo", "mktemp", (dst.parent / f".tmp_{dst.name}_XXXXXX").as_posix()],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        mock_run.assert_any_call(["sudo", "cp", "-p", src.as_posix(), (self.root / ".tmp_dst_file_123456").as_posix()], check=True, capture_output=True, shell=False)
        mock_run.assert_any_call(["sudo", "mv", "-f", (self.root / ".tmp_dst_file_123456").as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

        # 2. Fallback when mktemp fails
        mock_run.reset_mock()
        mock_run.side_effect = [Exception("mktemp failed"), MagicMock()]
        copy_file(src, dst, sudo=True)
        mock_run.assert_any_call(["sudo", "cp", "-p", src.as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

    @unittest.skipIf(sys.platform == "win32", "POSIX mktemp is POSIX-only")
    def test_mktemp_full_path_template_real_execution(self) -> None:
        """Executes the real system 'mktemp' binary with a full-path template (without GNU '-p')
        to verify cross-platform compatibility across Linux and macOS/BSD.
        """
        import subprocess
        template_arg = (self.root / ".tmp_real_mktemp_XXXXXX").as_posix()
        res = subprocess.run(["mktemp", template_arg], check=True, capture_output=True, text=True)
        created_path = Path(res.stdout.strip())
        try:
            self.assertTrue(created_path.is_file())
            self.assertEqual(created_path.parent, self.root)
            self.assertTrue(created_path.name.startswith(".tmp_real_mktemp_"))
        finally:
            created_path.unlink(missing_ok=True)

    @unittest.skipIf(sys.platform == "win32", "External cp/mv commands are POSIX-only")
    @patch("subprocess.run")
    def test_copy_or_move_file_or_dir_external(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"

        # 1. Non-sudo copy file (resolve_symlinks=True)
        copy_tree(src, dst, sudo=False, chown=False, resolve_symlinks=True)
        mock_run.assert_any_call(["cp", "-L", src.as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

        # 2. Sudo copy dir (resolve_symlinks=False, chown=True)
        dir_src = self.root / "src_dir"
        dir_src.mkdir()
        copy_tree(dir_src, dst, sudo=True, chown=True, resolve_symlinks=False)
        mock_run.assert_any_call(["sudo", "cp", "-RP", dir_src.as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

        # 3. Move file (sudo=False, resolve_symlinks=False)
        move_tree(src, dst, sudo=False, chown=False, resolve_symlinks=False)
        mock_run.assert_any_call(["mv", src.as_posix(), dst.as_posix()], check=True, capture_output=True, shell=False)

    def test_file_operations_windows_fallback(self) -> None:
        """Verifies that all sudo/external file operations fallback to Python builtins on Windows."""
        src_file = self.root / "win_src.txt"
        src_file.write_text("windows file content", encoding="utf-8")
        dst_file = self.root / "win_dst.txt"
        target_dir = self.root / "win_dir"

        with patch("sys.platform", "win32"), patch("subprocess.run") as mock_run:
            # 1. ensure_dir
            ensure_dir(target_dir, sudo=True)
            self.assertTrue(target_dir.is_dir())
            mock_run.assert_not_called()

            # 2. copy_file
            copy_file(src_file, dst_file, sudo=True)
            self.assertTrue(dst_file.exists())
            self.assertEqual(dst_file.read_text(encoding="utf-8"), "windows file content")
            mock_run.assert_not_called()

            # 3. copy_tree
            dst_copy = self.root / "win_copy.txt"
            copy_tree(src_file, dst_copy, sudo=True)
            self.assertTrue(dst_copy.exists())
            self.assertEqual(dst_copy.read_text(encoding="utf-8"), "windows file content")
            mock_run.assert_not_called()

            # 4. remove
            remove_tree(dst_copy, sudo=True)
            self.assertFalse(dst_copy.exists())
            mock_run.assert_not_called()

            # 5. move_tree on symlinks (resolve_symlinks=True vs False)
            if CAN_SYMLINK:
                # 5a. Relative file symlink moved to deeper directory with resolve_symlinks=True
                rel_sym = self.root / "rel_link.txt"
                rel_sym.symlink_to(Path("win_src.txt"))
                deep_dst = self.root / "backup" / "pkg" / "overwritten" / "rel_link.txt"
                move_tree(rel_sym, deep_dst, resolve_symlinks=True)
                self.assertFalse(rel_sym.exists() or rel_sym.is_symlink())
                self.assertTrue(deep_dst.is_file())
                self.assertFalse(deep_dst.is_symlink())
                self.assertEqual(deep_dst.read_text(encoding="utf-8"), "windows file content")
                # Original target file remains intact
                self.assertTrue(src_file.is_file())

                # 5b. Directory symlink moved with resolve_symlinks=True
                real_dir = self.root / "real_dir_target"
                real_dir.mkdir(exist_ok=True)
                (real_dir / "item.txt").write_text("dir item", encoding="utf-8")
                dir_sym = self.root / "dir_link"
                dir_sym.symlink_to(real_dir, target_is_directory=True)
                moved_dir_dst = self.root / "backup" / "pkg" / "overwritten" / "dir_link"
                move_tree(dir_sym, moved_dir_dst, resolve_symlinks=True)
                self.assertFalse(dir_sym.exists() or dir_sym.is_symlink())
                self.assertTrue(moved_dir_dst.is_dir())
                self.assertFalse(moved_dir_dst.is_symlink())
                self.assertEqual((moved_dir_dst / "item.txt").read_text(encoding="utf-8"), "dir item")
                # Original target directory remains intact
                self.assertTrue((real_dir / "item.txt").is_file())

                # 5c. Broken symlink moved with resolve_symlinks=False preserves raw link target
                broken_sym = self.root / "broken_win_link"
                broken_sym.symlink_to(Path("nonexistent_win_target.txt"))
                broken_dst = self.root / "backup" / "pkg" / "overwritten" / "broken_win_link"
                move_tree(broken_sym, broken_dst, resolve_symlinks=False)
                self.assertFalse(broken_sym.exists() or broken_sym.is_symlink())
                self.assertTrue(broken_dst.is_symlink())
                self.assertEqual(os.readlink(broken_dst), "nonexistent_win_target.txt")

    def test_windows_extended_length_readlink_prefix_stripping(self) -> None:
        """Verifies copy_symlink and _check_symlink_points_to_source strip Windows '\\\\?\\' prefix from os.readlink."""
        from drift.core.folder_delivery import _check_symlink_points_to_source

        src_file = self.root / "real_source.txt"
        src_file.write_text("data", encoding="utf-8")
        fake_link = self.root / "fake_link.txt"
        fake_link.write_text("placeholder", encoding="utf-8")
        dst_link = self.root / "copied_link.txt"

        win_prefixed_target = f"\\\\?\\{src_file.resolve()}"
        real_readlink = os.readlink

        with patch("sys.platform", "win32"), patch(
            "os.readlink",
            side_effect=lambda p: win_prefixed_target if Path(p) == fake_link else real_readlink(p),
        ):
            self.assertTrue(_check_symlink_points_to_source(fake_link, src_file))
            with patch.object(Path, "symlink_to") as mock_symlink_to:
                copy_symlink(fake_link, dst_link)
                mock_symlink_to.assert_called_once_with(str(src_file.resolve()))

    @unittest.skipUnless(CAN_SYMLINK, "Symlink creation requires elevated privileges on Windows")
    def test_copy_symlink(self) -> None:
        # 1. Broken symlink
        src = self.root / "broken_link"
        src.symlink_to("another_non_existent")
        dst = self.root / "dst_link"
        
        copy_symlink(src, dst)
        self.assertTrue(dst.is_symlink())
        self.assertEqual(os.readlink(dst), "another_non_existent")

        # 2. Valid symlink
        target_file = self.root / "target.txt"
        target_file.write_text("content", encoding="utf-8")
        src_valid = self.root / "valid_link"
        src_valid.symlink_to(target_file)
        dst_valid = self.root / "dst_valid_link"

        copy_symlink(src_valid, dst_valid)
        self.assertTrue(dst_valid.is_symlink())
        self.assertEqual(os.readlink(dst_valid).removeprefix("\\\\?\\"), str(target_file))

        # 3. Overwriting existing destination symlink
        copy_symlink(src, dst_valid)
        self.assertTrue(dst_valid.is_symlink())
        self.assertEqual(os.readlink(dst_valid), "another_non_existent")

    def test_expand_user_and_env(self) -> None:
        from drift.utils.path_utils import expand_path

        # 1. Test empty string
        self.assertEqual(expand_path(""), Path("."))

        # 2. Test '~' expansion
        home = Path.home()
        self.assertEqual(expand_path("~"), home)
        self.assertEqual(expand_path("~/my_config"), home / "my_config")

        # 3. Test Windows %VAR% and backslash expansion when platform is win32
        with patch("sys.platform", "win32"):
            self.assertEqual(expand_path(r"~\my_config"), home / "my_config")
            with patch.dict(os.environ, {"CUSTOM_APP_PATH": "/custom/path", "USERPROFILE": "/custom/user"}):
                self.assertEqual(expand_path("%CUSTOM_APP_PATH%/sub"), Path("/custom/path/sub"))
                self.assertEqual(expand_path("%USERPROFILE%/config"), Path("/custom/user/config"))

            # Test Windows %VAR% expansion with fallback dictionary (when APPDATA/LOCALAPPDATA not in os.environ)
            with patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}, clear=True):
                res_appdata = expand_path("%APPDATA%/myapp")
                self.assertEqual(res_appdata, home / "AppData" / "Roaming" / "myapp")

                res_localappdata = expand_path("%LOCALAPPDATA%/myapp")
                self.assertEqual(res_localappdata, home / "AppData" / "Local" / "myapp")

            # Test that expand_path raises RuntimeError when Path.home() fails
            with patch.dict(os.environ, {}, clear=True), patch(
                "pathlib.Path.home", side_effect=RuntimeError("Could not determine home directory.")
            ):
                with self.assertRaises(RuntimeError):
                    expand_path("~/my_config")
                with self.assertRaises(RuntimeError):
                    expand_path("%APPDATA%/myapp")

        # 4. Test non-Windows platform preserves %VAR% literally
        with patch("sys.platform", "linux"):
            self.assertEqual(expand_path("%USERPROFILE%/config"), Path("%USERPROFILE%/config"))

        # 5. Test POSIX $VAR / ${VAR} expansion across platforms
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg/config"}):
            self.assertEqual(expand_path("$XDG_CONFIG_HOME/app"), Path("/xdg/config/app"))
            self.assertEqual(expand_path("${XDG_CONFIG_HOME}/app"), Path("/xdg/config/app"))

    def test_has_admin_privileges_and_run_sudo_command(self) -> None:
        from drift.utils.process_utils import has_admin_privileges, run_command

        # Linux non-root
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000, create=True):
            self.assertFalse(has_admin_privileges())
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                run_command(["echo", "hello"], sudo=True)
                mock_run.assert_called_with(["sudo", "echo", "hello"], check=True, capture_output=True, shell=False)

        # Linux root
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=0, create=True):
            self.assertTrue(has_admin_privileges())
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                run_command(["echo", "hello"], sudo=True)
                mock_run.assert_called_with(["echo", "hello"], check=True, capture_output=True, shell=False)

    def test_assert_can_escalate(self) -> None:
        from drift.utils.process_utils import assert_can_escalate

        # Linux root
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=0, create=True):
            assert_can_escalate()

        # Linux non-root success
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000, create=True):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                assert_can_escalate()
                mock_run.assert_called_with(["sudo", "-v"], check=False)

        # Linux non-root failure
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000, create=True):
            with patch("subprocess.run", return_value=MagicMock(returncode=1)):
                with self.assertRaises(PermissionError):
                    assert_can_escalate()

        # Windows non-admin failure
        with patch("sys.platform", "win32"), patch("drift.utils.process_utils.has_admin_privileges", return_value=False):
            with self.assertRaises(PermissionError):
                assert_can_escalate()

        # Windows admin success
        with patch("sys.platform", "win32"), patch("drift.utils.process_utils.has_admin_privileges", return_value=True):
            assert_can_escalate()

    def test_is_binary_file(self) -> None:
        from drift.utils.file_inspect import is_binary_file

        text_file = self.root / "sample.txt"
        text_file.write_text("Hello world!\nLine 2\n", encoding="utf-8")
        self.assertFalse(is_binary_file(text_file))

        bin_file = self.root / "sample.bin"
        bin_file.write_bytes(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00")
        self.assertTrue(is_binary_file(bin_file))

    def test_normalize_newlines_bytes(self) -> None:
        from drift.utils.file_inspect import normalize_newlines
        from drift.core.constants import LineEnding

        # to CRLF
        raw_lf = b"line1\nline2\nline3"
        self.assertEqual(normalize_newlines(raw_lf, line_ending=LineEnding.CRLF), b"line1\r\nline2\r\nline3")

        # to CRLF idempotent with existing CRLF
        mixed = b"line1\r\nline2\nline3"
        self.assertEqual(normalize_newlines(mixed, line_ending=LineEnding.CRLF), b"line1\r\nline2\r\nline3")

        # to LF
        crlf = b"line1\r\nline2\r\nline3"
        self.assertEqual(normalize_newlines(crlf, line_ending=LineEnding.LF), b"line1\nline2\nline3")

        # PRESERVE
        self.assertEqual(normalize_newlines(raw_lf, line_ending=LineEnding.PRESERVE), raw_lf)

    def test_write_file_contents_with_sudo(self) -> None:
        from drift.utils.file_ops import write_file

        target_file = self.root / "nested" / "dir" / "out.txt"
        write_file(target_file, "text content", sudo=False, permission=0o755)

        self.assertTrue(target_file.is_file())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "text content")
        if sys.platform != "win32":
            self.assertTrue(bool(target_file.stat().st_mode & 0o111))

    def test_atomic_copy_file_with_sudo_crlf_translation(self) -> None:
        from drift.utils.file_ops import copy_file
        from drift.core.constants import LineEnding

        # 1. Text file: LF -> CRLF
        src_text = self.root / "src_text.txt"
        src_text.write_bytes(b"hello\nworld\n")
        dst_crlf = self.root / "dst_crlf.txt"
        copy_file(src_text, dst_crlf, sudo=False, line_ending=LineEnding.CRLF)
        self.assertEqual(dst_crlf.read_bytes(), b"hello\r\nworld\r\n")

        # 2. Text file: CRLF -> LF
        dst_lf = self.root / "dst_lf.txt"
        copy_file(dst_crlf, dst_lf, sudo=False, line_ending=LineEnding.LF)
        self.assertEqual(dst_lf.read_bytes(), b"hello\nworld\n")

        # 3. Binary file: remains byte-identical regardless of line_ending
        src_bin = self.root / "src.bin"
        src_bin.write_bytes(b"data\x00with\nnulls\r\n")
        dst_bin = self.root / "dst.bin"
        copy_file(src_bin, dst_bin, sudo=False, line_ending=LineEnding.CRLF)
        self.assertEqual(dst_bin.read_bytes(), b"data\x00with\nnulls\r\n")

    def test_file_contents_differ_convert_line_endings(self) -> None:
        from drift.utils.file_inspect import contents_differ

        f1 = self.root / "f1.txt"
        f2 = self.root / "f2.txt"

        f1.write_bytes(b"line1\nline2\n")
        f2.write_bytes(b"line1\r\nline2\r\n")

        # convert_line_endings=True: they match
        self.assertFalse(contents_differ(f1, f2, convert_line_endings=True))

        # convert_line_endings=False: they differ
        self.assertTrue(contents_differ(f1, f2, convert_line_endings=False))

    def test_unlock_file_or_dir_if_windows(self) -> None:
        from drift.utils.file_ops import clear_readonly, remove_tree

        # 1. On non-windows: does nothing without errors
        target_file = self.root / "locked_file.txt"
        target_file.write_text("locked", encoding="utf-8")
        target_file.chmod(0o444)
        with patch("sys.platform", "linux"):
            clear_readonly(target_file)

        # 2. On windows: removes read-only attribute
        with patch("sys.platform", "win32"):
            clear_readonly(target_file)
            # Check that write permission is restored
            self.assertTrue(bool(target_file.stat().st_mode & 0o200))

        # 3. On directory with read-only files on windows
        target_dir = self.root / "locked_dir"
        target_dir.mkdir()
        child_file = target_dir / "child.txt"
        child_file.write_text("child", encoding="utf-8")
        child_file.chmod(0o444)

        with patch("sys.platform", "win32"):
            clear_readonly(target_dir)
            self.assertTrue(bool(child_file.stat().st_mode & 0o200))

        # Clean removal
        remove_tree(target_file)
        remove_tree(target_dir)
        self.assertFalse(target_file.exists())
        self.assertFalse(target_dir.exists())

    def test_atomic_copy_file(self) -> None:
        from drift.utils.file_ops import copy_file
        from drift.core.constants import LineEnding
        src = self.root / "source.txt"
        dst = self.root / "dest_dir" / "dest.txt"
        src.write_text("hello atomic copy", encoding="utf-8")

        # 1. Normal atomic copy to new destination
        copy_file(src, dst)
        self.assertTrue(dst.exists())
        self.assertEqual(dst.read_text(encoding="utf-8"), "hello atomic copy")

        # 2. Overwrite existing file atomically
        src.write_text("updated atomic copy", encoding="utf-8")
        copy_file(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "updated atomic copy")

        # 3. Copy with CRLF normalization
        src_crlf = self.root / "crlf.txt"
        src_crlf.write_bytes(b"line1\r\nline2\r\n")
        dst_lf = self.root / "dest_dir" / "dest_lf.txt"
        copy_file(src_crlf, dst_lf, line_ending=LineEnding.LF)
        self.assertEqual(dst_lf.read_bytes(), b"line1\nline2\n")

        # 4. Copy symlink without following
        if CAN_SYMLINK:
            symlink_src = self.root / "sym_src.txt"
            symlink_src.symlink_to(src)
            dst_sym = self.root / "dest_dir" / "dest_sym.txt"
            copy_file(symlink_src, dst_sym, follow_symlinks=False)
            self.assertTrue(dst_sym.is_symlink())
            self.assertEqual(os.readlink(dst_sym).removeprefix("\\\\?\\"), str(src))

    def test_file_permissions_differ_and_mode_only(self) -> None:
        import sys
        from drift.utils.file_inspect import permissions_differ, is_mode_only_change
        from drift.utils.file_ops import copy_permissions, copy_file
        if sys.platform == "win32":
            return

        f1 = self.root / "f1.sh"
        f2 = self.root / "f2.sh"
        f1.write_text("#!/bin/bash\n", encoding="utf-8")
        f2.write_text("#!/bin/bash\n", encoding="utf-8")
        f1.chmod(0o755)
        f2.chmod(0o644)

        self.assertTrue(permissions_differ(f1, f2))
        self.assertTrue(is_mode_only_change(f1, f2))

        # Copy mode directly
        copy_permissions(f1, f2, sudo=False)
        self.assertFalse(permissions_differ(f1, f2))
        self.assertFalse(is_mode_only_change(f1, f2))

        # Test copy_file mode-only update
        f2.chmod(0o644)
        copy_file(f1, f2)
        self.assertTrue(bool(f2.stat().st_mode & 0o111))

        # Test non-executable permission bit difference (e.g. 0o644 vs 0o600)
        f1.chmod(0o644)
        f2.chmod(0o600)
        self.assertTrue(permissions_differ(f1, f2))
        self.assertTrue(is_mode_only_change(f1, f2))

    def test_run_command_debug_logging(self) -> None:
        """Verifies run_command logs external command and stdout/stderr in debug mode."""
        import sys
        from drift.utils.process_utils import run_command

        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                with self.assertLogs("drift.utils.process_utils", level="DEBUG") as cm: 
                    run_command([sys.executable, "-c", "import sys; sys.stdout.write('hello out\\n'); sys.stderr.write('hello err\\n')"])

            logs = "\n".join(cm.output)
            self.assertIn("[External] ", logs)
            self.assertIn("stdout:\nhello out", logs)
            self.assertIn("stderr:\nhello err", logs)
        finally:
            set_test_mode(True, enable_logging=False)


    def test_is_editor_or_os_temporary_file(self) -> None:
        """Verifies is_temp_file correctly matches temporary files and ignores normal files."""
        from drift.utils.file_inspect import is_temp_file

        # Temporary / editor / OS files
        self.assertTrue(is_temp_file(".gitignore"))
        self.assertTrue(is_temp_file("#file.txt#"))
        self.assertTrue(is_temp_file(".#file.txt"))
        self.assertTrue(is_temp_file("file.txt~"))
        self.assertTrue(is_temp_file(".file.txt.swp"))
        self.assertTrue(is_temp_file(".file.txt.swo"))
        self.assertTrue(is_temp_file(".file.txt.swa"))
        self.assertTrue(is_temp_file(".file.txt.un~"))
        self.assertTrue(is_temp_file(".DS_Store"))
        self.assertTrue(is_temp_file("Thumbs.db"))
        self.assertTrue(is_temp_file("/path/to/nested/#emacs_save#"))

        # Normal and configuration files
        self.assertFalse(is_temp_file("drift_package.toml"))
        self.assertFalse(is_temp_file("drift_package.local.toml"))
        self.assertFalse(is_temp_file(".drift_ignore"))
        self.assertFalse(is_temp_file("file.txt"))
        self.assertFalse(is_temp_file("main.py"))
        self.assertFalse(is_temp_file("README.md"))
        self.assertFalse(is_temp_file("swp_file.py"))

    def test_copy_file_and_file_ops_raise_on_directory_target(self) -> None:
        """Verifies that copy_file, create_symlink, copy_symlink, and write_file raise IsADirectoryError rather than deleting directories."""
        from drift.utils.file_ops import copy_file, create_symlink, copy_symlink, write_file

        src_file = self.root / "sample_src.txt"
        src_file.write_text("sample content", encoding="utf-8")

        dst_dir = self.root / "blocking_dir"
        dst_dir.mkdir(parents=True, exist_ok=True)
        canary = dst_dir / "canary.txt"
        canary.write_text("preserve me", encoding="utf-8")

        # 1. copy_file to existing directory dst raises IsADirectoryError
        with self.assertRaises(IsADirectoryError):
            copy_file(src_file, dst_dir)
        self.assertTrue(dst_dir.is_dir())
        self.assertTrue(canary.is_file())

        # 2. copy_file from existing directory src raises IsADirectoryError
        nonexistent_dst = self.root / "new_file.txt"
        with self.assertRaises(IsADirectoryError):
            copy_file(dst_dir, nonexistent_dst)
        self.assertFalse(nonexistent_dst.exists())

        # 3. create_symlink to existing directory dst raises IsADirectoryError
        with self.assertRaises(IsADirectoryError):
            create_symlink(src_file, dst_dir)
        self.assertTrue(dst_dir.is_dir())
        self.assertTrue(canary.is_file())

        # 4. copy_symlink to existing directory dst raises IsADirectoryError
        if CAN_SYMLINK:
            sym_src = self.root / "sample_symlink"
            sym_src.symlink_to(src_file)
            with self.assertRaises(IsADirectoryError):
                copy_symlink(sym_src, dst_dir)
            self.assertTrue(dst_dir.is_dir())
            self.assertTrue(canary.is_file())

        # 5. write_file to existing directory dst raises IsADirectoryError
        with self.assertRaises(IsADirectoryError):
            write_file(dst_dir, "new content")
        self.assertTrue(dst_dir.is_dir())
        self.assertTrue(canary.is_file())

    def test_remove_file_or_empty_dir_behavior(self) -> None:
        # Non-existent path returns False
        non_existent = self.root / "does_not_exist.txt"
        self.assertFalse(remove_file_or_empty_dir(non_existent))

        # Regular file removal returns True
        f = self.root / "regular.txt"
        f.write_text("hello", encoding="utf-8")
        self.assertTrue(remove_file_or_empty_dir(f))
        self.assertFalse(f.exists())

        # Empty directory removal returns True
        empty_d = self.root / "empty_dir"
        empty_d.mkdir()
        self.assertTrue(remove_file_or_empty_dir(empty_d))
        self.assertFalse(empty_d.exists())

        # Non-empty directory is preserved and returns False
        non_empty_d = self.root / "non_empty_dir"
        non_empty_d.mkdir()
        child = non_empty_d / "child.txt"
        child.write_text("protected", encoding="utf-8")
        self.assertFalse(remove_file_or_empty_dir(non_empty_d))
        self.assertTrue(non_empty_d.is_dir())
        self.assertTrue(child.is_file())

        if CAN_SYMLINK:
            # Symlink to file: unlinks symlink, target intact, returns True
            sym_file = self.root / "sym_to_child"
            sym_file.symlink_to(child)
            self.assertTrue(remove_file_or_empty_dir(sym_file))
            self.assertFalse(sym_file.is_symlink())
            self.assertTrue(child.is_file())

            # Symlink to directory: unlinks symlink, directory intact, returns True
            sym_dir = self.root / "sym_to_dir"
            sym_dir.symlink_to(non_empty_d)
            self.assertTrue(remove_file_or_empty_dir(sym_dir))
            self.assertFalse(sym_dir.is_symlink())
            self.assertTrue(non_empty_d.is_dir())

            # Broken symlink returns True
            broken_sym = self.root / "broken_link"
            broken_sym.symlink_to(self.root / "missing_target")
            self.assertTrue(remove_file_or_empty_dir(broken_sym))
            self.assertFalse(broken_sym.is_symlink())

    @unittest.skipIf(sys.platform == "win32", "Sudo is POSIX-only")
    @patch("subprocess.run")
    def test_remove_file_or_empty_dir_with_sudo(self, mock_run) -> None:
        # File under sudo -> sudo rm -f
        f = self.root / "sudo_file.txt"
        f.touch()
        self.assertTrue(remove_file_or_empty_dir(f, sudo=True))
        mock_run.assert_called_with(["sudo", "rm", "-f", f.as_posix()], check=True, capture_output=True, shell=False)

        mock_run.reset_mock()
        # Empty dir under sudo -> sudo rmdir
        d = self.root / "sudo_empty_dir"
        d.mkdir()
        self.assertTrue(remove_file_or_empty_dir(d, sudo=True))
        mock_run.assert_called_with(["sudo", "rmdir", d.as_posix()], check=True, capture_output=True, shell=False)

    def test_remove_tree_recursive(self) -> None:
        # remove_tree recursively removes directory with children
        tree_dir = self.root / "tree_dir"
        sub_dir = tree_dir / "sub"
        sub_dir.mkdir(parents=True)
        (sub_dir / "nested.txt").write_text("data", encoding="utf-8")
        remove_tree(tree_dir)
        self.assertFalse(tree_dir.exists())

    def test_patch_no_symlink_for_tests(self) -> None:
        """Verifies _patch_no_symlink_for_tests disables os.symlink, Path.symlink_to, and check_can_symlink when DRIFT_TEST_NO_SYMLINK=1."""
        from tests import _patch_no_symlink_for_tests

        orig_os_symlink = os.symlink
        orig_path_symlink_to = Path.symlink_to
        try:
            with patch.dict(os.environ, {"DRIFT_TEST_NO_SYMLINK": "1"}):
                _patch_no_symlink_for_tests()
                self.assertFalse(check_can_symlink())
                with self.assertRaises(OSError) as ctx1:
                    os.symlink("a", "b")
                self.assertEqual(ctx1.exception.errno, 1314)
                with self.assertRaises(OSError) as ctx2:
                    (self.root / "dst").symlink_to(self.root / "src")
                self.assertEqual(ctx2.exception.errno, 1314)
        finally:
            os.symlink = orig_os_symlink
            Path.symlink_to = orig_path_symlink_to  # type: ignore[method-assign]
            check_can_symlink.cache_clear()

    def test_backup_file_action_non_optional_fields_and_serialization(self) -> None:
        """Verifies FileAction, NormalFileAction, and BackupFileAction type hierarchy and JSON serialization."""
        from drift.core.file_action import FileAction, NormalFileAction, BackupFileAction, FileActionType

        normal_action = NormalFileAction(
            action_type=FileActionType.ENSURE_DIR,
            dst_path=self.root / "dir",
        )
        self.assertIsInstance(normal_action, FileAction)
        self.assertIsInstance(normal_action, NormalFileAction)
        self.assertFalse(isinstance(normal_action, BackupFileAction))

        backup_action = BackupFileAction(
            action_type=FileActionType.BACKUP_OVERWRITE,
            src_path=self.root / "target" / "file.txt",
            dst_path=self.root / "backup" / "pkg" / "overwritten" / "file.txt",
            reason="Colliding regular file",
            backup_pkg_root=self.root / "backup" / "pkg",
        )
        self.assertIsInstance(backup_action, FileAction)
        self.assertFalse(isinstance(backup_action, NormalFileAction))
        self.assertIsInstance(backup_action, BackupFileAction)

        # Round-trip serialization for NormalFileAction and BackupFileAction
        normal_serialized = normal_action.to_dict()
        self.assertEqual(NormalFileAction.from_dict(normal_serialized), normal_action)
        self.assertEqual(FileAction.from_dict(normal_serialized), normal_action)
        self.assertIsInstance(FileAction.from_dict(normal_serialized), NormalFileAction)

        serialized = backup_action.to_dict()
        self.assertEqual(serialized["action_type"], "BACKUP_OVERWRITE")
        self.assertEqual(serialized["reason"], "Colliding regular file")
        self.assertEqual(Path(serialized["backup_pkg_root"]), self.root / "backup" / "pkg")

        restored = BackupFileAction.from_dict(serialized)
        self.assertEqual(restored, backup_action)
        self.assertEqual(restored.backup_pkg_root, self.root / "backup" / "pkg")
        self.assertEqual(FileAction.from_dict(serialized), backup_action)
        self.assertIsInstance(FileAction.from_dict(serialized), BackupFileAction)

    def test_backup_item_and_wal_stashing(self) -> None:
        """Verifies backup_item moves existing backup_dest into wal/stash/ via rebase_path and preserves earliest stash."""
        from drift.core.file_action import restore_wal_stash

        backup_pkg_root = self.root / "backup" / "pkg_stash"
        backup_dest_overwritten = backup_pkg_root / "overwritten" / "nested" / "app.conf"
        stash_dest_overwritten = backup_pkg_root / "wal" / "stash" / "overwritten" / "nested" / "app.conf"

        # 1. Cold backup: no existing backup at backup_dest -> no stash created
        src1 = self.root / "host_v1.conf"
        src1.write_text("original host v1", encoding="utf-8")
        backup_item(src1, backup_dest_overwritten, backup_pkg_root=backup_pkg_root, sudo=False)
        self.assertFalse(src1.exists())
        self.assertEqual(backup_dest_overwritten.read_text(encoding="utf-8"), "original host v1")
        self.assertFalse(stash_dest_overwritten.exists())

        # 2. Second backup to same backup_dest: existing v1 is stashed to wal/stash/overwritten/nested/app.conf
        src2 = self.root / "host_v2.conf"
        src2.write_text("intermediate host v2", encoding="utf-8")
        backup_item(src2, backup_dest_overwritten, backup_pkg_root=backup_pkg_root, sudo=False)
        self.assertFalse(src2.exists())
        self.assertEqual(backup_dest_overwritten.read_text(encoding="utf-8"), "intermediate host v2")
        self.assertTrue(stash_dest_overwritten.is_file())
        self.assertEqual(stash_dest_overwritten.read_text(encoding="utf-8"), "original host v1")

        # 3. Third backup in same uncommitted transaction: earliest stash (v1) is preserved, not overwritten by v2
        src3 = self.root / "host_v3.conf"
        src3.write_text("latest host v3", encoding="utf-8")
        backup_item(src3, backup_dest_overwritten, backup_pkg_root=backup_pkg_root, sudo=False)
        self.assertEqual(backup_dest_overwritten.read_text(encoding="utf-8"), "latest host v3")
        self.assertEqual(stash_dest_overwritten.read_text(encoding="utf-8"), "original host v1")

        # 4. Also test stashing in deleted/ subfolder
        backup_dest_deleted = backup_pkg_root / "deleted" / "old.txt"
        stash_dest_deleted = backup_pkg_root / "wal" / "stash" / "deleted" / "old.txt"
        del_v1 = self.root / "del_v1.txt"
        del_v1.write_text("deleted v1", encoding="utf-8")
        backup_item(del_v1, backup_dest_deleted, backup_pkg_root=backup_pkg_root, sudo=False)

        del_v2 = self.root / "del_v2.txt"
        del_v2.write_text("deleted v2", encoding="utf-8")
        backup_item(del_v2, backup_dest_deleted, backup_pkg_root=backup_pkg_root, sudo=False)
        self.assertEqual(backup_dest_deleted.read_text(encoding="utf-8"), "deleted v2")
        self.assertEqual(stash_dest_deleted.read_text(encoding="utf-8"), "deleted v1")

        # 5. Restore WAL stash: restores v1 to both overwritten/ and deleted/, cleans up wal/stash/
        restore_wal_stash(backup_pkg_root, sudo=False)
        self.assertEqual(backup_dest_overwritten.read_text(encoding="utf-8"), "original host v1")
        self.assertEqual(backup_dest_deleted.read_text(encoding="utf-8"), "deleted v1")
        self.assertFalse((backup_pkg_root / "wal" / "stash").exists())

    def test_wal_recording_and_rollback_package_wal(self) -> None:
        """Verifies WAL recording of reversible actions (including newly created ENSURE_DIR chains) and LIFO reversal."""
        from drift.core.file_action import (
            FileActionExecutionContext,
            FileActionType,
            NormalFileAction,
            BackupFileAction,
            execute_delivery_actions,
            load_wal_actions,
            rollback_package_wal,
        )

        target_dir = self.root / "target"
        pre_existing_dir = target_dir / "existing_parent"
        pre_existing_dir.mkdir(parents=True, exist_ok=True)

        pkg_src_dir = self.root / "pkg_src"
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        src_copy = pkg_src_dir / "copied.txt"
        src_copy.write_text("pkg copied content", encoding="utf-8")

        backup_pkg_root = self.root / "backup" / "pkg_wal"
        wal_dir = backup_pkg_root / "wal"

        # Pre-existing backup in overwritten/ that will be overwritten during this transaction
        prior_backup = backup_pkg_root / "overwritten" / "collide.txt"
        prior_backup.parent.mkdir(parents=True, exist_ok=True)
        prior_backup.write_text("earliest backup before transaction", encoding="utf-8")

        # Host colliding file and orphan file
        host_collide = target_dir / "collide.txt"
        host_collide.write_text("host file at start of transaction", encoding="utf-8")
        host_orphan = target_dir / "orphan.txt"
        host_orphan.write_text("orphan file at start of transaction", encoding="utf-8")

        new_dir_chain_leaf = pre_existing_dir / "new_level1" / "new_level2"
        copied_target = new_dir_chain_leaf / "copied.txt"
        keep_target = new_dir_chain_leaf / ".drift_keep"

        actions = [
            # Pre-existing dir -> should NOT record in WAL
            NormalFileAction(action_type=FileActionType.ENSURE_DIR, dst_path=pre_existing_dir),
            # Newly created 2-level dir chain -> should record new_level1 then new_level2 in WAL
            NormalFileAction(action_type=FileActionType.ENSURE_DIR, dst_path=new_dir_chain_leaf),
            # Backup overwrite (also triggers stash of prior_backup)
            BackupFileAction(
                action_type=FileActionType.BACKUP_OVERWRITE,
                src_path=host_collide,
                dst_path=prior_backup,
                reason="Colliding regular file",
                backup_pkg_root=backup_pkg_root,
            ),
            # Backup prune
            BackupFileAction(
                action_type=FileActionType.BACKUP_PRUNE,
                src_path=host_orphan,
                dst_path=backup_pkg_root / "deleted" / "orphan.txt",
                reason="Orphan prune",
                backup_pkg_root=backup_pkg_root,
            ),
            # Create copy and keep file
            NormalFileAction(action_type=FileActionType.CREATE_COPY, src_path=src_copy, dst_path=copied_target),
            NormalFileAction(action_type=FileActionType.CREATE_KEEP_FILE, dst_path=keep_target),
        ]

        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=wal_dir)
        execute_delivery_actions(exec_ctx, actions)

        # Verify WAL recorded 6 actions as 6 JSON lines (new_level1, new_level2, BACKUP_OVERWRITE, BACKUP_PRUNE, CREATE_COPY, CREATE_KEEP_FILE)
        wal_file = wal_dir / "actions.json"
        raw_lines = [line for line in wal_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(raw_lines), 6)
        recorded = load_wal_actions(wal_file)
        self.assertEqual(len(recorded), 6)
        self.assertEqual(recorded[0].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(recorded[0].dst_path, pre_existing_dir / "new_level1")
        self.assertEqual(recorded[1].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(recorded[1].dst_path, new_dir_chain_leaf)

        # Verify intermediate state before rollback
        self.assertTrue(copied_target.is_file())
        self.assertTrue(keep_target.is_file())
        self.assertFalse(host_collide.exists())
        self.assertFalse(host_orphan.exists())
        self.assertEqual(prior_backup.read_text(encoding="utf-8"), "host file at start of transaction")
        self.assertTrue((backup_pkg_root / "wal" / "stash" / "overwritten" / "collide.txt").is_file())

        # Execute WAL rollback
        reverted = rollback_package_wal(backup_pkg_root, sudo=False)
        self.assertTrue(reverted)

        # Verify all mutations were cleanly reversed in LIFO order:
        self.assertFalse(copied_target.exists())
        self.assertFalse(keep_target.exists())
        self.assertFalse(new_dir_chain_leaf.exists())
        self.assertFalse((pre_existing_dir / "new_level1").exists())
        # Pre-existing ancestor directory must remain untouched
        self.assertTrue(pre_existing_dir.is_dir())
        # Backed up files restored to host
        self.assertEqual(host_collide.read_text(encoding="utf-8"), "host file at start of transaction")
        self.assertEqual(host_orphan.read_text(encoding="utf-8"), "orphan file at start of transaction")
        # Stashed prior backup restored to backup_pkg_root/overwritten/collide.txt
        self.assertEqual(prior_backup.read_text(encoding="utf-8"), "earliest backup before transaction")
        # WAL directory cleaned up
        self.assertFalse(wal_dir.exists())

    def test_append_wal_actions_append_only_and_malformed_line_resilience(self) -> None:
        """Verifies _append_wal_actions strictly appends without rewriting prior content and load_wal_actions skips malformed lines."""
        from drift.core.file_action import (
            BackupFileAction,
            FileActionType,
            NormalFileAction,
            _append_wal_actions,
            load_wal_actions,
        )

        wal_dir = self.root / "backup" / "pkg_append" / "wal"
        wal_file = wal_dir / "actions.json"

        # 1. Empty actions sequence is a no-op
        _append_wal_actions(wal_dir, [])
        self.assertFalse(wal_dir.exists())

        # 2. First append creates wal_dir and writes 1 JSON line
        action1 = NormalFileAction(action_type=FileActionType.ENSURE_DIR, dst_path=self.root / "d1")
        _append_wal_actions(wal_dir, [action1])
        self.assertTrue(wal_file.is_file())

        # 3. Simulate a corrupted/malformed line in the middle, then append a second valid action
        #    If _append_wal_actions tried to parse and rewrite the whole file, action1 would be lost!
        with wal_file.open("a", encoding="utf-8") as f:
            f.write("{corrupted_json_line\n\n")

        action2 = BackupFileAction(
            action_type=FileActionType.BACKUP_OVERWRITE,
            src_path=self.root / "src_f",
            dst_path=self.root / "backup" / "pkg_append" / "overwritten" / "src_f",
            reason="Colliding file",
            backup_pkg_root=self.root / "backup" / "pkg_append",
        )
        _append_wal_actions(wal_dir, [action2])

        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                with self.assertLogs("drift.core.file_action", level="WARNING") as cm:
                    loaded = load_wal_actions(wal_file)
            self.assertEqual(loaded, [action1, action2])
            self.assertTrue(any("Skipping malformed WAL entry" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_rollback_package_wal_missing_file_logs_warning_and_skips(self) -> None:
        """Verifies rollback_package_wal logs a warning and returns False when wal/actions.json does not exist."""
        from drift.core.file_action import rollback_package_wal

        missing_pkg_backup = self.root / "backup" / "nonexistent_pkg"
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                with self.assertLogs("drift.core.file_action", level="WARNING") as cm:
                    reverted = rollback_package_wal(missing_pkg_backup, sudo=False)
            self.assertFalse(reverted)
            self.assertTrue(any("WAL file not found" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)


@unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
class TestWriteFilePermissions(unittest.TestCase):
    """Locks down write_file permission semantics (explicit / preserve existing / umask default)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.old_umask = os.umask(0o022)

    def tearDown(self) -> None:
        os.umask(self.old_umask)
        self.temp_dir.cleanup()

    @staticmethod
    def _mode(path: Path) -> int:
        import stat
        return stat.S_IMODE(path.stat().st_mode)

    def test_new_file_uses_umask_default_not_0600(self) -> None:
        from drift.utils.file_ops import write_file
        for umask, expected in ((0o022, 0o644), (0o077, 0o600), (0o002, 0o664), (0o000, 0o666)):
            with self.subTest(umask=oct(umask)):
                os.umask(umask)
                dst = self.root / f"new_{umask:o}.txt"
                write_file(dst, "x")
                self.assertEqual(self._mode(dst), expected)

    def test_existing_file_mode_preserved_when_permission_none(self) -> None:
        from drift.utils.file_ops import write_file
        for mode in (0o600, 0o640, 0o644, 0o755, 0o444):
            with self.subTest(mode=oct(mode)):
                dst = self.root / f"existing_{mode:o}.txt"
                dst.write_text("old", encoding="utf-8")
                dst.chmod(mode)
                write_file(dst, "new")
                self.assertEqual(dst.read_text(encoding="utf-8"), "new")
                self.assertEqual(self._mode(dst), mode)

    def test_explicit_permission_wins_for_new_and_existing(self) -> None:
        from drift.utils.file_ops import write_file
        new_dst = self.root / "explicit_new.txt"
        write_file(new_dst, "x", permission=0o700)
        self.assertEqual(self._mode(new_dst), 0o700)

        existing = self.root / "explicit_existing.txt"
        existing.write_text("old", encoding="utf-8")
        existing.chmod(0o644)
        write_file(existing, "x", permission=0o600)
        self.assertEqual(self._mode(existing), 0o600)

    def test_symlink_destination_is_replaced_without_inheriting_target_mode(self) -> None:
        from drift.utils.file_ops import write_file
        target = self.root / "target.txt"
        target.write_text("target", encoding="utf-8")
        target.chmod(0o700)
        link = self.root / "link.txt"
        link.symlink_to(target)

        write_file(link, "replaced")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(encoding="utf-8"), "replaced")
        self.assertEqual(self._mode(link), 0o644)
        # Symlink target is untouched
        self.assertEqual(target.read_text(encoding="utf-8"), "target")
        self.assertEqual(self._mode(target), 0o700)

    def test_two_pass_rewrite_keeps_mode_and_leaves_no_temp_files(self) -> None:
        from drift.utils.file_ops import write_file
        dst = self.root / "two_pass.txt"
        write_file(dst, "a")
        dst.chmod(0o640)
        write_file(dst, "a")
        write_file(dst, b"b")
        self.assertEqual(dst.read_bytes(), b"b")
        self.assertEqual(self._mode(dst), 0o640)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["two_pass.txt"])

    def test_str_and_bytes_content_preserved_verbatim(self) -> None:
        from drift.utils.file_ops import write_file
        dst = self.root / "crlf.txt"
        write_file(dst, "a\r\nb\r\n")
        self.assertEqual(dst.read_bytes(), b"a\r\nb\r\n")
        write_file(dst, "é".encode("utf-8"))
        self.assertEqual(dst.read_bytes(), "é".encode("utf-8"))

    def test_failed_write_cleans_temp_and_keeps_original(self) -> None:
        from drift.utils import file_ops
        dst = self.root / "keep.txt"
        dst.write_text("original", encoding="utf-8")
        with patch.object(file_ops.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                file_ops.write_file(dst, "new")
        self.assertEqual(dst.read_text(encoding="utf-8"), "original")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["keep.txt"])

    def test_umask_restored_after_write(self) -> None:
        from drift.utils.file_ops import write_file
        os.umask(0o027)
        write_file(self.root / "u.txt", "x")
        current = os.umask(0o027)
        self.assertEqual(current, 0o027)

    def test_resolve_write_permission_handles_lstat_permission_error(self) -> None:
        """If dst.lstat() raises PermissionError (e.g. root-only parent dir before sudo mv), falls back to None."""
        from drift.utils.file_ops import _resolve_write_permission
        dst = self.root / "restricted.txt"
        with patch.object(Path, "lstat", side_effect=PermissionError("EACCES")):
            self.assertIsNone(_resolve_write_permission(dst, None))
            self.assertEqual(_resolve_write_permission(dst, 0o600), 0o600)

    def test_as_posix_and_as_windows(self) -> None:
        """Verifies explicit POSIX and Windows path string conversion helpers."""
        from pathlib import PurePosixPath, PureWindowsPath
        from drift.utils.path_utils import as_posix, as_windows

        self.assertEqual(as_posix(Path("a/b/c.txt")), "a/b/c.txt")
        self.assertEqual(as_posix(PurePosixPath("/opt/app/run.sh")), "/opt/app/run.sh")
        self.assertEqual(as_posix(PureWindowsPath(r"C:\scripts\install.sh")), "C:/scripts/install.sh")
        self.assertEqual(as_posix("/already/posix"), "/already/posix")

        self.assertEqual(as_windows(Path("a/b/c.txt")), r"a\b\c.txt")
        self.assertEqual(as_windows(PureWindowsPath(r"C:\scripts\install.ps1")), r"C:\scripts\install.ps1")
        self.assertEqual(as_windows("C:/scripts/install.ps1"), r"C:\scripts\install.ps1")
        self.assertEqual(as_windows(r"C:\scripts\install.bat"), r"C:\scripts\install.bat")


if __name__ == "__main__":
    unittest.main()

