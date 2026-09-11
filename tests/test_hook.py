import os
import sys
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.package_config import PACKAGE_CONFIG_FILE_NAME
from drift.trigger_hook import run_primitive_trigger_hook
from drift.lifecycle_hooks import HookExecFlags
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
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.target_dir,
            ),
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

        # pre_uninstall: CWD is target_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "pre_uninstall")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.target_dir / "pre_uninstall_out.txt").is_file())

        # post_uninstall: CWD is install_pkg_dir
        res = run_primitive_trigger_hook(self.workspace_config, "pkg_hook", "post_uninstall")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((install_pkg_dir / "post_uninstall_out.txt").is_file())

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
        (config_dir / "drift_workspace.toml").write_text('[workspace]\nsource_directory = "src"\n[packages.enable]\npkg_hook = true\n', encoding="utf-8")

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
            with patch("drift.trigger_hook.run_primitive_trigger_hook") as mock_trigger:
                mock_trigger.return_value = HookResult.skipped(package="pkg_a", hook_name="pre_source")
                with self.assertRaises(SystemExit) as cm:
                    execute_hook(self.drift_root, "pkg_a", "pre_source")
                self.assertEqual(cm.exception.code, ExitCode.HOOK_SKIPPED)

            # 2. Failed hook
            with patch("drift.trigger_hook.run_primitive_trigger_hook") as mock_trigger:
                mock_trigger.return_value = HookResult(package="pkg_a", hook_name="pre_source", status="FAILED", exit_code=1, error_message="Fail")
                with self.assertRaises(SystemExit) as cm:
                    execute_hook(self.drift_root, "pkg_a", "pre_source")
                self.assertEqual(cm.exception.code, ExitCode.GENERAL_ERROR)

    def test_trigger_pre_source_hook_return_types(self) -> None:
        """Verifies that trigger_pre_source_hook returns HookResult."""
        from drift.lifecycle_hooks import (
            trigger_pre_source_hook,
            execute_hook_script,
            HookExecFlags,
        )
        from drift.package_config import load_package_config_from_source_dir

        # 1. Successful execution -> status == "SUCCESS", duration_ms >= 0
        res = trigger_pre_source_hook(self.workspace_config, "pkg_hook")
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(bool(res))
        self.assertGreaterEqual(res.duration_ms, 0.0)
        self.assertIn("pre_source", res.hook_name)

        # 2. no_hooks=True -> status == "SKIPPED", bool(res) == False
        res_no_hooks = trigger_pre_source_hook(
            self.workspace_config, "pkg_hook", flags=HookExecFlags(no_hooks=True)
        )
        self.assertEqual(res_no_hooks.status, "SKIPPED")
        self.assertFalse(bool(res_no_hooks))
        self.assertEqual(res_no_hooks.duration_ms, 0.0)

        # 3. Missing package source dir -> raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            trigger_pre_source_hook(self.workspace_config, "nonexistent_pkg")

        # 3b. Package with no drift_package.toml at all -> status == "SKIPPED"
        pkg_no_config_dir = self.drift_root / "src" / "pkg_no_config"
        pkg_no_config_dir.mkdir(parents=True, exist_ok=True)
        res_no_config = trigger_pre_source_hook(self.workspace_config, "pkg_no_config")
        self.assertEqual(res_no_config.status, "SKIPPED")
        self.assertFalse(bool(res_no_config))

        # 3c. Package with no pre_source hook configured -> status == "SKIPPED"
        pkg_no_hook_dir = self.drift_root / "src" / "pkg_no_hook"
        pkg_no_hook_dir.mkdir(parents=True, exist_ok=True)
        (pkg_no_hook_dir / "drift_package.toml").write_text("[package]\nname = 'pkg_no_hook'\n", encoding="utf-8")
        res_unconfigured = trigger_pre_source_hook(self.workspace_config, "pkg_no_hook")
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
            trigger_pre_source_hook(self.workspace_config, "pkg_corrupt")

        # 3e. Package with missing declared hook script -> raises FileNotFoundError
        pkg_missing_script_dir = self.drift_root / "src" / "pkg_missing_script"
        pkg_missing_script_dir.mkdir(parents=True, exist_ok=True)
        (pkg_missing_script_dir / "drift_package.toml").write_text(
            "[package]\nname = 'pkg_missing_script'\n[hooks]\npre_source = 'non_existent.sh'\n",
            encoding="utf-8"
        )
        with self.assertRaises(FileNotFoundError):
            trigger_pre_source_hook(self.workspace_config, "pkg_missing_script")

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
        res_rendered = pkg_config.hooks.trigger_pre_source(workspace_config=self.workspace_config)
        self.assertEqual(res_rendered.status, "SUCCESS")

        res_rendered_alias = pkg_config.hooks.trigger_pre_source_with_render(workspace_config=self.workspace_config)
        self.assertEqual(res_rendered_alias.status, "SUCCESS")

        res_direct = pkg_config.hooks.trigger_pre_source_without_render(source_dir=self.src_pkg_dir)
        self.assertEqual(res_direct.status, "SUCCESS")

        # 6. PackageHooks no_hooks=True -> status == "SKIPPED"
        from drift.lifecycle_hooks import HookExecFlags
        res_pkg_no_hooks = pkg_config.hooks.trigger_pre_source(
            workspace_config=self.workspace_config,
            flags=HookExecFlags(no_hooks=True)
        )
        self.assertEqual(res_pkg_no_hooks.status, "SKIPPED")

        res_pkg_without_render_no_hooks = pkg_config.hooks.trigger_pre_source_without_render(
            source_dir=self.src_pkg_dir,
            flags=HookExecFlags(no_hooks=True)
        )
        self.assertEqual(res_pkg_without_render_no_hooks.status, "SKIPPED")

    def test_render_package_ensures_hooks_executable(self) -> None:
        from drift.render_package import render_package
        if sys.platform == "win32":
            return

        post_render_file = self.scripts_dir / "post_render.sh"
        post_render_file.write_text("#!/bin/bash\necho post_render\n", encoding="utf-8")
        post_render_file.chmod(0o644)  # Explicitly non-executable

        render_package(
            self.workspace_config, self.src_pkg_dir, flags=HookExecFlags(streaming=False)
        )

        # Verify src copy became 0755
        self.assertTrue(bool(post_render_file.stat().st_mode & 0o111))

        # Verify render copy is 0755
        render_hook_file = self.workspace_config.render_path / "pkg_hook" / "scripts" / "post_render.sh"
        self.assertTrue(render_hook_file.exists())
        self.assertTrue(bool(render_hook_file.stat().st_mode & 0o111))

    def test_build_hook_execution_command_fallback_and_no_disk_mutation(self) -> None:
        from drift.lifecycle_hooks import build_hook_execution_command, execute_hook_script
        from drift.package_config import load_package_config_from_source_dir
        if sys.platform == "win32":
            return

        test_script = self.drift_root / "test_non_exec.sh"
        test_script.write_text("#!/bin/bash\necho non_exec\n", encoding="utf-8")
        test_script.chmod(0o644)

        # 1. build_hook_execution_command returns interpreter fallback
        cmd = build_hook_execution_command(test_script)
        self.assertEqual(cmd, ["/bin/bash", str(test_script)])

        # 2. execute_hook_script executes without mutating test_script mode on disk
        pkg_config = load_package_config_from_source_dir(self.src_pkg_dir, self.workspace_config)
        res = execute_hook_script(
            hook_path=test_script,
            pkg="pkg_hook",
            hook_name="test",
            metadata=pkg_config,
            cwd=self.drift_root,
            flags=HookExecFlags(streaming=False),
        )
        self.assertEqual(res.status, "SUCCESS")
        # Ensure disk mode remained 0644 (not mutated during execution)
        self.assertFalse(bool(test_script.stat().st_mode & 0o111))


    def test_render_package_ensures_templated_hooks_and_executable_templates_are_executable(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import RenderEngineConfig
        if sys.platform == "win32":
            return

        input_file = self.drift_root / "config" / "env.sh"
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text("export FOO=bar\n", encoding="utf-8")

        from drift.render_engine_config import RenderEngineConfig, RenderEngineRegistry

        self.workspace_config.render_engine_configs = RenderEngineRegistry({
            "envsubst": RenderEngineConfig(
                name="envsubst",
                suffix="envst",
                input_file=Path("env.sh"),
                render_command="bash -c 'source %i && envsubst < %s'"
            )
        })

        # 1. Templated hook file (post_install configured as scripts/post_install.sh, source is scripts/post_install.envst.sh)
        tmpl_hook = self.scripts_dir / "post_install.envst.sh"
        tmpl_hook.write_text("#!/bin/bash\necho ${DRIFT_SAMPLE_ENV_EDITOR}\n", encoding="utf-8")
        tmpl_hook.chmod(0o644)

        # 2. General executable template (not in hooks, but had chmod +x in src)
        tmpl_tool = self.src_pkg_dir / "tool.envst.sh"
        tmpl_tool.write_text("#!/bin/bash\necho tool\n", encoding="utf-8")
        tmpl_tool.chmod(0o755)

        render_package(self.workspace_config, self.src_pkg_dir)

        # Output rendered hook file in render/ got chmod 0755
        rendered_hook = self.workspace_config.render_path / "pkg_hook" / "scripts" / "post_install.sh"
        self.assertTrue(rendered_hook.exists())
        self.assertTrue(bool(rendered_hook.stat().st_mode & 0o111))

        # Source template file also got chmod 0755
        self.assertTrue(bool(tmpl_hook.stat().st_mode & 0o111))

        # Output rendered tool file in render/ preserved chmod 0755 from source template
        rendered_tool = self.workspace_config.render_path / "pkg_hook" / "tool.sh"
        self.assertTrue(rendered_tool.exists())
        self.assertTrue(bool(rendered_tool.stat().st_mode & 0o111))

    def test_trigger_pre_source_hook_with_rendering(self) -> None:
        """Verifies that pre_source hook specified as a template file is rendered to render/ before execution."""
        from drift.lifecycle_hooks import trigger_pre_source_hook
        from drift.render_engine_config import RenderEngineConfig, RenderEngineRegistry

        self.workspace_config.render_engine_configs = RenderEngineRegistry({
            "envst": RenderEngineConfig(
                name="envst",
                suffix="envst",
                render_command="internal"
            )
        })

        # Create package with templated pre_source hook
        pkg_b_dir = self.drift_root / "src" / "pkg_templated_pre_source"
        pkg_b_dir.mkdir(parents=True)
        scripts_b = pkg_b_dir / "scripts"
        scripts_b.mkdir(parents=True)

        pre_source_tmpl = scripts_b / "gen.sh.envst"
        pre_source_tmpl.write_text("""#!/bin/sh
echo "VALUE=$DYNAMIC_VAL"
""", encoding="utf-8")
        pre_source_tmpl.chmod(0o755)

        (pkg_b_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [env.override]
        DYNAMIC_VAL = "rendered_at_runtime"

        [hooks]
        pre_source = "scripts/gen.sh"
        """, encoding="utf-8")

        # Trigger pre_source hook
        res = trigger_pre_source_hook(
            workspace_config=self.workspace_config,
            package_name="pkg_templated_pre_source",
            flags=HookExecFlags(streaming=False),
        )
        self.assertEqual(res.status, "SUCCESS")

        # Verify gen.sh was rendered into render/pkg_templated_pre_source/scripts/gen.sh
        rendered_script = self.workspace_config.render_path / "pkg_templated_pre_source" / "scripts" / "gen.sh"
        self.assertTrue(rendered_script.exists())
        self.assertIn('VALUE=rendered_at_runtime', rendered_script.read_text(encoding="utf-8"))

    def test_trigger_hook_with_from_stage(self) -> None:
        """Verifies choosing from 'source' vs 'install' stage explicitly."""
        from drift.constants import PackageStage

        # 1. from_stage=SOURCE triggers directly from src even without install/ directory for pre_source
        res_source = run_primitive_trigger_hook(
            self.workspace_config,
            "pkg_hook",
            "pre_source",
            from_stage=PackageStage.SOURCE
        )
        self.assertEqual(res_source.status, "SUCCESS")
        self.assertTrue((self.src_pkg_dir / "pre_source_out.txt").is_file())

        # 2. from_stage=INSTALL raises FileNotFoundError when install directory does not exist
        with self.assertRaises(FileNotFoundError):
            run_primitive_trigger_hook(
                self.workspace_config,
                "pkg_hook",
                "pre_install",
                from_stage=PackageStage.INSTALL
            )

        # 3. Create install dir and run from_stage=INSTALL
        install_pkg_dir = self.drift_root / "install" / "pkg_hook"
        install_pkg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.src_pkg_dir, install_pkg_dir, dirs_exist_ok=True)

        res_install = run_primitive_trigger_hook(
            self.workspace_config,
            "pkg_hook",
            "pre_install",
            from_stage=PackageStage.INSTALL
        )
        self.assertEqual(res_install.status, "SUCCESS")
        self.assertTrue((install_pkg_dir / "pre_install_out.txt").is_file())

    def test_cli_hook_with_from_stage_flag(self) -> None:
        """Verifies drift hook CLI with --from source and --from install."""
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "drift_workspace.toml").write_text('[workspace]\nsource_directory = "src"\n[packages.enable]\npkg_hook = true\n', encoding="utf-8")

        # 1. CLI with --from source for pre_source hook
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            run_argparse_cli(["-C", str(self.drift_root), "--no-git-root", "hook", "pkg_hook", "pre_source", "--from", "source"])
        self.assertIn("Successfully executed hook 'pre_source' for package 'pkg_hook'", stdout.getvalue())

    def test_hook_execution_streaming(self) -> None:
        """Verifies hook streaming outputs to stdout in real time."""
        from drift.lifecycle_hooks import HookExecFlags
        (self.scripts_dir / "pre_source.sh").write_text("#!/bin/sh\necho 'LIVE_HOOK_STREAM'\n", encoding="utf-8")
        stdout = StringIO()
        with patch("sys.stdout", stdout):
            res = run_primitive_trigger_hook(
                self.workspace_config,
                "pkg_hook",
                "pre_source",
                flags=HookExecFlags(streaming=True)
            )
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("LIVE_HOOK_STREAM", stdout.getvalue())

    def test_package_hooks_streaming_forwarding(self) -> None:
        """Verifies that PackageHooks trigger methods accept and forward the streaming parameter and flags."""
        from drift.package_config import PackageConfig, PackageHooks
        from drift.lifecycle_hooks import HookExecFlags
        hooks = PackageHooks(
            probe=Path("scripts/probe.sh"),
            pre_source=Path("scripts/pre_source.sh"),
            post_render=Path("scripts/post_render.sh"),
            pre_install=Path("scripts/pre_install.sh"),
            post_install=Path("scripts/post_install.sh"),
            pre_update=Path("scripts/pre_update.sh"),
            post_update=Path("scripts/post_update.sh"),
            pre_uninstall=Path("scripts/pre_uninstall.sh"),
            post_uninstall=Path("scripts/post_uninstall.sh"),
            health=Path("scripts/health.sh")
        )
        pkg_config = PackageConfig(name="pkg_hook", hooks=hooks)

        with patch("drift.lifecycle_hooks.trigger_package_hook") as mock_trigger:
            mock_trigger.return_value = MagicMock()
            hooks.trigger("pre_install", hook_base_dir=self.drift_root, cwd=self.drift_root, flags=HookExecFlags(streaming=False))
            mock_trigger.assert_called_with(
                pkg="pkg_hook",
                hook_name="pre_install",
                metadata=pkg_config,
                hook_base_dir=self.drift_root,
                cwd=self.drift_root,
                flags=HookExecFlags(no_hooks=False, streaming=False, inject_non_interactive_envs=True)
            )

        with patch("drift.lifecycle_hooks.trigger_probe_hook") as mock_probe:
            mock_probe.return_value = MagicMock()
            hooks.trigger_probe(workspace_config=self.workspace_config, flags=HookExecFlags(streaming=False))
            mock_probe.assert_called_with(
                workspace_config=self.workspace_config,
                package_name="pkg_hook",
                pkg_config=pkg_config,
                flags=HookExecFlags(streaming=False)
            )

    def test_hook_non_interactive_envs_injected(self) -> None:
        """Verifies hook execution injects anti-pager and non-interactive envs, then cleans up."""
        (self.scripts_dir / "pre_source.sh").write_text(
            "#!/bin/sh\n"
            "echo \"PAGER=$PAGER\"\n"
            "echo \"GIT_PAGER=$GIT_PAGER\"\n"
            "echo \"CI=$CI\"\n"
            "echo \"DRIFT_HOOK=$DRIFT_HOOK\"\n"
            "echo \"DRIFT_NON_INTERACTIVE=$DRIFT_NON_INTERACTIVE\"\n",
            encoding="utf-8"
        )
        res = run_primitive_trigger_hook(
            self.workspace_config,
            "pkg_hook",
            "pre_source",
            flags=HookExecFlags(streaming=False),
        )
        self.assertEqual(res.status, "SUCCESS")
        stdout = res.stdout or ""
        self.assertIn("PAGER=cat", stdout)
        self.assertIn("GIT_PAGER=cat", stdout)
        self.assertIn("CI=true", stdout)
        self.assertIn("DRIFT_HOOK=1", stdout)
        self.assertIn("DRIFT_NON_INTERACTIVE=1", stdout)

        # Ensure parent environment is not contaminated
        self.assertNotIn("DRIFT_HOOK", os.environ)
        self.assertNotIn("DRIFT_NON_INTERACTIVE", os.environ)

    def test_hook_exec_flags_dataclass_and_resolve(self) -> None:
        """Verifies HookExecFlags dataclass creation, defaults, and resolve classmethod."""
        from drift.lifecycle_hooks import HookExecFlags

        # 1. Defaults
        flags = HookExecFlags()
        self.assertFalse(flags.no_hooks)
        self.assertTrue(flags.streaming)
        self.assertTrue(flags.inject_non_interactive_envs)
        self.assertTrue(flags.raise_on_error)
        self.assertTrue(flags.load_envs)

        # 2. Resolve without flags
        resolved_default = HookExecFlags.resolve()
        self.assertEqual(
            resolved_default,
            HookExecFlags(
                no_hooks=False,
                streaming=True,
                inject_non_interactive_envs=True,
                raise_on_error=True,
                load_envs=True
            )
        )

        # 3. Resolve with flags instance
        base_flags = HookExecFlags(
            no_hooks=True,
            streaming=False,
            inject_non_interactive_envs=False,
            raise_on_error=False,
            load_envs=False
        )
        resolved_from_instance = HookExecFlags.resolve(flags=base_flags)
        self.assertEqual(resolved_from_instance, base_flags)

    def test_hook_load_envs_flag(self) -> None:
        """Verifies load_envs flag in HookExecFlags controls loading package_envs context."""
        from drift.lifecycle_hooks import (
            HookExecFlags,
            trigger_pre_source_hook,
        )

        # Package with custom env override
        pkg_env_dir = self.drift_root / "src" / "pkg_env_test"
        pkg_env_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_env_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (pkg_env_dir / "drift_package.toml").write_text("""
        [package]
        name = "pkg_env_test"

        [env.override]
        CUSTOM_PKG_VAR = "loaded_by_drift"

        [hooks]
        pre_source = "scripts/check_env.sh"
        """, encoding="utf-8")

        (scripts_dir / "check_env.sh").write_text("""#!/bin/sh
echo "CUSTOM_PKG_VAR=$CUSTOM_PKG_VAR"
""", encoding="utf-8")
        (scripts_dir / "check_env.sh").chmod(0o755)

        # 1. load_envs=True (default) -> CUSTOM_PKG_VAR is loaded
        res_loaded = trigger_pre_source_hook(
            workspace_config=self.workspace_config,
            package_name="pkg_env_test",
            flags=HookExecFlags(load_envs=True, streaming=False)
        )
        self.assertEqual(res_loaded.status, "SUCCESS")
        self.assertIn("CUSTOM_PKG_VAR=loaded_by_drift", res_loaded.stdout or "")

        # 2. load_envs=False -> CUSTOM_PKG_VAR is not loaded
        res_unloaded = trigger_pre_source_hook(
            workspace_config=self.workspace_config,
            package_name="pkg_env_test",
            flags=HookExecFlags(load_envs=False, streaming=False)
        )
        self.assertEqual(res_unloaded.status, "SUCCESS")
        self.assertNotIn("CUSTOM_PKG_VAR=loaded_by_drift", res_unloaded.stdout or "")

    def test_hook_non_interactive_envs_flag_disabled(self) -> None:
        """Verifies that setting inject_non_interactive_envs=False disables injecting DEFAULT_HOOK_NON_INTERACTIVE_ENVS."""
        from drift.lifecycle_hooks import HookExecFlags, execute_hook_script
        from drift.package_config import load_package_config_from_source_dir

        (self.scripts_dir / "pre_source.sh").write_text(
            "#!/bin/sh\n"
            "echo \"PAGER=$PAGER\"\n"
            "echo \"DRIFT_HOOK=$DRIFT_HOOK\"\n",
            encoding="utf-8"
        )
        pkg_config = load_package_config_from_source_dir(self.src_pkg_dir, self.workspace_config)

        with patch.dict(os.environ, {"PAGER": "custom_more_pager"}, clear=False):
            # 1. inject_non_interactive_envs=True (default) -> PAGER overwritten to cat
            res_default = execute_hook_script(
                hook_path=self.scripts_dir / "pre_source.sh",
                pkg="pkg_hook",
                hook_name="pre_source",
                metadata=pkg_config,
                cwd=self.src_pkg_dir,
                flags=HookExecFlags(inject_non_interactive_envs=True, streaming=False)
            )
            self.assertIn("PAGER=cat", res_default.stdout or "")
            self.assertIn("DRIFT_HOOK=1", res_default.stdout or "")

            # 2. inject_non_interactive_envs=False -> PAGER preserved as custom_more_pager, DRIFT_HOOK not injected
            res_disabled = execute_hook_script(
                hook_path=self.scripts_dir / "pre_source.sh",
                pkg="pkg_hook",
                hook_name="pre_source",
                metadata=pkg_config,
                cwd=self.src_pkg_dir,
                flags=HookExecFlags(inject_non_interactive_envs=False, streaming=False)
            )
            self.assertIn("PAGER=custom_more_pager", res_disabled.stdout or "")
            self.assertNotIn("DRIFT_HOOK=1", res_disabled.stdout or "")

    def test_package_hooks_methods_signatures_and_cwd(self) -> None:
        """Verifies trigger_pre_install, trigger_pre_update, trigger_post_uninstall use install_dir as CWD."""
        from drift.package_config import PackageConfig, PackageHooks
        from drift.lifecycle_hooks import HookExecFlags

        hooks = PackageHooks(
            pre_install=Path("scripts/pre_install.sh"),
            pre_update=Path("scripts/pre_update.sh"),
            post_uninstall=Path("scripts/post_uninstall.sh"),
        )
        pkg_config = PackageConfig(name="pkg_hook", hooks=hooks)
        install_dir = self.drift_root / "install" / "pkg_hook"
        install_dir.mkdir(parents=True, exist_ok=True)

        with patch("drift.lifecycle_hooks.trigger_package_hook") as mock_trigger:
            mock_trigger.return_value = MagicMock()

            # 1. trigger_pre_install without redundant cwd
            hooks.trigger_pre_install(install_dir=install_dir)
            mock_trigger.assert_called_with(
                pkg="pkg_hook",
                hook_name="pre_install",
                metadata=pkg_config,
                hook_base_dir=install_dir,
                cwd=install_dir,
                flags=HookExecFlags(no_hooks=False, streaming=True, inject_non_interactive_envs=True)
            )

            # 2. trigger_pre_update without redundant cwd
            hooks.trigger_pre_update(install_dir=install_dir)
            mock_trigger.assert_called_with(
                pkg="pkg_hook",
                hook_name="pre_update",
                metadata=pkg_config,
                hook_base_dir=install_dir,
                cwd=install_dir,
                flags=HookExecFlags(no_hooks=False, streaming=True, inject_non_interactive_envs=True)
            )

            # 3. trigger_post_uninstall without redundant cwd
            hooks.trigger_post_uninstall(install_dir=install_dir)
            mock_trigger.assert_called_with(
                pkg="pkg_hook",
                hook_name="post_uninstall",
                metadata=pkg_config,
                hook_base_dir=install_dir,
                cwd=install_dir,
                flags=HookExecFlags(no_hooks=False, streaming=True, inject_non_interactive_envs=True)
            )

    def test_hook_raise_on_error_flag(self) -> None:
        """Verifies raise_on_error in HookExecFlags controls exception raising vs returning FAILED HookResult."""
        from drift.lifecycle_hooks import (
            HookExecFlags,
            execute_hook_script,
            trigger_probe_hook,
        )
        from drift.package_config import load_package_config_from_source_dir

        failing_script = self.scripts_dir / "fail.sh"
        failing_script.write_text("#!/bin/sh\necho 'error details' >&2\nexit 42\n", encoding="utf-8")
        failing_script.chmod(0o755)

        pkg_config = load_package_config_from_source_dir(self.src_pkg_dir, self.workspace_config)

        # 1. Default (raise_on_error=True) raises RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            execute_hook_script(
                hook_path=failing_script,
                pkg="pkg_hook",
                hook_name="fail_test",
                metadata=pkg_config,
                cwd=self.src_pkg_dir,
                flags=HookExecFlags(streaming=False),
            )
        self.assertIn("failed with exit code 42", str(ctx.exception))

        # 2. raise_on_error=False returns HookResult(status='FAILED')
        res_no_raise = execute_hook_script(
            hook_path=failing_script,
            pkg="pkg_hook",
            hook_name="fail_test",
            metadata=pkg_config,
            cwd=self.src_pkg_dir,
            flags=HookExecFlags(raise_on_error=False, streaming=False),
        )
        self.assertEqual(res_no_raise.status, "FAILED")
        self.assertEqual(res_no_raise.exit_code, 42)
        self.assertIn("error details", res_no_raise.stderr or "")

        # 3. trigger_probe_hook defaults to raise_on_error=False
        (self.src_pkg_dir / "drift_package.toml").write_text(
            '[package]\nname = "pkg_hook"\n[hooks]\nprobe = "scripts/fail.sh"\n',
            encoding="utf-8"
        )
        probe_res = trigger_probe_hook(
            workspace_config=self.workspace_config,
            package_name="pkg_hook",
            flags=HookExecFlags(streaming=False),
        )
        self.assertEqual(probe_res.status, "FAILED")
        self.assertEqual(probe_res.exit_code, 42)

    def test_package_hooks_rollback_on_failure_parsing_and_validation(self) -> None:
        """Verifies parsing and validation for rollback_on_failure in PackageHooks."""
        from drift.package_config import PackageHooks
        from drift.exceptions import ConfigError

        # 1. Default is True
        h_default = PackageHooks.from_dict({})
        self.assertTrue(h_default.should_rollback_on_failure("post_update"))
        self.assertTrue(h_default.should_rollback_on_failure("pre_install"))

        # 2. Boolean False
        h_false = PackageHooks.from_dict({"rollback_on_failure": False})
        self.assertFalse(h_false.should_rollback_on_failure("post_update"))
        self.assertFalse(h_false.should_rollback_on_failure("pre_install"))

        # 3. List of hook names
        h_list = PackageHooks.from_dict({"rollback_on_failure": ["pre_install", "post_install"]})
        self.assertTrue(h_list.should_rollback_on_failure("pre_install"))
        self.assertTrue(h_list.should_rollback_on_failure("post_install"))
        self.assertFalse(h_list.should_rollback_on_failure("post_update"))

        # 4. Invalid hook name in list raises ConfigError
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"rollback_on_failure": ["invalid_hook_name"]})

        # Non-installation lifecycle hooks (probe, post_render, health, uninstall) raise ConfigError
        for non_install_hook in ("probe", "pre_source", "post_render", "pre_uninstall", "post_uninstall", "health"):
            with self.assertRaises(ConfigError):
                PackageHooks.from_dict({"rollback_on_failure": [non_install_hook]})

        # Non-installation hooks always return False for should_rollback_on_failure
        self.assertFalse(h_default.should_rollback_on_failure("probe"))
        self.assertFalse(h_default.should_rollback_on_failure("post_render"))
        self.assertFalse(h_default.should_rollback_on_failure("health"))

        # 5. Invalid type raises ConfigError
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"rollback_on_failure": 123})

    def test_execute_hook_script_raises_hook_execution_error_with_rollback_flag(self) -> None:
        """Verifies execute_hook_script raises HookExecutionError with requires_rollback matching config."""
        from drift.lifecycle_hooks import HookExecFlags, execute_hook_script
        from drift.package_config import PackageConfig, PackageHooks
        from drift.exceptions import HookExecutionError

        failing_script = self.scripts_dir / "fail.sh"
        failing_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        failing_script.chmod(0o755)

        # 1. Package with rollback_on_failure = False
        pkg_no_rb = PackageConfig(
            name="pkg_hook",
            hooks=PackageHooks(rollback_on_failure=False)
        )
        with self.assertRaises(HookExecutionError) as ctx:
            execute_hook_script(
                hook_path=failing_script,
                pkg="pkg_hook",
                hook_name="post_update",
                metadata=pkg_no_rb,
                cwd=self.src_pkg_dir,
                flags=HookExecFlags(streaming=False),
            )
        self.assertFalse(ctx.exception.requires_rollback)
        self.assertEqual(ctx.exception.hook_name, "post_update")
        self.assertEqual(ctx.exception.package, "pkg_hook")

        # 2. Package with rollback_on_failure = True (default)
        pkg_rb = PackageConfig(
            name="pkg_hook",
            hooks=PackageHooks(rollback_on_failure=True)
        )
        with self.assertRaises(HookExecutionError) as ctx:
            execute_hook_script(
                hook_path=failing_script,
                pkg="pkg_hook",
                hook_name="post_update",
                metadata=pkg_rb,
                cwd=self.src_pkg_dir,
                flags=HookExecFlags(streaming=False),
            )
        self.assertTrue(ctx.exception.requires_rollback)


if __name__ == "__main__":
    unittest.main()


