import os
import shutil
import tempfile
import unittest
import logging

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.stage_repo import run_primitive_4_stage_render_to_install
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

        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(added, ["pkg_a/file1.txt"])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, [])
        self.assertEqual(redeploy, ["pkg_a"])

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

        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(added, [])
        self.assertEqual(modified, ["pkg_a/file1.txt"])
        self.assertEqual(deleted, [])
        self.assertEqual(redeploy, ["pkg_a"])

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

        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        self.assertEqual(added, [])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, ["pkg_a/file2.txt"])
        self.assertEqual(redeploy, ["pkg_a"])

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
            added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_b")
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

        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_ignored")

        self.assertEqual(added, ["pkg_ignored/valid.txt"])
        self.assertEqual(redeploy, ["pkg_ignored"])

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

        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_misspelled")

        self.assertEqual(added, ["pkg_misspelled/valid.txt"])

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
        """Tests file_contents_differ utility function using hash comparisons."""
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
        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(empty_config)
        self.assertEqual(added, [])
        self.assertEqual(modified, [])
        self.assertEqual(deleted, [])
        self.assertEqual(redeploy, [])

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
        added, modified, deleted, redeploy = run_primitive_4_stage_render_to_install(self.workspace_config, "pkg_a")

        # It should be deleted!
        self.assertEqual(deleted, ["pkg_a/file1.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.install_dir, "pkg_a", "file1.txt")))
        
        # Verify it was backed up
        backup_file = os.path.join(self.backup_dir, "pkg_a", "deleted_files", "file1.txt")
        self.assertTrue(os.path.isfile(backup_file))


if __name__ == "__main__":
    unittest.main()
