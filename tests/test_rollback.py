import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.state_registry import load_state_registry, save_state_registry
from drift.rollback_repo import run_primitive_8_rollback_recovery


class TestRollback(unittest.TestCase):
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

        # Initialize install as an empty git repo
        subprocess.run(["git", "init"], cwd=str(self.install_dir), check=True, capture_output=True)
        # Configure dummy git user
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), check=True, capture_output=True)

        self.workspace_config = WorkspaceConfig(
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={
                "pkg_a": True,
            },
            packages_enable_default=False,
            drift_root_path=self.drift_root,
            default_target_directory=self.system_target_dir
        )

        # 1. Setup pkg_a in source
        self.pkg_a_src = self.source_dir / "pkg_a"
        self.pkg_a_src.mkdir(parents=True, exist_ok=True)
        with open(self.pkg_a_src / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # 2. Setup initial committed state in install/
        self.pkg_a_install = self.install_dir / "pkg_a"
        self.pkg_a_install.mkdir(parents=True, exist_ok=True)
        with open(self.pkg_a_install / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("clean content")

        # Initial state registry
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.set_package_deployed_files("pkg_a", [Path("file.txt")])
        save_state_registry(state_file, registry)

        # Initial commit in install repo
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial deploy"], cwd=str(self.install_dir), check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rollback_reverts_uncommitted_changes_and_reinstalls_clean_state(self) -> None:
        # Simulate a failed midway deployment/staging:
        # 1. We modify file.txt in install/ to represent dirty uncommitted state
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("dirty failed content")
            
        # 2. We add an untracked file to install/pkg_a/
        with open(self.pkg_a_install / "untracked.txt", "w", encoding="utf-8") as f:
            f.write("untracked")

        # 3. We update state.toml to "deploying" (representing midway failure)
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "deploying")
        save_state_registry(state_file, registry)

        # 4. We also dirty the target system file
        with open(self.system_target_dir / "file.txt", "w", encoding="utf-8") as f:
            f.write("system dirty content")

        # Run rollback
        run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)

        # Verify:
        # 1. install/pkg_a/file.txt is restored to HEAD ("clean content")
        self.assertEqual((self.pkg_a_install / "file.txt").read_text(encoding="utf-8"), "clean content")

        # 2. install/pkg_a/untracked.txt is deleted (git clean)
        self.assertFalse((self.pkg_a_install / "untracked.txt").exists())

        # 3. target system file is re-deployed and matches "clean content"
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")

        # 4. state.toml is restored to "installed" state
        reloaded_registry = load_state_registry(state_file)
        self.assertEqual(reloaded_registry.get_package_state("pkg_a"), "installed")

    def test_rollback_first_time_package_uninstalls_and_restores_backups(self) -> None:
        """Verifies that a first-time package failing midway is cleanly uninstalled and backups restored on rollback."""
        pkg_first = "pkg_first_time"

        # 1. Setup in src/
        pkg_src = self.source_dir / pkg_first
        pkg_src.mkdir(parents=True, exist_ok=True)
        with open(pkg_src / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_first}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # 2. Setup in install/ (uncommitted, first-time stage)
        pkg_install = self.install_dir / pkg_first
        pkg_install.mkdir(parents=True, exist_ok=True)
        with open(pkg_install / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_first}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)
        with open(pkg_install / "app_config.json", "w", encoding="utf-8") as f:
            f.write('{"installed": true}')

        # 3. Simulate state.toml having recorded the package as "deploying" with deployed_files
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg_first, "deploying", install_method="copy")
        registry.set_package_deployed_files(pkg_first, [Path("app_config.json")])
        save_state_registry(state_file, registry)

        # 4. Simulate target host having the partially delivered file
        (self.system_target_dir / "app_config.json").write_text('{"installed": true}', encoding="utf-8")

        # 5. Simulate an existing host file that was backed up during collision guard
        backup_overwritten = self.backup_dir / pkg_first / "overwritten"
        backup_overwritten.mkdir(parents=True, exist_ok=True)
        (backup_overwritten / "existing_host_file.txt").write_text("original user content", encoding="utf-8")

        # Enable in workspace config
        workspace_cfg = WorkspaceConfig(
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={
                pkg_first: True,
            },
            packages_enable_default=False,
            drift_root_path=self.drift_root,
            default_target_directory=self.system_target_dir
        )

        # Execute rollback on first-time package
        restored = run_primitive_8_rollback_recovery(workspace_cfg, [pkg_first], force=False)
        self.assertIn(pkg_first, restored)

        # Verify:
        # 1. Newly delivered file on host target is removed
        self.assertFalse((self.system_target_dir / "app_config.json").exists())

        # 2. Original backed up file is restored to the host target
        self.assertTrue((self.system_target_dir / "existing_host_file.txt").exists())
        self.assertEqual(
            (self.system_target_dir / "existing_host_file.txt").read_text(encoding="utf-8"),
            "original user content"
        )

        # 3. install/pkg_first directory is removed
        self.assertFalse(pkg_install.exists())

        # 4. state.toml does not contain pkg_first
        reloaded_registry = load_state_registry(state_file)
        self.assertNotIn(pkg_first, reloaded_registry.packages)

    def test_rollback_mixed_first_time_and_existing_packages(self) -> None:
        """Verifies rollback correctly redeploys existing packages from HEAD while uninstalling first-time packages."""
        pkg_first = "pkg_brand_new"

        # Setup first-time package in src & install
        pkg_src = self.source_dir / pkg_first
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_first}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        pkg_install = self.install_dir / pkg_first
        pkg_install.mkdir(parents=True, exist_ok=True)
        (pkg_install / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_first}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (pkg_install / "brand_new.txt").write_text("brand new", encoding="utf-8")

        # Dirty existing pkg_a
        (self.pkg_a_install / "file.txt").write_text("corrupted pkg_a", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("corrupted pkg_a on host", encoding="utf-8")

        # Put new file on host
        (self.system_target_dir / "brand_new.txt").write_text("brand new on host", encoding="utf-8")

        # Set both packages to "deploying"
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "deploying")
        registry.set_package_state(pkg_first, "deploying", install_method="copy")
        registry.set_package_deployed_files(pkg_first, [Path("brand_new.txt")])
        save_state_registry(state_file, registry)

        workspace_cfg = WorkspaceConfig(
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={
                "pkg_a": True,
                pkg_first: True,
            },
            packages_enable_default=False,
            drift_root_path=self.drift_root,
            default_target_directory=self.system_target_dir
        )

        restored = run_primitive_8_rollback_recovery(workspace_cfg, ["pkg_a", pkg_first], force=False)
        self.assertEqual(set(restored), {"pkg_a", pkg_first})

        # Verify pkg_a is restored to clean committed state ("clean content")
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded = load_state_registry(state_file)
        self.assertEqual(reloaded.get_package_state("pkg_a"), "installed")

        # Verify pkg_first is uninstalled
        self.assertFalse((self.system_target_dir / "brand_new.txt").exists())
        self.assertFalse(pkg_install.exists())
        self.assertNotIn(pkg_first, reloaded.packages)


if __name__ == "__main__":
    unittest.main()
