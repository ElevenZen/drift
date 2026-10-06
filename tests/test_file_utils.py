"""Tests for file_utils operations."""

import os
import shutil
import tempfile
import unittest
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
    find_symlink_ancestor,
    is_concrete_dir,
    is_internal_lock_file,
    is_diff_candidate,
)
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
from drift.core.sync_ops import (
    backup_file_or_dir_external,
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

        dir_symlink = self.root / "dir_symlink"
        dir_symlink.symlink_to(real_dir)
        file_symlink = self.root / "file_symlink"
        file_symlink.symlink_to(real_file)
        broken_symlink = self.root / "broken_symlink"
        broken_symlink.symlink_to(self.root / "nonexistent")
        missing_path = self.root / "does_not_exist"

        self.assertTrue(is_concrete_dir(real_dir))
        self.assertFalse(is_concrete_dir(real_file))
        self.assertFalse(is_concrete_dir(dir_symlink))
        self.assertFalse(is_concrete_dir(file_symlink))
        self.assertFalse(is_concrete_dir(broken_symlink))
        self.assertFalse(is_concrete_dir(missing_path))

    def test_is_diff_candidate(self) -> None:
        """Verifies is_diff_candidate filters out concrete directories, temp files, and internal lockfiles."""
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

        symlink_to_file = sub_dir / "link_to_file"
        symlink_to_file.symlink_to(valid_file)
        symlink_to_dir = sub_dir / "link_to_dir"
        symlink_to_dir.symlink_to(self.root)

        # 1. Direct path checks
        self.assertTrue(is_diff_candidate(valid_file))
        self.assertTrue(is_diff_candidate(symlink_to_file))
        self.assertTrue(is_diff_candidate(symlink_to_dir))          # symlinks are inspectable candidates
        self.assertFalse(is_diff_candidate(sub_dir))                # concrete directory rejected
        self.assertFalse(is_diff_candidate(temp_file))              # editor/OS temp file rejected

        # Internal lockfile rejected, but user lockfile and control plane configs accepted
        self.assertTrue(is_internal_lock_file(internal_lock_file))
        self.assertFalse(is_diff_candidate(internal_lock_file))     # internal lockfile in .drift/ rejected
        self.assertFalse(is_internal_lock_file(user_lock_file))
        self.assertTrue(is_diff_candidate(user_lock_file))          # user file named render_lock.json outside .drift/ accepted
        self.assertFalse(is_internal_lock_file(pkg_toml_file))
        self.assertTrue(is_diff_candidate(pkg_toml_file))           # drift_package.toml in .drift/ accepted

        # 2. Checks with base_dir resolution
        self.assertTrue(is_diff_candidate(Path("sub_dir/valid.txt"), base_dir=self.root))
        self.assertFalse(is_diff_candidate(Path("sub_dir"), base_dir=self.root))
        self.assertFalse(is_diff_candidate(Path("sub_dir/.DS_Store"), base_dir=self.root))
        self.assertFalse(is_diff_candidate(Path("sub_dir/.drift/render_lock.json"), base_dir=self.root))
        self.assertTrue(is_diff_candidate(Path("sub_dir/render_lock.json"), base_dir=self.root))

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

        parent_symlink = find_symlink_ancestor(file_path, drift_root)
        self.assertEqual(parent_symlink, symlink_dir)

        # 1. file_path itself is a symlink pointing into link_target_range -> returns file_path
        file_symlink = target_dir / "direct_symlink"
        file_symlink.symlink_to(src_dir / "file.txt")
        self.assertEqual(find_symlink_ancestor(file_symlink, drift_root), file_symlink)

        # 2. No symlinked parent -> returns None
        normal_file = target_dir / "normal_file.txt"
        normal_file.touch()
        self.assertIsNone(find_symlink_ancestor(normal_file, drift_root))

        # 3. Parent is a symlink pointing OUTSIDE of link_target_range -> returns None
        external_dir = self.root / "external_dir"
        external_dir.mkdir()
        external_symlink = target_dir / "external_symlink"
        external_symlink.symlink_to(external_dir)
        nested_file_external = external_symlink / "some_file.txt"
        self.assertIsNone(find_symlink_ancestor(nested_file_external, drift_root))

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

    @patch("subprocess.run")
    def test_ensure_dir_with_sudo(self, mock_run) -> None:
        path = self.root / "sudo_dir"
        ensure_dir(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "mkdir", "-p", str(path)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_remove_tree_file_or_dir_with_sudo(self, mock_run) -> None:
        path = self.root / "file_to_remove"
        path.touch()
        remove_tree(path, sudo=True)
        mock_run.assert_called_once_with(["sudo", "rm", "-f", str(path)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_create_symlink_manually_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_link"
        create_symlink(src, dst, sudo=True)
        # Verify run was called with sudo ln -s
        mock_run.assert_any_call(["sudo", "ln", "-s", str(src), str(dst)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_atomic_copy_file_with_sudo(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"

        # 1. Pipeline success (mktemp -> cp -> mv)
        mock_proc = MagicMock()
        mock_proc.stdout = str(self.root / ".tmp_dst_file_123456")
        mock_run.return_value = mock_proc

        copy_file(src, dst, sudo=True)
        mock_run.assert_any_call(["sudo", "mktemp", "-p", str(dst.parent), f".tmp_{dst.name}_XXXXXX"], check=True, capture_output=True, text=True)
        mock_run.assert_any_call(["sudo", "cp", "-p", str(src), str(self.root / ".tmp_dst_file_123456")], check=True, capture_output=True)
        mock_run.assert_any_call(["sudo", "mv", "-f", str(self.root / ".tmp_dst_file_123456"), str(dst)], check=True, capture_output=True)

        # 2. Fallback when mktemp fails
        mock_run.reset_mock()
        mock_run.side_effect = [Exception("mktemp failed"), MagicMock()]
        copy_file(src, dst, sudo=True)
        mock_run.assert_any_call(["sudo", "cp", "-p", str(src), str(dst)], check=True, capture_output=True)

    @patch("subprocess.run")
    def test_copy_or_move_file_or_dir_external(self, mock_run) -> None:
        src = self.root / "src_file"
        dst = self.root / "dst_file"

        # 1. Non-sudo copy file (resolve_symlinks=True)
        copy_tree(src, dst, sudo=False, chown=False, resolve_symlinks=True)
        mock_run.assert_any_call(["cp", "-L", str(src), str(dst)], check=True, capture_output=True)

        # 2. Sudo copy dir (resolve_symlinks=False, chown=True)
        dir_src = self.root / "src_dir"
        dir_src.mkdir()
        copy_tree(dir_src, dst, sudo=True, chown=True, resolve_symlinks=False)
        mock_run.assert_any_call(["sudo", "cp", "-RP", str(dir_src), str(dst)], check=True, capture_output=True)

        # 3. Move file (sudo=False, resolve_symlinks=False)
        move_tree(src, dst, sudo=False, chown=False, resolve_symlinks=False)
        mock_run.assert_any_call(["mv", str(src), str(dst)], check=True, capture_output=True)

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
        self.assertEqual(os.readlink(dst_valid), str(target_file))

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
        self.assertEqual(expand_path(r"~\my_config"), home / "my_config")

        # 3. Test Windows %VAR% expansion when platform is win32
        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {"CUSTOM_APP_PATH": "/custom/path", "USERPROFILE": "/custom/user"}):
                self.assertEqual(expand_path("%CUSTOM_APP_PATH%/sub"), Path("/custom/path/sub"))
                self.assertEqual(expand_path("%USERPROFILE%/config"), Path("/custom/user/config"))

            # Test Windows %VAR% expansion with fallback dictionary (when not in os.environ)
            with patch.dict(os.environ, {}, clear=True):
                res_appdata = expand_path("%APPDATA%/myapp")
                self.assertEqual(res_appdata, home / "AppData" / "Roaming" / "myapp")

                res_localappdata = expand_path("%LOCALAPPDATA%/myapp")
                self.assertEqual(res_localappdata, home / "AppData" / "Local" / "myapp")

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
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000):
            self.assertFalse(has_admin_privileges())
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                run_command(["echo", "hello"], sudo=True)
                mock_run.assert_called_with(["sudo", "echo", "hello"], check=True, capture_output=True)

        # Linux root
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=0):
            self.assertTrue(has_admin_privileges())
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                run_command(["echo", "hello"], sudo=True)
                mock_run.assert_called_with(["echo", "hello"], check=True, capture_output=True)

    def test_assert_can_escalate(self) -> None:
        from drift.utils.process_utils import assert_can_escalate

        # Linux root
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=0):
            assert_can_escalate()

        # Linux non-root success
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                assert_can_escalate()
                mock_run.assert_called_with(["sudo", "-v"], check=False)

        # Linux non-root failure
        with patch("sys.platform", "linux"), patch("os.geteuid", return_value=1000):
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
        symlink_src = self.root / "sym_src.txt"
        symlink_src.symlink_to(src)
        dst_sym = self.root / "dest_dir" / "dest_sym.txt"
        copy_file(symlink_src, dst_sym, follow_symlinks=False)
        self.assertTrue(dst_sym.is_symlink())
        self.assertEqual(os.readlink(dst_sym), str(src))

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

    @patch("subprocess.run")
    def test_remove_file_or_empty_dir_with_sudo(self, mock_run) -> None:
        # File under sudo -> sudo rm -f
        f = self.root / "sudo_file.txt"
        f.touch()
        self.assertTrue(remove_file_or_empty_dir(f, sudo=True))
        mock_run.assert_called_with(["sudo", "rm", "-f", str(f)], check=True, capture_output=True)

        mock_run.reset_mock()
        # Empty dir under sudo -> sudo rmdir
        d = self.root / "sudo_empty_dir"
        d.mkdir()
        self.assertTrue(remove_file_or_empty_dir(d, sudo=True))
        mock_run.assert_called_with(["sudo", "rmdir", str(d)], check=True, capture_output=True)

    def test_remove_tree_recursive(self) -> None:
        # remove_tree recursively removes directory with children
        tree_dir = self.root / "tree_dir"
        sub_dir = tree_dir / "sub"
        sub_dir.mkdir(parents=True)
        (sub_dir / "nested.txt").write_text("data", encoding="utf-8")
        remove_tree(tree_dir)
        self.assertFalse(tree_dir.exists())


if __name__ == "__main__":
    unittest.main()

