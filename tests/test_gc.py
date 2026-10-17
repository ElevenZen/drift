"""Tests for Primitive 9: Workspace Garbage Collection (drift gc)."""

import os
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.primitives.workspace_gc import (
    run_primitive_9_purge_workspace_garbage,
    purge_render_folders,
    purge_install_folders,
    purge_redundant_wal_folders,
    check_has_package_wal_dir,
)
from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    CONFIG_DIR_NAME,
    BackupSubfolder,
    WAL_ACTIONS_FILE_NAME,
    set_test_mode,
)
from drift.cli.actions import execute_gc


class TestWorkspaceGc(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True, enable_logging=False)
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
        set_test_mode(True, enable_logging=False)
        self.temp_dir.cleanup()

    def test_gc_purges_disabled_package_from_render(self) -> None:
        """Verifies that packages disabled in workspace configuration are purged from render/."""
        pkg = "pkg_disabled"
        (self.source_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.source_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.render_dir / pkg / "rendered_file.txt").write_text("rendered", encoding="utf-8")

        # Create a stray config/ directory in render/ — a forbidden-name dir that should be purged.
        (self.render_dir / CONFIG_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / CONFIG_DIR_NAME / "drift_workspace.toml").write_text("# config", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = False

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)

        self.assertEqual(result.status, "SUCCESS")
        self.assertIn(pkg, result.purged_render_zombies)
        self.assertFalse((self.render_dir / pkg).exists())
        # Stray forbidden-name dirs in render/ are now purged.
        self.assertIn(CONFIG_DIR_NAME, result.purged_render_zombies)
        self.assertFalse((self.render_dir / CONFIG_DIR_NAME).exists())

    def test_gc_purges_missing_source_package_from_render(self) -> None:
        """Verifies that packages in render/ whose source directory is deleted from src/ are purged."""
        pkg = "pkg_deleted_from_src"
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

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

        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
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
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

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

        from drift.primitives.workspace_repair import repair_drift_workspace
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            repair_drift_workspace(self.drift_root)
            execute_gc(self.drift_root, dry_run=False, json_mode=False, no_hooks=True)

    def test_gc_preserves_legacy_root_package_config_with_warning(self) -> None:
        """Verifies that packages with legacy root drift_package.toml are not purged as zombies."""
        from drift.core.constants import set_test_mode
        pkg = "pkg_legacy"
        (self.source_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.source_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        (self.render_dir / pkg).mkdir(parents=True, exist_ok=True)
        (self.render_dir / pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.render_dir / pkg / "file.txt").write_text("content", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = True

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs(level="INFO") as cm:
                result = run_primitive_9_purge_workspace_garbage(self.workspace_config)
                self.assertEqual(result.status, "SUCCESS")
                self.assertNotIn(pkg, result.purged_render_zombies)
                self.assertTrue((self.render_dir / pkg).exists())
                self.assertTrue(any("DEPRECATION" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_gc_purges_ghost_packages_from_state_registry(self) -> None:
        """Verifies that ghost packages (in state.toml but missing directory in install/) are purged from state."""
        from drift.core.state_registry import StateRegistry, PackageState, load_state_registry

        # Setup active package with valid directory
        normal_pkg = "pkg_normal"
        (self.source_dir / normal_pkg).mkdir(parents=True, exist_ok=True)
        (self.source_dir / normal_pkg / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.render_dir / normal_pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / normal_pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        (self.install_dir / normal_pkg / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.install_dir / normal_pkg / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        self.workspace_config.packages_enable[normal_pkg] = True

        # Ghost package: registered in state.toml, but has NO directory in install/
        ghost_pkg = "pkg_ghost"
        self.workspace_config.packages_enable[ghost_pkg] = True

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={
                normal_pkg: PackageState(state="installed"),
                ghost_pkg: PackageState(state="installed"),
            },
            state_file=state_file,
        )
        registry.save()

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)
        self.assertEqual(result.status, "SUCCESS")

        reloaded = load_state_registry(state_file)
        self.assertIn(normal_pkg, reloaded.packages)
        self.assertNotIn(ghost_pkg, reloaded.packages)

    def test_gc_dry_run_preserves_ghost_packages_in_state_registry(self) -> None:
        """Verifies that dry-run mode does not purge ghost packages from state.toml."""
        from drift.core.state_registry import StateRegistry, PackageState, load_state_registry

        ghost_pkg = "pkg_ghost_dry"
        self.workspace_config.packages_enable[ghost_pkg] = True

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={
                ghost_pkg: PackageState(state="installed"),
            },
            state_file=state_file,
        )
        registry.save()

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config, dry_run=True)
        self.assertEqual(result.status, "SUCCESS")

        reloaded = load_state_registry(state_file)
        self.assertIn(ghost_pkg, reloaded.packages)

    def _setup_valid_pkg_dirs(self, pkg: str) -> None:
        """Helper to create valid package directories across src/, render/, and install/."""
        for base in (self.source_dir, self.render_dir, self.install_dir):
            internal = base / pkg / DRIFT_INTERNAL_DIR_NAME
            internal.mkdir(parents=True, exist_ok=True)
            (internal / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")
        self.workspace_config.packages_enable[pkg] = True

    def _create_pkg_wal(self, pkg: str, with_overwritten_backup: bool = False) -> Path:
        """Helper to create backup/<pkg>/wal/actions.json and optional overwritten backup file."""
        pkg_backup = self.backup_dir / pkg
        wal_dir = pkg_backup / BackupSubfolder.WAL
        wal_dir.mkdir(parents=True, exist_ok=True)
        (wal_dir / WAL_ACTIONS_FILE_NAME).write_text("[]", encoding="utf-8")
        if with_overwritten_backup:
            overwritten_dir = pkg_backup / BackupSubfolder.OVERWRITTEN
            overwritten_dir.mkdir(parents=True, exist_ok=True)
            (overwritten_dir / "saved.conf").write_text("saved", encoding="utf-8")
        return wal_dir

    def test_gc_purges_redundant_wal_folders_and_preserves_rollback_eligible(self) -> None:
        """Verifies GC purges redundant WALs (installed, unregistered, ghost) while keeping rollback-eligible WALs."""
        from drift.core.state_registry import StateRegistry, PackageState

        # 1. Installed package with redundant WAL + real overwritten backup
        pkg_installed = "pkg_installed"
        self._setup_valid_pkg_dirs(pkg_installed)
        wal_installed = self._create_pkg_wal(pkg_installed, with_overwritten_backup=True)

        # 2. Unregistered package with redundant WAL only (should prune backup/<pkg> completely)
        pkg_unregistered = "pkg_unregistered"
        wal_unregistered = self._create_pkg_wal(pkg_unregistered, with_overwritten_backup=False)

        # 3. Ghost package in 'installing' state (missing install/<pkg> dir -> ghost -> WAL is redundant)
        pkg_ghost_installing = "pkg_ghost_installing"
        self.workspace_config.packages_enable[pkg_ghost_installing] = True
        wal_ghost = self._create_pkg_wal(pkg_ghost_installing, with_overwritten_backup=False)

        # 4. Rollback-eligible packages ('staging', 'staged', 'installing') -> WAL must be preserved
        rollback_pkgs = {
            "pkg_staging": "staging",
            "pkg_staged": "staged",
            "pkg_installing": "installing",
        }
        for r_pkg in rollback_pkgs:
            self._setup_valid_pkg_dirs(r_pkg)
            self._create_pkg_wal(r_pkg, with_overwritten_backup=False)

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={
                pkg_installed: PackageState(state="installed"),
                pkg_ghost_installing: PackageState(state="installing"),
                **{k: PackageState(state=v) for k, v in rollback_pkgs.items()},
            },
            state_file=state_file,
        )
        registry.save()

        # Pass 1 (Cold / Mutation Pass)
        result = run_primitive_9_purge_workspace_garbage(self.workspace_config)
        self.assertEqual(result.status, "SUCCESS")
        self.assertCountEqual(
            result.purged_wal_packages,
            [pkg_installed, pkg_unregistered, pkg_ghost_installing],
        )

        # Installed package: WAL removed, overwritten backup preserved
        self.assertFalse(wal_installed.exists())
        self.assertTrue((self.backup_dir / pkg_installed / BackupSubfolder.OVERWRITTEN / "saved.conf").is_file())

        # Unregistered & Ghost packages: WAL removed and empty backup/<pkg> pruned
        self.assertFalse(wal_unregistered.exists())
        self.assertFalse((self.backup_dir / pkg_unregistered).exists())
        self.assertFalse(wal_ghost.exists())
        self.assertFalse((self.backup_dir / pkg_ghost_installing).exists())

        # Rollback-eligible packages: WAL preserved intact
        for r_pkg in rollback_pkgs:
            self.assertTrue(check_has_package_wal_dir(self.backup_dir / r_pkg))
            self.assertTrue((self.backup_dir / r_pkg / BackupSubfolder.WAL / WAL_ACTIONS_FILE_NAME).is_file())

        # Pass 2 (Warm / Idempotent Pass)
        result_warm = run_primitive_9_purge_workspace_garbage(self.workspace_config)
        self.assertEqual(result_warm.status, "SUCCESS")
        self.assertEqual(result_warm.purged_wal_packages, [])
        for r_pkg in rollback_pkgs:
            self.assertTrue(check_has_package_wal_dir(self.backup_dir / r_pkg))

    def test_gc_dry_run_reports_redundant_wal_without_deleting(self) -> None:
        """Verifies dry_run=True reports redundant WAL packages without removing WAL files on disk."""
        from drift.core.state_registry import StateRegistry, PackageState

        pkg_installed = "pkg_installed_dry"
        self._setup_valid_pkg_dirs(pkg_installed)
        wal_installed = self._create_pkg_wal(pkg_installed)

        pkg_installing = "pkg_installing_dry"
        self._setup_valid_pkg_dirs(pkg_installing)
        wal_installing = self._create_pkg_wal(pkg_installing)

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={
                pkg_installed: PackageState(state="installed"),
                pkg_installing: PackageState(state="installing"),
            },
            state_file=state_file,
        )
        registry.save()

        result = run_primitive_9_purge_workspace_garbage(self.workspace_config, dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.purged_wal_packages, [pkg_installed])
        self.assertTrue(wal_installed.exists())
        self.assertTrue(wal_installing.exists())

    def test_purge_redundant_wal_folders_standalone_without_state_registry_arg(self) -> None:
        """Verifies purge_redundant_wal_folders loads state.toml automatically when state_registry is None."""
        from drift.core.state_registry import StateRegistry, PackageState

        pkg_installed = "pkg_auto_state"
        self._create_pkg_wal(pkg_installed)

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={pkg_installed: PackageState(state="installed")},
            state_file=state_file,
        )
        registry.save()

        purged = purge_redundant_wal_folders(self.workspace_config)
        self.assertEqual(purged, [pkg_installed])
        self.assertFalse(check_has_package_wal_dir(self.backup_dir / pkg_installed))
        self.assertEqual(purge_redundant_wal_folders(self.workspace_config), [])

    def test_install_commit_purges_only_stable_target_package_wals(self) -> None:
        """Verifies run_primitive_6_commit_install_repo reuses purge_redundant_wal_folders and preserves midway WALs."""
        from drift.core.state_registry import StateRegistry, PackageState
        from drift.primitives.install_repo import run_primitive_6_commit_install_repo

        pkg_a = "pkg_a"
        pkg_b = "pkg_b"
        pkg_midway = "pkg_midway"
        for pkg in (pkg_a, pkg_b, pkg_midway):
            self._setup_valid_pkg_dirs(pkg)
            self._create_pkg_wal(pkg)

        state_file = self.install_dir / "state.toml"
        registry = StateRegistry(
            packages={
                pkg_a: PackageState(state="installed"),
                pkg_b: PackageState(state="installed"),
                pkg_midway: PackageState(state="installing"),
            },
            state_file=state_file,
        )
        registry.save()

        # 1. Scoped install-commit for pkg_a: only pkg_a's WAL is cleared; pkg_b and pkg_midway keep their WALs
        run_primitive_6_commit_install_repo(self.workspace_config, "Commit pkg_a", target_pkgs=[pkg_a])
        self.assertFalse(check_has_package_wal_dir(self.backup_dir / pkg_a))
        self.assertTrue(check_has_package_wal_dir(self.backup_dir / pkg_b))
        self.assertTrue(check_has_package_wal_dir(self.backup_dir / pkg_midway))

        # 2. Unscoped install-commit: pkg_b's WAL is cleared (stable 'installed' state),
        #    while pkg_midway's WAL is preserved (uncommitted 'installing' state)
        run_primitive_6_commit_install_repo(self.workspace_config, "Commit all")
        self.assertFalse(check_has_package_wal_dir(self.backup_dir / pkg_b))
        self.assertTrue(check_has_package_wal_dir(self.backup_dir / pkg_midway))


if __name__ == "__main__":
    unittest.main()
