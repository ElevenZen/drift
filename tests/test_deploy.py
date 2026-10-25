import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.core.constants import PACKAGE_CONFIG_FILE_NAME, InstallMethod
from drift.models import DeployOptions, NextActionType
from drift.core.exceptions import DriftDetectedError, HookMissingError, TargetPermissionError
from drift.config.workspace_config import WorkspaceConfig
from drift.hooks.lifecycle_hooks import HookExecFlags
from drift.core.state_registry import load_state_registry, save_state_registry
from drift.primitives.deploy_repo import (
    execute_deploy_pipeline,
    run_primitive_deploy_pipeline,
)
from drift.primitives.rollback_repo import run_primitive_8_rollback_recovery
from drift.utils.git_utils import get_git_status_porcelain


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
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

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
default_target_directory = "{self.system_target_dir.as_posix()}"
default_install_method = "copy"

[packages.enable]
pkg_a = true
""", encoding="utf-8")

        # Create initial state.toml in install/
        self.state_file = self.install_dir / "state.toml"
        self.state_file.write_text("[packages]\n", encoding="utf-8")
        subprocess.run(["git", "add", "state.toml"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.install_dir, check=True, capture_output=True)

        # Build workspace config
        self.workspace_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.workspace_config.workspace.default_target_directory = self.system_target_dir

        # Set up a clean source package
        self.pkg_dir = self.source_dir / "pkg_a"
        self.pkg_dir.mkdir()
        self.package_config_file = self.pkg_dir / "drift_package.toml"
        self.package_config_file.write_text(f"""
[package]
name = "pkg_a"
install_method = "copy"
target_directory = "{self.system_target_dir.as_posix()}"
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
        self.assertEqual(target_file.read_text(encoding="utf-8"), "Hello source config!")

        # 2. State registry in install/ should be set to "installed"
        registry = load_state_registry(self.state_file)
        self.assertEqual(registry.get_package_state("pkg_a"), "installed")

        # 3. install/ and render/ git status should be clean (committed successfully)
        install_status = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True, check=True)
        self.assertEqual(install_status.stdout.strip(), "")

        render_status = subprocess.run(["git", "status", "--porcelain"], cwd=self.render_dir, capture_output=True, text=True, check=True)
        self.assertEqual(render_status.stdout.strip(), "")

    def test_deploy_pipeline_aborts_on_system_drift(self) -> None:
        """Verifies that the sentinel drift guard aborts execution when active drift is found."""
        # 1. Initially run a deploy to establish baseline tracking
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Simulate drift by modifying target file directly on host system
        target_file = self.system_target_dir / "file.txt"
        target_file.write_text("Modified on host system directly!", encoding="utf-8")

        # 3. Running deploy should raise DriftDetectedError (exit code 3) because of drift
        with self.assertRaises(DriftDetectedError) as context:
            execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        self.assertIn("[DEPLOY ABORTED] System drift detected", str(context.exception))
        self.assertEqual(context.exception.packages, ["pkg_a"])

    def test_deploy_pipeline_overrides_drift_with_force(self) -> None:
        """Verifies that passing force=True bypasses the drift guard, commits a drift snapshot, and overwrites modifications."""
        # 1. Initially run a deploy to establish baseline tracking
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Simulate drift by modifying target file directly on host system
        target_file = self.system_target_dir / "file.txt"
        target_file.write_text("Modified on host system directly!", encoding="utf-8")

        # 3. Running deploy with force=True should succeed and overwrite active drift
        run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            options=DeployOptions(force=True),
        )

        self.assertEqual(target_file.read_text(encoding="utf-8"), "Hello source config!")

        # 4. Verify install/ git log contains both the drift snapshot commit and deploy install commit
        git_log = subprocess.run(
            ["git", "log", "--oneline", "-n", "3"],
            cwd=self.install_dir,
            capture_output=True,
            text=True,
            check=True
        ).stdout
        self.assertIn("Deploy Install: Automatically commit deployed changes for pkg_a", git_log)
        self.assertIn("Drift Snapshot: Capture host modifications for pkg_a before forced deployment", git_log)

        # 5. Verify the drift snapshot commit in install/ recorded the modified host content
        snapshot_content = subprocess.run(
            ["git", "show", "HEAD~1:pkg_a/file.txt"],
            cwd=self.install_dir,
            capture_output=True,
            text=True,
            check=True
        ).stdout
        self.assertEqual(snapshot_content, "Modified on host system directly!")

    @patch("drift.primitives.deploy_repo.execute_install")
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
                execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        finally:
            sys.stderr = original_stderr

        self.assertIn("Midway crash: Step 4", str(context.exception))
        
        # Verify emergency recovery card is printed in captured stderr
        printed_card = stderr_capture.getvalue()
        self.assertIn("CRITICAL FAILURE", printed_card)
        self.assertIn("EMERGENCY RECOVERY REQUIRED", printed_card)
        self.assertIn("drift rollback pkg_a", printed_card)

    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_pipeline_step1_failure_shows_retry(self, mock_render) -> None:
        """Verifies that Step 1 (rendering) failure logs retry and doesn't print emergency recovery card."""
        mock_render.side_effect = ValueError("rendering error")
        
        with self.assertRaises(RuntimeError) as context:
            execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        
        self.assertIn("Step 1 (Template Rendering) failed.", str(context.exception))

    @patch("drift.primitives.deploy_repo.run_primitive_6_commit_install_repo")
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
                execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        finally:
            sys.stderr = original_stderr
        
        self.assertIn("Step 5 (State Database Committing) failed.", str(context.exception))
        printed_msg = stderr_capture.getvalue()
        self.assertIn("drift install-commit -m", printed_msg)

    @patch("drift.primitives.deploy_repo.run_primitive_9_purge_workspace_garbage")
    def test_global_deploy_calls_gc(self, mock_gc) -> None:
        """Verifies that global deploy skips GC by default and calls GC when DeployOptions(gc=True) is set."""
        # 1. Default global deploy (gc=False) must NOT call GC
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=())
        mock_gc.assert_not_called()

        # 2. Global deploy with gc=True calls GC with resolved hook flags
        opts_gc = DeployOptions(gc=True)
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=(), options=opts_gc)
        mock_gc.assert_called_once_with(
            self.workspace_config,
            dry_run=False,
            flags=opts_gc.get_hook_flags(settings=self.workspace_config.settings),
        )

        mock_gc.reset_mock()
        opts_no_hooks = DeployOptions(gc=True, no_hooks=True)
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=(), options=opts_no_hooks)
        mock_gc.assert_called_once_with(
            self.workspace_config,
            dry_run=False,
            flags=opts_no_hooks.get_hook_flags(settings=self.workspace_config.settings),
        )
        self.assertTrue(mock_gc.call_args.kwargs["flags"].no_hooks)

    def test_deploy_pipeline_checks_git_configs(self) -> None:
        """Verifies that deploy pipeline verifies git config user.name and user.email exists."""
        # Unconfigure user.name inside render repo temporarily
        subprocess.run(["git", "config", "--unset", "user.name"], cwd=self.render_dir, check=True)
        
        env_override = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "",
            "GIT_COMMITTER_NAME": "",
        }
        with patch.dict(os.environ, env_override, clear=False):
            # Deploy should fail on pre-flight checks
            with self.assertRaises(RuntimeError) as context:
                execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
            
            self.assertIn("Git configuration error: 'user.name' is not configured", str(context.exception))

    def test_deploy_post_update_hook_failure_with_failure_is_fatal_false(self) -> None:
        """Verifies that when failure_is_fatal=False, a failing post_update hook stops and reports without leaving package in installing state."""
        # 1. First-time deploy succeeds
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 2. Add a failing post_update hook script and set failure_is_fatal = false
        pkg_dir = self.source_dir / "pkg_a"
        scripts_dir = pkg_dir / "drift_hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        hook_file = scripts_dir / "post_update.sh"
        hook_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        post_update = "drift_hooks/post_update.sh"
        failure_is_fatal = false
        """, encoding="utf-8")

        (pkg_dir / "extra.conf").write_text("extra content", encoding="utf-8")

        # 3. Deploy update - should fail on hook but NOT report midway crash
        with self.assertRaises(RuntimeError) as ctx:
            execute_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a"],
                options=DeployOptions(flags=HookExecFlags(streaming=False)),
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
            options=DeployOptions(flags=HookExecFlags(streaming=False)),
        )
        self.assertTrue((self.system_target_dir / "extra.conf").is_file())

    def test_deploy_post_update_hook_failure_with_failure_is_fatal_true(self) -> None:
        """Verifies that when failure_is_fatal=True (default), a failing post_update hook triggers emergency midway crash state."""
        # 1. First-time deploy succeeds
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 2. Add a failing post_update hook script with default failure_is_fatal = true
        pkg_dir = self.source_dir / "pkg_a"
        scripts_dir = pkg_dir / "drift_hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        hook_file = scripts_dir / "post_update.sh"
        hook_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        post_update = "drift_hooks/post_update.sh"
        failure_is_fatal = true
        """, encoding="utf-8")

        # 3. Deploy update - should fail with Midway crash
        with patch("sys.stderr", new=StringIO()):
            with self.assertRaises(RuntimeError) as ctx:
                execute_deploy_pipeline(
                    self.workspace_config,
                    packages_to_deploy=["pkg_a"],
                    options=DeployOptions(flags=HookExecFlags(streaming=False)),
                )

        self.assertIn("Midway crash", str(ctx.exception))

        # 4. Verify package is in 'installing' state requiring rollback
        state_file = self.install_dir / "state.toml"
        state_registry = load_state_registry(state_file)
        self.assertEqual(state_registry.get_package_state("pkg_a"), "installing")

        # 5. Subsequent deploy without force or rollback aborts with safety check
        from drift.primitives.install_repo import run_primitive_5_install, InstallOptions
        with self.assertRaises(RuntimeError) as ctx2:
            run_primitive_5_install(
                workspace_config=self.workspace_config,
                target_pkgs=["pkg_a"],
                options=InstallOptions(resolve_symlinks=True, force=False),
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
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("pkg_b initial content", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

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
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello updated source config!")

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

    def test_deploy_pipeline_with_reinstall_flag(self) -> None:
        """Verifies that passing reinstall=True forces reinstallation even when zero stage changes exist."""
        # 1. Initial deploy
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")

        # 2. Second deploy with reinstall=True
        res2 = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            options=DeployOptions(reinstall=True),
        )
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(len(res2.deployed_packages), 1)
        self.assertEqual(res2.deployed_packages[0].package, "pkg_a")

    def test_deploy_pipeline_redeploys_on_hook_modification(self) -> None:
        """Verifies that modifying a lifecycle hook script triggers redeployment even if deployable files are unchanged."""
        # 1. Setup hook script and configure it
        scripts_dir = self.pkg_dir / "drift_hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        hook_file = scripts_dir / "post_install.sh"
        marker_file = self.drift_root / "hook_executed.txt"
        hook_file.write_text(f"#!/bin/sh\necho 'v1' > '{marker_file}'\n", encoding="utf-8")
        hook_file.chmod(0o755)

        (self.pkg_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        post_install = "drift_hooks/post_install.sh"
        post_update = "drift_hooks/post_install.sh"
        """, encoding="utf-8")

        # Initial deploy: executes v1
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)
        self.assertEqual(marker_file.read_text(encoding="utf-8").strip(), "v1")

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
        self.assertEqual(marker_file.read_text(encoding="utf-8").strip(), "v2 updated")

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
        target_directory = "{self.system_target_dir.as_posix()}"
        fully_controlled_dirs = ["conf.d"]
        """, encoding="utf-8")

        # Third deploy: must NOT be skipped
        res3 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res3.status, "SUCCESS")
        self.assertEqual(len(res3.deployed_packages), 1)
        self.assertEqual(res3.deployed_packages[0].package, "pkg_a")

    def test_deploy_aborts_on_midway_transaction_state(self) -> None:
        """Verifies that Stage 1 Sentinel aborts when package is in midway transaction state and suggests rollback."""
        from drift.core.state_registry import load_state_registry, save_state_registry

        # 1. Initial deployment
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")

        # 2. Simulate package in midway 'installing' state
        state_file = self.install_dir / "state.toml"
        state_registry = load_state_registry(state_file)
        state_registry.set_package_state("pkg_a", state="installing")
        save_state_registry(state_registry)

        # 3. Deploy should abort and suggest drift rollback
        with self.assertRaises(RuntimeError) as context:
            execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        err_msg = str(context.exception)
        self.assertIn("Package(s) in midway transaction state", err_msg)
        self.assertIn("drift rollback pkg_a", err_msg)

        # 4. Deploy with --force should bypass the midway check
        res_force = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            options=DeployOptions(force=True),
        )
        self.assertEqual(res_force.status, "SUCCESS")

    def test_deploy_pipeline_skipped_packages_retain_installed_state(self) -> None:
        """Verifies that packages skipped due to no physical changes retain 'installed' state in state.toml."""
        from drift.core.state_registry import load_state_registry

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

    def test_execute_deploy_fails_fast_when_workspace_structure_broken(self) -> None:
        """Verifies execute_deploy fails with ConfigError and hints 'drift repair' when workspace structure is broken."""
        from drift.cli.actions import execute_deploy
        from drift.core.constants import PACKAGE_CONFIG_FILE_NAME
        from drift.core.exceptions import ConfigError
        from drift.primitives.workspace_init import init_drift_workspace

        init_drift_workspace(self.drift_root, force=True)

        # Put a legacy root metadata file in render/pkg_a
        pkg_render = self.render_dir / "pkg_a"
        pkg_render.mkdir(parents=True, exist_ok=True)
        (pkg_render / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

    def test_deploy_warns_and_skips_reverse_sync_when_install_config_missing(self) -> None:
        """Verifies that when install/<pkg> exists without a package config file, Stage 1 warns and skips reverse-sync without crashing."""
        from drift.core.constants import set_test_mode, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME
        from drift.primitives.deploy_repo import check_and_prevent_system_drifts

        # 1. Initial deployment
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Corrupt install/pkg_a by deleting its package configuration file
        pkg_install_dir = self.install_dir / "pkg_a"
        config_path = pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        if config_path.exists():
            config_path.unlink()
        root_config = pkg_install_dir / PACKAGE_CONFIG_FILE_NAME
        if root_config.exists():
            root_config.unlink()
        subprocess.run(["git", "add", "-A"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Corrupt config"], cwd=self.install_dir, check=True, capture_output=True)

        # 3. Running check_and_prevent_system_drifts should NOT crash with ValueError; it should warn and return clean drifts
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()), \
                 self.assertLogs("drift.config.workspace_config", level="WARNING") as cm:
                drifted_pkgs, drifted_files = check_and_prevent_system_drifts(
                    self.workspace_config, target_pkgs=["pkg_a"]
                )
                self.assertEqual(drifted_pkgs, [])
                self.assertTrue(any("missing its package configuration file" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

        # 4. Running full deploy repairs and restores package config file in install/
        res = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            options=DeployOptions(reinstall=True),
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).exists())

    def test_deploy_step4a_prepare_failure_for_copy_package_avoids_rollback_and_mutation(self) -> None:
        """When prepare_install fails for COPY packages, it aborts before staging without mutating install/ or state.toml."""
        # 1. Initial successful deployment of copy package
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        reg = load_state_registry(self.state_file)
        self.assertEqual(reg.get_package_install_method("pkg_a"), InstallMethod.COPY)
        self.assertEqual(reg.get_package_state("pkg_a"), "installed")

        # 2. Modify package file so staging plan detects changes and proceeds to Step 4a
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / "file.txt").write_text("modified content", encoding="utf-8")

        # 3. Deploy update with prepare_install failing on target permission
        import sys
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=TargetPermissionError("Target directory not writable", packages=["pkg_a"]),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertIn("Step 4 (Deployment Pre-flight) failed.", res.failure.error_message)

        # Verify emergency recovery card was NOT printed and install/ + state.toml remain completely untouched
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])
        reg_after = load_state_registry(self.state_file)
        self.assertEqual(reg_after.get_package_state("pkg_a"), "installed")
        self.assertEqual((self.install_dir / "pkg_a" / "file.txt").read_text(encoding="utf-8"), "Hello source config!")

    def test_deploy_step4a_prepare_failure_for_symlink_package_avoids_rollback_and_mutation(self) -> None:
        """When prepare_install fails for SYMLINK packages, it aborts before staging so live symlinks and install/ remain untouched."""
        # 1. Set pkg_a to symlink in source config and deploy initially
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "symlink"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")

        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        reg = load_state_registry(self.state_file)
        expected_method = InstallMethod.COPY if sys.platform == "win32" else InstallMethod.SYMLINK
        self.assertEqual(reg.get_package_install_method("pkg_a"), expected_method)

        # 2. Modify package file so staging plan detects changes and proceeds to Step 4a
        (pkg_dir / "file.txt").write_text("symlink modified content", encoding="utf-8")

        # 3. Deploy update with prepare_install failing on pre-flight: no staging has happened yet!
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=TargetPermissionError("Target directory not writable", packages=["pkg_a"]),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")

        # Verify emergency recovery card was NOT printed and live symlink target still has original content
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")

    def test_deploy_step4a_prepare_failure_for_symlink_with_generic_error_avoids_rollback(self) -> None:
        """When prepare_install fails with a generic error and package is SYMLINK, no rollback is needed because install/ was not mutated."""
        # 1. Configure pkg_a as SYMLINK and initial deploy
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "symlink"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Modify file so changes are detected
        (self.source_dir / "pkg_a" / "file.txt").write_text("updated", encoding="utf-8")

        # 3. Patch prepare_install to raise generic error without packages attribute
        import sys
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=RuntimeError("Generic unexpected failure"),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())

    def test_deploy_step4a_prepare_failure_for_copy_with_generic_error_avoids_rollback(self) -> None:
        """When prepare_install fails with a generic error for COPY packages, no rollback is needed because install/ was not mutated."""
        # 1. Initial deployment of copy package
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Modify file so changes are detected
        (self.source_dir / "pkg_a" / "file.txt").write_text("updated", encoding="utf-8")

        # 3. Patch prepare_install to raise generic error without packages attribute
        import sys
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=RuntimeError("Generic unexpected failure"),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())

    def test_deploy_step4a_prepare_failure_with_symlink_and_copy_packages_leaves_all_clean(self) -> None:
        """When deploying both SYMLINK and COPY packages and Step 4a fails, neither package is staged or enters midway state."""
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("content b", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

        pkg_a_dir = self.source_dir / "pkg_a"
        (pkg_a_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "symlink"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")

        # Initial deployment of both
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"])
        reg = load_state_registry(self.state_file)
        expected_method = InstallMethod.COPY if sys.platform == "win32" else InstallMethod.SYMLINK
        self.assertEqual(reg.get_package_install_method("pkg_a"), expected_method)
        self.assertEqual(reg.get_package_install_method("pkg_b"), InstallMethod.COPY)

        # Modify both packages
        (pkg_a_dir / "file.txt").write_text("symlink modified", encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("copy modified", encoding="utf-8")

        # Deploy both, but prepare_install fails on pkg_b
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=TargetPermissionError("Collision on pkg_b", packages=["pkg_b"]),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())

        # Both packages remain in clean 'installed' state with zero uncommitted changes in install/
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])
        reg_after = load_state_registry(self.state_file)
        self.assertEqual(reg_after.get_package_state("pkg_a"), "installed")
        self.assertEqual(reg_after.get_package_state("pkg_b"), "installed")

    def test_deploy_step4a_prepare_failure_with_multiple_copy_packages_leaves_all_clean(self) -> None:
        """When deploying multiple COPY packages and Step 4a fails, neither package is staged or requires rollback."""
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("content b", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

        # Initial deployment
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"])

        # Modify both
        (self.source_dir / "pkg_a" / "file.txt").write_text("mod a", encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("mod b", encoding="utf-8")

        import sys
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=TargetPermissionError("Collision on pkg_b", packages=["pkg_b"]),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_step4a_prepare_failure_for_first_time_package_leaves_install_clean(self) -> None:
        """When prepare_install fails for first-time installed package, install/ and state.toml remain completely untouched."""
        # 1. State registry is initially empty (pkg_a has never been deployed)
        reg = load_state_registry(self.state_file)
        self.assertIsNone(reg.get_package_install_method("pkg_a"))

        # 2. Deploy for the first time, but prepare_install fails on pre-flight
        import sys
        stderr_capture = StringIO()
        orig_stderr = sys.stderr
        sys.stderr = stderr_capture
        try:
            with patch(
                "drift.primitives.deploy_repo.prepare_install",
                side_effect=TargetPermissionError("Target directory not writable", packages=["pkg_a"]),
            ):
                res = run_primitive_deploy_pipeline(
                    self.workspace_config, packages_to_deploy=["pkg_a"]
                )
        finally:
            sys.stderr = orig_stderr

        self.assertEqual(res.status, "FAILED")
        self.assertIsNotNone(res.failure)
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        self.assertEqual(res.failure.recommended_command, "drift deploy")
        self.assertIn("Step 4 (Deployment Pre-flight) failed.", res.failure.error_message)
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())
        self.assertFalse((self.install_dir / "pkg_a").exists())
        reg_after = load_state_registry(self.state_file)
        self.assertIsNone(reg_after.get_package_state("pkg_a"))

    def test_deploy_pipeline_deploys_staged_package_without_staging_changes(self) -> None:
        """Mandatory Two-Pass Test: Verifies that a package marked as 'staged' in state registry

        is deployed to the host even when staging detected zero physical file changes.
        """
        from drift.core.state_registry import save_state_registry

        # 1. Initial deployment: pkg_a is cleanly deployed and recorded as 'installed'
        res0 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res0.status, "SUCCESS")
        self.assertEqual(len(res0.deployed_packages), 1)

        target_file = self.system_target_dir / "file.txt"
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "Hello source config!")

        # 2. Simulate package marked as 'staged' in state registry without any physical file changes
        reg = load_state_registry(self.state_file)
        reg.set_package_state("pkg_a", "staged")
        save_state_registry(reg)

        reg_check = load_state_registry(self.state_file)
        self.assertEqual(reg_check.get_package_state("pkg_a"), "staged")

        # Pass 1 (Cold / Mutation Pass): deploy must notice 'staged' state and execute physical deploy
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(len(res1.deployed_packages), 1)
        self.assertEqual(res1.deployed_packages[0].package, "pkg_a")

        # Post-condition: state is now updated to 'installed'
        reg_after1 = load_state_registry(self.state_file)
        self.assertEqual(reg_after1.get_package_state("pkg_a"), "installed")

        # Pass 2 (Warm / Idempotent Pass): re-deploy must skip because it is now 'installed' and has no changes
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(len(res2.deployed_packages), 0)
        reg_after2 = load_state_registry(self.state_file)
        self.assertEqual(reg_after2.get_package_state("pkg_a"), "installed")

    def test_resolve_packages_to_install_selection_matrix(self) -> None:
        """Unit test for resolve_packages_to_install covering changes, staged states, and reinstall."""
        from drift.primitives.deploy_repo import resolve_packages_to_install
        from drift.primitives.stage_repo import StagePlan
        from drift.config.package_config import PackageConfig, PackageSectionConfig

        reg = load_state_registry(self.state_file)
        reg.set_package_state("pkg_staged", "staged")
        reg.set_package_state("pkg_installed", "installed")
        reg.set_package_state("pkg_changed", "installed")

        # Note: stage_plan.pkg_metadata only ever contains packages with enable_install=True
        mock_metadata = {
            "pkg_staged": PackageConfig(
                package=PackageSectionConfig(name="pkg_staged", enable_install=True)
            ),
            "pkg_installed": PackageConfig(
                package=PackageSectionConfig(name="pkg_installed", enable_install=True)
            ),
            "pkg_changed": PackageConfig(
                package=PackageSectionConfig(name="pkg_changed", enable_install=True)
            ),
        }

        stage_plan = StagePlan(
            pkg_metadata=mock_metadata,
            state_registry=reg,
            packages_stage_order=["pkg_changed"],
        )

        all_target_pkgs = ["pkg_staged", "pkg_installed", "pkg_changed", "pkg_disabled"]

        # Case 1: Normal run (no reinstall)
        # Should include: pkg_changed (staging changes) + pkg_staged ('staged' status)
        # Should exclude: pkg_installed (already installed, no changes) + pkg_disabled (enable_install=False)
        resolved = resolve_packages_to_install(
            stage_plan=stage_plan,
            target_pkgs=all_target_pkgs,
            reinstall=False,
        )
        self.assertEqual(resolved, ["pkg_staged", "pkg_changed"])

        # Case 2: Reinstall run (reinstall=True)
        # Should include all enabled packages, but strictly exclude pkg_disabled
        reinstall_resolved = resolve_packages_to_install(
            stage_plan=stage_plan,
            target_pkgs=all_target_pkgs,
            reinstall=True,
        )
        self.assertEqual(reinstall_resolved, ["pkg_staged", "pkg_installed", "pkg_changed"])

    def test_deploy_convergent_drift_auto_resolves_without_abort(self) -> None:
        """Two-Pass Idempotency Test: When both src/ and live host are modified identically, deploy auto-commits convergence."""
        # 1. Initial deployment
        res0 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res0.status, "SUCCESS")

        # 2. User edits both src/pkg_a/file.txt and live host file.txt identically
        (self.source_dir / "pkg_a" / "file.txt").write_text("converged content", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("converged content", encoding="utf-8")

        # Pass 1 (Cold / Convergent Resolution Pass):
        # Render compiles src/ -> render/, reverse-sync pulls host -> install/,
        # adopt convergence check detects install/pkg_a/file.txt == render/pkg_a/file.txt and commits!
        res1 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "converged content")
        self.assertEqual((self.install_dir / "pkg_a" / "file.txt").read_text(encoding="utf-8"), "converged content")

        # Pass 2 (Warm / Idempotent Pass):
        res2 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.deployed_packages, [])
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_convergent_deleted_file_auto_resolves(self) -> None:
        """When a file is deleted from both src/ and the live host, deploy auto-commits the converged deletion."""
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / "extra.txt").write_text("extra file", encoding="utf-8")
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "extra.txt").exists())

        # Remove extra.txt from both src/pkg_a and live host
        (pkg_dir / "extra.txt").unlink()
        (self.system_target_dir / "extra.txt").unlink()

        res = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse((self.install_dir / "pkg_a" / "extra.txt").exists())
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_multi_file_convergent_one_file_and_template_update_another_file(self) -> None:
        """When file_a converges on host & src while file_b is updated only in src, deploy commits file_a and deploys file_b."""
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / "file_b.txt").write_text("initial b", encoding="utf-8")
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # file.txt is updated identically in src/ and host (convergent drift)
        # file_b.txt is updated only in src/ (normal template update)
        (pkg_dir / "file.txt").write_text("converged a", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("converged a", encoding="utf-8")
        (pkg_dir / "file_b.txt").write_text("updated b from template", encoding="utf-8")

        res = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(len(res.deployed_packages), 1)
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "converged a")
        self.assertEqual((self.system_target_dir / "file_b.txt").read_text(encoding="utf-8"), "updated b from template")
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_partial_convergence_commits_converged_file_and_aborts_on_divergent_file(self) -> None:
        """When file_a converges and file_b diverges on the host, deploy commits file_a and aborts only on file_b."""
        pkg_dir = self.source_dir / "pkg_a"
        (pkg_dir / "file_b.txt").write_text("initial b", encoding="utf-8")
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # file.txt converges between src/ and host; file_b.txt diverges on host
        (pkg_dir / "file.txt").write_text("converged a", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("converged a", encoding="utf-8")
        (self.system_target_dir / "file_b.txt").write_text("divergent host edit b", encoding="utf-8")

        res = run_primitive_deploy_pipeline(
            self.workspace_config, packages_to_deploy=["pkg_a"]
        )
        self.assertEqual(res.status, "ABORTED_DRIFT")
        assert res.failure is not None
        self.assertEqual(res.failure.next_action_type, NextActionType.ADOPT_OR_FORCE)

        # Verify file.txt was committed cleanly in install/, leaving only file_b.txt dirty in git status
        dirty_lines = get_git_status_porcelain(self.workspace_config.install_path, "pkg_a/")
        self.assertEqual(len(dirty_lines), 1)
        self.assertIn("pkg_a/file_b.txt", dirty_lines[0])

    def test_deploy_render_failure_aborts_before_reverse_sync_touches_install_repo(self) -> None:
        """Verifies that a template error in Step 1 (render) aborts before reverse-sync touches install/."""
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # Modify live host file AND break package config in src/pkg_a
        (self.system_target_dir / "file.txt").write_text("host edit while template broken", encoding="utf-8")
        (self.source_dir / "pkg_a" / PACKAGE_CONFIG_FILE_NAME).write_text("invalid = [toml", encoding="utf-8")

        res = run_primitive_deploy_pipeline(
            self.workspace_config, packages_to_deploy=["pkg_a"]
        )
        self.assertEqual(res.status, "FAILED")
        assert res.failure is not None
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)
        # Because render ran before reverse-sync, install/ was never dirtied by reverse-sync
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_pre_stage_install_plan_resolves_external_dependency_in_install_only(self) -> None:
        """When pkg_b depends on pkg_a which is installed in install/pkg_a but absent from render/, deploy succeeds."""
        import shutil

        # 1. Deploy pkg_a first
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # 2. Remove render/pkg_a to simulate pkg_a not being present in render/ during targeted deploy of pkg_b
        shutil.rmtree(self.render_dir / "pkg_a")
        subprocess.run(["git", "add", "-A"], cwd=self.render_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "remove pkg_a from render"], cwd=self.render_dir, check=True, capture_output=True)

        # 3. Create pkg_b depending on pkg_a
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        dependencies = ["pkg_a"]
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("hello from pkg_b", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

        # 4. Targeted deploy of pkg_b only: Step 4a must resolve pkg_a from install_path_mask (canonical install/)
        res = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_b"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual([p.package for p in res.deployed_packages], ["pkg_b"])
        self.assertEqual((self.system_target_dir / "file_b.txt").read_text(encoding="utf-8"), "hello from pkg_b")

    def test_deploy_auto_rollback_reverts_entire_multi_package_batch_and_allows_immediate_retry(self) -> None:
        """Verifies that auto_rollback=true on a failing package reverts the entire batch (including earlier installed packages) and suppresses manual rollback prompt."""
        pkg_a_dir = self.source_dir / "pkg_a"
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        auto_rollback = true
        dependencies = ["pkg_a"]
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("v1 content b", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

        # 1. Initial clean deploy of both packages
        res0 = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a", "pkg_b"])
        self.assertEqual(res0.status, "SUCCESS")
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual((self.system_target_dir / "file_b.txt").read_text(encoding="utf-8"), "v1 content b")

        # 2. Update both packages, and add a failing post_update hook on pkg_b (which has auto_rollback = true)
        (pkg_a_dir / "file.txt").write_text("v2 content a", encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("v2 content b", encoding="utf-8")
        hooks_dir = pkg_b_dir / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        fail_hook = hooks_dir / "post_update.py"
        fail_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        fail_hook.chmod(0o755)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        auto_rollback = true
        dependencies = ["pkg_a"]

        [hooks]
        post_update = "drift_hooks/post_update.py"
        """, encoding="utf-8")

        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res_fail = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a", "pkg_b"],
                options=DeployOptions(flags=HookExecFlags(streaming=False)),
            )

        self.assertEqual(res_fail.status, "FAILED")
        assert res_fail.failure is not None
        # Auto-rollback succeeded, so manual rollback is NOT required
        self.assertFalse(res_fail.failure.requires_rollback)
        self.assertEqual(res_fail.failure.recommended_command, "drift deploy")
        self.assertNotIn("EMERGENCY RECOVERY REQUIRED", stderr_capture.getvalue())

        # Both pkg_a (which had already reached 'installed' before pkg_b failed) and pkg_b are reverted to v1!
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual((self.system_target_dir / "file_b.txt").read_text(encoding="utf-8"), "v1 content b")
        self.assertEqual((self.install_dir / "pkg_a" / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual((self.install_dir / "pkg_b" / "file_b.txt").read_text(encoding="utf-8"), "v1 content b")
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

        reg_after = load_state_registry(self.state_file)
        self.assertEqual(reg_after.get_package_state("pkg_a"), "installed")
        self.assertEqual(reg_after.get_package_state("pkg_b"), "installed")
        self.assertFalse((self.workspace_config.backup_path / "pkg_a" / "wal").exists())
        self.assertFalse((self.workspace_config.backup_path / "pkg_b" / "wal").exists())

        # 3. Fix hook and retry deploy immediately without needing manual rollback or --force
        fail_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
        res_retry = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a", "pkg_b"],
            options=DeployOptions(flags=HookExecFlags(streaming=False)),
        )
        self.assertEqual(res_retry.status, "SUCCESS")
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "v2 content a")
        self.assertEqual((self.system_target_dir / "file_b.txt").read_text(encoding="utf-8"), "v2 content b")

    def test_deploy_and_apply_rollback_policy_precedence_matrix(self) -> None:
        """Verifies CLI --rollback > workspace [settings] rollback > package auto_rollback across deploy and apply."""
        from drift.core.constants import RollbackPolicy
        from drift.config.workspace_config import SettingsConfig
        from drift.primitives.install_repo import run_primitive_5_install, InstallOptions
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.render_repo import run_primitive_2_render_packages, run_primitive_3_commit_render_repo

        pkg_a_dir = self.source_dir / "pkg_a"
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        hooks_dir = pkg_a_dir / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        fail_hook = hooks_dir / "post_update.py"
        fail_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        fail_hook.chmod(0o755)

        def _write_pkg_a_config(auto_rb: bool) -> None:
            (pkg_a_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            auto_rollback = {"true" if auto_rb else "false"}

            [hooks]
            post_update = "drift_hooks/post_update.py"
            """, encoding="utf-8")

        # Case 1: Package auto_rollback = false, Workspace [settings] rollback = ALWAYS -> triggers auto-rollback
        _write_pkg_a_config(auto_rb=False)
        (pkg_a_dir / "file.txt").write_text("v2", encoding="utf-8")
        self.workspace_config.settings = SettingsConfig(rollback=RollbackPolicy.ALWAYS)
        with patch("sys.stderr", StringIO()):
            res1 = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a"],
                options=DeployOptions(flags=HookExecFlags(streaming=False)),
            )
        self.assertEqual(res1.status, "FAILED")
        assert res1.failure is not None
        self.assertFalse(res1.failure.requires_rollback)
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_a"), "installed")

        # Case 2: Package auto_rollback = true, Workspace [settings] rollback = ALWAYS, CLI rollback = NEVER -> no auto-rollback
        _write_pkg_a_config(auto_rb=True)
        self.workspace_config.settings = SettingsConfig(rollback=RollbackPolicy.ALWAYS)
        with patch("sys.stderr", StringIO()):
            res2 = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a"],
                options=DeployOptions(
                    rollback=RollbackPolicy.NEVER,
                    flags=HookExecFlags(streaming=False),
                ),
            )
        self.assertEqual(res2.status, "FAILED")
        assert res2.failure is not None
        self.assertTrue(res2.failure.requires_rollback)
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_a"), "installing")

        # Clean up midway state via manual rollback before Case 3
        from drift.primitives.rollback_repo import run_primitive_8_rollback_recovery
        run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)

        # Case 3: Package auto_rollback = false, Workspace [settings] rollback = NEVER, CLI rollback = ALWAYS -> triggers auto-rollback
        _write_pkg_a_config(auto_rb=False)
        self.workspace_config.settings = SettingsConfig(rollback=RollbackPolicy.NEVER)
        with patch("sys.stderr", StringIO()):
            res3 = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a"],
                options=DeployOptions(
                    rollback=RollbackPolicy.ALWAYS,
                    flags=HookExecFlags(streaming=False),
                ),
            )
        self.assertEqual(res3.status, "FAILED")
        assert res3.failure is not None
        self.assertFalse(res3.failure.requires_rollback)
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_a"), "installed")

        # Case 4: Standalone 'apply' (run_primitive_5_install) with CLI rollback = ALWAYS triggers auto-rollback
        run_primitive_2_render_packages(self.workspace_config, ["pkg_a"])
        run_primitive_3_commit_render_repo(self.workspace_config, "Render v2", ["pkg_a"])
        run_primitive_4_stage_render_to_install(self.workspace_config, ["pkg_a"])
        with self.assertRaises(RuntimeError) as cm:
            run_primitive_5_install(
                self.workspace_config,
                target_pkgs=["pkg_a"],
                options=InstallOptions(
                    rollback=RollbackPolicy.ALWAYS,
                    flags=HookExecFlags(streaming=False),
                ),
            )
        self.assertTrue(getattr(cm.exception, "auto_rolled_back", False))
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_a"), "installed")
        self.assertEqual(get_git_status_porcelain(self.workspace_config.install_path), [])

    def test_deploy_non_fatal_hook_failure_does_not_trigger_auto_rollback(self) -> None:
        """Verifies that a non-fatal hook failure (failure_is_fatal=false) never triggers auto-rollback even with rollback=ALWAYS."""
        from drift.core.constants import RollbackPolicy

        pkg_a_dir = self.source_dir / "pkg_a"
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        hooks_dir = pkg_a_dir / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        fail_hook = hooks_dir / "post_update.py"
        fail_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        fail_hook.chmod(0o755)
        (pkg_a_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        auto_rollback = true

        [hooks]
        post_update = "drift_hooks/post_update.py"
        failure_is_fatal = false
        """, encoding="utf-8")
        (pkg_a_dir / "file.txt").write_text("v2 non-fatal", encoding="utf-8")

        res = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a"],
            options=DeployOptions(
                rollback=RollbackPolicy.ALWAYS,
                flags=HookExecFlags(streaming=False),
            ),
        )
        self.assertEqual(res.status, "FAILED")
        assert res.failure is not None
        self.assertFalse(res.failure.requires_rollback)
        # File update remains in place because hook failure was non-fatal (not rolled back)
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "v2 non-fatal")
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_a"), "installed")

    def test_pre_deploy_and_pre_install_purges_stale_wal_for_clean_installed_package(self) -> None:
        """Verifies stale WAL folders on clean 'installed' packages are purged at the start of deploy and install."""
        from drift.core.constants import BackupSubfolder, WAL_ACTIONS_FILE_NAME

        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        # Create a bogus stale WAL for pkg_a (which is in clean committed 'installed' state)
        stale_wal_dir = self.workspace_config.backup_path / "pkg_a" / BackupSubfolder.WAL
        stale_wal_dir.mkdir(parents=True, exist_ok=True)
        stale_wal_file = stale_wal_dir / WAL_ACTIONS_FILE_NAME
        stale_wal_file.write_text('[{"corrupt_or_stale": true}]', encoding="utf-8")
        self.assertTrue(stale_wal_file.is_file())

        # Re-running deploy (even when up-to-date) purges the stale WAL at the start of the pipeline
        res = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse(stale_wal_dir.exists())

    def test_deploy_auto_rollback_reverts_unattempted_staged_in_batch_without_touching_unrelated_packages(self) -> None:
        """Verifies batch auto-rollback reverts unattempted 'staged' packages in the batch while ignoring unrelated packages outside the batch."""
        pkg_a_dir = self.source_dir / "pkg_a"
        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_c_dir = self.source_dir / "pkg_c"
        pkg_unrel_dir = self.source_dir / "pkg_unrelated"

        for d, name, deps in [
            (pkg_b_dir, "pkg_b", ["pkg_a"]),
            (pkg_c_dir, "pkg_c", ["pkg_b"]),
            (pkg_unrel_dir, "pkg_unrelated", []),
        ]:
            d.mkdir(parents=True, exist_ok=True)
            deps_str = "[" + ", ".join(f'"{dep}"' for dep in deps) + "]"
            (d / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "{name}"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            dependencies = {deps_str}
            """, encoding="utf-8")
            (d / f"{name}.txt").write_text(f"v1 {name}", encoding="utf-8")
            self.workspace_config.packages_enable[name] = True

        # 1. Initial clean deploy of all 4 packages
        res0 = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_a", "pkg_b", "pkg_c", "pkg_unrelated"],
        )
        self.assertEqual(res0.status, "SUCCESS")

        # 2. Simulate pkg_unrelated having uncommitted staged state outside our targeted batch
        (self.install_dir / "pkg_unrelated" / "pkg_unrelated.txt").write_text("dirty unrelated", encoding="utf-8")
        reg = load_state_registry(self.state_file)
        reg.set_package_state("pkg_unrelated", "staged")
        save_state_registry(reg)

        # 3. Update pkg_a, pkg_b, pkg_c; configure pkg_b with auto_rollback=true and a failing post_update hook
        # Because pkg_c depends on pkg_b, Step 4b stages all 3 (pkg_a, pkg_b, pkg_c -> 'staged'),
        # Step 4c installs pkg_a ('installed'), fails on pkg_b ('installing'), and never reaches pkg_c (still 'staged').
        (pkg_a_dir / "file.txt").write_text("v2 pkg_a", encoding="utf-8")
        (pkg_b_dir / "pkg_b.txt").write_text("v2 pkg_b", encoding="utf-8")
        (pkg_c_dir / "pkg_c.txt").write_text("v2 pkg_c", encoding="utf-8")
        hooks_dir = pkg_b_dir / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        fail_hook = hooks_dir / "post_update.py"
        fail_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        fail_hook.chmod(0o755)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        auto_rollback = true
        dependencies = ["pkg_a"]

        [hooks]
        post_update = "drift_hooks/post_update.py"
        """, encoding="utf-8")

        with patch("sys.stderr", StringIO()):
            res_fail = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_a", "pkg_b", "pkg_c"],
                options=DeployOptions(flags=HookExecFlags(streaming=False)),
            )

        self.assertEqual(res_fail.status, "FAILED")
        assert res_fail.failure is not None
        self.assertFalse(res_fail.failure.requires_rollback)

        # Verify pkg_a, pkg_b, AND unattempted pkg_c were all rolled back to v1 and 'installed'
        reg_after = load_state_registry(self.state_file)
        self.assertEqual(reg_after.get_package_state("pkg_a"), "installed")
        self.assertEqual(reg_after.get_package_state("pkg_b"), "installed")
        self.assertEqual(reg_after.get_package_state("pkg_c"), "installed")
        self.assertEqual((self.install_dir / "pkg_a" / "file.txt").read_text(encoding="utf-8"), "Hello source config!")
        self.assertEqual((self.install_dir / "pkg_b" / "pkg_b.txt").read_text(encoding="utf-8"), "v1 pkg_b")
        self.assertEqual((self.install_dir / "pkg_c" / "pkg_c.txt").read_text(encoding="utf-8"), "v1 pkg_c")

        # Verify pkg_unrelated (outside the targeted batch) was NOT touched by the batch auto-rollback
        self.assertEqual(reg_after.get_package_state("pkg_unrelated"), "staged")
        self.assertEqual(
            (self.install_dir / "pkg_unrelated" / "pkg_unrelated.txt").read_text(encoding="utf-8"),
            "dirty unrelated",
        )

    def test_deploy_disabled_package_blocked_by_default_and_allowed_by_adhoc_or_force(self) -> None:
        """Two-Pass & Safeguard Matrix Test: Verifies disabled packages are blocked by default on targeted deploy,
        allowed with --adhoc (while still enforcing Sentinel drift and dependency checks) and --force (bypasses all).
        """
        from drift.core.exceptions import (
            ConfigError,
            MissingDependencyError,
            PackageDisabledError,
            SafeguardError,
        )

        pkg_dis_dir = self.source_dir / "pkg_disabled"
        pkg_dis_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dis_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_disabled"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_dis_dir / "disabled.txt").write_text("v1 disabled payload", encoding="utf-8")

        # Explicitly disable pkg_disabled in workspace config
        self.workspace_config.packages_enable["pkg_disabled"] = False

        # 1. Default targeted deploy (adhoc=False, force=False) -> Blocked by PackageDisabledError (SafeguardError)
        with self.assertRaises(PackageDisabledError) as cm:
            execute_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_disabled"])
        self.assertIsInstance(cm.exception, SafeguardError)
        self.assertEqual(cm.exception.packages, ["pkg_disabled"])
        self.assertIn("[SAFEGUARD] Cannot deploy disabled package(s): pkg_disabled", str(cm.exception))
        self.assertIn("drift enable pkg_disabled", str(cm.exception))
        self.assertIn("drift deploy --adhoc pkg_disabled", str(cm.exception))
        self.assertFalse((self.install_dir / "pkg_disabled").exists())
        self.assertFalse((self.system_target_dir / "disabled.txt").exists())

        res_blocked = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_disabled"])
        self.assertEqual(res_blocked.status, "FAILED")
        assert res_blocked.failure is not None
        self.assertEqual(res_blocked.failure.error_type, "PackageDisabledError")
        self.assertEqual(res_blocked.failure.step_name, "disabled_package_check")
        self.assertFalse(res_blocked.failure.requires_rollback)
        self.assertEqual(res_blocked.failure.recommended_command, "drift deploy --adhoc pkg_disabled")

        # 2. Pass 1 (Cold / Mutation Pass with adhoc=True): deploys disabled package without enabling in config
        res_adhoc1 = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_disabled"],
            options=DeployOptions(adhoc=True),
        )
        self.assertEqual(res_adhoc1.status, "SUCCESS")
        self.assertEqual([p.package for p in res_adhoc1.deployed_packages], ["pkg_disabled"])
        self.assertEqual((self.system_target_dir / "disabled.txt").read_text(encoding="utf-8"), "v1 disabled payload")
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_disabled"), "installed")
        self.assertFalse(self.workspace_config.is_package_enabled("pkg_disabled"))

        # 3. Pass 2 (Warm / Idempotent Pass with adhoc=True): zero changes, skips physical install
        res_adhoc2 = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_disabled"],
            options=DeployOptions(adhoc=True),
        )
        self.assertEqual(res_adhoc2.status, "SUCCESS")
        self.assertEqual(res_adhoc2.deployed_packages, [])

        # 4. Isolation Check: adhoc=True does NOT bypass Sentinel host drift or missing dependencies
        (self.system_target_dir / "disabled.txt").write_text("host drift on disabled pkg", encoding="utf-8")
        with self.assertRaises(DriftDetectedError):
            execute_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_disabled"],
                options=DeployOptions(adhoc=True),
            )

        # 5. force=True bypasses BOTH disabled-package safeguard AND Sentinel host drift
        res_force = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_disabled"],
            options=DeployOptions(force=True),
        )
        self.assertEqual(res_force.status, "SUCCESS")
        self.assertEqual((self.system_target_dir / "disabled.txt").read_text(encoding="utf-8"), "v1 disabled payload")

        # 6. Dependency check isolation: adhoc=True still enforces missing dependency guard, force=True bypasses it
        pkg_dep_dir = self.source_dir / "pkg_dis_dep"
        pkg_dep_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dep_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_dis_dep"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        dependencies = ["nonexistent_prereq"]
        """, encoding="utf-8")
        (pkg_dep_dir / "dep.txt").write_text("needs prereq", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_dis_dep"] = False

        with patch("sys.stderr", StringIO()), self.assertRaises(RuntimeError) as cm_dep:
            execute_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_dis_dep"],
                options=DeployOptions(adhoc=True),
            )
        cause = cm_dep.exception.__cause__
        self.assertIsInstance(cause, MissingDependencyError)
        self.assertIsInstance(cause, SafeguardError)
        self.assertIsInstance(cause, ConfigError)
        assert isinstance(cause, MissingDependencyError)
        self.assertEqual(cause.packages, ["pkg_dis_dep"])
        self.assertEqual(cause.missing_dependencies, {"pkg_dis_dep": ["nonexistent_prereq"]})

        with patch("sys.stderr", StringIO()):
            res_dep_fail = run_primitive_deploy_pipeline(
                self.workspace_config,
                packages_to_deploy=["pkg_dis_dep"],
                options=DeployOptions(adhoc=True),
            )
        self.assertEqual(res_dep_fail.status, "FAILED")
        assert res_dep_fail.failure is not None
        self.assertEqual(res_dep_fail.failure.recommended_command, "drift deploy --no-deps pkg_dis_dep")

        res_force_dep = run_primitive_deploy_pipeline(
            self.workspace_config,
            packages_to_deploy=["pkg_dis_dep"],
            options=DeployOptions(force=True),
        )
        self.assertEqual(res_force_dep.status, "SUCCESS")

    def test_global_deploy_preserves_disabled_installed_packages_and_gc_flag_uninstalls_them(self) -> None:
        """Verifies default global deploy preserves disabled installed packages and emits a hint,
        while DeployOptions(gc=True) uninstalls them and reports both installed and uninstalled packages.
        """
        from drift.core.constants import set_test_mode

        pkg_b_dir = self.source_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("pkg_b content", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_b"] = True

        # 1. Deploy both pkg_a and pkg_b while enabled
        res_init = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=())
        self.assertEqual(res_init.status, "SUCCESS")
        self.assertTrue((self.system_target_dir / "file.txt").is_file())
        self.assertTrue((self.system_target_dir / "file_b.txt").is_file())

        # 2. Disable pkg_b in workspace config (or simulate pkg_b having been deployed via --adhoc)
        self.workspace_config.packages_enable["pkg_b"] = False
        (self.pkg_dir / "file.txt").write_text("pkg_a v2", encoding="utf-8")

        # 3. Default global deploy (gc=False): updates pkg_a, preserves pkg_b on host, and logs hint
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()), \
                 self.assertLogs("drift.primitives.deploy_repo", level="INFO") as cm:
                res_no_gc = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=())
        finally:
            set_test_mode(True, enable_logging=False)

        self.assertEqual(res_no_gc.status, "SUCCESS")
        self.assertIsNone(res_no_gc.gc)
        self.assertEqual(res_no_gc.disabled_installed_packages, ["pkg_b"])
        self.assertTrue((self.system_target_dir / "file_b.txt").is_file())
        self.assertEqual(load_state_registry(self.state_file).get_package_state("pkg_b"), "installed")
        log_text = "\n".join(cm.output)
        self.assertIn("✨ Deployment completed (installed: pkg_a).", log_text)
        self.assertIn("💡 Hint: Installed package(s) [pkg_b] are currently disabled in workspace config.", log_text)
        self.assertIn("drift deploy --gc", log_text)
        self.assertIn("drift uninstall pkg_b", log_text)

        # 4. Global deploy with gc=True: updates pkg_a and uninstalls disabled pkg_b
        (self.pkg_dir / "file.txt").write_text("pkg_a v3", encoding="utf-8")
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()), \
                 self.assertLogs("drift.primitives.deploy_repo", level="INFO") as cm_gc:
                res_gc = run_primitive_deploy_pipeline(
                    self.workspace_config,
                    packages_to_deploy=(),
                    options=DeployOptions(gc=True),
                )
        finally:
            set_test_mode(True, enable_logging=False)

        self.assertEqual(res_gc.status, "SUCCESS")
        self.assertIsNotNone(res_gc.gc)
        assert res_gc.gc is not None
        self.assertEqual(res_gc.gc.uninstalled_orphans, ["pkg_b"])
        self.assertEqual(res_gc.disabled_installed_packages, [])
        self.assertFalse((self.system_target_dir / "file_b.txt").exists())
        self.assertIsNone(load_state_registry(self.state_file).get_package_state("pkg_b"))
        gc_log_text = "\n".join(cm_gc.output)
        self.assertIn("✨ Deployment completed (installed: pkg_a; uninstalled (gc): pkg_b).", gc_log_text)
        self.assertNotIn("💡 Hint:", gc_log_text)

    def test_global_deploy_when_all_packages_disabled_handles_hint_and_gc(self) -> None:
        """Verifies global deploy when all packages are disabled still emits hint (without --gc) and runs GC (with --gc)."""
        from drift.core.constants import set_test_mode

        # 1. Deploy pkg_a initially
        run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 2. Disable all packages in workspace config
        self.workspace_config.packages_enable["pkg_a"] = False
        self.workspace_config.packages_enable_default = False

        # 3. Global deploy without --gc: skips deployment, keeps pkg_a installed, and emits hint
        res_skip = run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=())
        self.assertEqual(res_skip.status, "SUCCESS")
        self.assertEqual(res_skip.target_packages, [])
        self.assertEqual(res_skip.disabled_installed_packages, ["pkg_a"])
        self.assertTrue((self.system_target_dir / "file.txt").is_file())

        # 4. Global deploy with --gc: runs GC even though target_pkgs is empty, uninstalling pkg_a
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()), \
                 self.assertLogs("drift.primitives.deploy_repo", level="INFO") as cm_gc:
                res_gc = run_primitive_deploy_pipeline(
                    self.workspace_config,
                    packages_to_deploy=(),
                    options=DeployOptions(gc=True),
                )
        finally:
            set_test_mode(True, enable_logging=False)

        self.assertEqual(res_gc.status, "SUCCESS")
        assert res_gc.gc is not None
        self.assertEqual(res_gc.gc.uninstalled_orphans, ["pkg_a"])
        self.assertFalse((self.system_target_dir / "file.txt").exists())
        self.assertIn("✨ Deployment completed (uninstalled (gc): pkg_a).", "\n".join(cm_gc.output))

    def test_format_deploy_summary_and_hint_unit(self) -> None:
        """Unit test for format_deploy_summary and format_disabled_installed_packages_hint helpers."""
        from drift.primitives.deploy_repo import format_deploy_summary, format_disabled_installed_packages_hint
        from drift.models import PackageInstallPlan, PackageInstallResult, GcResult

        pkg_res = PackageInstallResult(plan=PackageInstallPlan(package="shell"), status="SUCCESS")
        gc_empty = GcResult(uninstalled_orphans=[])
        gc_with_orphan = GcResult(uninstalled_orphans=["vim"])

        self.assertEqual(
            format_deploy_summary([], None),
            "✨ Deployment completed: all packages are already up-to-date (no changes detected).",
        )
        self.assertEqual(
            format_deploy_summary([pkg_res], gc_empty),
            "✨ Deployment completed (installed: shell).",
        )
        self.assertEqual(
            format_deploy_summary([], gc_with_orphan),
            "✨ Deployment completed (uninstalled (gc): vim).",
        )
        self.assertEqual(
            format_deploy_summary([pkg_res], gc_with_orphan),
            "✨ Deployment completed (installed: shell; uninstalled (gc): vim).",
        )

        self.assertIsNone(format_disabled_installed_packages_hint([]))
        hint = format_disabled_installed_packages_hint(["vim", "tmux"])
        assert hint is not None
        self.assertIn("[vim, tmux]", hint)
        self.assertIn("drift deploy --gc", hint)
        self.assertIn("drift uninstall vim tmux", hint)


if __name__ == "__main__":
    unittest.main()




