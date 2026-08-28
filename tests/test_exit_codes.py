"""Unit tests for standardized Drift exit codes and CLI error boundary."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.constants import ExitCode, set_test_mode
from drift.exceptions import (
    DriftError,
    ConfigError,
    DriftDetectedError,
    RenderError,
    CollisionError,
)
from drift.cli.error_boundary import cli_error_boundary
from drift.cli.argparse_backend import run_argparse_cli
from drift.cli.actions import (
    execute_init,
    execute_new_package,
    execute_health,
    execute_status,
)


class TestExitCodes(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_error_boundary_success(self) -> None:
        executed = False
        with cli_error_boundary():
            executed = True
        self.assertTrue(executed)

    def test_cli_error_boundary_config_error(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=False):
                    raise ConfigError("Invalid TOML configuration")
            self.assertEqual(cm.exception.code, ExitCode.CONFIG_ERROR)
        self.assertIn("Invalid TOML configuration", stderr_buf.getvalue())

    def test_cli_error_boundary_drift_detected_error(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=False):
                    raise DriftDetectedError("Live system drift detected")
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)

    def test_cli_error_boundary_render_error(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=False):
                    raise RenderError("Template compilation failed")
            self.assertEqual(cm.exception.code, ExitCode.RENDER_ERROR)

    def test_cli_error_boundary_collision_error(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=False):
                    raise CollisionError("Path collision abort")
            self.assertEqual(cm.exception.code, ExitCode.COLLISION_ERROR)

    def test_cli_error_boundary_general_error(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=False):
                    raise RuntimeError("Unexpected internal crash")
            self.assertEqual(cm.exception.code, ExitCode.GENERAL_ERROR)

    def test_cli_error_boundary_json_mode_suppresses_stderr(self) -> None:
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                with cli_error_boundary(json_mode=True):
                    raise ConfigError("Invalid TOML configuration")
            self.assertEqual(cm.exception.code, ExitCode.CONFIG_ERROR)
        # In JSON mode, human-readable stderr banner is suppressed
        self.assertEqual(stderr_buf.getvalue().strip(), "")

    def test_argparse_config_error_exit_code(self) -> None:
        # Running render on a directory without drift.toml returns CONFIG_ERROR (2)
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with self.assertRaises(SystemExit) as cm:
                run_argparse_cli(["-C", str(self.drift_root), "--no-git-root", "render"])
            self.assertEqual(cm.exception.code, ExitCode.CONFIG_ERROR)
        self.assertIn("Workspace main configuration file not found", stderr_buf.getvalue())

    def test_health_check_failed_exit_code(self) -> None:
        # Initialize a real workspace
        execute_init(self.drift_root, force=True, no_git_root=True)
        execute_new_package(self.drift_root, "failing_pkg", force=True)

        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        custom_target = Path(target_temp.name).resolve()

        # Configure a failing health probe
        pkg_src = self.drift_root / "src" / "failing_pkg"
        probe_file = pkg_src / "probe.sh"
        probe_file.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        probe_file.chmod(0o755)

        pkg_toml = pkg_src / "drift_package.toml"
        pkg_toml.write_text(f"""
[package]
name = "failing_pkg"
enable_render = true
enable_install = true
install_method = "copy"
target_directory = "{custom_target}"

[hooks]
health = "probe.sh"
""", encoding="utf-8")

        from drift.cli.actions import execute_deploy
        execute_deploy(self.drift_root, ["failing_pkg"], force=True)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            with self.assertRaises(SystemExit) as cm:
                execute_health(self.drift_root, package_names=["failing_pkg"])
            self.assertEqual(cm.exception.code, ExitCode.HEALTH_CHECK_FAILED)

    def test_status_drift_detected_exit_code(self) -> None:
        # Initialize workspace and package
        execute_init(self.drift_root, force=True, no_git_root=True)
        execute_new_package(self.drift_root, "status_pkg", force=True)
        
        target_temp = tempfile.TemporaryDirectory()
        self.addCleanup(target_temp.cleanup)
        custom_target = Path(target_temp.name).resolve()

        # Configure package with custom target outside drift_root
        pkg_src = self.drift_root / "src" / "status_pkg"
        (pkg_src / "sample.conf").write_text("initial content", encoding="utf-8")
        pkg_toml = pkg_src / "drift_package.toml"
        pkg_toml.write_text(f"""
[package]
name = "status_pkg"
enable_render = true
enable_install = true
install_method = "copy"
target_directory = "{custom_target}"
""", encoding="utf-8")

        # Deploy with custom target
        from drift.cli.actions import execute_deploy
        execute_deploy(self.drift_root, ["status_pkg"], force=True)

        # Manually alter a host file
        host_file = custom_target / "sample.conf"
        host_file.write_text("modified live on host", encoding="utf-8")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            with self.assertRaises(SystemExit) as cm:
                execute_status(self.drift_root, ["status_pkg"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)


if __name__ == "__main__":
    unittest.main()
