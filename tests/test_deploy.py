import os
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.lifecycle_hooks import HookExecFlags
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
        self.config_file = self.config_dir / "drift_workspace.toml"
        self.config_file.write_text(f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "{self.system_target_dir}"
default_install_method = "copy"

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
        self.workspace_config = load_workspace_config(self.drift_root)
        self.workspace_config.workspace.default_target_directory = self.system_target_dir

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
        """Verifies that global deploy (packages_to_deploy=()) calls GC at the end."""
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=())
        mock_gc.assert_called_once_with(self.workspace_config, dry_run=False, flags=None)

    def test_deploy_pipeline_checks_git_configs(self) -> None:
        """Verifies that deploy pipeline verifies git config user.name and user.email exists."""
        # Unconfigure user.name inside render repo temporarily
        subprocess.run(["git", "config", "--unset", "user.name"], cwd=str(self.render_dir), check=True)
        
        env_override = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "",
            "GIT_COMMITTER_NAME": "",
        }
        with patch.dict(os.environ, env_override, clear=False):
            # Deploy should fail on pre-flight checks
            with self.assertRaises(RuntimeError) as context:
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
            
            self.assertIn("Git configuration error: 'user.name' is not configured", str(context.exception))

    def test_deploy_post_update_hook_failure_with_rollback_on_failure_false(self) -> None:
        """Verifies that when rollback_on_failure=False, a failing post_update hook stops and reports without leaving package in deploying state."""
        # 1. First-time deploy succeeds
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 2. Add a failing post_update hook script and set rollback_on_failure = false
        pkg_dir = self.source_dir / "pkg_a"
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        hook_file = scripts_dir / "post_update.sh"
        hook_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        post_update = "scripts/post_update.sh"
        rollback_on_failure = false
        """, encoding="utf-8")

        (pkg_dir / "extra.conf").write_text("extra content", encoding="utf-8")

        # 3. Deploy update - should fail on hook but NOT report midway crash
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a"],
                flags=HookExecFlags(streaming=False),
            )

        self.assertNotIn("Midway crash", str(ctx.exception))
        self.assertIn("stopped due to hook failure", str(ctx.exception))

        # 4. Verify extra.conf was successfully delivered and package is recorded as installed
        self.assertTrue((self.system_target_dir / "extra.conf").is_file())
        state_file = self.install_dir / "state.toml"
        state_registry = load_state_registry(state_file)
        self.assertEqual(state_registry.get_package_state("pkg_a"), "installed")

        # 5. Fix hook and re-deploy without needing rollback or force
        hook_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            flags=HookExecFlags(streaming=False),
        )
        self.assertTrue((self.system_target_dir / "extra.conf").is_file())

    def test_deploy_post_update_hook_failure_with_rollback_on_failure_true(self) -> None:
        """Verifies that when rollback_on_failure=True (default), a failing post_update hook triggers emergency midway crash state."""
        # 1. First-time deploy succeeds
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 2. Add a failing post_update hook script with default rollback_on_failure = true
        pkg_dir = self.source_dir / "pkg_a"
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        hook_file = scripts_dir / "post_update.sh"
        hook_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        post_update = "scripts/post_update.sh"
        rollback_on_failure = true
        """, encoding="utf-8")

        # 3. Deploy update - should fail with Midway crash
        with patch("sys.stderr", new=StringIO()):
            with self.assertRaises(RuntimeError) as ctx:
                run_primitive_deploy_pipeline(
                    self.workspace_config,
                    packages_to_deploy=["pkg_a"],
                    flags=HookExecFlags(streaming=False),
                )

        self.assertIn("Midway crash", str(ctx.exception))

        # 4. Verify package is in 'deploying' state requiring rollback
        state_file = self.install_dir / "state.toml"
        state_registry = load_state_registry(state_file)
        self.assertEqual(state_registry.get_package_state("pkg_a"), "deploying")

        # 5. Subsequent deploy without force or rollback aborts with safety check
        from drift.install_repo import deploy_one_package_with_error_wrapping
        with self.assertRaises(RuntimeError) as ctx2:
            deploy_one_package_with_error_wrapping(
                workspace_config=self.workspace_config,
                state_registry=state_registry,
                pkg="pkg_a",
                resolve_symlinks=True,
                force=False,
            )
        self.assertIn("Safety Abort", str(ctx2.exception))
        self.assertIn("drift rollback pkg_a", str(ctx2.exception))

    def test_deploy_pipeline_skips_unchanged_packages(self) -> None:
        """Verifies that packages with no stage changes are skipped during physical deployment."""
        # 1. Create a second package pkg_b
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir()
        (pkg_b_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("pkg_b initial content", encoding="utf-8")

        # Initial deploy of both packages
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 2)

        # 2. Modify only pkg_a
        (self.pkg_dir / "file.txt").write_text("Hello updated source config!", encoding="utf-8")

        # Deploy again
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"])
        self.assertEqual(res2.status, "SUCCESS")
        deployed_names = [p.package for p in res2.deployed_packages]
        self.assertEqual(deployed_names, ["pkg_a"])
        self.assertEqual((self.system_target_dir / "file.txt").read_text(), "Hello updated source config!")

    def test_deploy_pipeline_skips_all_when_no_changes(self) -> None:
        """Verifies that when zero packages have stage changes, physical install and commit steps are skipped."""
        # 1. Initial deploy
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)

        # 2. Second deploy with no changes
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.deployed_packages, [])

    def test_deploy_pipeline_with_redeploy_flag(self) -> None:
        """Verifies that passing redeploy=True forces redeployment even when zero stage changes exist."""
        # 1. Initial deploy
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")

        # 2. Second deploy with redeploy=True
        res2 = run_primitive_deploy_pipeline(
            self.workspace_config, packages_to_deploy=["pkg_a"], redeploy=True
        )
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(len(res2.deployed_packages), 1)
        self.assertEqual(res2.deployed_packages[0].package, "pkg_a")

    def test_deploy_pipeline_redeploys_on_hook_modification(self) -> None:
        """Verifies that modifying a lifecycle hook script triggers redeployment even if deployable files are unchanged."""
        # 1. Setup hook script and configure it
        hook_file = self.pkg_dir / "post_install.sh"
        marker_file = self.drift_root / "hook_executed.txt"
        hook_file.write_text(f"#!/bin/sh\necho 'v1' > '{marker_file}'\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (self.pkg_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        post_install = "post_install.sh"
        post_update = "post_install.sh"
        """, encoding="utf-8")

        # Initial deploy: executes v1
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)
        self.assertEqual(marker_file.read_text().strip(), "v1")

        # Second deploy with no modifications: skipped
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.deployed_packages, [])

        # 2. Modify ONLY the hook script (deployable payload file.txt is untouched)
        hook_file.write_text(f"#!/bin/sh\necho 'v2 updated' > '{marker_file}'\n", encoding="utf-8")

        # Third deploy: must NOT be skipped, must re-execute deployment with updated hook
        res3 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res3.status, "SUCCESS")
        self.assertEqual(len(res3.deployed_packages), 1)
        self.assertEqual(res3.deployed_packages[0].package, "pkg_a")
        self.assertEqual(marker_file.read_text().strip(), "v2 updated")

    def test_deploy_pipeline_redeploys_on_config_modification(self) -> None:
        """Verifies that modifying drift_package.toml (e.g. adding a hook or changing settings) triggers redeployment."""
        # 1. Initial deploy without hooks
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)

        # Second deploy with no modifications: skipped
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.deployed_packages, [])

        # 2. Modify drift_package.toml (payload untouched)
        (self.pkg_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        fully_controlled_dirs = ["conf.d"]
        """, encoding="utf-8")

        # Third deploy: must NOT be skipped
        res3 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res3.status, "SUCCESS")
        self.assertEqual(len(res3.deployed_packages), 1)
        self.assertEqual(res3.deployed_packages[0].package, "pkg_a")

    def test_deploy_aborts_on_midway_transaction_state(self) -> None:
        """Verifies that Stage 1 Sentinel aborts when package is in midway transaction state and suggests rollback."""
        from drift.state_registry import load_state_registry, save_state_registry

        # 1. Initial deployment
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")

        # 2. Simulate package in midway 'deploying' state
        state_file = self.install_dir / "state.toml"
        state_registry = load_state_registry(state_file)
        state_registry.set_package_state("pkg_a", state="deploying")
        save_state_registry(state_registry)

        # 3. Deploy should abort and suggest drift rollback
        with self.assertRaises(RuntimeError) as context:
            run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        err_msg = str(context.exception)
        self.assertIn("Package(s) in midway transaction state", err_msg)
        self.assertIn("drift rollback pkg_a", err_msg)

        # 4. Deploy with --force should bypass the midway check
        res_force = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"], force=True)
        self.assertEqual(res_force.status, "SUCCESS")

    def test_deploy_pipeline_skipped_packages_retain_installed_state(self) -> None:
        """Verifies that packages skipped due to no physical changes retain 'installed' state in state.toml."""
        from drift.state_registry import load_state_registry

        # 1. Initial deployment
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)

        reg1 = load_state_registry(self.state_file)
        self.assertEqual(reg1.get_package_state("pkg_a"), "installed")

        # 2. Second deploy with no modifications (skipped)
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.deployed_packages, [])

        # 3. State in state.toml should still be "installed", not stuck in "staged"
        reg2 = load_state_registry(self.state_file)
        self.assertEqual(reg2.get_package_state("pkg_a"), "installed")


if __name__ == "__main__":
    unittest.main()

