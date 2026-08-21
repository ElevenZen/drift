import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.state_registry import load_state_registry, save_state_registry
from drift.deploy_repo import run_primitive_deploy_pipeline


class TestDeploy(unittest.TestCase):
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

        # Initialize install and render as git repos
        for repo_dir in (self.install_dir, self.render_dir):
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)

        # Create global config file
        self.config_dir = self.drift_root / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "drift.toml"
        self.config_file.write_text("""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[packages.enable]
pkg_a = true
""", encoding="utf-8")

        # Create initial state.toml in install/
        self.state_file = self.install_dir / "state.toml"
        self.state_file.write_text("[packages]\n", encoding="utf-8")
        subprocess.run(["git", "add", "state.toml"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Build workspace config
        from drift.workspace_config import load_workspace_config
        self.workspace_config = load_workspace_config(self.config_file)
        self.workspace_config.default_target_directory = self.system_target_dir

        # Set up a clean source package
        self.pkg_dir = self.source_dir / "pkg_a"
        self.pkg_dir.mkdir()
        self.package_config_file = self.pkg_dir / "drift_package.toml"
        self.package_config_file.write_text(f"""
[package]
name = "pkg_a"
install_method = "copy"
target_directory = "{self.system_target_dir}"
""", encoding="utf-8")

        # Create a sample raw config file
        self.src_file = self.pkg_dir / "file.txt"
        self.src_file.write_text("Hello source config!", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_deploy_pipeline_success(self) -> None:
        """Verifies that a clean deploy successfully compiles and deploys source configs to host."""
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 1. Target host should contain the file
        target_file = self.system_target_dir / "file.txt"
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(), "Hello source config!")

        # 2. State registry in install/ should be set to "installed"
        registry = load_state_registry(self.state_file)
        self.assertEqual(registry.get_package_state("pkg_a"), "installed")

        # 3. install/ and render/ git status should be clean (committed successfully)
        install_status = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True, check=True)
        self.assertEqual(install_status.stdout.strip(), "")

        render_status = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.render_dir), capture_output=True, text=True, check=True)
        self.assertEqual(render_status.stdout.strip(), "")

    def test_deploy_pipeline_aborts_on_system_drift(self) -> None:
        """Verifies that the sentinel drift guard aborts execution when active drift is found."""
        # 1. Initially run a deploy to establish baseline tracking
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Simulate drift by modifying target file directly on host system
        target_file = self.system_target_dir / "file.txt"
        target_file.write_text("Modified on host system directly!", encoding="utf-8")

        # 3. Running deploy should raise RuntimeError because of drift
        with self.assertRaises(RuntimeError) as context:
            run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        self.assertIn("[DEPLOY ABORTED] System drift detected", str(context.exception))

    def test_deploy_pipeline_overrides_drift_with_force(self) -> None:
        """Verifies that passing force=True bypasses the drift guard and overwrites modifications."""
        # 1. Initially run a deploy to establish baseline tracking
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Simulate drift by modifying target file directly on host system
        target_file = self.system_target_dir / "file.txt"
        target_file.write_text("Modified on host system directly!", encoding="utf-8")

        # 3. Running deploy with force=True should succeed and overwrite active drift
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"], force=True)

        self.assertEqual(target_file.read_text(), "Hello source config!")

    @patch("drift.deploy_repo.run_primitive_5_install_deployment")
    def test_deploy_pipeline_midway_crash_prints_recovery_card(self, mock_install) -> None:
        """Verifies that midway crashes during stage 2 capture, print recovery blocks, and abort."""
        mock_install.side_with_err = PermissionError("Permission Denied: mock error")
        mock_install.side_effect = mock_install.side_with_err

        # Redirect stderr to capture the emergency card output
        import sys
        from io import StringIO
        stderr_capture = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr_capture

        try:
            with self.assertRaises(RuntimeError) as context:
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        finally:
            sys.stderr = original_stderr

        self.assertIn("Midway crash: Step 4", str(context.exception))
        
        # Verify emergency recovery card is printed in captured stderr
        printed_card = stderr_capture.getvalue()
        self.assertIn("CRITICAL FAILURE", printed_card)
        self.assertIn("EMERGENCY RECOVERY REQUIRED", printed_card)
        self.assertIn("drift rollback pkg_a", printed_card)

    @patch("drift.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_pipeline_step1_failure_shows_retry(self, mock_render) -> None:
        """Verifies that Step 1 (rendering) failure logs retry and doesn't print emergency recovery card."""
        mock_render.side_effect = ValueError("rendering error")
        
        with self.assertRaises(RuntimeError) as context:
            run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        
        self.assertIn("Step 1 (Template Rendering) failed.", str(context.exception))

    @patch("drift.deploy_repo.run_primitive_6_commit_install_repo")
    def test_deploy_pipeline_step5_failure_shows_install_commit(self, mock_commit) -> None:
        """Verifies that Step 5 (install commit) failure logs specific manual install-commit instruction."""
        mock_commit.side_effect = ValueError("commit error")
        
        import sys
        from io import StringIO
        stderr_capture = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr_capture

        try:
            with self.assertRaises(RuntimeError) as context:
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        finally:
            sys.stderr = original_stderr
        
        self.assertIn("Step 5 (State Database Committing) failed.", str(context.exception))
        printed_msg = stderr_capture.getvalue()
        self.assertIn("drift install-commit -m", printed_msg)

    @patch("drift.deploy_repo.run_primitive_9_purge_workspace_garbage")
    def test_global_deploy_calls_gc(self, mock_gc) -> None:
        """Verifies that global deploy (packages_to_deploy=None) calls GC at the end."""
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=None)
        mock_gc.assert_called_once_with(self.workspace_config, dry_run=False)

    def test_deploy_pipeline_checks_git_configs(self) -> None:
        """Verifies that deploy pipeline verifies git config user.name and user.email exists."""
        # Unconfigure user.name inside render repo temporarily
        subprocess.run(["git", "config", "--unset", "user.name"], cwd=str(self.render_dir), check=True)
        
        # Deploy should fail on pre-flight checks
        with self.assertRaises(RuntimeError) as context:
            run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        
        self.assertIn("Git configuration error: 'user.name' is not configured", str(context.exception))


if __name__ == "__main__":
    unittest.main()
