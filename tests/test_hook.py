import os
import sys
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.workspace_config import WorkspaceConfig
from drift.package_config import PACKAGE_CONFIG_FILE_NAME
from drift.package_hook import run_primitive_trigger_hook
from drift.exceptions import ConfigError
from drift.cli import main, run_argparse_cli


class TestPackageHook(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name) / "drift_workspace"
        self.drift_root.mkdir(parents=True, exist_ok=True)
        self.target_dir = Path(self.temp_dir.name) / "target_home"
        self.target_dir.mkdir(parents=True, exist_ok=True)

        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.target_dir,
            packages_enable={"pkg_hook": True},
            packages_enable_default=False
        )

        # Setup source pkg
        self.src_pkg_dir = self.drift_root / "src" / "pkg_hook"
        self.src_pkg_dir.mkdir(parents=True, exist_ok=True)
        self.scripts_dir = self.src_pkg_dir / "scripts"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

        # Config with various hooks
        (self.src_pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_hook"
        install_method = "copy"
        target_directory = "{self.target_dir.as_posix()}"

        [hooks]
        pre_source = "scripts/pre_source.sh"
        post_render = "scripts/post_render.sh"
        pre_install = "scripts/pre_install.sh"
        post_install = "scripts/post_install.sh"
        pre_update = "scripts/pre_update.sh"
        post_update = "scripts/post_update.sh"
        pre_uninstall = "scripts/pre_uninstall.sh"
        post_uninstall = "scripts/post_uninstall.sh"
        health = "scripts/health.sh"
        """, encoding="utf-8")

        # Create all hook scripts
        (self.scripts_dir / "pre_source.sh").write_text("#!/bin/sh\necho 'PRE_SOURCE' > pre_source_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "post_render.sh").write_text("#!/bin/sh\necho 'POST_RENDER' > post_render_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "pre_install.sh").write_text("#!/bin/sh\necho 'PRE_INSTALL' > pre_install_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "post_install.sh").write_text("#!/bin/sh\necho 'POST_INSTALL' > post_install_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "pre_update.sh").write_text("#!/bin/sh\necho 'PRE_UPDATE' > pre_update_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "post_update.sh").write_text("#!/bin/sh\necho 'POST_UPDATE' > post_update_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "pre_uninstall.sh").write_text("#!/bin/sh\necho 'PRE_UNINSTALL' > pre_uninstall_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "post_uninstall.sh").write_text("#!/bin/sh\necho 'POST_UNINSTALL' > post_uninstall_out.txt\n", encoding="utf-8")
        (self.scripts_dir / "health.sh").write_text("#!/bin/sh\necho 'HEALTH' > health_out.txt\n", encoding="utf-8")

        for s in self.scripts_dir.glob("*.sh"):
            s.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_invalid_hook_name_raises_config_error(self) -> None:
        """Verifies that an invalid hook name raises ConfigError."""
        with self.assertRaises(ConfigError) as cm:
            run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "invalid_hook")
        self.assertIn("Invalid lifecycle hook 'invalid_hook'", str(cm.exception))

    def test_trigger_pre_source_hook(self) -> None:
        """Verifies that pre_source hook is executed from src/."""
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "pre_source")
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.package, "pkg_hook")
        self.assertEqual(res.hook_name, "pre_source")
        self.assertTrue((self.src_pkg_dir / "pre_source_out.txt").is_file())

    def test_trigger_post_render_hook_missing_render_dir(self) -> None:
        """Verifies that post_render raises FileNotFoundError if package has not been rendered."""
        with self.assertRaises(FileNotFoundError) as cm:
            run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_render")
        self.assertIn("has not been rendered", str(cm.exception))

    def test_trigger_post_render_hook_success(self) -> None:
        """Verifies that post_render hook is executed from render/ directory."""
        render_pkg_dir = self.drift_root / "render" / "pkg_hook"
        render_pkg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.src_pkg_dir, render_pkg_dir, dirs_exist_ok=True)

        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_render")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((render_pkg_dir / "post_render_out.txt").is_file())

    def test_trigger_install_hooks_missing_install_dir(self) -> None:
        """Verifies that install hooks raise FileNotFoundError if package is not installed."""
        for hook_name in ("pre_install", "post_install", "pre_update", "post_update", "pre_uninstall", "post_uninstall", "health"):
            with self.assertRaises(FileNotFoundError) as cm:
                run_primitive_trigger_hook(self.workspace_config, "pkg_hook", hook_name)
            self.assertIn("is not installed in the state database", str(cm.exception))

    def test_trigger_install_hooks_success(self) -> None:
        """Verifies execution of install-stage hooks and verifies appropriate working directories."""
        install_pkg_dir = self.drift_root / "install" / "pkg_hook"
        install_pkg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.src_pkg_dir, install_pkg_dir, dirs_exist_ok=True)

        # pre_install: CWD is install_pkg_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "pre_install")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((install_pkg_dir / "pre_install_out.txt").is_file())

        # post_install: CWD is target_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_install")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.target_dir / "post_install_out.txt").is_file())

        # pre_update: CWD is install_pkg_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "pre_update")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((install_pkg_dir / "pre_update_out.txt").is_file())

        # post_update: CWD is target_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_update")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.target_dir / "post_update_out.txt").is_file())

        # pre_uninstall: CWD is install_pkg_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "pre_uninstall")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((install_pkg_dir / "pre_uninstall_out.txt").is_file())

        # post_uninstall: CWD is target_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_uninstall")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.target_dir / "post_uninstall_out.txt").is_file())

        # health: CWD is target_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "health")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.target_dir / "health_out.txt").is_file())

    def test_trigger_unconfigured_hook_raises_config_error(self) -> None:
        """Verifies that triggering a hook that is not configured in drift_package.toml raises ConfigError."""
        # Create empty config without hooks
        pkg_b_dir = self.drift_root / "src" / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).write_text('[package]\nname = "pkg_b"\n', encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            run_primitive_trigger_hook(self.workspace_config, "pkg_b", "pre_source")
        self.assertIn("No 'pre_source' hook configured", str(cm.exception))

    def test_cli_hook_command_typer_and_argparse(self) -> None:
        """Verifies CLI hook command in both Typer and Argparse backends."""
        # Setup git workspace config
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "drift.toml").write_text('[workspace]\nsource_directory = "src"\n[packages.enable]\npkg_hook = true\n', encoding="utf-8")

        # 1. Typer CLI
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "--no-git-root", "hook", "pkg_hook", "pre_source"])
        self.assertIn("Successfully executed hook 'pre_source' for package 'pkg_hook'", stdout.getvalue())

        # 2. Typer CLI with --json
        stdout_json = StringIO()
        with patch("sys.stdout", stdout_json):
            main(["-C", str(self.drift_root), "--no-git-root", "hook", "pkg_hook", "pre_source", "--json"])
        self.assertIn('"command": "hook"', stdout_json.getvalue())
        self.assertIn('"status": "SUCCESS"', stdout_json.getvalue())

        # 3. Argparse CLI
        stdout_argparse = StringIO()
        with patch("sys.stdout", stdout_argparse):
            run_argparse_cli(["-C", str(self.drift_root), "--no-git-root", "hook", "pkg_hook", "pre_source"])
        self.assertIn("Successfully executed hook 'pre_source' for package 'pkg_hook'", stdout_argparse.getvalue())

        # 4. Argparse CLI with --json
        stdout_argparse_json = StringIO()
        with patch("sys.stdout", stdout_argparse_json):
            run_argparse_cli(["-C", str(self.drift_root), "--no-git-root", "hook", "pkg_hook", "pre_source", "--json"])
        self.assertIn('"command": "hook"', stdout_argparse_json.getvalue())
        self.assertIn('"status": "SUCCESS"', stdout_argparse_json.getvalue())

    def test_execute_hook_skipped_exits_with_hook_skipped_code(self) -> None:
        """Verifies that execute_hook exits with ExitCode.HOOK_SKIPPED (7) when the hook is SKIPPED."""
        from drift.cli.actions import execute_hook
        from drift.constants import ExitCode
        from unittest.mock import patch
        from io import StringIO
        from drift.result_models import HookResult

        stdout_buf = StringIO()
        with patch("sys.stdout", stdout_buf), patch("drift.cli.actions.load_workspace_config_default", return_value=self.workspace_config):
            # 1. Skipped hook
            with patch("drift.package_hook.run_primitive_trigger_hook") as mock_trigger:
                mock_trigger.return_value = HookResult.skipped(package="pkg_a", hook_name="pre_source")
                with self.assertRaises(SystemExit) as cm:
                    execute_hook(self.drift_root, "pkg_a", "pre_source")
                self.assertEqual(cm.exception.code, ExitCode.HOOK_SKIPPED)

            # 2. Failed hook
            with patch("drift.package_hook.run_primitive_trigger_hook") as mock_trigger:
                mock_trigger.return_value = HookResult(package="pkg_a", hook_name="pre_source", status="FAILED", exit_code=1, error_message="Fail")
                with self.assertRaises(SystemExit) as cm:
                    execute_hook(self.drift_root, "pkg_a", "pre_source")
                self.assertEqual(cm.exception.code, ExitCode.GENERAL_ERROR)

    def test_trigger_pre_source_lifecycle_hook_return_types(self) -> None:
        """Verifies that trigger_pre_source_lifecycle_hook returns HookResult."""
        from drift.lifecycle_hooks import trigger_pre_source_lifecycle_hook, execute_hook_script
        from drift.package_config import load_package_config_from_source_dir

        # 1. Successful execution -> status == "SUCCESS", duration_ms >= 0
        res = trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_hook")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(bool(res))
        self.assertGreaterEqual(res.duration_ms, 0.0)
        self.assertIn("pre_source", res.hook_name)

        # 2. no_hooks=True -> status == "SKIPPED", bool(res) == False
        res_no_hooks = trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_hook", no_hooks=True)
        self.assertEqual(res_no_hooks.status, "SKIPPED")
        self.assertFalse(bool(res_no_hooks))
        self.assertEqual(res_no_hooks.duration_ms, 0.0)

        # 3. Missing package source dir -> raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            trigger_pre_source_lifecycle_hook(self.workspace_config, "nonexistent_pkg")

        # 3b. Package with no drift_package.toml at all -> status == "SKIPPED"
        pkg_no_config_dir = self.drift_root / "src" / "pkg_no_config"
        pkg_no_config_dir.mkdir(parents=True, exist_ok=True)
        res_no_config = trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_no_config")
        self.assertEqual(res_no_config.status, "SKIPPED")
        self.assertFalse(bool(res_no_config))

        # 3c. Package with no pre_source hook configured -> status == "SKIPPED"
        pkg_no_hook_dir = self.drift_root / "src" / "pkg_no_hook"
        pkg_no_hook_dir.mkdir(parents=True, exist_ok=True)
        (pkg_no_hook_dir / "drift_package.toml").write_text("[package]\nname = 'pkg_no_hook'\n", encoding="utf-8")
        res_unconfigured = trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_no_hook")
        self.assertEqual(res_unconfigured.status, "SKIPPED")
        self.assertFalse(bool(res_unconfigured))

        # 3d. Package with invalid/corrupt drift_package.toml -> raises ConfigError
        pkg_corrupt_dir = self.drift_root / "src" / "pkg_corrupt"
        pkg_corrupt_dir.mkdir(parents=True, exist_ok=True)
        (pkg_corrupt_dir / "drift_package.toml").write_text(
            "[package]\nname = 'pkg_corrupt'\n[hooks]\npre_source = 12345\n",
            encoding="utf-8"
        )
        with self.assertRaises(Exception):
            trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_corrupt")

        # 3e. Package with missing declared hook script -> raises FileNotFoundError
        pkg_missing_script_dir = self.drift_root / "src" / "pkg_missing_script"
        pkg_missing_script_dir.mkdir(parents=True, exist_ok=True)
        (pkg_missing_script_dir / "drift_package.toml").write_text(
            "[package]\nname = 'pkg_missing_script'\n[hooks]\npre_source = 'non_existent.sh'\n",
            encoding="utf-8"
        )
        with self.assertRaises(FileNotFoundError):
            trigger_pre_source_lifecycle_hook(self.workspace_config, "pkg_missing_script")

        # 4. Direct execute_hook_script -> returns HookResult with duration_ms
        pkg_config = load_package_config_from_source_dir(self.src_pkg_dir, self.workspace_config)
        hook_script_path = self.scripts_dir / "pre_source.sh"
        exec_res = execute_hook_script(
            hook_path=hook_script_path,
            pkg="pkg_hook",
            hook_name="pre_source",
            metadata=pkg_config,
            cwd=self.src_pkg_dir
        )
        self.assertEqual(exec_res.status, "SUCCESS")
        self.assertEqual(exec_res.exit_code, 0)
        self.assertGreaterEqual(exec_res.duration_ms, 0.0)
        self.assertTrue(bool(exec_res))

        # 5. PackageHooks.trigger_pre_source (with render by default) and trigger_pre_source_without_render
        res_rendered = pkg_config.hooks.trigger_pre_source(source_dir=self.src_pkg_dir, workspace_config=self.workspace_config)
        self.assertEqual(res_rendered.status, "SUCCESS")

        res_rendered_alias = pkg_config.hooks.trigger_pre_source_with_render(source_dir=self.src_pkg_dir, workspace_config=self.workspace_config)
        self.assertEqual(res_rendered_alias.status, "SUCCESS")

        res_direct = pkg_config.hooks.trigger_pre_source_without_render(source_dir=self.src_pkg_dir)
        self.assertEqual(res_direct.status, "SUCCESS")

        # 6. PackageHooks no_hooks=True -> status == "SKIPPED"
        res_pkg_no_hooks = pkg_config.hooks.trigger_pre_source(
            source_dir=self.src_pkg_dir,
            workspace_config=self.workspace_config,
            no_hooks=True
        )
        self.assertEqual(res_pkg_no_hooks.status, "SKIPPED")

        res_pkg_without_render_no_hooks = pkg_config.hooks.trigger_pre_source_without_render(
            source_dir=self.src_pkg_dir,
            no_hooks=True
        )
        self.assertEqual(res_pkg_without_render_no_hooks.status, "SKIPPED")


if __name__ == "__main__":
    unittest.main()


