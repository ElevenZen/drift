"""Unit and integration tests for package runtime health check probes and 'drift health'."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.workspace_config import WorkspaceConfig
from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.state_registry import load_state_registry, save_state_registry
from drift.package_health import (
    run_single_package_health_probe,
    run_primitive_health_checks,
)
from drift.result_models import PackageHealthStatus, HealthResult
from drift.cli.argparse_backend import run_argparse_cli


class TestPackageHealth(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()

        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"

        # Override HOME environment variable for the duration of the test
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.system_target_dir)

        self.config_dir = self.drift_root / "config"
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        for d in [self.config_dir, self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)

        (self.config_dir / "drift.toml").write_text(f"""
[workspace]
default_target_directory = "{self.system_target_dir}"
[packages]
[packages.enable]
DEFAULT = true
""", encoding="utf-8")

        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.system_target_dir,
            packages_enable={"pkg_a": True}
        )
        self.workspace_config.default_target_directory = self.system_target_dir

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        self.temp_dir.cleanup()

    def test_health_single_package_pass(self):
        """Verifies that a passing health probe hook returns HEALTHY status with output and correct CWD."""
        pkg = "pkg_healthy"
        pkg_install_dir = self.install_dir / pkg
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "health_check.sh"
        hook_script.write_text(f"""#!/bin/sh
echo "OK: $drift_package_name is running in $(pwd)"
exit 0
""", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        health = "scripts/health_check.sh"
        """, encoding="utf-8")

        res = run_single_package_health_probe(self.workspace_config, pkg)
        self.assertEqual(res.status, PackageHealthStatus.HEALTHY)
        self.assertEqual(res.exit_code, 0)
        self.assertIn(f"OK: {pkg} is running in {self.system_target_dir}", res.stdout)
        self.assertGreaterEqual(res.duration_ms, 0.0)

    def test_health_single_package_fail(self):
        """Verifies that a failing health probe hook returns UNHEALTHY status with stderr."""
        pkg = "pkg_unhealthy"
        pkg_install_dir = self.install_dir / pkg
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "health_check.sh"
        hook_script.write_text("""#!/bin/sh
echo "ERROR: Daemon unreachable on port 8080" >&2
exit 2
""", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        health = "scripts/health_check.sh"
        """, encoding="utf-8")

        res = run_single_package_health_probe(self.workspace_config, pkg)
        self.assertEqual(res.status, PackageHealthStatus.UNHEALTHY)
        self.assertEqual(res.exit_code, 2)
        self.assertIn("ERROR: Daemon unreachable on port 8080", res.stderr)

    def test_health_single_package_timeout(self):
        """Verifies that a probe exceeding its timeout returns TIMEOUT status."""
        pkg = "pkg_timeout"
        pkg_install_dir = self.install_dir / pkg
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "health_check.sh"
        hook_script.write_text("""#!/bin/sh
sleep 10
exit 0
""", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        health = "scripts/health_check.sh"
        """, encoding="utf-8")

        res = run_single_package_health_probe(self.workspace_config, pkg, custom_timeout=1)
        self.assertEqual(res.status, PackageHealthStatus.TIMEOUT)
        self.assertIn("timed out", res.error_message.lower())

    def test_health_missing_hook_file(self):
        """Verifies that a configured hook whose file is missing returns MISSING_HOOK."""
        pkg = "pkg_missing"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        health = "scripts/non_existent.sh"
        """, encoding="utf-8")

        res = run_single_package_health_probe(self.workspace_config, pkg)
        self.assertEqual(res.status, PackageHealthStatus.MISSING_HOOK)

    def test_health_no_hook_configured(self):
        """Verifies that a package without a health hook configured returns NO_HOOK."""
        pkg = "pkg_no_hook"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        """, encoding="utf-8")

        res = run_single_package_health_probe(self.workspace_config, pkg)
        self.assertEqual(res.status, PackageHealthStatus.NO_HOOK)

    def test_health_not_installed(self):
        """Verifies that querying health for an uninstalled package returns NOT_INSTALLED."""
        res = run_single_package_health_probe(self.workspace_config, "non_existent_pkg")
        self.assertEqual(res.status, PackageHealthStatus.NOT_INSTALLED)

    def test_health_sudo_elevation(self):
        """Verifies that a health probe runs with sudo if sudo = true on the package."""
        pkg = "pkg_sudo_health"
        pkg_install_dir = self.install_dir / pkg
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "health_check.sh"
        hook_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        sudo = true

        [hooks]
        health = "scripts/health_check.sh"
        """, encoding="utf-8")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "OK"
            mock_run.return_value.stderr = ""
            res = run_single_package_health_probe(self.workspace_config, pkg)
            called_cmd = mock_run.call_args[0][0]
            self.assertEqual(called_cmd[0], "sudo")
            self.assertEqual(called_cmd[1], str(hook_script))
            self.assertEqual(res.status, PackageHealthStatus.HEALTHY)

    def test_primitive_health_checks_aggregation(self):
        """Verifies multi-package health check aggregation across passing, failing, and skipped packages."""
        # 1. Setup pkg1: passing
        pkg1 = "pkg1"
        (self.install_dir / pkg1 / "scripts").mkdir(parents=True, exist_ok=True)
        (self.install_dir / pkg1 / "scripts" / "h.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (self.install_dir / pkg1 / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg1}"
        install_method = "copy"
        [hooks]
        health = "scripts/h.sh"
        """, encoding="utf-8")

        # 2. Setup pkg2: failing
        pkg2 = "pkg2"
        (self.install_dir / pkg2 / "scripts").mkdir(parents=True, exist_ok=True)
        (self.install_dir / pkg2 / "scripts" / "h.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (self.install_dir / pkg2 / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg2}"
        install_method = "copy"
        [hooks]
        health = "scripts/h.sh"
        """, encoding="utf-8")

        # 3. Setup pkg3: no hook
        pkg3 = "pkg3"
        (self.install_dir / pkg3).mkdir(parents=True, exist_ok=True)
        (self.install_dir / pkg3 / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg3}"
        install_method = "copy"
        """, encoding="utf-8")

        # Setup state.toml
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg1, "installed")
        registry.set_package_state(pkg2, "installed")
        registry.set_package_state(pkg3, "installed")
        save_state_registry(state_file, registry)

        # Run all
        all_res = run_primitive_health_checks(self.workspace_config)
        self.assertEqual(all_res.status, "FAILED")
        self.assertEqual(all_res.healthy_count, 1)
        self.assertEqual(all_res.unhealthy_count, 1)
        self.assertEqual(all_res.skipped_count, 1)
        self.assertEqual(len(all_res.packages), 3)

        # Run only pkg1
        pkg1_res = run_primitive_health_checks(self.workspace_config, package_names=[pkg1])
        self.assertEqual(pkg1_res.status, "SUCCESS")
        self.assertEqual(pkg1_res.healthy_count, 1)
        self.assertEqual(pkg1_res.unhealthy_count, 0)
        self.assertEqual(pkg1_res.skipped_count, 0)

        # Verify format_text formatting
        text_out = all_res.format_text()
        self.assertIn("HEALTHY", text_out)
        self.assertIn("UNHEALTHY", text_out)
        self.assertIn("NO_HOOK", text_out)
        self.assertIn("Health Summary: 1 Healthy, 1 Unhealthy, 1 Skipped", text_out)

    def test_cli_health_json_output(self):
        """Verifies CLI execution with --json output mode."""
        pkg = "cli_pkg"
        pkg_install_dir = self.install_dir / pkg
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "health_check.sh"
        hook_script.write_text("#!/bin/sh\necho 'CLI Health OK'\nexit 0\n", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        health = "scripts/health_check.sh"
        """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        save_state_registry(state_file, registry)

        with patch("sys.stdout.write") as mock_stdout:
            run_argparse_cli(["-C", str(self.drift_root), "--no-git-root", "health", pkg, "--json"])
            # Collect stdout calls
            output = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(output)
            self.assertEqual(data["command"], "health")
            self.assertEqual(data["status"], "SUCCESS")
            self.assertEqual(data["healthy_count"], 1)
            self.assertEqual(data["packages"][0]["package"], pkg)
            self.assertEqual(data["packages"][0]["status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
