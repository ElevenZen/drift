"""Tests for Primitive 1 Reverse Sync."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.package_config import PackageConfig
from drift.reverse_sync import run_primitive_1_reverse_sync


class TestReverseSync(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name).resolve()
        self.drift_root = temp_root / "drift_workspace"
        self.system_target_dir = temp_root / "system_home"

        # Create workspace structures
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        # Default WorkspaceConfig
        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reverse_sync_stow_missing_symlink(self) -> None:
        """Verifies that if a symlink on the system is missing, its counterpart in install/ is deleted."""
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected files in local DB
        test_file = pkg_install_dir / "dot-bashrc"
        test_file.write_text("Hello bashrc", encoding="utf-8")

        # System target does not exist at all (simulates deletion on system)
        system_target = self.system_target_dir / ".bashrc"
        self.assertFalse(system_target.exists())

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should be deleted
        self.assertFalse(test_file.exists())

    def test_reverse_sync_stow_replaced_by_regular_file(self) -> None:
        """Verifies that if a symlink is replaced by a regular physical file containing edits, those contents are copied back."""
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected files in local DB
        test_file = pkg_install_dir / "dot-bashrc"
        test_file.write_text("Hello bashrc", encoding="utf-8")

        # Create system target as a physical file with edits instead of a symlink
        system_target = self.system_target_dir / ".bashrc"
        system_target.write_text("Slightly edited local bashrc config", encoding="utf-8")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should have the updated contents
        self.assertTrue(test_file.exists())
        self.assertEqual(test_file.read_text(encoding="utf-8"), "Slightly edited local bashrc config")

    def test_reverse_sync_copy_missing_file(self) -> None:
        """Verifies that if a copied system file is missing, its counterpart in install/ is deleted."""
        pkg = "pkg_copy"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected files in local DB
        test_file = pkg_install_dir / "config.ini"
        test_file.write_text("some content", encoding="utf-8")

        # System target is missing
        system_target = self.system_target_dir / "config.ini"
        self.assertFalse(system_target.exists())

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should be deleted
        self.assertFalse(test_file.exists())

    def test_reverse_sync_copy_modified_file(self) -> None:
        """Verifies that if a copied system file is modified, its edits are synced back to install/."""
        pkg = "pkg_copy"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected files in local DB
        test_file = pkg_install_dir / "config.ini"
        test_file.write_text("original content", encoding="utf-8")

        # System target contains modifications
        system_target = self.system_target_dir / "config.ini"
        system_target.write_text("modified content directly on host", encoding="utf-8")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should be updated
        self.assertTrue(test_file.exists())
        self.assertEqual(test_file.read_text(encoding="utf-8"), "modified content directly on host")

    def test_reverse_sync_fully_controlled_dirs(self) -> None:
        """Verifies that untracked files in FCD subdirectories are reverse-synchronized back to install/."""
        pkg = "pkg_fcd"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            fully_controlled_dirs = ["nested_sub"]
            """)

        # System target FCD exists with an untracked file starting with '.'
        target_fcd = self.system_target_dir / "nested_sub"
        target_fcd.mkdir(parents=True, exist_ok=True)
        untracked_file = target_fcd / ".wild_config"
        untracked_file.write_text("FCD content", encoding="utf-8")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Untracked file should be reverse copied and name translated to dot-
        expected_install_file = pkg_install_dir / "nested_sub" / "dot-wild_config"
        self.assertTrue(expected_install_file.is_file())
        self.assertEqual(expected_install_file.read_text(encoding="utf-8"), "FCD content")

    def test_reverse_sync_disabled_package(self) -> None:
        """Verifies that if enable_install is False, reverse sync is completely skipped."""
        pkg = "pkg_disabled"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Write config with enable_install = false
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            enable_install = false
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected files in local DB
        test_file = pkg_install_dir / "config.txt"
        test_file.write_text("some content", encoding="utf-8")

        # File is missing on host, which would trigger deletion if sync ran
        system_target = self.system_target_dir / "config.txt"
        self.assertFalse(system_target.exists())

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should NOT be deleted because sync was skipped
        self.assertTrue(test_file.exists())

    def test_reverse_sync_system_target_is_directory(self) -> None:
        """Verifies that if a system target becomes a directory, its contents are copied back recursively."""
        pkg = "pkg_dir_sync"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected file in local DB
        test_file = pkg_install_dir / "target_item"
        test_file.write_text("file content", encoding="utf-8")

        # System target is actually a directory with files
        system_target = self.system_target_dir / "target_item"
        system_target.mkdir(parents=True, exist_ok=True)
        (system_target / "sub_file.txt").write_text("sub file content", encoding="utf-8")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should now be a directory containing the subfile
        self.assertTrue(test_file.is_dir())
        self.assertTrue((test_file / "sub_file.txt").is_file())
        self.assertEqual((test_file / "sub_file.txt").read_text(encoding="utf-8"), "sub file content")

    def test_reverse_sync_broken_symlink_copying(self) -> None:
        """Verifies that broken symlinks on system are copied back as broken symlinks to install/."""
        pkg = "pkg_broken_link"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Setup expected file in local DB
        test_file = pkg_install_dir / "my_link"
        test_file.write_text("old text", encoding="utf-8")

        # Create broken link on system target
        system_target = self.system_target_dir / "my_link"
        system_target.symlink_to("non_existent_target_path")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Counterpart in install/ should now be a broken symlink pointing to the same target
        self.assertTrue(test_file.is_symlink())
        self.assertEqual(os.readlink(test_file), "non_existent_target_path")

    def test_reverse_sync_fcd_file_and_broken_link_at_root(self) -> None:
        """Verifies that if an FCD target path is a file or broken symlink (instead of dir), they are synced back."""
        pkg = "pkg_fcd_root"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            fully_controlled_dirs = ["file_fcd", "broken_link_fcd"]
            """)

        # 1. FCD target is a file
        system_file = self.system_target_dir / "file_fcd"
        system_file.write_text("fcd file content", encoding="utf-8")

        # 2. FCD target is a broken link
        system_link = self.system_target_dir / "broken_link_fcd"
        system_link.symlink_to("fcd_broken_dest")

        # Run reverse sync
        run_primitive_1_reverse_sync(self.workspace_config, [pkg])

        # Verify file FCD is synced back
        install_file = pkg_install_dir / "file_fcd"
        self.assertTrue(install_file.is_file())
        self.assertEqual(install_file.read_text(encoding="utf-8"), "fcd file content")

        # Verify broken link FCD is synced back
        install_link = pkg_install_dir / "broken_link_fcd"
        self.assertTrue(install_link.is_symlink())
        self.assertEqual(os.readlink(install_link), "fcd_broken_dest")

    def test_reverse_sync_missing_target_managed_config_files_not_deleted(self) -> None:
        """Verifies that missing managed config files (drift_package.toml, .drift_ignore, .stow-local-ignore) on target system do NOT trigger deletion in install/."""
        from drift.constants import DRIFT_IGNORE_FILE_NAME
        pkg = "pkg_managed_configs"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup install/ with managed config files and a regular file
        pkg_config_path = pkg_install_dir / PACKAGE_CONFIG_FILE_NAME
        pkg_config_path.write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        drift_ignore_path = pkg_install_dir / DRIFT_IGNORE_FILE_NAME
        drift_ignore_path.write_text("*.tmp\n", encoding="utf-8")

        stow_ignore_path = pkg_install_dir / ".stow-local-ignore"
        stow_ignore_path.write_text("*.bak\n", encoding="utf-8")

        regular_file = pkg_install_dir / "regular_file.txt"
        regular_file.write_text("I should be deleted", encoding="utf-8")

        # 2. On host system, none of the above files exist
        self.assertFalse((self.system_target_dir / PACKAGE_CONFIG_FILE_NAME).exists())
        self.assertFalse((self.system_target_dir / DRIFT_IGNORE_FILE_NAME).exists())
        self.assertFalse((self.system_target_dir / ".stow-local-ignore").exists())
        self.assertFalse((self.system_target_dir / "regular_file.txt").exists())

        # 3. Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 4. Managed config files MUST still exist in install/
        self.assertTrue(pkg_config_path.exists(), "drift_package.toml in install/ must not be deleted!")
        self.assertTrue(drift_ignore_path.exists(), ".drift_ignore in install/ must not be deleted!")
        self.assertTrue(stow_ignore_path.exists(), ".stow-local-ignore in install/ must not be deleted!")

        # 5. Regular file should have been deleted because it was missing on host
        self.assertFalse(regular_file.exists(), "regular_file.txt should be deleted as it is missing on host!")


if __name__ == "__main__":
    unittest.main()
