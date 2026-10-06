"""Unit and Integration Tests for Drift Deployment Planning and Preview (drift plan / deploy --dry-run)."""

import os
import io
import json
import shutil
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.cli import main, run_argparse_cli
from drift.core.constants import ExitCode, set_test_mode
from drift.core.file_action import FileActionType
from drift.config import load_workspace_config
from drift.primitives.plan_repo import prepare_deploy_preview, DeployOptions
from tests.test_utils import TestCaseUtilityMixin


class TestPlan(TestCaseUtilityMixin, unittest.TestCase):
    """Test suite verifying full-cycle deployment planning, host drift auditing, and CLI IO."""

    def setUp(self) -> None:
        set_test_mode(True, enable_logging=False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)

        self.drift_root = self.test_dir / "drift_workspace"
        self.drift_root.mkdir(parents=True, exist_ok=True)
        self.src_dir = self.drift_root / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.drift_root / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.host_target = self.test_dir / "host_home"
        self.host_target.mkdir(parents=True, exist_ok=True)

        # Write workspace configuration
        (self.config_dir / "drift_workspace.toml").write_text("""
        [workspace]
        name = "test_plan_ws"
        enable_render = true
        enable_install = true
        """, encoding="utf-8")

        # Initialize workspace repositories
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            main(["-C", str(self.drift_root), "init", "--force"])
            render_dir = self.drift_root / "render"
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=render_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=render_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=render_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial render commit"], cwd=render_dir, check=True, capture_output=True)

            install_dir = self.drift_root / "install"
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=install_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=install_dir, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=install_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial install commit"], cwd=install_dir, check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _setup_package(self, pkg_name: str, files: dict, deps: list = None, install_method: str = "copy") -> Path:
        """Helper to create a source package with drift_package.toml and content files."""
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.host_target / pkg_name
        target_dir.mkdir(parents=True, exist_ok=True)

        dep_str = ", ".join(f'"{d}"' for d in (deps or []))
        config_text = f"""
        [package]
        name = "{pkg_name}"
        enable_render = true
        enable_install = true
        install_method = "{install_method}"
        target_directory = "{target_dir}"
        dependencies = [{dep_str}]
        """
        (pkg_dir / "drift_package.toml").write_text(config_text, encoding="utf-8")

        for rel_path, content in files.items():
            fpath = pkg_dir / rel_path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

        return pkg_dir

    def _deploy_package_clean(self, pkg_name: str) -> None:
        """Deploys a package to host and commits all intermediate stages."""
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            main(["-C", str(self.drift_root), "render", pkg_name])
            main(["-C", str(self.drift_root), "render-commit", "-m", f"Render {pkg_name}", pkg_name])
            main(["-C", str(self.drift_root), "stage", pkg_name])
            main(["-C", str(self.drift_root), "install-commit", "-m", f"Stage {pkg_name}", pkg_name])
            main(["-C", str(self.drift_root), "apply", pkg_name])
            main(["-C", str(self.drift_root), "install-commit", "-m", f"Apply {pkg_name}"])

    def test_plan_clean_workspace_no_changes_no_drift(self) -> None:
        """Verifies planning on fully synchronized workspace reports success with zero changes or drift."""
        self._setup_package("pkg_clean", {"app.conf": "key=val\n"})
        self._deploy_package_clean("pkg_clean")

        workspace_config = load_workspace_config(self.drift_root)
        preview = prepare_deploy_preview(workspace_config, ["pkg_clean"])

        self.assertEqual(preview.status, "SUCCESS")
        self.assertFalse(preview.has_changes)
        self.assertFalse(preview.has_drift)
        self.assertEqual(preview.packages_install_order, ["pkg_clean"])
        self.assertEqual(preview.packages_unchanged, ["pkg_clean"])
        self.assertEqual(preview.packages_with_changes, [])
        self.assertEqual(preview.packages_with_drift, [])

        # Invariant: install/ repository working tree MUST remain 100% clean
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.drift_root / "install",
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.stdout.strip(), "")

    def test_plan_detects_host_drift_without_altering_install_repo(self) -> None:
        """Verifies that Phase 0 host drift inspection detects divergence without modifying install/ repo."""
        self._setup_package("pkg_drift", {"config.ini": "port=8080\n", "unchanged.txt": "same\n"})
        self._deploy_package_clean("pkg_drift")

        # Mutate the deployed file live on the host
        host_file = self.host_target / "pkg_drift" / "config.ini"
        host_file.write_text("port=9090\n", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = prepare_deploy_preview(workspace_config, ["pkg_drift"])

        self.assertEqual(preview.status, "DRIFT_DETECTED")
        self.assertTrue(preview.has_drift)
        self.assertIn("pkg_drift", preview.drift_warnings)
        self.assertIn("Host drift detected in 'pkg_drift'", preview.drift_warnings["pkg_drift"])

        pkg_preview = preview.package_previews["pkg_drift"]
        self.assertTrue(pkg_preview.has_drift)
        self.assertIsNotNone(pkg_preview.reverse_sync_plan)
        self.assertTrue(pkg_preview.reverse_sync_plan.has_changes)

        # Mutating action must be UPDATE_COPY
        mutations = [
            a for a in pkg_preview.reverse_sync_plan.actions
            if a.action_type not in (FileActionType.SKIP_IDENTICAL, FileActionType.INFO_MESSAGE)
        ]
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0].action_type, FileActionType.UPDATE_COPY)

        # Verify formatting excludes SKIP_IDENTICAL when not verbose
        text = pkg_preview.format_text(verbose=False)
        self.assertIn("Reverse Sync Plan", text)
        self.assertIn("UPDATE_COPY", text)
        self.assertNotIn("SKIP_IDENTICAL", text)

        # Verify formatting includes SKIP_IDENTICAL when verbose
        text_v = pkg_preview.format_text(verbose=True)
        self.assertIn("Reverse Sync Plan", text_v)
        self.assertIn("UPDATE_COPY", text_v)
        self.assertIn("SKIP_IDENTICAL", text_v)

        # Invariant: install/ git working tree MUST remain 100% clean (zero uncommitted mutations)
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.drift_root / "install",
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.stdout.strip(), "")

    def test_plan_force_bypasses_drift_status(self) -> None:
        """Verifies that DeployOptions.force sets status to SUCCESS even when host drift exists."""
        self._setup_package("pkg_force", {"settings.json": '{"debug": false}\n'})
        self._deploy_package_clean("pkg_force")

        # Mutate on host
        (self.host_target / "pkg_force" / "settings.json").write_text('{"debug": true}\n', encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        options = DeployOptions(force=True)
        preview = prepare_deploy_preview(workspace_config, ["pkg_force"], options=options)

        self.assertEqual(preview.status, "SUCCESS")
        self.assertTrue(preview.has_drift)
        self.assertIn("pkg_force", preview.drift_warnings)

    def test_plan_cli_exit_codes_and_json_mode(self) -> None:
        """Verifies CLI exit codes across Typer and Argparse backends on clean vs drifted workspace."""
        self._setup_package("pkg_cli", {"test.txt": "hello\n"})
        self._deploy_package_clean("pkg_cli")

        # 1. Clean workspace -> exit code 0 across both backends
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "plan", "pkg_cli"])
        self.assertIn("pkg_cli", stdout.getvalue())

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            run_argparse_cli(["-C", str(self.drift_root), "plan", "pkg_cli"])
        self.assertIn("pkg_cli", stdout.getvalue())

        # 2. Introduce host drift
        (self.host_target / "pkg_cli" / "test.txt").write_text("modified on host\n", encoding="utf-8")

        # Text mode (Typer) -> exits with ExitCode.DRIFT_DETECTED (3)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                main(["-C", str(self.drift_root), "plan", "pkg_cli"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
        self.assertIn("DRIFT_DETECTED", stdout.getvalue())

        # Text mode (Argparse) -> exits with ExitCode.DRIFT_DETECTED (3)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                run_argparse_cli(["-C", str(self.drift_root), "plan", "pkg_cli"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
        self.assertIn("DRIFT_DETECTED", stdout.getvalue())

        # JSON mode (Typer) -> exits with ExitCode.DRIFT_DETECTED (3) and valid JSON output
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                main(["-C", str(self.drift_root), "plan", "--json", "pkg_cli"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["status"], "DRIFT_DETECTED")
        self.assertIn("pkg_cli", data["drift_warnings"])

        # Force mode (Typer) -> exits with 0
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "plan", "--force", "pkg_cli"])
        self.assertIn("pkg_cli", stdout.getvalue())

    def test_deploy_dry_run_flag_alias(self) -> None:
        """Verifies that 'drift deploy --dry-run' functions as an exact planning alias."""
        self._setup_package("pkg_deploy_dry", {"file.conf": "123\n"})
        self._deploy_package_clean("pkg_deploy_dry")

        # Typer deploy --dry-run
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "deploy", "--dry-run", "pkg_deploy_dry"])
        self.assertIn("=== Workspace Deployment Preview: deploy", stdout.getvalue())

        # Argparse deploy -n
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            run_argparse_cli(["-C", str(self.drift_root), "deploy", "-n", "pkg_deploy_dry"])
        self.assertIn("=== Workspace Deployment Preview: deploy", stdout.getvalue())

    def test_plan_dependency_topological_ordering(self) -> None:
        """Verifies that plan orders packages according to DAG dependencies."""
        self._setup_package("pkg_base", {"base.txt": "base"})
        self._setup_package("pkg_mid", {"mid.txt": "mid"}, deps=["pkg_base"])
        self._setup_package("pkg_top", {"top.txt": "top"}, deps=["pkg_mid"])

        workspace_config = load_workspace_config(self.drift_root)
        # Pass in reverse order to verify topological sort
        preview = prepare_deploy_preview(workspace_config, ["pkg_top", "pkg_mid", "pkg_base"])

        self.assertEqual(preview.packages_install_order, ["pkg_base", "pkg_mid", "pkg_top"])


if __name__ == "__main__":
    unittest.main()
