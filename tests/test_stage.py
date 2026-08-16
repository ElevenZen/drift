import os
import shutil
import tempfile
import unittest
import logging

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.stage_repo import run_primitive_4_stage_render_to_install, PackageStageChanges
from drift.render_package import render_package


class TestStageRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = os.path.abspath(self.temp_dir.name)

        # Create workspace config structure
        self.source_dir = os.path.join(self.drift_root, "src")
        self.render_dir = os.path.join(self.drift_root, "render")
        self.install_dir = os.path.join(self.drift_root, "install")
        self.backup_dir = os.path.join(self.drift_root, "backup")

        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.render_dir, exist_ok=True)
        os.makedirs(self.install_dir, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)

        # Create WorkspaceConfig
        self.workspace_config = WorkspaceConfig(
            source_directory="src",
            render_directory="render",
            install_directory="install",
            backup_directory="backup",
            packages_enable={
                "pkg_a": True,
                "pkg_b": True,
                "pkg_ignored": True,
                "pkg_misspelled": True
            },
            packages_enable_default=False,
            drift_root_path=self.drift_root
        )

        # 1. Set up pkg_a (regular)
        self.pkg_a_src = os.path.join(self.source_dir, "pkg_a")
        os.makedirs(self.pkg_a_src, exist_ok=True)
        with open(os.path.join(self.pkg_a_src, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "pkg_a"
            enable_install = true
            """)

        # 2. Set up pkg_b (with enable_install = false)
        self.pkg_b_src = os.path.join(self.source_dir, "pkg_b")
        os.makedirs(self.pkg_b_src, exist_ok=True)
        with open(os.path.join(self.pkg_b_src, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "pkg_b"
            enable_install = false
            """)

        # 3. Set up pkg_ignored (with .drift_ignore)
        self.pkg_ignored_src = os.path.join(self.source_dir, "pkg_ignored")
        os.makedirs(self.pkg_ignored_src, exist_ok=True)
        with open(os.path.join(self.pkg_ignored_src, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "pkg_ignored"
            enable_install = true
            """)
        # We write patterns using Stow PCRE matching format
        with open(os.path.join(self.pkg_ignored_src, ".drift_ignore"), "w", encoding="utf-8") as f:
            f.write("""
            # Ignore patterns with PCRE
            ignored_file.txt
            ignored_dir/.*
            .*\\.log
            # Escaped hash symbol
            escaped\\#comment
            """)

        # 4. Set up pkg_misspelled (with .driftignore instead of .drift_ignore)
        self.pkg_misspelled_src = os.path.join(self.source_dir, "pkg_misspelled")
        os.makedirs(self.pkg_misspelled_src, exist_ok=True)
        with open(os.path.join(self.pkg_misspelled_src, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "pkg_misspelled"
            enable_install = true
            """)
        with open(os.path.join(self.pkg_misspelled_src, ".driftignore"), "w", encoding="utf-8") as f:
            f.write("""
            misspelled_ignored.txt
            """)

        # Render active packages to establish render sandbox and metadata
        render_package(self.workspace_config, self.pkg_a_src)
        render_package(self.workspace_config, self.pkg_b_src)
        render_package(self.workspace_config, self.pkg_ignored_src)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stage_additions(self) -> None:
        """Verifies that new files in render/ are staged and recorded as added."""
        # Create a file in render/pkg_a/
        pkg_a_render = os.path.join(self.render_dir, "pkg_a")
        os.makedirs(pkg_a_render, exist_ok=True)
        file_path = os.path.join(pkg_a_render, "file1.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("Hello additions")

        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_a")
        self.assertEqual(changes[0].added_files, ["file1.txt"])
        self.assertEqual(changes[0].modified_files, [])
        self.assertEqual(changes[0].deleted_files, [])

        # Check file exists in install/
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_a", "file1.txt")))
        with open(os.path.join(self.install_dir, "pkg_a", "file1.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Hello additions")

    def test_stage_modifications(self) -> None:
        """Verifies that modified files in render/ are staged and recorded as modified."""
        # First stage to add it
        pkg_a_render = os.path.join(self.render_dir, "pkg_a")
        os.makedirs(pkg_a_render, exist_ok=True)
        file_path = os.path.join(pkg_a_render, "file1.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("Original content")

        run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        # Modify content in render/
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("Modified content")

        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_a")
        self.assertEqual(changes[0].added_files, [])
        self.assertEqual(changes[0].modified_files, ["file1.txt"])
        self.assertEqual(changes[0].deleted_files, [])

        # Check modified file in install/
        with open(os.path.join(self.install_dir, "pkg_a", "file1.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "Modified content")

    def test_stage_deletions(self) -> None:
        """Verifies that deleted files in render/ are pruned from install/, backed up, and recorded."""
        # First stage to add files
        pkg_a_render = os.path.join(self.render_dir, "pkg_a")
        os.makedirs(pkg_a_render, exist_ok=True)
        file1 = os.path.join(pkg_a_render, "file1.txt")
        file2 = os.path.join(pkg_a_render, "file2.txt")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("File 1 content")
        with open(file2, "w", encoding="utf-8") as f:
            f.write("File 2 content")

        run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        # Verify both exist in install
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_a", "file1.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_a", "file2.txt")))

        # Remove file2 from render/
        os.remove(file2)

        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_a")
        self.assertEqual(changes[0].added_files, [])
        self.assertEqual(changes[0].modified_files, [])
        self.assertEqual(changes[0].deleted_files, ["file2.txt"])

        # Verify file2 is removed from install/
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_a", "file2.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.install_dir, "pkg_a", "file1.txt")))

        # Verify file2 is backed up under backup/
        backup_file = os.path.join(self.backup_dir, "pkg_a", "deleted_files", "file2.txt")
        self.assertTrue(os.path.isfile(backup_file))
        with open(backup_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "File 2 content")

    def test_stage_skips_enable_install_false(self) -> None:
        """Verifies that packages with enable_install=false are skipped from stage."""
        pkg_b_render = os.path.join(self.render_dir, "pkg_b")
        os.makedirs(pkg_b_render, exist_ok=True)
        with open(os.path.join(pkg_b_render, "file_b.txt"), "w", encoding="utf-8") as f:
            f.write("Should not be copied")

        with self.assertRaises(RuntimeError) as cm:
            changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_b")
        self.assertIn("No active packages are enabled", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_b", "file_b.txt")))

    def test_stage_stow_ignores_and_symlinking(self) -> None:
        """Verifies that files matching PCRE .drift_ignore are ignored, and a symlink is created."""
        # 1. First render pkg_ignored
        render_package(self.workspace_config, self.pkg_ignored_src)

        # Create valid and ignored files in render folder
        pkg_ignored_render = os.path.join(self.render_dir, "pkg_ignored")
        with open(os.path.join(pkg_ignored_render, "valid.txt"), "w") as f:
            f.write("valid")
        with open(os.path.join(pkg_ignored_render, "ignored_file.txt"), "w") as f:
            f.write("ignored")
        with open(os.path.join(pkg_ignored_render, "error.log"), "w") as f:
            f.write("ignored log")
        
        ignored_sub_dir = os.path.join(pkg_ignored_render, "ignored_dir")
        os.makedirs(ignored_sub_dir, exist_ok=True)
        with open(os.path.join(ignored_sub_dir, "nested.txt"), "w") as f:
            f.write("ignored nested")

        # Config files themselves are also automatically ignored
        with open(os.path.join(pkg_ignored_render, PACKAGE_CONFIG_FILE_NAME), "w") as f:
            f.write("name = 'pkg_ignored'")

        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_ignored")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_ignored")
        self.assertEqual(changes[0].added_files, ["valid.txt"])
        self.assertEqual(changes[0].modified_files, [])
        self.assertEqual(changes[0].deleted_files, [])

        # Check that only valid.txt and ignore-related files exist in install/
        self.assertTrue(os.path.exists(os.path.join(self.install_dir, "pkg_ignored", PACKAGE_CONFIG_FILE_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_ignored", "valid.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_ignored", "ignored_file.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_ignored", "error.log")))
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_ignored", "ignored_dir")))

        # Check .drift_ignore was copied to install
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_ignored", ".drift_ignore")))

        # Check .stow-local-ignore file was created and contains ^/.drift_ignore
        stow_ignore_path = os.path.join(self.install_dir, "pkg_ignored", ".stow-local-ignore")
        self.assertTrue(os.path.isfile(stow_ignore_path))
        self.assertFalse(os.path.islink(stow_ignore_path))
        with open(stow_ignore_path, "r", encoding="utf-8") as f:
            stow_content = f.read()
        self.assertIn("^/.drift_ignore", stow_content)

    def test_stage_misspelled_driftignore_warning_and_handling(self) -> None:
        """Verifies that misspelled .driftignore is renamed/handled during render phase with warnings."""
        # Custom logger spy to capture warnings
        spy_log = []
        class SpyHandler(logging.Handler):
            def emit(self, record):
                spy_log.append(record.getMessage())
        
        logger = logging.getLogger("drift.render_package")
        handler = SpyHandler()
        logger.addHandler(handler)

        try:
            # Render pkg_misspelled
            render_package(self.workspace_config, self.pkg_misspelled_src)
        finally:
            logger.removeHandler(handler)

        # Check that warning was printed
        warning_found = any(".driftignore" in msg and "misspelled" in msg for msg in spy_log)
        self.assertTrue(warning_found)

        # Check that it was written to render/pkg_misspelled as .drift_ignore, and .driftignore was skipped
        pkg_misspelled_render = os.path.join(self.render_dir, "pkg_misspelled")
        self.assertTrue(os.path.isfile(os.path.join(pkg_misspelled_render, ".drift_ignore")))
        self.assertFalse(os.path.exists(os.path.join(pkg_misspelled_render, ".driftignore")))

        # Create valid and ignored files
        with open(os.path.join(pkg_misspelled_render, "valid.txt"), "w") as f:
            f.write("valid")
        with open(os.path.join(pkg_misspelled_render, "misspelled_ignored.txt"), "w") as f:
            f.write("ignored")

        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_misspelled")

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_misspelled")
        self.assertEqual(changes[0].added_files, ["valid.txt"])

        # Check that install/pkg_misspelled has .drift_ignore and .stow-local-ignore file
        install_pkg_misspelled = os.path.join(self.install_dir, "pkg_misspelled")
        self.assertTrue(os.path.isfile(os.path.join(install_pkg_misspelled, "valid.txt")))
        self.assertFalse(os.path.exists(os.path.join(install_pkg_misspelled, "misspelled_ignored.txt")))
        self.assertTrue(os.path.isfile(os.path.join(install_pkg_misspelled, ".drift_ignore")))
        
        stow_ignore_path = os.path.join(install_pkg_misspelled, ".stow-local-ignore")
        self.assertTrue(os.path.isfile(stow_ignore_path))
        self.assertFalse(os.path.islink(stow_ignore_path))
        with open(stow_ignore_path, "r", encoding="utf-8") as f:
            stow_content = f.read()
        self.assertIn("^/.drift_ignore", stow_content)

    def test_tree_relative_files_utility(self) -> None:
        """Tests tree_relative_files utility function."""
        from drift.file_utils import tree_relative_files
        nested_dir = os.path.join(self.drift_root, "nested_util")
        os.makedirs(nested_dir, exist_ok=True)
        
        # Test empty or nonexistent dir
        self.assertEqual(tree_relative_files(os.path.join(self.drift_root, "nonexistent")), [])
        
        # Create nested files
        with open(os.path.join(nested_dir, "file1.txt"), "w") as f:
            f.write("f1")
        sub = os.path.join(nested_dir, "subdir")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "file2.txt"), "w") as f:
            f.write("f2")
            
        self.assertEqual(tree_relative_files(nested_dir), ["file1.txt", "subdir/file2.txt"])

    def test_file_contents_differ_utility(self) -> None:
        """Tests file_contents_differ utility function."""
        from drift.file_utils import file_contents_differ
        util_dir = os.path.join(self.drift_root, "util_differ")
        os.makedirs(util_dir, exist_ok=True)
        
        f1 = os.path.join(util_dir, "f1.txt")
        f2 = os.path.join(util_dir, "f2.txt")
        f3 = os.path.join(util_dir, "f3.txt")
        
        with open(f1, "w") as f:
            f.write("hello world")
        with open(f2, "w") as f:
            f.write("hello world")
        with open(f3, "w") as f:
            f.write("hello world different")
            
        # Same contents and size
        self.assertFalse(file_contents_differ(f1, f2))
        # Different size/contents
        self.assertTrue(file_contents_differ(f1, f3))

    def test_rmdir_parents_utility(self) -> None:
        """Tests rmdir_parents utility function."""
        from drift.file_utils import rmdir_parents
        limit = os.path.join(self.drift_root, "limit_dir")
        os.makedirs(limit, exist_ok=True)
        
        sub = os.path.join(limit, "parent", "child", "grandchild")
        os.makedirs(sub, exist_ok=True)
        
        # Pruning from grandchild up to limit
        rmdir_parents(sub, limit)
        
        # grandchild, child, and parent should be removed
        self.assertFalse(os.path.exists(os.path.join(limit, "parent")))
        self.assertTrue(os.path.exists(limit))

    def test_stage_force_flag_not_discovered(self) -> None:
        """Verifies that staging a non-discovered package raises ValueError unless force is enabled."""
        with self.assertRaises(ValueError):
            run_primitive_4_stage_render_to_install(self.workspace_config, "nonexistent_package", force=False)
            
        # With force=True, it should proceed (but raise RuntimeError because RENDER folder doesn't exist for it, unless skipped)
        with self.assertRaises(RuntimeError) as cm:
            run_primitive_4_stage_render_to_install(self.workspace_config, "nonexistent_package", force=True)
        self.assertIn("Render sandbox directory for package 'nonexistent_package' does not exist", str(cm.exception))

    def test_stage_active_packages_empty_early_exit(self) -> None:
        """Verifies that stage returns early if there are no active packages."""
        # Create config with no enabled packages
        empty_config = WorkspaceConfig(
            source_directory="src",
            render_directory="render",
            install_directory="install",
            backup_directory="backup",
            packages_enable={},
            packages_enable_default=False,
            drift_root_path=self.drift_root
        )
        changes = run_primitive_4_stage_render_to_install(empty_config)
        self.assertEqual(changes, [])

    def test_stage_newly_ignored_file_gets_pruned_and_backed_up(self) -> None:
        """Verifies that if a tracked file in install/ becomes ignored, it gets pruned and backed up."""
        # 1. Stage pkg_a with file1.txt
        pkg_a_render = os.path.join(self.render_dir, "pkg_a")
        os.makedirs(pkg_a_render, exist_ok=True)
        with open(os.path.join(pkg_a_render, "file1.txt"), "w") as f:
            f.write("content")
            
        run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")
        self.assertTrue(os.path.isfile(os.path.join(self.install_dir, "pkg_a", "file1.txt")))

        # 2. Now write a .drift_ignore inside render/pkg_a/ to ignore file1.txt
        with open(os.path.join(pkg_a_render, ".drift_ignore"), "w") as f:
            f.write("file1.txt\n")

        # 3. Stage again
        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        # It should be deleted!
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_a")
        self.assertEqual(changes[0].deleted_files, ["file1.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_a", "file1.txt")))
        
        # Verify it was backed up
        backup_file = os.path.join(self.backup_dir, "pkg_a", "deleted_files", "file1.txt")
        self.assertTrue(os.path.isfile(backup_file))

    def test_backup_and_delete_file_utility(self) -> None:
        """Tests backup_and_delete_file utility function."""
        from drift.file_utils import backup_and_delete_file
        util_dir = os.path.join(self.drift_root, "util_backup_delete")
        os.makedirs(util_dir, exist_ok=True)
        
        limit_dir = os.path.join(util_dir, "limit")
        sub_dir = os.path.join(limit_dir, "nested", "dirs")
        os.makedirs(sub_dir, exist_ok=True)
        
        file_path = os.path.join(sub_dir, "test.txt")
        backup_path = os.path.join(util_dir, "backup", "test_backup.txt")
        
        with open(file_path, "w") as f:
            f.write("hello backup")
            
        backup_and_delete_file(file_path, backup_path, limit_dir=limit_dir)
        
        # Verify file is deleted
        self.assertFalse(os.path.exists(file_path))
        # Verify nested parent directories are pruned up to limit_dir
        self.assertFalse(os.path.exists(os.path.join(limit_dir, "nested")))
        self.assertTrue(os.path.exists(limit_dir))
        # Verify backup is created with same content
        self.assertTrue(os.path.isfile(backup_path))
        with open(backup_path, "r") as f:
            self.assertEqual(f.read(), "hello backup")

    def test_stage_aborts_on_uncommitted_local_modifications(self) -> None:
        """Verifies that stage raises RuntimeError if there are uncommitted modifications in install repo, unless force is True."""
        import subprocess
        # 1. Initialize git in self.install_dir to make it a tracked repository
        subprocess.run(["git", "init"], cwd=self.install_dir, check=True, capture_output=True)
        # Configure git identity so commit works everywhere
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.install_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.install_dir, check=True)

        # Create a package dir inside install/
        pkg_a_install = os.path.join(self.install_dir, "pkg_a")
        os.makedirs(pkg_a_install, exist_ok=True)
        
        # Create and commit a file to establish clean state
        test_file = os.path.join(pkg_a_install, "file1.txt")
        with open(test_file, "w") as f:
            f.write("clean content")
            
        subprocess.run(["git", "add", "pkg_a/file1.txt"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Verify staging works when repository is clean
        # Let's ensure render directory has a different content or same
        pkg_a_render = os.path.join(self.render_dir, "pkg_a")
        os.makedirs(pkg_a_render, exist_ok=True)
        with open(os.path.join(pkg_a_render, "file1.txt"), "w") as f:
            f.write("staged content")

        # 3. Create uncommitted modification (modify the file in install/)
        with open(test_file, "w") as f:
            f.write("modified uncommitted content")

        # Now, staging should raise RuntimeError because of uncommitted modifications
        with self.assertRaises(RuntimeError) as cm:
            run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a", force=False)
        self.assertIn("has uncommitted local modifications", str(cm.exception))

        # Staging with force=True should bypass the check and succeed
        changes = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a", force=True)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].package_name, "pkg_a")
        self.assertEqual(changes[0].modified_files, ["file1.txt"])


if __name__ == "__main__":
    unittest.main()
