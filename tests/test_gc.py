"""Tests for Primitive 9: Workspace Garbage Collection (drift gc)."""

import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.workspace_gc import (
    run_primitive_9_purge_workspace_garbage,
    purge_render_folders,
    purge_install_folders,
    purge_zombie_folders,
)
from drift.constants import PACKAGE_CONFIG_FILE_NAME, CONFIG_DIR_NAME
from drift.cli.actions import execute_gc


class TestWorkspaceGc(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()

        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"

        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        for d in [self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Initialize Git repos in render/ and install/
        for repo_dir in [self.render_dir, self.install_dir]:
            subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), capture_output=True, check=True)

        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir,
            ),
            packages_enable={}
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_gc_purges_disabled_package_from_render(self) -> None:
        """Verifies that packages disabled in workspace configuration are purged from render/."""
        pkg = "pkg_disabled"
        (self.source_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.source_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        (self.render_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.render_dir / pkg / "rendered_file.txt").write_text("rendered", encoding="utf-8")

        # Create config/ directory in render/ which should be preserved
        (self.render_dir / CONFIG_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / CONFIG_DIR_NAME / "drift_workspace.toml").write_text("# config", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = False

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)

        self.assertEqual(result.status, "SUCCESS")
        self.assertIn(pkg, result.purged_render_zombies)
        self.assertFalse((self.render_dir / pkg).exists())
        self.assertTrue((self.render_dir / CONFIG_DIR_NAME).exists())

    def test_gc_purges_missing_source_package_from_render(self) -> None:
        """Verifies that packages in render/ whose source directory is deleted from src/ are purged."""
        pkg = "pkg_deleted_from_src"
        (self.render_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        self.workspace_config.packages_enable_default = True

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)

        self.assertEqual(result.status, "SUCCESS")
        self.assertIn(pkg, result.purged_render_zombies)
        self.assertFalse((self.render_dir / pkg).exists())

    def test_gc_preserves_enabled_packages_in_render(self) -> None:
        """Verifies that active and enabled packages in render/ are not touched."""
        pkg = "pkg_active"
        (self.source_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.source_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        (self.render_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.render_dir / pkg / "file.txt").write_text("content", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = True

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)

        self.assertEqual(result.status, "SUCCESS")
        self.assertNotIn(pkg, result.purged_render_zombies)
        self.assertTrue((self.render_dir / pkg).exists())
        self.assertTrue((self.render_dir / pkg / "file.txt").exists())

    def test_gc_purges_zombies_in_render_and_install(self) -> None:
        """Verifies that folders without drift_package.toml (zombies) are purged."""
        zombie_r = self.render_dir / "zombie_render"
        zombie_r.mkdir(parents=True, exist_ok=True)
        (zombie_r / "dummy.txt").write_text("garbage", encoding="utf-8")

        zombie_i = self.install_dir / "zombie_install"
        zombie_i.mkdir(parents=True, exist_ok=True)
        (zombie_i / "dummy.txt").write_text("garbage", encoding="utf-8")

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)

        self.assertIn("zombie_render", result.purged_render_zombies)
        self.assertIn("zombie_install", result.purged_install_zombies)
        self.assertFalse(zombie_r.exists())
        self.assertFalse(zombie_i.exists())

    def test_gc_dry_run(self) -> None:
        """Verifies that dry_run=True reports what would be purged without deleting."""
        pkg = "pkg_dry"
        (self.render_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = False

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config, dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertIn(pkg, result.purged_render_zombies)
        self.assertTrue((self.render_dir / pkg).exists())

    def test_execute_gc_cli(self) -> None:
        """Verifies execute_gc CLI action works smoothly."""
        # Create config file for load_workspace_config
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "drift_workspace.toml").write_text("[workspace]\n[packages.enable]\n", encoding="utf-8")

        # Run execute_gc
        execute_gc(self.drift_root, dry_run=False, json_mode=False, no_hooks=True)


if __name__ == "__main__":
    unittest.main()
