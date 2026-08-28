"""Tests for file_utils operations."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from drift.file_utils import (
    is_relative_to,
    resolve_system_target,
    translate_dot_prefixes,
    translate_dot_prefixes_reverse,
    tree_relative_files,
    get_relative_path,
    compute_file_hash,
    file_contents_differ,
    rmdir_parents,
    get_symlinked_parent,
    backup_and_delete_one_file,
    copy_or_move_file_or_dir_external,
    ensure_directory_writable,
    ensure_dir_exists_with_sudo,
    remove_file_or_dir_with_sudo,
    create_symlink_manually_with_sudo,
    copy_file_contents_with_sudo,
    sync_broken_symlink,
)
from drift.sync_ops import (
    backup_file_or_dir_external,
    reverse_sync_file_or_dir,
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
        self.assertFalse(is_relative_to(self.root, path))

    def test_resolve_system_target(self) -> None:
        base = self.root / "target"
        rel_path1 = Path("dot-config/nvim/init.lua")
        rel_path2 = Path("normal_dir/file.txt")

        res1 = resolve_system_target(rel_path1, base)
        res2 = resolve_system_target(rel_path2, base)

        self.assertEqual(res1, base / ".config" / "nvim" / "init.lua")
        self.assertEqual(res2, base / "normal_dir" / "file.txt")

    def test_translate_dot_prefixes(self) -> None:
        """Verifies translate_dot_prefixes converts 'dot-' to leading '.', skips 'dot-'/'dot-.',
        and only translates segments that start with 'dot-'.
        """
        self.assertEqual(translate_dot_prefixes(Path("dot-bashrc")), Path(".bashrc"))
        self.assertEqual(translate_dot_prefixes(Path("dot-config/nvim/init.lua")), Path(".config/nvim/init.lua"))
        self.assertEqual(translate_dot_prefixes(Path("dot-config/dot-vimrc")), Path(".config/.vimrc"))
        self.assertEqual(translate_dot_prefixes(Path("normal_dir/file.txt")), Path("normal_dir/file.txt"))

        # Segments 'dot-' and 'dot-.' are preserved (not translated to '.' or '..')
        self.assertEqual(translate_dot_prefixes(Path("dot-")), Path("dot-"))
        self.assertEqual(translate_dot_prefixes(Path("dot-.")), Path("dot-."))
        self.assertEqual(translate_dot_prefixes(Path("config/dot-/file.txt")), Path("config/dot-/file.txt"))
        self.assertEqual(translate_dot_prefixes(Path("config/dot-./file.txt")), Path("config/dot-./file.txt"))

        # Segments starting with 'dot-'
        self.assertEqual(translate_dot_prefixes(Path("dot--foo")), Path(".-foo"))
        self.assertEqual(translate_dot_prefixes(Path("dot-.bar")), Path("..bar"))

    def test_translate_dot_prefixes_reverse(self) -> None:
        """Verifies translate_dot_prefixes_reverse converts leading '.' to 'dot-',
        and never translates '.' or '..' segments.
        """
        self.assertEqual(translate_dot_prefixes_reverse(Path(".bashrc")), Path("dot-bashrc"))
        self.assertEqual(translate_dot_prefixes_reverse(Path(".config/nvim/init.lua")), Path("dot-config/nvim/init.lua"))
        self.assertEqual(translate_dot_prefixes_reverse(Path(".config/.vimrc")), Path("dot-config/dot-vimrc"))
        self.assertEqual(translate_dot_prefixes_reverse(Path("normal_dir/file.txt")), Path("normal_dir/file.txt"))

        # '.' and '..' segments are preserved and never converted to 'dot-'
        self.assertEqual(translate_dot_prefixes_reverse(Path(".")), Path("."))
        self.assertEqual(translate_dot_prefixes_reverse(Path("..")), Path(".."))
        self.assertEqual(translate_dot_prefixes_reverse(Path("config/./file.txt")), Path("config/file.txt"))

        # Other leading dot names
        self.assertEqual(translate_dot_prefixes_reverse(Path("..bar")), Path("dot-.bar"))
        self.assertEqual(translate_dot_prefixes_reverse(Path(".-foo")), Path("dot--foo"))

    def test_tree_relative_files(self) -> None:
        subdir = self.root / "subdir"
        subdir.mkdir()
        file1 = subdir / "file1.txt"
        file1.touch()
        subsubdir = subdir / "nested"
        subsubdir.mkdir()
        file2 = subsubdir / "file2.txt"
        file2.touch()

        files = tree_relative_files(subdir)
        self.assertEqual(files, [Path("file1.txt"), Path("nested/file2.txt")])

        # Test non-existent dir
        self.assertEqual(tree_relative_files(self.root / "nonexistent"), [])

    def test_get_relative_path(self) -> None:
        dir1 = self.root / "a" / "b" / "c"
        dir2 = self.root / "a" / "d" / "e"

        # Resolve them so resolve() can work
        dir1.mkdir(parents=True, exist_ok=True)
        dir2.mkdir(parents=True, exist_ok=True)

        rel = get_relative_path(dir1, dir2)
        self.assertEqual(rel, Path("../../d/e"))

    def test_compute_file_hash_and_differ(self) -> None:
        file1 = self.root / "file1.txt"
        file2 = self.root / "file2.txt"
        file3 = self.root / "file3.txt"

        file1.write_text("hello", encoding="utf-8")
        file2.write_text("hello", encoding="utf-8")
        file3.write_text("world", encoding="utf-8")

        hash1 = compute_file_hash(file1)
        hash2 = compute_file_hash(file2)
        hash3 = compute_file_hash(file3)

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash3)

        self.assertFalse(file_contents_differ(file1, file2))
        self.assertTrue(file_contents_differ(file1, file3))

        # Edge cases:
        # 1. Both files do not exist
        non_existent1 = self.root / "non_existent1.txt"
        non_existent2 = self.root / "non_existent2.txt"
        self.assertFalse(file_contents_differ(non_existent1, non_existent2))

        # 2. One file exists, the other doesn't
        self.assertTrue(file_contents_differ(file1, non_existent1))
        self.assertTrue(file_contents_differ(non_existent1, file1))

        # 3. Same resolved path
        self.assertFalse(file_contents_differ(file1, file1))

        # 4. Non-file path (e.g., directory) raises ValueError
        dir_path = self.root / "some_directory"
        dir_path.mkdir()
        with self.assertRaises(ValueError):
            file_contents_differ(file1, dir_path)

        # 5. Different file sizes
        file_large = self.root / "large.txt"
        file_large.write_text("hello world long text", encoding="utf-8")
        self.assertTrue(file_contents_differ(file1, file_large))

    def test_rmdir_parents(self) -> None:
        nested = self.root / "a" / "b" / "c"
        nested.mkdir(parents=True)
        file_path = nested / "file.txt"
        file_path.touch()

        # It won't remove since it's not empty
        rmdir_parents(nested, self.root)
        self.assertTrue(nested.exists())

        # Delete the file
        file_path.unlink()

        # Run rmdir_parents
        rmdir_parents(nested, self.root)

        # nested "a/b/c" and "a/b" and "a" should be cleaned up
        self.assertFalse((self.root / "a").exists())

    def test_get_symlinked_parent(self) -> None:
        drift_root = self.root / "drift"
        drift_root.mkdir()
        src_dir = drift_root / "src_pkg"
        src_dir.mkdir()
        (src_dir / "file.txt").touch()

        # Symlink target range is drift_root
        target_dir = self.root / "target_dir"
        target_dir.mkdir()
        symlink_dir = target_dir / "nested_app"
        
        # Link nested_app -> drift/src_pkg
        symlink_dir.symlink_to(src_dir)

        # File is nested_app/file.txt
        file_path = symlink_dir / "file.txt"

        parent_symlink = get_symlinked_parent(file_path, drift_root)
        self.assertEqual(parent_symlink, symlink_dir)

        # 1. file_path itself is a symlink pointing into link_target_range -> returns file_path
        file_symlink = target_dir / "direct_symlink"
        file_symlink.symlink_to(src_dir / "file.txt")
        self.assertEqual(get_symlinked_parent(file_symlink, drift_root), file_symlink)

        # 2. No symlinked parent -> returns None
        normal_file = target_dir / "normal_file.txt"
        normal_file.touch()
        self.assertIsNone(get_symlinked_parent(normal_file, drift_root))

        # 3. Parent is a symlink pointing OUTSIDE of link_target_range -> returns None
        external_dir = self.root / "external_dir"
        external_dir.mkdir()
        external_symlink = target_dir / "external_symlink"
        external_symlink.symlink_to(external_dir)
        nested_file_external = external_symlink / "some_file.txt"
        self.assertIsNone(get_symlinked_parent(nested_file_external, drift_root))

    def test_backup_and_delete_one_file(self) -> None:
        file_path = self.root / "a" / "b" / "file.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("original content", encoding="utf-8")

        backup_path = self.root / "backup" / "file.txt"
        backup_and_delete_one_file(file_path, backup_path, limit_dir=self.root)

        self.assertFalse(file_path.exists())
        self.assertFalse((self.root / "a").exists())  # Empty parent cleaned up
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_text(encoding="utf-8"), "original content")

    def test_backup_and_delete_one_file_overwrites(self) -> None:
        # Verify that backup_and_delete_one_file overwrites existing file/dir at backup_dest
        file_path = self.root / "a" / "b" / "file.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("fresh content", encoding="utf-8")

        # Pre-create conflicting file at backup destination
        backup_path = self.root / "backup" / "file.txt"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text("old stale conflict", encoding="utf-8")

        backup_and_delete_one_file(file_path, backup_path, limit_dir=self.root)

        self.assertFalse(file_path.exists())
        self.assertTrue(backup_path.exists())
        # Confirms it was overwritten!
        self.assertEqual(backup_path.read_text(encoding="utf-8"), "fresh content")

    def test_ensure_directory_writable(self) -> None:
        writable_dir = self.root / "writable"
        writable_dir.mkdir()
        
        # Should complete gracefully
        ensure_directory_writable(writable_dir, sudo=False)
        ensure_directory_writable(writable_dir, sudo=True)

        # Non-existent dir resolves closest parent
        ensure_directory_writable(writable_dir / "nonexistent" / "subdir", sudo=False)

    def test_ensure_dir_exists_with_sudo(self) -> None:
        path = self.root / "new_dir"
        ensure_dir_exists_with_sudo(path, sudo=False)
        self.assertTrue(path.is_dir())

    @patch("subprocess.run")
    def test_ensure_dir_exists_with_sudo_and_true(self, mock_run) -> None:
        path = self.root / "sudo_dir"
        ensure_dir_exists_with_sudo(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "mkdir", "-p", str(path)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_remove_file_or_dir_with_sudo(self, mock_run) -> None:
        path = self.root / "file_to_remove"
        path.touch()
        remove_file_or_dir_with_sudo(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "rm", "-f", str(path)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_create_symlink_manually_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_link"
        create_symlink_manually_with_sudo(src, dst, sudo=True)
        # Verify run was called with sudo ln -s
        mock_run.assert_any_call(["sudo", "ln", "-s", str(src), str(dst)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_copy_file_contents_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"
        copy_file_contents_with_sudo(src, dst, sudo=True)
        mock_run.assert_any_call(["sudo", "cp", str(src), str(dst)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_copy_or_move_file_or_dir_external(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"

        # 1. Non-sudo copy file (resolve_symlinks=True)
        copy_or_move_file_or_dir_external(src, dst, sudo=False, chown=False, move=False, resolve_symlinks=True)
        mock_run.assert_any_call(["cp", "-L", str(src), str(dst)], check=True, capture_output=True)

        # 2. Sudo copy dir (resolve_symlinks=False, chown=True)
        dir_src = self.root / "src_dir"
        dir_src.mkdir()
        copy_or_move_file_or_dir_external(dir_src, dst, sudo=True, chown=True, move=False, resolve_symlinks=False)
        mock_run.assert_any_call(["sudo", "cp", "-RP", str(dir_src), str(dst)], check=True, capture_output=True)

        # 3. Move file (sudo=False, resolve_symlinks=False)
        copy_or_move_file_or_dir_external(src, dst, sudo=False, chown=False, move=True, resolve_symlinks=False)
        mock_run.assert_any_call(["mv", str(src), str(dst)], check=True, capture_output=True)

    def test_reverse_sync_file_or_dir_deletion(self) -> None:
        # If src does not exist, and dst exists, dst should be deleted
        src = self.root / "nonexistent"
        dst = self.root / "local_file"
        dst.touch()
        self.assertTrue(dst.exists())
        reverse_sync_file_or_dir(src, dst)
        self.assertFalse(dst.exists())

    def test_reverse_sync_file_or_dir_directory(self) -> None:
        # If src is a directory, it should be copied recursively
        src = self.root / "src_dir"
        src.mkdir()
        (src / "child.txt").write_text("child content", encoding="utf-8")
        
        dst = self.root / "dst_dir"
        reverse_sync_file_or_dir(src, dst)
        
        self.assertTrue(dst.is_dir())
        self.assertTrue((dst / "child.txt").exists())
        self.assertEqual((dst / "child.txt").read_text(encoding="utf-8"), "child content")

    def test_reverse_sync_file_or_dir_file_content_modification(self) -> None:
        # If src is a file and content differs from dst, dst should be updated
        src = self.root / "src_file.txt"
        src.write_text("updated", encoding="utf-8")
        dst = self.root / "dst_file.txt"
        dst.write_text("old", encoding="utf-8")
        
        reverse_sync_file_or_dir(src, dst)
        self.assertEqual(dst.read_text(encoding="utf-8"), "updated")

    def test_reverse_sync_file_or_dir_broken_symlink(self) -> None:
        # If src is a broken symlink, it should be synced as a broken symlink at dst
        src = self.root / "broken_link"
        src.symlink_to("non_existent")
        dst = self.root / "dst_link"
        
        reverse_sync_file_or_dir(src, dst)
        self.assertTrue(dst.is_symlink())
        self.assertEqual(os.readlink(dst), "non_existent")

    def test_sync_broken_symlink(self) -> None:
        # Verify direct sync_broken_symlink execution
        src = self.root / "broken_link"
        src.symlink_to("another_non_existent")
        dst = self.root / "dst_link"
        
        sync_broken_symlink(src, dst)
        self.assertTrue(dst.is_symlink())
        self.assertEqual(os.readlink(dst), "another_non_existent")

    def test_reverse_sync_file_or_dir_valid_symlink(self) -> None:
        # If src is a valid symlink, it should recursively sync the resolved target content
        target = self.root / "target.txt"
        target.write_text("target content", encoding="utf-8")
        src = self.root / "valid_link"
        src.symlink_to(target)
        
        dst = self.root / "dst_file.txt"
        
        reverse_sync_file_or_dir(src, dst)
        self.assertFalse(dst.is_symlink())
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_text(encoding="utf-8"), "target content")

    def test_reverse_sync_file_or_dir_empty_sub_directory(self) -> None:
        """Verifies that an empty sub-folder in src is correctly synced and created at dst."""
        src = self.root / "src_dir"
        src.mkdir()
        empty_sub = src / "empty_sub_folder"
        empty_sub.mkdir()

        dst = self.root / "dst_dir"

        reverse_sync_file_or_dir(src, dst)

        self.assertTrue(dst.is_dir())
        self.assertTrue((dst / "empty_sub_folder").is_dir())
        # Confirm it is empty
        self.assertEqual(list((dst / "empty_sub_folder").iterdir()), [])

    def test_reverse_sync_file_or_dir_nested_healthy_symlink(self) -> None:
        """Verifies that a valid symlink deep within a sub-folder resolves and syncs content."""
        src = self.root / "src_dir"
        src.mkdir()
        sub_dir = src / "nested_sub"
        sub_dir.mkdir()

        # Create a valid symlink target external to the folder structure
        target_file = self.root / "external_target.txt"
        target_file.write_text("external content", encoding="utf-8")

        # Create nested valid symlink
        nested_link = sub_dir / "valid_nested_link"
        nested_link.symlink_to(target_file)

        dst = self.root / "dst_dir"

        reverse_sync_file_or_dir(src, dst)

        self.assertTrue((dst / "nested_sub").is_dir())
        dest_file = dst / "nested_sub" / "valid_nested_link"
        # Since it is a valid symlink, reverse sync copies the actual target physical content back
        self.assertFalse(dest_file.is_symlink())
        self.assertTrue(dest_file.is_file())
        self.assertEqual(dest_file.read_text(encoding="utf-8"), "external content")

    def test_reverse_sync_file_or_dir_nested_broken_symlink(self) -> None:
        """Verifies that a broken symlink deep within a sub-folder is copied back as a broken symlink."""
        src = self.root / "src_dir"
        src.mkdir()
        sub_dir = src / "nested_sub"
        sub_dir.mkdir()

        # Create nested broken symlink
        nested_link = sub_dir / "broken_nested_link"
        nested_link.symlink_to("nested_non_existent")

        dst = self.root / "dst_dir"

        reverse_sync_file_or_dir(src, dst)

        self.assertTrue((dst / "nested_sub").is_dir())
        dest_link = dst / "nested_sub" / "broken_nested_link"
        # It should copy the broken link itself
        self.assertTrue(dest_link.is_symlink())
        self.assertEqual(os.readlink(dest_link), "nested_non_existent")

    def test_expand_user_and_env(self) -> None:
        from drift.file_utils import expand_user_and_env

        # 1. Test empty string
        self.assertEqual(expand_user_and_env(""), Path("."))

        # 2. Test '~' expansion
        home = Path.home()
        self.assertEqual(expand_user_and_env("~"), home)
        self.assertEqual(expand_user_and_env("~/my_config"), home / "my_config")
        self.assertEqual(expand_user_and_env(r"~\my_config"), home / "my_config")

        # 3. Test Windows %VAR% expansion when platform is win32
        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {"CUSTOM_APP_PATH": "/custom/path", "USERPROFILE": "/custom/user"}):
                self.assertEqual(expand_user_and_env("%CUSTOM_APP_PATH%/sub"), Path("/custom/path/sub"))
                self.assertEqual(expand_user_and_env("%USERPROFILE%/config"), Path("/custom/user/config"))

            # Test Windows %VAR% expansion with fallback dictionary (when not in os.environ)
            with patch.dict(os.environ, {}, clear=True):
                res_appdata = expand_user_and_env("%APPDATA%/myapp")
                self.assertEqual(res_appdata, home / "AppData" / "Roaming" / "myapp")

                res_localappdata = expand_user_and_env("%LOCALAPPDATA%/myapp")
                self.assertEqual(res_localappdata, home / "AppData" / "Local" / "myapp")

        # 4. Test non-Windows platform preserves %VAR% literally
        with patch("sys.platform", "linux"):
            self.assertEqual(expand_user_and_env("%USERPROFILE%/config"), Path("%USERPROFILE%/config"))

        # 5. Test POSIX $VAR / ${VAR} expansion across platforms
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg/config"}):
            self.assertEqual(expand_user_and_env("$XDG_CONFIG_HOME/app"), Path("/xdg/config/app"))
            self.assertEqual(expand_user_and_env("${XDG_CONFIG_HOME}/app"), Path("/xdg/config/app"))

    def test_safe_relative_to(self) -> None:
        from drift.file_utils import safe_relative_to

        # Normal relative path within same directory tree
        child = self.root / "a" / "b" / "file.txt"
        base = self.root / "a"
        self.assertEqual(safe_relative_to(child, base), Path("b/file.txt"))

        # Cross-drive or unrelated path simulation (where relative_to raises ValueError)
        other_path = MagicMock(spec=Path)
        other_path.resolve.return_value = other_path
        other_path.relative_to.side_effect = ValueError("Different drives")
        base_mock = MagicMock(spec=Path)
        base_mock.resolve.return_value = base_mock

        res = safe_relative_to(other_path, base_mock)
        self.assertEqual(res, other_path)


if __name__ == "__main__":
    unittest.main()
