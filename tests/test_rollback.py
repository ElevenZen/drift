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

    def test_rollback_aborts_without_force_if_no_midway_failure(self) -> None:
        # Attempt to run rollback under healthy conditions (pkg_a state is "installed")
        # Should raise a RuntimeError unless force=True
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertIn("The following packages are not in a failed midway/conflict state", str(ctx.exception))

        # With force=True, it should proceed and succeed
        run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=True)


if __name__ == "__main__":
    unittest.main()
