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
from drift.primitives.plan_repo import preview_deploy, DeployOptions
from tests.test_utils import TestCaseUtilityMixin


class TestPlan(TestCaseUtilityMixin, unittest.TestCase):
    """Test suite verifying full-cycle deployment planning, host drift auditing, and CLI IO."""

    def setUp(self) -> None:
        set_test_mode(True, enable_logging=False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name).resolve()

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

        [render.j2]
        suffix = "j2"
        render_command = "internal"
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

        # Configure deterministic built-in template engine for testing
        (self.config_dir / "drift_workspace.local.toml").write_text("""
        [render.tmpl]
        suffix = "tmpl"
        render_command = "internal"
        """, encoding="utf-8")

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
        target_directory = "{target_dir.as_posix()}"
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
        preview = preview_deploy(workspace_config, ["pkg_clean"])

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

    def test_plan_templated_engine_input_cache_hit_idempotency(self) -> None:
        """Verifies that drift plan achieves 100% cache hits and zero unplanned renders
        on packages with templated configurations and payload files depending on engine inputs.
        """
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        # 1. Add envsubst engine with input_file to drift_workspace.toml
        (self.config_dir / "env.sh").write_text("export APP_ENV=production\n", encoding="utf-8")
        ws_toml = (self.config_dir / "drift_workspace.toml").read_text(encoding="utf-8")
        ws_toml += f"""
        [render.envst]
        suffix = "envst"
        input_file = "{(self.config_dir / 'env.sh').as_posix()}"
        render_command = "bash -c 'source %i && envsubst < %s'"
        """
        (self.config_dir / "drift_workspace.toml").write_text(ws_toml, encoding="utf-8")

        pkg_name = "pkg_tmpl_eng"
        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.host_target / pkg_name
        target_dir.mkdir(parents=True, exist_ok=True)

        # 2. Setup templated drift_package.envst.toml and templated payload
        (pkg_dir / "drift_package.envst.toml").write_text(f"""
        [package]
        name = "{pkg_name}"
        enable_render = true
        enable_install = true
        install_method = "copy"
        target_directory = "{target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_dir / "app.envst.conf").write_text("env=${APP_ENV}\n", encoding="utf-8")

        # 3. Clean full deploy (Cold mutation pass)
        self._deploy_package_clean(pkg_name)

        # 4. Plan on deployed package (Warm idempotent pass)
        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        self.assertFalse(preview.has_changes)
        self.assertFalse(preview.has_drift)
        self.assertEqual(preview.packages_unchanged, [pkg_name])
        self.assertEqual(preview.packages_with_changes, [])

        pkg_preview = preview.package_previews.get(pkg_name)
        self.assertIsNotNone(pkg_preview)
        self.assertIsNotNone(pkg_preview.render_plan)
        self.assertEqual(pkg_preview.render_plan.status, "UP_TO_DATE")

        for action in pkg_preview.render_plan.actions:
            self.assertEqual(
                action.action_type,
                FileActionType.SKIP_IDENTICAL,
                f"Expected SKIP_IDENTICAL in plan render actions, got {action.action_type} for {action.dst_path}",
            )

        # 5. Dual CLI Backend tests (Typer and Argparse)
        stdout_typer = io.StringIO()
        with patch("sys.stdout", stdout_typer), patch("sys.stderr", io.StringIO()):
            main(["-C", str(self.drift_root), "plan", "-v", pkg_name])
        typer_out = stdout_typer.getvalue()
        self.assertIn("0 with changes", typer_out)
        self.assertIn("1 unchanged", typer_out)
        self.assertIn("SKIP_IDENTICAL", typer_out)

        stdout_argparse = io.StringIO()
        with patch("sys.stdout", stdout_argparse), patch("sys.stderr", io.StringIO()):
            run_argparse_cli(["-C", str(self.drift_root), "plan", "-v", pkg_name])
        argparse_out = stdout_argparse.getvalue()
        self.assertIn("0 with changes", argparse_out)
        self.assertIn("1 unchanged", argparse_out)
        self.assertIn("SKIP_IDENTICAL", argparse_out)

    def test_plan_detects_host_drift_without_altering_install_repo(self) -> None:
        """Verifies that Phase 0 host drift inspection detects divergence without modifying install/ repo."""
        self._setup_package("pkg_drift", {"config.ini": "port=8080\n", "unchanged.txt": "same\n"})
        self._deploy_package_clean("pkg_drift")

        # Mutate the deployed file live on the host
        host_file = self.host_target / "pkg_drift" / "config.ini"
        host_file.write_text("port=9090\n", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_drift"])

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
        preview = preview_deploy(workspace_config, ["pkg_force"], options=options)

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
        preview = preview_deploy(workspace_config, ["pkg_top", "pkg_mid", "pkg_base"])

        self.assertEqual(preview.packages_install_order, ["pkg_base", "pkg_mid", "pkg_top"])

    def test_plan_dry_run_leaves_render_repo_clean(self) -> None:
        """Verifies that plan loads package configurations with dry_run=True, never writing to render/."""
        pkg_name = "pkg_unrendered"
        self._setup_package(pkg_name, {"file.txt": "hello\n"})

        render_pkg_dir = self.drift_root / "render" / pkg_name
        self.assertFalse(render_pkg_dir.exists())

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertIn(pkg_name, preview.packages_install_order)
        self.assertIn(pkg_name, preview.package_previews)
        # Verify render/<pkg> was never created
        self.assertFalse(render_pkg_dir.exists())

        # Also verify CLI plan does not write to render/
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "plan", pkg_name])
        self.assertFalse(render_pkg_dir.exists())

        # Verify git status in render/ repository remains pristine clean
        res = subprocess.run(
            ["git", "-C", str(self.drift_root / "render"), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(res.stdout.strip(), "")

    def test_plan_renders_templates_in_sandbox_with_canonical_paths(self) -> None:
        """Verifies template compilation in sandbox with action paths rebased to canonical render/."""
        pkg_name = "pkg_tmpl"
        self._setup_package(
            pkg_name,
            {
                "file.txt.tmpl": "greeting = ${USER}\n",
                "static.txt": "static_content\n",
            },
        )

        render_pkg_dir = self.drift_root / "render" / pkg_name
        self.assertFalse(render_pkg_dir.exists())

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        self.assertIn(pkg_name, preview.package_previews)
        pkg_preview = preview.package_previews[pkg_name]
        self.assertIsNone(pkg_preview.error)
        self.assertIsNotNone(pkg_preview.render_plan)
        render_plan = pkg_preview.render_plan
        self.assertEqual(render_plan.status, "SUCCESS")

        # Find RENDER_ITEM and CREATE_COPY actions
        render_action = next((a for a in render_plan.actions if a.action_type == FileActionType.RENDER_ITEM), None)
        self.assertIsNotNone(render_action)
        self.assertEqual(render_action.dst_path, self.drift_root / "render" / pkg_name / "file.txt")

        static_action = next((a for a in render_plan.actions if a.action_type == FileActionType.CREATE_COPY and a.dst_path.name == "static.txt"), None)
        self.assertIsNotNone(static_action)
        self.assertEqual(static_action.dst_path, self.drift_root / "render" / pkg_name / "static.txt")

        # Canonical render directory must never be created
        self.assertFalse(render_pkg_dir.exists())

        # Canonical render repository must remain 100% clean
        res = subprocess.run(
            ["git", "-C", str(self.drift_root / "render"), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(res.stdout.strip(), "")

    def test_plan_template_rendering_cache_hit_when_prerendered(self) -> None:
        """Verifies that pre-rendered packages with templates hit lockfile cache (SKIP_IDENTICAL) in drift plan."""
        pkg_name = "pkg_tmpl_cache"
        self._setup_package(
            pkg_name,
            {
                "config.conf.tmpl": "pkg_name={{ drift_package_name }}\nrender_dir={{ drift_package_render_dir }}\n",
            },
        )
        self._deploy_package_clean(pkg_name)

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        pkg_preview = preview.package_previews[pkg_name]
        self.assertIsNotNone(pkg_preview.render_plan)
        actions = pkg_preview.render_plan.actions
        non_skip = [a for a in actions if a.action_type not in (FileActionType.SKIP_IDENTICAL, FileActionType.INFO_MESSAGE)]
        self.assertEqual(non_skip, [], f"Expected only SKIP_IDENTICAL actions, got: {non_skip}")

    def test_plan_render_cache_hit_and_no_cache_flag(self) -> None:
        """Verifies UP_TO_DATE cache hits on unchanged packages and forced re-rendering with --no-cache."""
        pkg_name = "pkg_cache"
        self._setup_package(pkg_name, {"app.conf": "port = 8080\n"})
        self._deploy_package_clean(pkg_name)

        workspace_config = load_workspace_config(self.drift_root)

        # 1. Normal plan: cache hit -> UP_TO_DATE
        preview = preview_deploy(workspace_config, [pkg_name])
        self.assertEqual(preview.status, "SUCCESS")
        render_plan = preview.package_previews[pkg_name].render_plan
        self.assertIsNotNone(render_plan)
        self.assertEqual(render_plan.status, "UP_TO_DATE")
        self.assertTrue(all(a.action_type == FileActionType.SKIP_IDENTICAL for a in render_plan.actions))

        # 2. Plan with --no-cache: forces re-render plan -> SUCCESS
        preview_nc = preview_deploy(workspace_config, [pkg_name], options=DeployOptions(no_cache=True))
        self.assertEqual(preview_nc.status, "SUCCESS")
        render_plan_nc = preview_nc.package_previews[pkg_name].render_plan
        self.assertIsNotNone(render_plan_nc)
        self.assertEqual(render_plan_nc.status, "SUCCESS")
        self.assertTrue(any(a.action_type != FileActionType.SKIP_IDENTICAL for a in render_plan_nc.actions))

    def test_plan_render_error_isolation_and_dependency_blocking(self) -> None:
        """Verifies template compilation error captures, prerequisite failure blocking, and independent isolation."""
        # 1. pkg_fail with a broken custom render command that exits with non-zero status
        pkg_fail_dir = self.src_dir / "pkg_fail"
        pkg_fail_dir.mkdir(parents=True, exist_ok=True)
        (pkg_fail_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_fail"
        enable_render = true
        enable_install = true
        target_directory = "{(self.host_target / 'pkg_fail').as_posix()}"

        [render.broken]
        suffix = "fail"
        input_file = "fail.json"
        render_command = "sh -c 'exit 1' %s %i"
        """, encoding="utf-8")
        (pkg_fail_dir / "fail.json").write_text("{}", encoding="utf-8")
        (pkg_fail_dir / "broken.txt.fail").write_text("broken", encoding="utf-8")

        # 2. pkg_dep depends on pkg_fail
        self._setup_package("pkg_dep", {"child.txt": "child\n"}, deps=["pkg_fail"])

        # 3. pkg_indep independent package
        self._setup_package("pkg_indep", {"good.txt": "good\n"})

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_fail", "pkg_dep", "pkg_indep"])

        # Overall preview must report failure
        self.assertEqual(preview.status, "FAILED")

        # pkg_fail must capture render failure
        preview_fail = preview.package_previews["pkg_fail"]
        self.assertIsNotNone(preview_fail.error)

        # pkg_dep must be blocked due to failed prerequisite
        preview_dep = preview.package_previews["pkg_dep"]
        self.assertIsNotNone(preview_dep.error)
        self.assertIn("Prerequisite package 'pkg_fail' failed to render.", str(preview_dep.error))

        # pkg_indep must succeed despite neighboring failures
        preview_indep = preview.package_previews["pkg_indep"]
        self.assertIsNone(preview_indep.error)
        self.assertIsNotNone(preview_indep.render_plan)
        self.assertEqual(preview_indep.render_plan.status, "SUCCESS")

    def test_plan_with_hooks_flag(self) -> None:
        """Verifies that pre_source lifecycle hooks are bypassed by default in plan but execute with --with-hooks."""
        pkg_name = "pkg_hook_plan"
        marker_file = self.test_dir / "hook_marker.txt"
        if marker_file.exists():
            marker_file.unlink()

        pkg_dir = self.src_dir / pkg_name
        pkg_dir.mkdir(parents=True, exist_ok=True)
        target_dir = self.host_target / pkg_name
        target_dir.mkdir(parents=True, exist_ok=True)

        config_text = f"""
        [package]
        name = "{pkg_name}"
        enable_render = true
        enable_install = true
        target_directory = "{target_dir.as_posix()}"

        [hooks]
        pre_source = "drift_hooks/pre_source.sh"
        """
        (pkg_dir / "drift_package.toml").write_text(config_text, encoding="utf-8")
        hooks_dir = pkg_dir / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_script = hooks_dir / "pre_source.sh"
        hook_script.write_text(f"""#!/bin/sh\necho "hook_ran" > "{marker_file.as_posix()}"\n""", encoding="utf-8")
        hook_script.chmod(0o755)

        workspace_config = load_workspace_config(self.drift_root)

        # 1. Default plan (with_hooks=False): hook is bypassed
        preview_no_hook = preview_deploy(workspace_config, [pkg_name])
        self.assertEqual(preview_no_hook.status, "SUCCESS")
        self.assertFalse(marker_file.exists())

        # 2. Plan with with_hooks=True: hook executes
        preview_with_hook = preview_deploy(workspace_config, [pkg_name], options=DeployOptions(with_hooks=True))
        self.assertEqual(preview_with_hook.status, "SUCCESS")
        self.assertTrue(marker_file.exists())
        self.assertEqual(marker_file.read_text(encoding="utf-8").strip(), "hook_ran")

    def test_load_source_package_metadata_uses_silent_mode(self) -> None:
        """Verifies load_source_package_metadata passes silent=True and dry_run=True when loading PackageConfig."""
        from drift.primitives.plan_repo import load_source_package_metadata
        from drift.config.package_config import PackageConfig

        pkg_name = "pkg_silent_cfg"
        self._setup_package(pkg_name, {"readme.txt": "hello\n"})
        workspace_config = load_workspace_config(self.drift_root)

        with patch.object(PackageConfig, "from_source_dir", wraps=PackageConfig.from_source_dir) as mock_load:
            metadata = load_source_package_metadata(workspace_config, [pkg_name])
            self.assertIn(pkg_name, metadata)
            mock_load.assert_called_once_with(
                workspace_config.source_path / pkg_name,
                workspace_config=workspace_config,
                dry_run=True,
                silent=True,
            )

    def test_plan_staging_diff_against_canonical_install(self) -> None:
        """Verifies Phase 2 staging diffing against real install/ with actions rebased to canonical render/."""
        pkg_name = "pkg_stage_diff"
        self._setup_package(pkg_name, {"service.conf": "port=8080\n"})
        self._deploy_package_clean(pkg_name)

        # Modify source template in src/
        (self.src_dir / pkg_name / "service.conf").write_text("port=9090\n", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        pkg_preview = preview.package_previews[pkg_name]
        self.assertIsNotNone(pkg_preview.stage_plan)
        stage_plan = pkg_preview.stage_plan
        self.assertTrue(stage_plan.has_changes)

        # Must have UPDATE_COPY with src pointing to canonical render/ and dst to canonical install/
        update_action = next((a for a in stage_plan.actions if a.action_type == FileActionType.UPDATE_COPY and a.dst_path.name == "service.conf"), None)
        self.assertIsNotNone(update_action)
        self.assertEqual(update_action.src_path, self.drift_root / "render" / pkg_name / "service.conf")
        self.assertEqual(update_action.dst_path, self.drift_root / "install" / pkg_name / "service.conf")

        # Zero mutation invariant on real repositories
        res_r = subprocess.run(["git", "-C", str(self.drift_root / "render"), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertEqual(res_r.stdout.strip(), "")
        res_i = subprocess.run(["git", "-C", str(self.drift_root / "install"), "status", "--porcelain"], capture_output=True, text=True, check=True)
        self.assertEqual(res_i.stdout.strip(), "")

    def test_plan_install_symlink_identity_and_skip_identical(self) -> None:
        """Verifies Phase 3 detects existing host symlinks pointing to canonical install/ as SKIP_IDENTICAL."""
        pkg_name = "pkg_sym_id"
        self._setup_package(pkg_name, {"tool.sh": "#!/bin/sh\nexit 0\n"}, install_method="symlink")
        self._deploy_package_clean(pkg_name)

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        pkg_preview = preview.package_previews[pkg_name]
        self.assertIsNotNone(pkg_preview.install_plan)
        install_plan = pkg_preview.install_plan
        self.assertFalse(install_plan.has_changes)

        skip_action = next((a for a in install_plan.actions if a.action_type == FileActionType.SKIP_IDENTICAL and a.dst_path.name == "tool.sh"), None)
        self.assertIsNotNone(skip_action)
        self.assertEqual(skip_action.src_path, self.drift_root / "install" / pkg_name / "tool.sh")
        self.assertEqual(skip_action.dst_path, self.host_target / pkg_name / "tool.sh")

    def test_plan_install_copy_method_content_diff(self) -> None:
        """Verifies Phase 3 detects content changes on host with copy install method and plans UPDATE_COPY."""
        pkg_name = "pkg_copy_diff"
        self._setup_package(pkg_name, {"data.json": '{"v": 1}\n'}, install_method="copy")
        self._deploy_package_clean(pkg_name)

        # Update in src/
        (self.src_dir / pkg_name / "data.json").write_text('{"v": 2}\n', encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        pkg_preview = preview.package_previews[pkg_name]
        self.assertIsNotNone(pkg_preview.install_plan)
        install_plan = pkg_preview.install_plan
        self.assertTrue(install_plan.has_changes)

        update_action = next((a for a in install_plan.actions if a.action_type == FileActionType.UPDATE_COPY and a.dst_path.name == "data.json"), None)
        self.assertIsNotNone(update_action)
        self.assertEqual(update_action.src_path, self.drift_root / "install" / pkg_name / "data.json")
        self.assertEqual(update_action.dst_path, self.host_target / pkg_name / "data.json")

    def test_plan_pruning_and_orphan_reconciliation(self) -> None:
        """Verifies Phase 2 plans DELETE_ITEM in install/ and Phase 3 plans host pruning for deleted files."""
        pkg_name = "pkg_prune"
        self._setup_package(
            pkg_name,
            {"stay.txt": "keep\n", "remove.txt": "delete_me\n"},
            install_method="copy",
        )
        self._deploy_package_clean(pkg_name)

        # Remove file from src/
        (self.src_dir / pkg_name / "remove.txt").unlink()

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, [pkg_name])

        self.assertEqual(preview.status, "SUCCESS")
        pkg_preview = preview.package_previews[pkg_name]

        # Stage plan must delete from install/
        self.assertIsNotNone(pkg_preview.stage_plan)
        del_stage = next((a for a in pkg_preview.stage_plan.actions if a.action_type == FileActionType.DELETE_ITEM and a.dst_path.name == "remove.txt"), None)
        self.assertIsNotNone(del_stage)
        self.assertEqual(del_stage.dst_path, self.drift_root / "install" / pkg_name / "remove.txt")

        # Install plan must prune from host target
        self.assertIsNotNone(pkg_preview.install_plan)
        del_install = next((a for a in pkg_preview.install_plan.actions if a.action_type in (FileActionType.DELETE_ITEM, FileActionType.BACKUP_PRUNE) and (a.src_path.name == "remove.txt" or a.dst_path.name == "remove.txt")), None)
        self.assertIsNotNone(del_install)
        host_target_file = del_install.src_path if del_install.action_type == FileActionType.BACKUP_PRUNE else del_install.dst_path
        self.assertEqual(host_target_file, self.host_target / pkg_name / "remove.txt")

    def test_plan_global_assertion_failure_trapped(self) -> None:
        """Verifies that cross-package collisions are trapped into preview.global_errors setting status FAILED."""
        shared_target = self.host_target / "shared_dest"
        shared_target.mkdir(parents=True, exist_ok=True)

        # Setup two packages that deploy to identical host target
        self._setup_package("pkg_col1", {"collision.txt": "from 1\n"})
        self._setup_package("pkg_col2", {"collision.txt": "from 2\n"})

        # Override target directory in both packages to collide
        for p in ("pkg_col1", "pkg_col2"):
            cfg = (self.src_dir / p / "drift_package.toml").read_text(encoding="utf-8")
            cfg = cfg.replace(f'target_directory = "{(self.host_target / p).as_posix()}"', f'target_directory = "{shared_target.as_posix()}"')
            (self.src_dir / p / "drift_package.toml").write_text(cfg, encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_col1", "pkg_col2"])

        self.assertEqual(preview.status, "FAILED")
        self.assertTrue(len(preview.global_errors) > 0)
        from drift.core.exceptions import CrossPackageCollisionError
        self.assertTrue(any(isinstance(err, CrossPackageCollisionError) for err in preview.global_errors))

    def test_plan_terminal_formatting_and_show_all(self) -> None:
        """Verifies summary banner formatting, unchanged package collapsing, --show-all expansion, and drift guidance."""
        self._setup_package("pkg_clean", {"clean.txt": "clean\n"})
        self._setup_package("pkg_modified", {"mod.txt": "v1\n"})
        self._deploy_package_clean("pkg_clean")
        self._deploy_package_clean("pkg_modified")

        # 1. Modify pkg_modified in src/ -> 1 package changed, 1 unchanged
        (self.src_dir / "pkg_modified" / "mod.txt").write_text("v2\n", encoding="utf-8")

        # Test default plan output (show_all=False): pkg_clean collapsed, summary banner present
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "plan", "pkg_clean", "pkg_modified"])
        out_str = stdout.getvalue()
        self.assertIn("📊 Summary: 2 package(s) planned | 1 with changes | 1 unchanged | 0 drifted", out_str)
        self.assertIn("✨ 1 package(s) unchanged: pkg_clean", out_str)
        self.assertIn("📦 Package: pkg_modified", out_str)
        # pkg_clean should NOT have detailed section
        self.assertNotIn("📦 Package: pkg_clean", out_str)

        # Test show_all=True via -a flag (Typer backend)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            main(["-C", str(self.drift_root), "plan", "-a", "pkg_clean", "pkg_modified"])
        out_a = stdout.getvalue()
        self.assertIn("📦 Package: pkg_clean", out_a)
        self.assertIn("📦 Package: pkg_modified", out_a)
        self.assertNotIn("✨ 1 package(s) unchanged", out_a)

        # Test show_all=True via --all flag (Argparse backend)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            run_argparse_cli(["-C", str(self.drift_root), "plan", "--all", "pkg_clean", "pkg_modified"])
        out_arg = stdout.getvalue()
        self.assertIn("📦 Package: pkg_clean", out_arg)
        self.assertIn("📦 Package: pkg_modified", out_arg)
        self.assertNotIn("✨ 1 package(s) unchanged", out_arg)

        # 2. Introduce host drift on pkg_modified -> verify actionable guidance banner
        (self.host_target / "pkg_modified" / "mod.txt").write_text("v_drift\n", encoding="utf-8")
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                main(["-C", str(self.drift_root), "plan", "pkg_clean", "pkg_modified"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
        out_drift = stdout.getvalue()
        self.assertIn("⚠️  Drift Warnings:", out_drift)
        self.assertIn("Host drift detected in 'pkg_modified'", out_drift)
        self.assertIn("💡 Host changes detected. Run 'drift adopt' to absorb changes into src/ or pass '--force' to overwrite.", out_drift)
        self.assertIn("📊 Summary: 2 package(s) planned | 1 with changes | 1 unchanged | 1 drifted", out_drift)

    def test_plan_json_serialization_metrics(self) -> None:
        """Verifies that to_dict(), to_json(), and CLI --json export comprehensive computed metrics."""
        self._setup_package("pkg_clean_j", {"data.txt": "same\n"})
        self._setup_package("pkg_mod_j", {"data.txt": "v1\n"})
        self._deploy_package_clean("pkg_clean_j")
        self._deploy_package_clean("pkg_mod_j")

        # Mutate pkg_mod_j in src
        (self.src_dir / "pkg_mod_j" / "data.txt").write_text("v2\n", encoding="utf-8")
        # Mutate on host to create drift
        (self.host_target / "pkg_mod_j" / "data.txt").write_text("v_host\n", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_clean_j", "pkg_mod_j"])

        # Inspect preview.to_dict() metrics
        d = preview.to_dict()
        self.assertTrue(d["has_changes"])
        self.assertTrue(d["has_drift"])
        self.assertIn("pkg_mod_j", d["packages_with_changes"])
        self.assertNotIn("pkg_clean_j", d["packages_with_changes"])
        self.assertIn("pkg_clean_j", d["packages_unchanged"])
        self.assertNotIn("pkg_mod_j", d["packages_unchanged"])
        self.assertIn("pkg_mod_j", d["packages_with_drift"])
        self.assertEqual(d["packages_with_errors"], [])

        # Inspect preview.to_json() string
        json_data = json.loads(preview.to_json())
        self.assertEqual(json_data["has_changes"], True)
        self.assertEqual(json_data["has_drift"], True)
        self.assertEqual(json_data["packages_with_changes"], ["pkg_mod_j"])
        self.assertEqual(json_data["packages_unchanged"], ["pkg_clean_j"])
        self.assertEqual(json_data["packages_with_drift"], ["pkg_mod_j"])
        self.assertIn("pkg_clean_j", json_data["package_previews"])
        self.assertIn("pkg_mod_j", json_data["package_previews"])

        # Inspect CLI --json across both backends
        for cli_call in [main, run_argparse_cli]:
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                with self.assertRaises(SystemExit) as cm:
                    cli_call(["-C", str(self.drift_root), "plan", "--json", "pkg_clean_j", "pkg_mod_j"])
                self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
            cli_data = json.loads(stdout.getvalue())
            self.assertEqual(cli_data["status"], "DRIFT_DETECTED")
            self.assertTrue(cli_data["has_changes"])
            self.assertTrue(cli_data["has_drift"])
            self.assertEqual(cli_data["packages_with_changes"], ["pkg_mod_j"])
            self.assertEqual(cli_data["packages_unchanged"], ["pkg_clean_j"])

        # Inspect deploy --dry-run --json alias
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                main(["-C", str(self.drift_root), "deploy", "--dry-run", "--json", "pkg_clean_j", "pkg_mod_j"])
            self.assertEqual(cm.exception.code, ExitCode.DRIFT_DETECTED)
        deploy_json_data = json.loads(stdout.getvalue())
        self.assertEqual(deploy_json_data["status"], "DRIFT_DETECTED")
        self.assertTrue(deploy_json_data["has_drift"])

    def test_plan_cli_flag_propagation_dual_backend(self) -> None:
        """Verifies handler routing and flag propagation across Typer and Argparse backends."""
        test_flags = [
            (["-a"], "show_all", True),
            (["--all"], "show_all", True),
            (["--show-all"], "show_all", True),
            (["-r"], "reinstall", True),
            (["--reinstall"], "reinstall", True),
            (["-c"], "no_cache", True),
            (["--clean"], "no_cache", True),
            (["--no-cache"], "no_cache", True),
            (["-f"], "force", True),
            (["--force"], "force", True),
            (["--no-deps"], "no_deps", True),
            (["--with-hooks"], "with_hooks", True),
            (["--no-hooks"], "no_hooks", True),
            (["--no-hook"], "no_hooks", True),
        ]

        for flag_list, attr_name, expected_val in test_flags:
            flag = flag_list[0]
            # Typer
            with patch("drift.cli.cli_handlers.execute_plan") as mock_action:
                with patch("sys.stdout", io.StringIO()):
                    main(["-C", str(self.drift_root), "plan", flag, "pkg_a"])
                self.assertTrue(mock_action.called, f"Typer plan with {flag} was not called")
                _, kwargs = mock_action.call_args
                opts = kwargs.get("options")
                self.assertIsNotNone(opts, f"Typer plan with {flag} missing options kwarg")
                self.assertEqual(getattr(opts, attr_name), expected_val, f"Typer flag {flag} did not set {attr_name}")

            # Argparse
            with patch("drift.cli.cli_handlers.execute_plan") as mock_action:
                with patch("sys.stdout", io.StringIO()):
                    run_argparse_cli(["-C", str(self.drift_root), "plan", flag, "pkg_a"])
                self.assertTrue(mock_action.called, f"Argparse plan with {flag} was not called")
                _, kwargs = mock_action.call_args
                opts = kwargs.get("options")
                self.assertIsNotNone(opts, f"Argparse plan with {flag} missing options kwarg")
                self.assertEqual(getattr(opts, attr_name), expected_val, f"Argparse flag {flag} did not set {attr_name}")

        # Mutex check for --no-hooks and --with-hooks together
        for cli_call in [main, run_argparse_cli]:
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli_call(["-C", str(self.drift_root), "plan", "--no-hooks", "--with-hooks", "pkg_a"])
                self.assertNotEqual(cm.exception.code, 0)

        # Deploy --dry-run flag propagation
        for flag in ["--dry-run", "-n"]:
            for cli_call in [main, run_argparse_cli]:
                with patch("drift.cli.cli_handlers.execute_plan") as mock_action:
                    with patch("sys.stdout", io.StringIO()):
                        cli_call(["-C", str(self.drift_root), "deploy", flag, "-f", "-r", "-c", "--no-deps", "--no-hooks", "pkg_a"])
                    self.assertTrue(mock_action.called)
                    _, kwargs = mock_action.call_args
                    self.assertEqual(kwargs.get("command_name"), "deploy")
                    opts = kwargs.get("options")
                    self.assertTrue(opts.dry_run)
                    self.assertTrue(opts.force)
                    self.assertTrue(opts.reinstall)
                    self.assertTrue(opts.no_cache)
                    self.assertTrue(opts.no_deps)
                    self.assertTrue(opts.no_hooks)

    def test_plan_stage_error_isolation_and_downstream_blocking(self) -> None:
        """Verifies stage planning exceptions are trapped in stage_errors, block install, and isolate failures."""
        self._setup_package("pkg_s_fail", {"file.txt": "v1\n"})
        self._setup_package("pkg_s_ok", {"file.txt": "v2\n"})

        workspace_config = load_workspace_config(self.drift_root)

        orig_plan_stage = __import__("drift.primitives.plan_repo", fromlist=["plan_package_stage"]).plan_package_stage

        def mock_plan_stage(pkg, install_base, render_base):
            if pkg == "pkg_s_fail":
                raise RuntimeError("Simulated stage failure on pkg_s_fail")
            return orig_plan_stage(pkg, install_base, render_base)

        with patch("drift.primitives.plan_repo.plan_package_stage", side_effect=mock_plan_stage):
            preview = preview_deploy(workspace_config, ["pkg_s_fail", "pkg_s_ok"])

        self.assertEqual(preview.status, "FAILED")

        # pkg_s_fail has stage error and install phase was blocked
        p_fail = preview.package_previews["pkg_s_fail"]
        self.assertIsNotNone(p_fail.error)
        self.assertIn("Simulated stage failure on pkg_s_fail", str(p_fail.error))
        self.assertIsNone(p_fail.install_plan)

        # pkg_s_ok succeeded completely
        p_ok = preview.package_previews["pkg_s_ok"]
        self.assertIsNone(p_ok.error)
        self.assertIsNotNone(p_ok.stage_plan)
        self.assertIsNotNone(p_ok.install_plan)

    def test_plan_install_error_isolation(self) -> None:
        """Verifies package-level install planning errors are isolated to the offending package."""
        self._setup_package("pkg_bad_target", {"file.txt": "bad\n"})
        self._setup_package("pkg_good_target", {"file.txt": "good\n"})

        # Override target directory of pkg_bad_target to resolve inside drift_root (InstallCollisionError)
        bad_cfg = (self.src_dir / "pkg_bad_target" / "drift_package.toml").read_text(encoding="utf-8")
        bad_cfg = bad_cfg.replace(f'target_directory = "{(self.host_target / "pkg_bad_target").as_posix()}"', f'target_directory = "{(self.drift_root / "invalid").as_posix()}"')
        (self.src_dir / "pkg_bad_target" / "drift_package.toml").write_text(bad_cfg, encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_bad_target", "pkg_good_target"])

        self.assertEqual(preview.status, "FAILED")

        # pkg_bad_target reports error
        p_bad = preview.package_previews["pkg_bad_target"]
        self.assertIsNotNone(p_bad.error)
        self.assertIn("drift workspace root", str(p_bad.error))

        # pkg_good_target is completely healthy and has valid install plan
        p_good = preview.package_previews["pkg_good_target"]
        self.assertIsNone(p_good.error)
        self.assertIsNotNone(p_good.install_plan)

    def test_plan_global_error_pipeline_failure_banner(self) -> None:
        """Verifies prominent failure warnings in format_text and global_errors serialization in to_dict."""
        shared_target = self.host_target / "shared_collision"
        shared_target.mkdir(parents=True, exist_ok=True)

        self._setup_package("pkg_c1", {"col.txt": "1\n"})
        self._setup_package("pkg_c2", {"col.txt": "2\n"})

        for p in ("pkg_c1", "pkg_c2"):
            cfg = (self.src_dir / p / "drift_package.toml").read_text(encoding="utf-8")
            cfg = cfg.replace(f'target_directory = "{(self.host_target / p).as_posix()}"', f'target_directory = "{shared_target.as_posix()}"')
            (self.src_dir / p / "drift_package.toml").write_text(cfg, encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        preview = preview_deploy(workspace_config, ["pkg_c1", "pkg_c2"])

        self.assertEqual(preview.status, "FAILED")
        self.assertTrue(len(preview.global_errors) > 0)

        # 1. Verify format_text prominent failure indicators
        text = preview.format_text()
        self.assertIn("🚨 Global Pre-Flight Errors (Real deploy pipeline will fail):", text)
        self.assertIn("💥 Real deployment pipeline will fail due to detected errors.", text)

        # 2. Verify to_dict serializes global_errors as list of strings
        d = preview.to_dict()
        self.assertIn("global_errors", d)
        self.assertTrue(len(d["global_errors"]) > 0)
        self.assertTrue(isinstance(d["global_errors"][0], str))
        self.assertIn("Cross-package destination conflicts", d["global_errors"][0])

        # 3. Verify CLI execution terminates with ExitCode.GENERAL_ERROR and prints failure banner
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as cm:
                main(["-C", str(self.drift_root), "plan", "pkg_c1", "pkg_c2"])
            self.assertEqual(cm.exception.code, ExitCode.GENERAL_ERROR)
        cli_out = stdout.getvalue()
        self.assertIn("Real deploy pipeline will fail", cli_out)


if __name__ == "__main__":
    unittest.main()


