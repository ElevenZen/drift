"""Tests for typed deploy guard exceptions and exit-code propagation.

Covers:
    * Sentinel drift guard raises `DriftDetectedError` (exit 3) carrying the drifted package list, and the
      midway guard raises `MidwayTransactionError` (exit 1); both remain `RuntimeError` subclasses.
    * `run_primitive_deploy_pipeline` classifies failures by exception type (not message text), records
      `DeployFailure.exit_code`, picks the right next action / rollback command, and no longer crashes on
      global deploys (`packages_to_deploy=None`).
    * `--force` bypasses both guards.
    * CLI exit codes on both Typer and Argparse backends, in text and `--json` modes, plus a clean redeploy
      after adoption-free recovery (`--force`) returning to success (two-pass cold/warm).
"""

import json
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, List
from unittest.mock import patch

from drift.cli import main
from drift.cli.argparse_backend import run_argparse_cli
from drift.config.workspace_config import WorkspaceConfig
from drift.core.constants import ExitCode
from drift.core.exceptions import DriftDetectedError, DriftError, MidwayTransactionError
from drift.core.state_registry import load_state_registry, save_state_registry
from drift.models import DeployOptions, NextActionType
from drift.primitives.deploy_repo import execute_deploy_pipeline, run_primitive_deploy_pipeline
from drift.primitives.workspace_init import init_drift_workspace

CLI_BACKENDS: Dict[str, Callable[[List[str]], None]] = {
    "typer": main,
    "argparse": run_argparse_cli,
}


class _DeployWorkspaceTestCase(unittest.TestCase):
    """An initialized workspace with one enabled copy-mode package `pkg_a` deployed into a temp home."""

    PKG = "pkg_a"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name).resolve()
        self.drift_root = tmp / "ws"
        self.home = tmp / "home"
        self.drift_root.mkdir()
        self.home.mkdir()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift_workspace.local.toml").write_text(
            f'[packages.enable]\n{self.PKG} = true\n\n[settings]\n'
            'git_user_name = "test_user"\ngit_user_email = "test_user@example.com"\n',
            encoding="utf-8",
        )
        for repo in ("render", "install"):
            for key, val in (("user.name", "test_user"), ("user.email", "test_user@example.com")):
                subprocess.run(["git", "config", key, val], cwd=self.drift_root / repo, check=True, capture_output=True)
        pkg = self.drift_root / "src" / self.PKG
        pkg.mkdir(parents=True)
        (pkg / "drift_package.toml").write_text(
            f'[package]\nname = "{self.PKG}"\ninstall_method = "copy"\ntarget_directory = "{self.home.as_posix()}"\n',
            encoding="utf-8",
        )
        (pkg / "file.txt").write_text("from source\n", encoding="utf-8")
        self.target_file = self.home / "file.txt"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self) -> WorkspaceConfig:
        return WorkspaceConfig.from_workspace_dir(self.drift_root, quiet=True)

    def deploy_ok(self) -> None:
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            res = run_primitive_deploy_pipeline(self.config(), packages_to_deploy=[self.PKG])
        self.assertEqual(res.status, "SUCCESS", res.failure)

    def make_drift(self) -> None:
        self.deploy_ok()
        self.target_file.write_text("edited on host\n", encoding="utf-8")

    def make_midway(self) -> None:
        self.deploy_ok()
        state_file = self.drift_root / "install" / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(self.PKG, state="installing")
        save_state_registry(registry)


# =============================================================================
# Guard exceptions
# =============================================================================

class TestDeployGuardExceptions(_DeployWorkspaceTestCase):

    def test_drift_guard_raises_drift_detected_error(self) -> None:
        self.make_drift()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            with self.assertRaises(DriftDetectedError) as cm:
                execute_deploy_pipeline(self.config(), packages_to_deploy=[self.PKG])
        err = cm.exception
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(err.exit_code, ExitCode.DRIFT_DETECTED)
        self.assertEqual(err.packages, [self.PKG])
        self.assertIn("[DEPLOY ABORTED] System drift detected", str(err))
        self.assertIn(f"drift adopt {self.PKG}", str(err))
        # The guard must not touch the drifted host file.
        self.assertEqual(self.target_file.read_text(encoding="utf-8"), "edited on host\n")

    def test_midway_guard_raises_midway_transaction_error(self) -> None:
        self.make_midway()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            with self.assertRaises(MidwayTransactionError) as cm:
                execute_deploy_pipeline(self.config(), packages_to_deploy=[self.PKG])
        err = cm.exception
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(err.exit_code, ExitCode.GENERAL_ERROR)
        self.assertEqual(err.packages, [self.PKG])
        self.assertIn("midway transaction state", str(err))

    def test_force_bypasses_drift_guard(self) -> None:
        self.make_drift()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            res = run_primitive_deploy_pipeline(
                self.config(), packages_to_deploy=[self.PKG], options=DeployOptions(force=True)
            )
        self.assertEqual(res.status, "SUCCESS", res.failure)
        self.assertEqual(self.target_file.read_text(encoding="utf-8"), "from source\n")


# =============================================================================
# DeployResult classification
# =============================================================================

class TestDeployResultClassification(_DeployWorkspaceTestCase):

    def _run(self, packages=None) -> object:
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            return run_primitive_deploy_pipeline(self.config(), packages_to_deploy=packages)

    def test_drift_is_aborted_drift_with_exit_code_3(self) -> None:
        self.make_drift()
        res = self._run([self.PKG])
        self.assertEqual(res.status, "ABORTED_DRIFT")
        self.assertEqual(res.failure.exit_code, ExitCode.DRIFT_DETECTED)
        self.assertEqual(res.failure.error_type, "DriftDetectedError")
        self.assertEqual(res.failure.next_action_type, NextActionType.ADOPT_OR_FORCE)
        self.assertEqual(res.failure.step_name, "sentinel_drift_check")
        self.assertEqual(json.loads(res.to_json())["failure"]["exit_code"], 3)

    def test_midway_requires_rollback_with_package_command(self) -> None:
        self.make_midway()
        res = self._run([self.PKG])
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.failure.exit_code, ExitCode.GENERAL_ERROR)
        self.assertEqual(res.failure.error_type, "MidwayTransactionError")
        self.assertTrue(res.failure.requires_rollback)
        self.assertEqual(res.failure.next_action_type, NextActionType.ROLLBACK)
        self.assertEqual(res.failure.recommended_command, f"drift rollback {self.PKG}")

    def test_global_deploy_failures_do_not_crash(self) -> None:
        for label, setup in (("drift", self.make_drift), ("midway", self.make_midway)):
            with self.subTest(case=label):
                self.tearDown()
                self.setUp()
                setup()
                res = self._run(None)
                self.assertEqual(res.target_packages, [])
                self.assertTrue(res.is_global_deploy)
                self.assertNotEqual(res.status, "SUCCESS")
                if label == "midway":
                    self.assertEqual(res.failure.recommended_command, f"drift rollback {self.PKG}")

    def test_message_text_alone_no_longer_classifies_as_drift(self) -> None:
        with patch("drift.primitives.deploy_repo.execute_deploy_pipeline",
                   side_effect=RuntimeError("System drift detected (but not a typed error)")):
            res = self._run([self.PKG])
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.failure.exit_code, ExitCode.GENERAL_ERROR)
        self.assertEqual(res.failure.next_action_type, NextActionType.FIX_TEMPLATE)

    def test_other_drift_errors_keep_their_exit_codes(self) -> None:
        class _Collision(DriftError):
            exit_code = ExitCode.COLLISION_ERROR

        with patch("drift.primitives.deploy_repo.execute_deploy_pipeline", side_effect=_Collision("boom")):
            res = self._run([self.PKG])
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.failure.exit_code, ExitCode.COLLISION_ERROR)


# =============================================================================
# CLI exit codes (both backends, text and JSON)
# =============================================================================

class TestDeployCliExitCodes(_DeployWorkspaceTestCase):

    def _exit_code(self, runner: Callable[[List[str]], None], argv: List[str]) -> object:
        stdout = StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", StringIO()):
            try:
                runner(argv)
            except SystemExit as e:
                return e.code, stdout.getvalue()
        return 0, stdout.getvalue()

    def test_drift_exits_3_in_text_and_json_modes(self) -> None:
        self.make_drift()
        for backend, runner in CLI_BACKENDS.items():
            for extra in ([], ["--json"]):
                with self.subTest(backend=backend, json=bool(extra)):
                    code, out = self._exit_code(runner, ["-C", self.drift_root.as_posix(), "deploy", self.PKG, *extra])
                    self.assertEqual(code, ExitCode.DRIFT_DETECTED)
                    if extra:
                        payload = json.loads(out)
                        self.assertEqual(payload["status"], "ABORTED_DRIFT")
                        self.assertEqual(payload["failure"]["exit_code"], 3)

    def test_midway_exits_1(self) -> None:
        self.make_midway()
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                code, _ = self._exit_code(runner, ["-C", self.drift_root.as_posix(), "deploy", self.PKG])
                self.assertEqual(code, ExitCode.GENERAL_ERROR)

    def test_force_recovers_then_warm_deploy_succeeds(self) -> None:
        self.make_drift()
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                self.target_file.write_text(f"edited by {backend}\n", encoding="utf-8")
                # Pass 1 (cold recovery): --force overwrites the drift.
                code, _ = self._exit_code(runner, ["-C", self.drift_root.as_posix(), "deploy", self.PKG, "--force"])
                self.assertEqual(code, 0)
                self.assertEqual(self.target_file.read_text(encoding="utf-8"), "from source\n")
                # Pass 2 (warm): plain deploy succeeds with no drift left.
                code, _ = self._exit_code(runner, ["-C", self.drift_root.as_posix(), "deploy", self.PKG])
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
