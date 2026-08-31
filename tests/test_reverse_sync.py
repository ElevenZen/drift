"""Tests for Primitive 1 Reverse Sync."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.package_config import PackageConfig
from drift.ignore import DriftIgnore
from drift.sync_ops import reverse_sync_file_or_dir
from drift.reverse_sync import (
    run_primitive_1_reverse_sync,
    reverse_sync_package,
    sync_tracked_files,
    sync_single_fcd,
    sync_fully_controlled_dirs,
    record_sync_result,
)


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

    def test_reverse_sync_ignored_file_in_target_dir_not_deleted_in_install(self) -> None:
        """Verifies that:
        1. An ignored file in install/ that does NOT exist in target dir is NOT deleted by reverse sync.
        2. An ignored file in install/ that exists in target dir is NOT overwritten or deleted.
        3. A non-ignored tracked file in install/ that is missing in target dir IS deleted.
        4. A non-ignored tracked file in install/ that is modified in target dir IS updated.
        """
        from drift.constants import DRIFT_IGNORE_FILE_NAME
        pkg = "pkg_ignored_reverse"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup install/ with config, .drift_ignore, ignored scripts, and tracked files
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / DRIFT_IGNORE_FILE_NAME).write_text("ignored_hook_present.sh\nignored_hook_missing_on_target.sh\n", encoding="utf-8")
        
        ignored_present_install = pkg_install_dir / "ignored_hook_present.sh"
        ignored_present_install.write_text("#!/bin/sh\necho 'hook present'\n", encoding="utf-8")

        ignored_missing_install = pkg_install_dir / "ignored_hook_missing_on_target.sh"
        ignored_missing_install.write_text("#!/bin/sh\necho 'hook missing on target'\n", encoding="utf-8")

        tracked_modified_install = pkg_install_dir / "app_modified.conf"
        tracked_modified_install.write_text("setting=1\n", encoding="utf-8")

        tracked_deleted_install = pkg_install_dir / "app_deleted.conf"
        tracked_deleted_install.write_text("delete_me=true\n", encoding="utf-8")

        # 2. Setup target dir:
        # - ignored_hook_present.sh exists on host with different content
        # - ignored_hook_missing_on_target.sh does NOT exist on host
        # - app_modified.conf is modified on host
        # - app_deleted.conf does NOT exist on host
        (self.system_target_dir / "ignored_hook_present.sh").write_text("#!/bin/sh\necho 'different on host'\n", encoding="utf-8")
        self.assertFalse((self.system_target_dir / "ignored_hook_missing_on_target.sh").exists())
        (self.system_target_dir / "app_modified.conf").write_text("setting=2\n", encoding="utf-8")
        self.assertFalse((self.system_target_dir / "app_deleted.conf").exists())

        # 3. Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 4. Assert:
        # A. ignored_hook_missing_on_target.sh in install/ is NOT deleted!
        self.assertTrue(ignored_missing_install.exists(), "Ignored file missing on target must not be deleted from install/!")
        self.assertEqual(ignored_missing_install.read_text(encoding="utf-8"), "#!/bin/sh\necho 'hook missing on target'\n")

        # B. ignored_hook_present.sh in install/ is NOT deleted and NOT overwritten!
        self.assertTrue(ignored_present_install.exists(), "Ignored file present on target must not be deleted from install/!")
        self.assertEqual(ignored_present_install.read_text(encoding="utf-8"), "#!/bin/sh\necho 'hook present'\n")

        # C. Non-ignored tracked file missing on host IS deleted
        self.assertFalse(tracked_deleted_install.exists(), "Tracked file missing on host should be deleted from install/!")

        # D. Non-ignored tracked file modified on host IS updated
        self.assertEqual(tracked_modified_install.read_text(encoding="utf-8"), "setting=2\n")

    def test_reverse_sync_fcd_with_ignore_patterns(self) -> None:
        """Verifies that ignored files inside an FCD directory are NOT reverse-synced,
        while non-ignored wild files inside the FCD are reverse-synced.
        """
        from drift.constants import DRIFT_IGNORE_FILE_NAME
        pkg = "pkg_fcd_ignores"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["themes"]
        """, encoding="utf-8")

        # Ignore .*\.log and themes/cache/ inside the package (Drift uses PCRE regex syntax)
        (pkg_install_dir / DRIFT_IGNORE_FILE_NAME).write_text(".*\\.log$\nthemes/cache/\n", encoding="utf-8")

        # Setup files on host system inside FCD
        host_themes = self.system_target_dir / "themes"
        host_themes.mkdir(parents=True, exist_ok=True)
        (host_themes / "valid_dark.theme").write_text("theme data", encoding="utf-8")
        (host_themes / "debug.log").write_text("log data", encoding="utf-8")
        
        host_cache = host_themes / "cache"
        host_cache.mkdir(parents=True, exist_ok=True)
        (host_cache / "temp.dat").write_text("cached data", encoding="utf-8")

        # Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 1. Non-ignored wild file in FCD is reverse-synced
        self.assertTrue((pkg_install_dir / "themes" / "valid_dark.theme").exists())
        self.assertEqual((pkg_install_dir / "themes" / "valid_dark.theme").read_text(encoding="utf-8"), "theme data")

        # 2. Ignored files in FCD are NOT reverse-synced
        self.assertFalse((pkg_install_dir / "themes" / "debug.log").exists())
        self.assertFalse((pkg_install_dir / "themes" / "cache").exists())

    def test_reverse_sync_fcd_dot_notation_variants(self) -> None:
        """Verifies that FCD entries defined with repo notation ('dot-config/app') or
        system dot notation ('.config/app') both correctly match host additions.
        """
        pkg = "pkg_fcd_dot_variants"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["dot-config/app/plugins", ".config/app/themes"]
        """, encoding="utf-8")

        # Host system has additions in both directories
        host_plugins = self.system_target_dir / ".config" / "app" / "plugins"
        host_plugins.mkdir(parents=True, exist_ok=True)
        (host_plugins / "ext.py").write_text("print('plugin')", encoding="utf-8")

        host_themes = self.system_target_dir / ".config" / "app" / "themes"
        host_themes.mkdir(parents=True, exist_ok=True)
        (host_themes / "dark.css").write_text("body { color: black; }", encoding="utf-8")

        # Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Both should be reverse-synced to repo path format (dot-config/...)
        synced_plugin = pkg_install_dir / "dot-config" / "app" / "plugins" / "ext.py"
        synced_theme = pkg_install_dir / "dot-config" / "app" / "themes" / "dark.css"

        self.assertTrue(synced_plugin.exists())
        self.assertEqual(synced_plugin.read_text(encoding="utf-8"), "print('plugin')")
        self.assertTrue(synced_theme.exists())
        self.assertEqual(synced_theme.read_text(encoding="utf-8"), "body { color: black; }")

    def test_reverse_sync_fcd_symlink_preservation(self) -> None:
        """Verifies that valid and broken symlinks created inside an FCD on host
        are safely preserved as symlinks when reverse-synced to install/.
        """
        pkg = "pkg_fcd_symlinks"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["plugins"]
        """, encoding="utf-8")

        # Setup host target with valid file outside FCD and symlinks inside FCD
        host_shared = self.system_target_dir / "shared"
        host_shared.mkdir(parents=True, exist_ok=True)
        (host_shared / "base_theme.json").write_text('{"theme": "base"}', encoding="utf-8")

        host_plugins = self.system_target_dir / "plugins"
        host_plugins.mkdir(parents=True, exist_ok=True)

        # 1. Valid symlink inside FCD
        valid_link = host_plugins / "current_theme.json"
        valid_link.symlink_to(host_shared / "base_theme.json")

        # 2. Broken symlink inside FCD
        broken_link = host_plugins / "broken_ext.so"
        broken_link.symlink_to("non_existent_binary.so")

        # Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Verify links in install/
        synced_valid_link = pkg_install_dir / "plugins" / "current_theme.json"
        synced_broken_link = pkg_install_dir / "plugins" / "broken_ext.so"

        self.assertTrue(synced_valid_link.exists() or synced_valid_link.is_symlink())
        self.assertTrue(synced_broken_link.is_symlink())
        self.assertEqual(os.readlink(synced_broken_link), "non_existent_binary.so")

    def test_reverse_sync_fcd_selective_directory_filtering(self) -> None:
        """Verifies that only additions in designated FCD directories are reverse-synced,
        while additions in non-FCD sibling directories or root are ignored.
        """
        pkg = "pkg_fcd_selective"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["controlled_zone"]
        """, encoding="utf-8")

        # 1. Addition inside FCD
        fcd_dir = self.system_target_dir / "controlled_zone"
        fcd_dir.mkdir(parents=True, exist_ok=True)
        (fcd_dir / "fcd_file.txt").write_text("in fcd", encoding="utf-8")

        # 2. Addition inside non-FCD directory
        uncontrolled_dir = self.system_target_dir / "uncontrolled_zone"
        uncontrolled_dir.mkdir(parents=True, exist_ok=True)
        (uncontrolled_dir / "other_file.txt").write_text("not in fcd", encoding="utf-8")

        # 3. Addition at root level
        (self.system_target_dir / "root_wild_file.txt").write_text("root file", encoding="utf-8")

        # Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # FCD addition is synced
        self.assertTrue((pkg_install_dir / "controlled_zone" / "fcd_file.txt").exists())
        self.assertEqual((pkg_install_dir / "controlled_zone" / "fcd_file.txt").read_text(encoding="utf-8"), "in fcd")

        # Non-FCD additions are NOT synced
        self.assertFalse((pkg_install_dir / "uncontrolled_zone").exists())
        self.assertFalse((pkg_install_dir / "root_wild_file.txt").exists())

    def test_reverse_sync_fcd_in_stow_package_lifecycle(self) -> None:
        """Verifies end-to-end FCD lifecycle under stow deployment:
        deploy stow -> create wild file on host -> reverse-sync -> deploy update.
        """
        from drift.install_repo import run_primitive_5_install_deployment
        pkg = "pkg_fcd_stow_lifecycle"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["plugins"]
        """, encoding="utf-8")

        (pkg_install_dir / "initial.conf").write_text("init=1\n", encoding="utf-8")
        (pkg_install_dir / "plugins" / "base.plugin").parent.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "plugins" / "base.plugin").write_text("base plugin", encoding="utf-8")

        # 1. Initial stow deployment
        res_dep1 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res_dep1.status, "SUCCESS")

        # 2. Host app dynamically creates a new plugin file
        host_plugins = self.system_target_dir / "plugins"
        # If stow created plugins as a symlink or directory, ensure target file exists
        if host_plugins.is_symlink():
            # In folded stow tree, plugins is a symlink to install/pkg/plugins
            # App writes new file inside the directory
            (host_plugins / "dynamic.plugin").write_text("dynamic plugin", encoding="utf-8")
        else:
            host_plugins.mkdir(parents=True, exist_ok=True)
            (host_plugins / "dynamic.plugin").write_text("dynamic plugin", encoding="utf-8")

        # 3. Reverse sync detects and syncs it back
        res_sync = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res_sync.status, "SUCCESS")

        self.assertTrue((pkg_install_dir / "plugins" / "dynamic.plugin").exists())
        self.assertEqual((pkg_install_dir / "plugins" / "dynamic.plugin").read_text(encoding="utf-8"), "dynamic plugin")

        # 4. Subsequent stow deployment succeeds without collision errors
        res_dep2 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res_dep2.status, "SUCCESS")

    def test_reverse_sync_tracked_dot_prefix_files_modification_and_deletion(self) -> None:
        """Verifies that tracked files using 'dot-' prefix in install/ are accurately detected
        and reverse-synced when modified or deleted on the host system.
        """
        pkg = "pkg_dot_tracked"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        # 1. Setup tracked files in install/ with 'dot-' prefixes
        dot_zshrc_install = pkg_install_dir / "dot-zshrc"
        dot_zshrc_install.write_text("export ORIGINAL=1\n", encoding="utf-8")

        nested_dot_dir = pkg_install_dir / "dot-config" / "myapp"
        nested_dot_dir.mkdir(parents=True, exist_ok=True)
        dot_settings_install = nested_dot_dir / "dot-settings.json"
        dot_settings_install.write_text('{"version": 1}', encoding="utf-8")

        normal_conf_install = nested_dot_dir / "config.ini"
        normal_conf_install.write_text("theme=light\n", encoding="utf-8")

        dot_local_data_install = pkg_install_dir / "dot-local" / "share" / "data.txt"
        dot_local_data_install.parent.mkdir(parents=True, exist_ok=True)
        dot_local_data_install.write_text("historical data\n", encoding="utf-8")

        # 2. Setup host system state:
        # - .zshrc is MODIFIED on host
        # - .config/myapp/.settings.json is DELETED on host (missing)
        # - .config/myapp/config.ini is MODIFIED on host
        # - .local/share/data.txt is DELETED on host (missing)
        host_zshrc = self.system_target_dir / ".zshrc"
        host_zshrc.write_text("export MODIFIED_ON_HOST=2\n", encoding="utf-8")

        host_myapp_dir = self.system_target_dir / ".config" / "myapp"
        host_myapp_dir.mkdir(parents=True, exist_ok=True)
        # Note: host_myapp_dir / ".settings.json" is deliberately NOT created (simulating deletion)
        host_config_ini = host_myapp_dir / "config.ini"
        host_config_ini.write_text("theme=dark\n", encoding="utf-8")
        # Note: self.system_target_dir / ".local" / "share" / "data.txt" is NOT created (simulating deletion)

        # 3. Run reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        pkg_res = res.packages[0]
        self.assertEqual(pkg_res.status, "SUCCESS")

        # 4. Assert:
        # A. Modified dot-file is updated in install/
        self.assertTrue(dot_zshrc_install.exists())
        self.assertEqual(dot_zshrc_install.read_text(encoding="utf-8"), "export MODIFIED_ON_HOST=2\n")

        # B. Deleted nested dot-file is removed from install/
        self.assertFalse(dot_settings_install.exists(), "dot-config/myapp/dot-settings.json should have been deleted!")

        # C. Modified regular file inside dot-folder is updated
        self.assertTrue(normal_conf_install.exists())
        self.assertEqual(normal_conf_install.read_text(encoding="utf-8"), "theme=dark\n")

        # D. Deleted deep dot-path is removed from install/
        self.assertFalse(dot_local_data_install.exists(), "dot-local/share/data.txt should have been deleted!")

        # Verify reported drifted and synced lists
        self.assertIn(".zshrc", pkg_res.drifted_files)
        self.assertIn("dot-zshrc", pkg_res.synced_files)
        self.assertIn(".config/myapp/.settings.json", pkg_res.drifted_files)
        self.assertIn("dot-config/myapp/dot-settings.json", pkg_res.synced_files)
        self.assertIn(".config/myapp/config.ini", pkg_res.drifted_files)
        self.assertIn("dot-config/myapp/config.ini", pkg_res.synced_files)
        self.assertIn(".local/share/data.txt", pkg_res.drifted_files)
        self.assertIn("dot-local/share/data.txt", pkg_res.synced_files)

    def test_record_sync_result_helper(self) -> None:
        """Verifies that record_sync_result correctly deduplicates entries."""
        drifted: list[str] = []
        synced: list[str] = []

        record_sync_result(".bashrc", "dot-bashrc", drifted, synced)
        self.assertEqual(drifted, [".bashrc"])
        self.assertEqual(synced, ["dot-bashrc"])

        # Duplicate addition should be ignored
        record_sync_result(".bashrc", "dot-bashrc", drifted, synced)
        self.assertEqual(drifted, [".bashrc"])
        self.assertEqual(synced, ["dot-bashrc"])

        # Different file should be appended
        record_sync_result(".zshrc", "dot-zshrc", drifted, synced)
        self.assertEqual(drifted, [".bashrc", ".zshrc"])
        self.assertEqual(synced, ["dot-bashrc", "dot-zshrc"])

    def test_sync_tracked_files_helper_direct(self) -> None:
        """Directly verifies the sync_tracked_files helper function."""
        pkg_dir = self.install_dir / "pkg_direct"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "dot-gitconfig").write_text("[user]\n  name = Original\n", encoding="utf-8")
        (pkg_dir / "deleted.conf").write_text("old=true\n", encoding="utf-8")

        # Host system has modified .gitconfig and missing deleted.conf
        (self.system_target_dir / ".gitconfig").write_text("[user]\n  name = Updated\n", encoding="utf-8")

        ignore_handler = DriftIgnore()
        drifted: list[str] = []
        synced: list[str] = []

        sync_tracked_files(pkg_dir, self.system_target_dir, ignore_handler, drifted, synced)

        # Assert:
        # dot-gitconfig updated
        self.assertEqual((pkg_dir / "dot-gitconfig").read_text(encoding="utf-8"), "[user]\n  name = Updated\n")
        # deleted.conf removed
        self.assertFalse((pkg_dir / "deleted.conf").exists())
        self.assertIn(".gitconfig", drifted)
        self.assertIn("dot-gitconfig", synced)
        self.assertIn("deleted.conf", drifted)
        self.assertIn("deleted.conf", synced)

    def test_sync_fully_controlled_dirs_helpers_direct(self) -> None:
        """Directly verifies sync_single_fcd and sync_fully_controlled_dirs helper functions."""
        pkg_dir = self.install_dir / "pkg_fcd_direct"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        host_fcd1 = self.system_target_dir / "fcd_one"
        host_fcd1.mkdir(parents=True, exist_ok=True)
        (host_fcd1 / "extra1.txt").write_text("extra 1", encoding="utf-8")

        host_fcd2 = self.system_target_dir / ".fcd_two"
        host_fcd2.mkdir(parents=True, exist_ok=True)
        (host_fcd2 / ".wild.json").write_text('{"wild": true}', encoding="utf-8")

        ignore_handler = DriftIgnore()
        drifted: list[str] = []
        synced: list[str] = []

        sync_fully_controlled_dirs(
            fully_controlled_dirs=[Path("fcd_one"), Path("dot-fcd_two")],
            install_pkg_dir=pkg_dir,
            target_dir_path=self.system_target_dir,
            ignore_handler=ignore_handler,
            drifted_files=drifted,
            synced_files=synced
        )

        # Assert:
        self.assertTrue((pkg_dir / "fcd_one" / "extra1.txt").exists())
        self.assertEqual((pkg_dir / "fcd_one" / "extra1.txt").read_text(encoding="utf-8"), "extra 1")

        self.assertTrue((pkg_dir / "dot-fcd_two" / "dot-wild.json").exists())
        self.assertEqual((pkg_dir / "dot-fcd_two" / "dot-wild.json").read_text(encoding="utf-8"), '{"wild": true}')

        self.assertIn("fcd_one/extra1.txt", drifted)
        self.assertIn("fcd_one/extra1.txt", synced)
        self.assertIn(".fcd_two/.wild.json", drifted)
        self.assertIn("dot-fcd_two/dot-wild.json", synced)

    def test_reverse_sync_package_fcd_with_symlink_to_source(self) -> None:
        """Verifies that reverse-syncing an FCD directory with additions and a symlink back to repo
        applies additions before deletions and safely synchronizes the package.
        """
        pkg = "pkg_fcd_symlink_corner"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["plugins"]
        """, encoding="utf-8")

        (pkg_install_dir / "plugins" / "base.plugin").parent.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "plugins" / "base.plugin").write_text("base plugin", encoding="utf-8")

        # Host system has additions inside plugins and a symlink pointing back to repo
        host_plugins = self.system_target_dir / "plugins"
        host_plugins.mkdir(parents=True, exist_ok=True)
        (host_plugins / "dynamic.plugin").write_text("dynamic plugin", encoding="utf-8")
        (host_plugins / "link_to_repo").symlink_to(pkg_install_dir / "plugins")

        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Verify additions synced
        self.assertTrue((pkg_install_dir / "plugins" / "dynamic.plugin").exists())
        self.assertEqual((pkg_install_dir / "plugins" / "dynamic.plugin").read_text(encoding="utf-8"), "dynamic plugin")

    def test_reverse_sync_fcd_multi_level_type_changes(self) -> None:
        """Verifies that FCD reverse sync processes deletions before additions,
        seamlessly resolving multi-level type changes (e.g. file replacing a directory tree and vice-versa).
        """
        pkg = "pkg_fcd_type_changes"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["plugins"]
        """, encoding="utf-8")

        # 1. Old install state:
        # Case A: 'plugins/tree_to_file/sub/leaf.txt' (deep directory)
        # Case B: 'plugins/file_to_tree' (regular file)
        (pkg_install_dir / "plugins" / "tree_to_file" / "sub").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "plugins" / "tree_to_file" / "sub" / "leaf.txt").write_text("old leaf", encoding="utf-8")

        (pkg_install_dir / "plugins").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "plugins" / "file_to_tree").write_text("old flat file", encoding="utf-8")

        # 2. Host system state (type transitions):
        # Case A: 'plugins/tree_to_file' is now a flat file
        # Case B: 'plugins/file_to_tree' is now a directory containing 'nested.txt'
        host_plugins = self.system_target_dir / "plugins"
        host_plugins.mkdir(parents=True, exist_ok=True)

        (host_plugins / "tree_to_file").write_text("new flat file content", encoding="utf-8")

        (host_plugins / "file_to_tree").mkdir(parents=True, exist_ok=True)
        (host_plugins / "file_to_tree" / "nested.txt").write_text("new nested content", encoding="utf-8")

        # 3. Execute reverse sync
        res = run_primitive_1_reverse_sync(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 4. Assert:
        # Case A: tree_to_file is now a physical regular file in install/
        self.assertTrue((pkg_install_dir / "plugins" / "tree_to_file").is_file())
        self.assertEqual((pkg_install_dir / "plugins" / "tree_to_file").read_text(encoding="utf-8"), "new flat file content")
        self.assertFalse((pkg_install_dir / "plugins" / "tree_to_file" / "sub").exists())

        # Case B: file_to_tree is now a physical directory containing nested.txt in install/
        self.assertTrue((pkg_install_dir / "plugins" / "file_to_tree").is_dir())
        self.assertTrue((pkg_install_dir / "plugins" / "file_to_tree" / "nested.txt").is_file())
        self.assertEqual((pkg_install_dir / "plugins" / "file_to_tree" / "nested.txt").read_text(encoding="utf-8"), "new nested content")


if __name__ == "__main__":
    unittest.main()
