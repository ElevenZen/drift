import os
import sys
import json
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path

from drift.cli import main
from drift.result_models import (
    SerializableModel,
    NextActionType,
    PackageInstallResult,
    FileOperations,
    InstallDeploymentResult,
    StatusResult,
    DeployResult,
    DeployFailure,
    UninstallResult,
    PackageUninstallResult,
    GcResult,
    AdoptResult,
    NewPackageResult,
    AddResourceResult,
    RollbackResult,
    RepairResult,
)
from tests.test_utils import TestCaseUtilityMixin


class TestResultModels(unittest.TestCase):
    """Unit tests for result models and serialization."""

    def test_serialization_primitives(self) -> None:
        ops = FileOperations(
            added=[Path("/tmp/a"), Path("/tmp/b")],
            modified=[],
            deleted=[]
        )
        pkg_res = PackageInstallResult(
            package="zsh",
            install_method="stow",
            target_directory="/home/user",
            operations=ops,
            is_first_time=True
        )
        data = pkg_res.to_dict()
        self.assertEqual(data["package"], "zsh")
        self.assertEqual(data["operations"]["added"], ["/tmp/a", "/tmp/b"])

        json_str = pkg_res.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed["package"], "zsh")
        self.assertEqual(parsed["is_first_time"], True)

    def test_deploy_failure_model(self) -> None:
        failure = DeployFailure(
            step_index=0,
            step_name="sentinel_drift_check",
            package="nvim",
            error_message="System drift detected in package 'nvim'",
            error_type="RuntimeError",
            requires_rollback=False,
            next_action_type=NextActionType.ADOPT_OR_FORCE,
            recommended_command="drift adopt nvim",
            drifted_files=["M nvim/init.lua"]
        )
        deploy_res = DeployResult(
            status="ABORTED_DRIFT",
            target_packages=["nvim"],
            failure=failure
        )
        parsed = json.loads(deploy_res.to_json())
        self.assertEqual(parsed["status"], "ABORTED_DRIFT")
        self.assertEqual(parsed["failure"]["next_action_type"], "adopt_or_force")
        self.assertEqual(parsed["failure"]["requires_rollback"], False)
        self.assertEqual(parsed["failure"]["recommended_command"], "drift adopt nvim")

    def test_uninstall_result_iteration(self) -> None:
        un = UninstallResult(
            packages=[
                PackageUninstallResult(package="pkg_a", install_method="stow", target_directory="/home/test"),
                PackageUninstallResult(package="pkg_b", install_method="copy", target_directory="/home/test", status="FAILED"),
            ]
        )
        # Verify backward compatibility iteration
        uninstalled = list(un)
        self.assertEqual(uninstalled, ["pkg_a"])
        self.assertEqual(len(un), 1)


class TestCLIJsonOutput(TestCaseUtilityMixin, unittest.TestCase):
    """Integration tests verifying CLI output with --json."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = os.path.join(self.temp_dir.name, "drift_workspace")
        os.makedirs(self.drift_root, exist_ok=True)

        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        from drift.workspace_init import init_drift_workspace
        init_drift_workspace(Path(self.drift_root), force=True, no_git_root=True)

        for d in [self.drift_root, os.path.join(self.drift_root, "render"), os.path.join(self.drift_root, "install")]:
            subprocess.run(["git", "-C", d, "config", "user.name", "Test User"], check=True, capture_output=True)
            subprocess.run(["git", "-C", d, "config", "user.email", "test@example.com"], check=True, capture_output=True)

        self.target_dir = os.path.join(self.temp_dir.name, "home")
        os.makedirs(self.target_dir, exist_ok=True)

        # Update drift.toml
        self.config_dir = os.path.join(self.drift_root, "config")
        with open(os.path.join(self.config_dir, "drift.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            source_directory = "src"
            render_directory = "render"
            install_directory = "install"

            [packages.enable]
            pkg_a = true
            pkg_b = false
            """)

        self.src_dir = os.path.join(self.drift_root, "src")
        pkg_path = os.path.join(self.src_dir, "pkg_a")
        os.makedirs(pkg_path, exist_ok=True)
        with open(os.path.join(pkg_path, "drift_package.toml"), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.target_dir}"
            """)
        with open(os.path.join(pkg_path, "test.txt"), "w", encoding="utf-8") as f:
            f.write("test content")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_cli_json(self, args: list) -> dict:
        stdout = StringIO()
        old_stdout = sys.stdout
        sys.stdout = stdout
        try:
            main(args)
        finally:
            sys.stdout = old_stdout
        raw = stdout.getvalue().strip()
        return json.loads(raw)

    def test_status_json(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "status", "--json"])
        self.assertEqual(res["command"], "status")
        self.assertIn("overall_status", res)
        self.assertIn("packages", res)
        self.assertTrue(any(p["name"] == "pkg_a" for p in res["packages"]))

    def test_diff_json(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "diff", "--json"])
        self.assertEqual(res["command"], "diff")
        self.assertIn("packages", res)

    def test_deploy_json_success(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "deploy", "pkg_a", "--json"])
        self.assertEqual(res["command"], "deploy")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("pkg_a", res["target_packages"])
        self.assertTrue(len(res["deployed_packages"]) > 0)
        self.assertTrue(len(res["completed_steps"]) > 0)

    def test_new_package_json(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "new", "pkg_c", "--json"])
        self.assertEqual(res["command"], "new")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["package"], "pkg_c")
        self.assertTrue(os.path.exists(res["config_file"]))

    def test_gc_json(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "gc", "--json"])
        self.assertEqual(res["command"], "gc")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("uninstalled_orphans", res)

    def test_repair_json(self) -> None:
        res = self._run_cli_json(["-C", self.drift_root, "--no-git-root", "repair", "--json"])
        self.assertEqual(res["command"], "repair")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("checks", res)

    def test_deploy_sentinel_drift_failure_json(self) -> None:
        # First deploy pkg_a
        self._run_cli_json(["-C", self.drift_root, "--no-git-root", "deploy", "pkg_a", "--json"])

        # Now drift the host file
        host_file = os.path.join(self.target_dir, "test.txt")
        with open(host_file, "w", encoding="utf-8") as f:
            f.write("drifted content on host")

        # Deploy without force should output error JSON with status ABORTED_DRIFT
        stdout = StringIO()
        old_stdout = sys.stdout
        sys.stdout = stdout
        try:
            with self.assertRaises(SystemExit) as cm:
                main(["-C", self.drift_root, "--no-git-root", "deploy", "pkg_a", "--json"])
            self.assertEqual(cm.exception.code, 3)
        finally:
            sys.stdout = old_stdout

        raw = stdout.getvalue().strip()
        res = json.loads(raw)
        self.assertEqual(res["status"], "ABORTED_DRIFT")
        self.assertIsNotNone(res["failure"])
        self.assertEqual(res["failure"]["next_action_type"], "adopt_or_force")
        self.assertEqual(res["failure"]["requires_rollback"], False)
        self.assertEqual(res["failure"]["recommended_command"], "drift adopt")
