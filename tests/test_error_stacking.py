"""Unit tests verifying error message stacking elimination and clean error popping paths."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from drift.core.constants import set_test_mode
from drift.core.exceptions import (
    DriftError,
    ConfigError,
    RenderError,
    HookExecutionError,
    mark_logged,
    is_logged,
    is_drift_error,
)
from drift.config.workspace_config import WorkspaceConfig
from drift.primitives.deploy_repo import run_primitive_deploy_pipeline
from drift.primitives.stage_repo import StagePlan


class TestErrorStacking(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True, enable_logging=True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()
        
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        (self.source_dir / "pkg_a").mkdir(parents=True, exist_ok=True)
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)

        for repo_dir in (self.install_dir, self.render_dir):
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)

        self.workspace_config = WorkspaceConfig(drift_root=self.drift_root)

    def tearDown(self) -> None:
        set_test_mode(True, enable_logging=False)
        self.temp_dir.cleanup()

    def test_error_tracking_helpers(self) -> None:
        """Verifies mark_logged, is_logged, and is_drift_error on built-in and domain exceptions."""
        err_builtin = RuntimeError("builtin failure")
        self.assertFalse(is_logged(err_builtin))
        self.assertFalse(is_drift_error(err_builtin))

        # Fluent chaining
        ret = mark_logged(err_builtin)
        self.assertIs(ret, err_builtin)
        self.assertTrue(is_logged(err_builtin))

        err_drift = ConfigError("config failure")
        self.assertFalse(is_logged(err_drift))
        self.assertTrue(is_drift_error(err_drift))
        mark_logged(err_drift)
        self.assertTrue(is_logged(err_drift))

    def test_package_loader_does_not_double_wrap_config_error(self) -> None:
        """Verifies that an inner ConfigError is re-raised directly without being caught by (TypeError, ValueError)."""
        from drift.config.package_loader import load_package_config_from_source_dir

        pkg_dir = self.drift_root / "src" / "pkg_test"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "drift_package.toml").write_text("[package]\ninstall_method = 'invalid_method'\n")

        with self.assertRaises(ConfigError) as ctx:
            load_package_config_from_source_dir(pkg_dir, self.workspace_config)

        err_str = str(ctx.exception)
        # Should contain the direct reason
        self.assertIn("Must be 'stow' or 'copy'", err_str)
        # Should NOT contain double-wrapping of "Invalid package configuration...: Invalid package configuration"
        count = err_str.count("Invalid")
        self.assertEqual(count, 1)

    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_step1_drift_error_logs_popping_path_without_appending_inner_message(self, mock_render) -> None:
        """When Step 1 fails with a DriftError, logs the step failure without appending redundant inner error."""
        inner_error_msg = "Variable '$SECRET_KEY' was missing in template"
        mock_render.side_effect = mark_logged(RenderError(inner_error_msg))

        with self.assertLogs("drift.primitives.deploy_repo", level="ERROR") as cm:
            with self.assertRaises(RuntimeError) as ctx:
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        logs = "\n".join(cm.output)
        # 1. Popping path is preserved:
        self.assertIn("❌ [CRITICAL] Step 1 (Template Rendering) failed.", logs)
        # 2. Inner error message is NOT repeated into the outer step log:
        self.assertNotIn(inner_error_msg, logs)
        # 3. Exception preserves the step context:
        self.assertIn("Step 1 (Template Rendering) failed.", str(ctx.exception))

    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_step1_unexpected_error_logs_details(self, mock_render) -> None:
        """When Step 1 fails with an unexpected unlogged error, the error details are logged."""
        mock_render.side_effect = KeyError("unexpected_system_dict_key")

        with self.assertLogs("drift.primitives.deploy_repo", level="ERROR") as cm:
            with self.assertRaises(RuntimeError):
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        logs = "\n".join(cm.output)
        # For unexpected unlogged error, details are included:
        self.assertIn("❌ [CRITICAL] Step 1 (Template Rendering) failed. Error: 'unexpected_system_dict_key'", logs)

    @patch("drift.primitives.deploy_repo.run_primitive_6_commit_install_repo")
    @patch("drift.primitives.deploy_repo.run_primitive_5_install_deployment")
    @patch("drift.primitives.deploy_repo.execute_stage_packages")
    @patch("drift.primitives.deploy_repo.prepare_stage_packages")
    @patch("drift.primitives.deploy_repo.run_primitive_3_commit_render_repo")
    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_step4_hook_error_does_not_repeat_hook_message_in_step_log(
        self, mock_p2, mock_p3, mock_prepare, mock_execute, mock_p5, mock_p6
    ) -> None:
        """When Step 4 encounters HookExecutionError, it logs the abort without duplicating the multi-line hook message."""
        mock_p2.return_value = MagicMock(status="SUCCESS")
        mock_prepare.return_value = StagePlan(pkg_metadata={"pkg_a": MagicMock()}, state_registry=MagicMock())
        mock_execute.return_value = {"pkg_a": MagicMock(has_changes=True)}
        
        hook_err = HookExecutionError(
            package="pkg_a",
            hook_name="pre_install",
            message="Hook failed with exit code 127.\nCommand: /bin/false\nStderr: No such file",
            requires_rollback=False,
        )
        mock_p5.side_effect = hook_err

        with self.assertLogs("drift.primitives.deploy_repo", level="ERROR") as cm:
            with self.assertRaises(RuntimeError):
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        logs = "\n".join(cm.output)
        # Popping path is logged:
        self.assertIn("❌ [DEPLOY ABORTED] Step 4 (Physical Deploy/Install) stopped due to hook failure in 'pkg_a'.", logs)
        # The multi-line stderr and command from hook_err are not dumped into the outer step log:
        self.assertNotIn("No such file", logs)
        self.assertNotIn("Command: /bin/false", logs)
        mock_p6.assert_called_once()

    @patch("drift.primitives.deploy_repo.print_emergency_recovery_card")
    @patch("drift.primitives.deploy_repo.prepare_stage_packages")
    @patch("drift.primitives.deploy_repo.run_primitive_3_commit_render_repo")
    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_step3_preflight_assert_failure_does_not_print_recovery_card(
        self, mock_p2, mock_p3, mock_prepare, mock_recovery_card
    ) -> None:
        """When Step 3 fails during pre-flight assertion, logs error without printing emergency recovery card."""
        from drift.core.exceptions import DriftDetectedError

        mock_p2.return_value = MagicMock(status="SUCCESS")
        mock_prepare.side_effect = DriftDetectedError(
            "Uncommitted modifications detected in install/pkg_a",
            packages=["pkg_a"],
        )

        with self.assertLogs("drift.primitives.deploy_repo", level="INFO") as cm:
            with self.assertRaises(RuntimeError) as ctx:
                run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        logs = "\n".join(cm.output)
        self.assertIn("❌ [CRITICAL] Step 3 (Sandbox Staging Pre-flight) failed.", logs)
        self.assertIn("👉 Problematic package(s): 'pkg_a'. Please resolve pre-flight issues and try 'drift deploy' again.", logs)
        self.assertIn("Step 3 (Sandbox Staging Pre-flight) failed.", str(ctx.exception))
        mock_recovery_card.assert_not_called()

    @patch("drift.primitives.deploy_repo.print_emergency_recovery_card")
    @patch("drift.primitives.deploy_repo.execute_stage_packages")
    @patch("drift.primitives.deploy_repo.prepare_stage_packages")
    @patch("drift.primitives.deploy_repo.run_primitive_3_commit_render_repo")
    @patch("drift.primitives.deploy_repo.run_primitive_2_render_packages")
    def test_deploy_step3_staging_execution_failure_prints_recovery_card(
        self, mock_p2, mock_p3, mock_prepare, mock_execute, mock_recovery_card
    ) -> None:
        """When Step 3 fails during physical staging execution, emergency recovery card is printed."""
        mock_p2.return_value = MagicMock(status="SUCCESS")
        mock_prepare.return_value = StagePlan(pkg_metadata={"pkg_a": MagicMock()}, state_registry=MagicMock())
        mock_execute.side_effect = OSError("Disk full while writing install state")

        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_deploy_pipeline(self.workspace_config, packages_to_deploy=["pkg_a"])

        self.assertIn("Midway crash: Step 3 (Sandbox Staging) failed.", str(ctx.exception))
        mock_recovery_card.assert_called_once()

    def test_render_packages_skips_inner_msg_if_already_logged(self) -> None:
        """When render_package raises an already logged exception, run_primitive_2_render_packages logs package context without repeating details."""
        import sys
        render_pkg_module = sys.modules["drift.render.render_package"]
        from drift.render.render_package import run_primitive_2_render_packages

        inner_err = mark_logged(ConfigError("Detailed syntax error in config"))
        with patch.object(render_pkg_module, "render_package", side_effect=inner_err):
            with self.assertLogs("drift.render.render_package", level="ERROR") as cm:
                res = run_primitive_2_render_packages(self.workspace_config, target_pkgs=["pkg_a"])

        self.assertEqual(res.status, "FAILED")
        logs = "\n".join(cm.output)
        self.assertIn("❌ Failed to render package 'pkg_a'.", logs)
        self.assertNotIn("Detailed syntax error in config", logs)

    @patch("drift.utils.git_utils.run_command")
    def test_git_utils_commit_error_is_marked_logged(self, mock_run) -> None:
        """When commit_repo_changes fails, it logs at origin and raises a RuntimeError marked as logged."""
        from drift.utils.git_utils import commit_repo_changes
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, ["git", "add"], stderr="nothing to commit")

        with self.assertLogs("drift.utils.git_utils", level="ERROR") as cm:
            with self.assertRaises(RuntimeError) as ctx:
                commit_repo_changes(self.render_dir, "test commit", repo_name="render repo")

        self.assertTrue(is_logged(ctx.exception))
        logs = "\n".join(cm.output)
        self.assertIn("Failed to stage changes in render repo. Stderr: nothing to commit", logs)

    @patch("drift.config.workspace_config.WorkspaceConfig.from_workspace_dir")
    def test_workspace_repair_does_not_double_wrap_config_error(self, mock_from_dir) -> None:
        """When repair_drift_workspace catches ConfigError, it re-raises it directly without wrapping."""
        from drift.primitives.workspace_repair import repair_drift_workspace

        original_err = ConfigError("Unsupported workspace setting")
        mock_from_dir.side_effect = original_err

        with self.assertRaises(ConfigError) as ctx:
            repair_drift_workspace(self.drift_root)

        self.assertIs(ctx.exception, original_err)
        self.assertEqual(str(ctx.exception), "Unsupported workspace setting")

