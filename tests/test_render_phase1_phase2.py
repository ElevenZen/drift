"""Targeted unit and integration tests for Incremental Render Phase 1 & Phase 2.

Tests the declarative AST compilation, intermediate file rendering, variable stitching,
lifecycle hooks digestion, lockfile persistence, scoped pruning, and permissions.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from drift.core.constants import (
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_RENDER_DIR_NAME,
    DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME,
    DRIFT_HOOKS_DIR_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_LOCAL_FILE_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
)
from drift.config.workspace_config import WorkspaceConfig
from drift.config.package_config import PackageConfig
from drift.config.package_loader import load_package_config_from_source_dir
from drift.config.render_engine_config import RenderEngineConfig, RenderEngineRegistry
from drift.render.render_hooks import render_hooks, ensure_configured_hook_permissions
from drift.render.render_lock import RenderLockfile, RenderBucket
from drift.hooks.lifecycle_hooks import resolve_hook_exec_path
from drift.render.render_package import render_package, RenderOptions
from drift.core.file_action import FileActionType
from drift.primitives.plan_repo import preview_deploy, DeployOptions


class TestRenderPhase1AndPhase2(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        # Workspace configuration layout
        self.config_dir = self.drift_root / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.src_dir = self.drift_root / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir = self.drift_root / "render"
        self.render_dir.mkdir(parents=True, exist_ok=True)

        # Setup envsubst engine with a static env file in config/
        self.env_sh = self.config_dir / "env.sh"
        self.env_sh.write_text("export TEST_VAR='phase1_success'\n", encoding="utf-8")

        self.workspace_config = WorkspaceConfig(drift_root=self.drift_root)
        self.workspace_config.render_engine_configs = RenderEngineRegistry({
            "envsubst": RenderEngineConfig(
                name="envsubst",
                suffix="envst",
                input_file=self.env_sh,
                render_command="bash -c 'source %i && envsubst < %s'",
            )
        })

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # =========================================================================
    # Phase 1: Package Configuration AST Compilation Tests
    # =========================================================================

    def test_package_config_template_rendering_in_drift_internal(self) -> None:
        """Verifies template configs render to .drift/render/ without tempdir, are merged & stitched,
        and written to .drift/drift_package.toml.
        """
        pkg_dir = self.src_dir / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # 1. Template drift_package.envst.toml
        (pkg_dir / "drift_package.envst.toml").write_text("""
        [package]
        name = "my_pkg"
        enable_render = true

        [env.override]
        greeting = "hello_${TEST_VAR}"
        """, encoding="utf-8")

        # 2. Local override drift_package.local.toml
        (pkg_dir / PACKAGE_CONFIG_LOCAL_FILE_NAME).write_text("""
        [package]
        target_directory = "custom_local"
        """, encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)

        # Check in-memory PackageConfig
        self.assertEqual(pkg_config.name, "my_pkg")
        self.assertEqual(pkg_config.package.target_directory, Path("custom_local"))
        self.assertEqual(pkg_config.env_resolve.impact.restricted_env().get("greeting"), "hello_phase1_success")

        # Check intermediate rendered candidate file
        intermediate = (
            self.render_dir
            / "my_pkg"
            / DRIFT_INTERNAL_DIR_NAME
            / DRIFT_INTERNAL_RENDER_DIR_NAME
            / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME
            / PACKAGE_CONFIG_FILE_NAME
        )
        self.assertTrue(intermediate.is_file())
        self.assertIn("hello_phase1_success", intermediate.read_text(encoding="utf-8"))

        # Check final stitched output file
        final_out = self.render_dir / "my_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(final_out.is_file())
        final_content = final_out.read_text(encoding="utf-8")
        self.assertIn("custom_local", final_content)

    def test_package_config_source_files_registration(self) -> None:
        """Verifies PackageConfig.source_files contains candidate source files AND drift_package.py."""
        pkg_dir = self.src_dir / "hook_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "hook_pkg"
        """, encoding="utf-8")

        hook_py = pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_py.write_text("""
def configure_package(context):
    context.config.setdefault("env", {}).setdefault("override", {})["hook_ran"] = "true"
    return context.config
""", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(pkg_config.env_resolve.impact.restricted_env().get("hook_ran"), "true")

        # Source files must include drift_package.toml and drift_package.py
        registered_names = [p.name for p in pkg_config.source_files]
        self.assertIn(PACKAGE_CONFIG_FILE_NAME, registered_names)
        self.assertIn(DEFAULT_PACKAGE_HOOK_FILE_NAME, registered_names)

    def test_package_config_node_pruning(self) -> None:
        """Verifies obsolete drift_package.local.toml is pruned when no longer present."""
        pkg_dir = self.src_dir / "prune_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'prune_pkg'\n", encoding="utf-8")
        local_cfg = pkg_dir / PACKAGE_CONFIG_LOCAL_FILE_NAME
        local_cfg.write_text("[package]\ntarget_directory = 'local_dir'\n", encoding="utf-8")

        # First run creates both in intermediate sandbox
        load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        local_rendered = (
            self.render_dir
            / "prune_pkg"
            / DRIFT_INTERNAL_DIR_NAME
            / DRIFT_INTERNAL_RENDER_DIR_NAME
            / DRIFT_INTERNAL_PACKAGE_INPUT_DIR_NAME
            / PACKAGE_CONFIG_LOCAL_FILE_NAME
        )
        self.assertTrue(local_rendered.is_file())

        # Delete local config in src/
        local_cfg.unlink()

        # Second run: local_rendered must be pruned
        load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        self.assertFalse(local_rendered.exists())

    def test_package_config_lockfile_updated(self) -> None:
        """Verifies lockfile.config_hashes is updated on render."""
        pkg_dir = self.src_dir / "lock_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'lock_pkg'\n", encoding="utf-8")

        load_package_config_from_source_dir(pkg_dir, self.workspace_config)

        pkg_render_dir = self.render_dir / "lock_pkg"
        lockfile = RenderLockfile.load_from_dir(pkg_render_dir)
        self.assertTrue(len(lockfile.config_hashes) > 0)

    def test_package_config_static_fallback(self) -> None:
        """Verifies fallback parsing when workspace_config is None without rendering to render/."""
        pkg_dir = self.src_dir / "fallback_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "fallback_pkg"
        [env.override]
        foo = "bar"
        """, encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, workspace_config=None)
        self.assertEqual(pkg_config.name, "fallback_pkg")
        self.assertEqual(pkg_config.env_resolve.impact.restricted_env().get("foo"), "bar")
        # render/ must not contain fallback_pkg
        self.assertFalse((self.render_dir / "fallback_pkg").exists())

    # =========================================================================
    # Phase 2: Lifecycle Hooks AST Compilation Tests
    # =========================================================================

    def test_render_hooks_static_and_template(self) -> None:
        """Renders static .sh and template .sh.envst into .drift/hooks/."""
        pkg_dir = self.src_dir / "hook_render_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "hook_render_pkg"
        [hooks]
        post_install = "drift_hooks/post_install.sh"
        pre_source = "drift_hooks/pre_source.sh"
        """, encoding="utf-8")

        (hooks_src / "post_install.sh").write_text("#!/bin/bash\necho static\n", encoding="utf-8")
        (hooks_src / "pre_source.envst.sh").write_text("#!/bin/bash\necho ${TEST_VAR}\n", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        res = render_hooks(self.workspace_config, pkg_config)

        self.assertGreaterEqual(res.rendered_count, 2)
        hooks_dest = self.render_dir / "hook_render_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        self.assertTrue((hooks_dest / "post_install.sh").is_file())
        self.assertTrue((hooks_dest / "pre_source.sh").is_file())
        self.assertIn("phase1_success", (hooks_dest / "pre_source.sh").read_text(encoding="utf-8"))

    def test_render_hooks_package_envs_scope(self) -> None:
        """Verifies template rendering inside render_hooks() receives package-defined environment variables."""
        pkg_dir = self.src_dir / "env_scope_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "env_scope_pkg"
        [env.override]
        custom_greeting = "from_pkg_env"
        [hooks]
        probe = "drift_hooks/probe.sh"
        """, encoding="utf-8")

        (hooks_src / "probe.envst.sh").write_text("#!/bin/bash\necho ${custom_greeting}\n", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        render_hooks(self.workspace_config, pkg_config)

        rendered_probe = self.render_dir / "env_scope_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "probe.sh"
        self.assertTrue(rendered_probe.is_file())
        self.assertIn("from_pkg_env", rendered_probe.read_text(encoding="utf-8"))

    def test_render_hooks_cache_hit(self) -> None:
        """Second call does 0 writes and skips all nodes."""
        pkg_dir = self.src_dir / "cache_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'cache_pkg'\n", encoding="utf-8")
        (hooks_src / "run.sh").write_text("#!/bin/bash\necho 1\n", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        res1 = render_hooks(self.workspace_config, pkg_config)
        self.assertEqual(res1.rendered_count, 1)
        self.assertEqual(res1.skipped_count, 0)

        # Second call
        res2 = render_hooks(self.workspace_config, pkg_config)
        self.assertEqual(res2.rendered_count, 0)
        self.assertEqual(res2.skipped_count, 1)

    def test_render_hooks_pruning(self) -> None:
        """Deleting source hook prunes obsolete rendered hook in .drift/hooks/."""
        pkg_dir = self.src_dir / "prune_hook_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'prune_hook_pkg'\n", encoding="utf-8")
        hook_file = hooks_src / "temp_hook.sh"
        hook_file.write_text("#!/bin/bash\necho temp\n", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        render_hooks(self.workspace_config, pkg_config)

        dest_hook = self.render_dir / "prune_hook_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "temp_hook.sh"
        self.assertTrue(dest_hook.is_file())

        # Delete source hook
        hook_file.unlink()

        # Second run must prune dest_hook
        res = render_hooks(self.workspace_config, pkg_config)
        self.assertFalse(dest_hook.exists())
        self.assertIn(dest_hook, res.pruned_paths)

    def test_ensure_configured_hook_permissions(self) -> None:
        """Chmods 0o755 on configured hooks and ignores unconfigured scripts."""
        if sys.platform == "win32":
            self.skipTest("POSIX file permissions not applicable on Windows")

        pkg_dir = self.src_dir / "perm_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "perm_pkg"
        [hooks]
        post_install = "drift_hooks/configured_hook.sh"
        """, encoding="utf-8")

        cfg_hook = hooks_src / "configured_hook.sh"
        cfg_hook.write_text("#!/bin/bash\n", encoding="utf-8")
        cfg_hook.chmod(0o644)

        uncfg_script = hooks_src / "helper_library.sh"
        uncfg_script.write_text("#!/bin/bash\n", encoding="utf-8")
        uncfg_script.chmod(0o644)

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        render_hooks(self.workspace_config, pkg_config)

        # Configured hook gets 0o755 in src/ by default (ensure_hooks_executable_in_src = True)
        self.assertTrue(bool(cfg_hook.stat().st_mode & 0o111))
        dest_cfg = self.render_dir / "perm_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "configured_hook.sh"
        self.assertTrue(bool(dest_cfg.stat().st_mode & 0o111))

        # Unconfigured helper should remain non-executable (0o644)
        dest_uncfg = self.render_dir / "perm_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "helper_library.sh"
        self.assertFalse(bool(dest_uncfg.stat().st_mode & 0o111))

    def test_ensure_configured_hook_permissions_opt_out(self) -> None:
        """Verifies ensure_hooks_executable_in_src=False preserves 0644 on src/ hooks while rendering 0755."""
        if sys.platform == "win32":
            self.skipTest("POSIX file permissions not applicable on Windows")

        self.workspace_config.settings.ensure_hooks_executable_in_src = False

        pkg_dir = self.src_dir / "perm_opt_out"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "perm_opt_out"
        [hooks]
        post_install = "drift_hooks/my_hook.sh"
        """, encoding="utf-8")

        cfg_hook = hooks_src / "my_hook.sh"
        cfg_hook.write_text("#!/bin/bash\n", encoding="utf-8")
        cfg_hook.chmod(0o644)

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        render_hooks(self.workspace_config, pkg_config)

        # Source hook in src/ must remain untouched (0o644)
        self.assertFalse(bool(cfg_hook.stat().st_mode & 0o111))
        # Destination hook in render/ must still receive 0o755
        dest_cfg = self.render_dir / "perm_opt_out" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "my_hook.sh"
        self.assertTrue(bool(dest_cfg.stat().st_mode & 0o111))

    def test_resolve_hook_exec_path_integration(self) -> None:
        """Verifies integration with resolve_hook_exec_path()."""
        pkg_dir = self.src_dir / "resolve_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        hooks_src = pkg_dir / DRIFT_HOOKS_DIR_NAME
        hooks_src.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "resolve_pkg"
        [hooks]
        post_install = "drift_hooks/my_post_install.sh"
        """, encoding="utf-8")

        hook_src_file = hooks_src / "my_post_install.envst.sh"
        hook_src_file.write_text("#!/bin/bash\necho done\n", encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, self.workspace_config)

        resolved_path = resolve_hook_exec_path(
            workspace_config=self.workspace_config,
            pkg_config=pkg_config,
            hook_name="post_install",
            hook_source_path=hook_src_file,
        )
        expected_path = self.render_dir / "resolve_pkg" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "my_post_install.sh"

        self.assertEqual(resolved_path.resolve(), expected_path.resolve())
        self.assertTrue(resolved_path.is_file())

    def test_package_config_skip_identical_when_unchanged(self) -> None:
        """Verifies Phase 1 skips writing drift_package.toml when content matches, preserving mtime."""
        pkg_dir = self.src_dir / "skip_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        config_path = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        config_path.write_text("""
        [package]
        name = "skip_pkg"
        target_directory = "initial_target"
        """, encoding="utf-8")

        # 1. First run: writes configuration
        load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        rendered_cfg = self.render_dir / "skip_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_cfg.is_file())
        initial_mtime = rendered_cfg.stat().st_mtime_ns

        # 2. Second run without changes: content matches, write skipped, mtime invariant
        load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        second_mtime = rendered_cfg.stat().st_mtime_ns
        self.assertEqual(initial_mtime, second_mtime)

        # 3. Third run with source modification: content differs, updates file and mtime
        config_path.write_text("""
        [package]
        name = "skip_pkg"
        target_directory = "updated_target"
        """, encoding="utf-8")
        load_package_config_from_source_dir(pkg_dir, self.workspace_config)
        third_mtime = rendered_cfg.stat().st_mtime_ns
        self.assertNotEqual(initial_mtime, third_mtime)
        self.assertIn("updated_target", rendered_cfg.read_text(encoding="utf-8"))

    def test_package_config_dry_run_cache_isolation_and_no_render_cache_pollution(self) -> None:
        """Verifies Phase 1 dry_run=True uses an isolated RenderCache and does not pollute workspace render_cache."""
        pkg_dir = self.src_dir / "pkg_dry_iso"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        (pkg_dir / "drift_package.envst.toml").write_text("""
        [package]
        name = "pkg_dry_iso"
        enable_render = true

        [env.override]
        val = "${TEST_VAR}"
        """, encoding="utf-8")

        initial_cache_entries = len(self.workspace_config.render_cache._cache)
        pkg_cfg = load_package_config_from_source_dir(pkg_dir, self.workspace_config, dry_run=True)
        self.assertEqual(pkg_cfg.name, "pkg_dry_iso")
        self.assertEqual(pkg_cfg.env_resolve.impact.restricted_env().get("val"), "phase1_success")

        # Workspace render_cache must remain uncontaminated
        self.assertEqual(len(self.workspace_config.render_cache._cache), initial_cache_entries)
        # Render destination in real workspace must not exist
        real_rendered_cfg = self.render_dir / "pkg_dry_iso" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertFalse(real_rendered_cfg.exists())

    def test_render_dry_run_and_plan_with_engine_inputs_cache_hits_two_pass(self) -> None:
        """Exhaustively tests that dry-run rendering and deployment planning achieve 100% cache hits
        on templated packages that depend on engine inputs (two-pass cold vs warm idempotency).
        """
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        with tempfile.TemporaryDirectory(prefix="host_target_") as host_tmp:
            target_dir = Path(host_tmp) / "target_pkg"
            target_dir.mkdir(parents=True, exist_ok=True)

            pkg_dir = self.src_dir / "pkg_templated"
            pkg_dir.mkdir(parents=True, exist_ok=True)

            # 1. Package config templated with envsubst
            (pkg_dir / "drift_package.envst.toml").write_text(f"""
            [package]
            name = "pkg_templated"
            enable_render = true
            target_directory = "{target_dir.as_posix()}"
            """, encoding="utf-8")

            # 2. Package payload file templated with envsubst
            (pkg_dir / "app.envst.conf").write_text("setting=${TEST_VAR}\n", encoding="utf-8")

            # ---------------------------------------------------------------------
            # Pass 1: Cold Mutation Pass (drift render)
            # ---------------------------------------------------------------------
            cold_res = render_package(self.workspace_config, pkg_dir, options=RenderOptions(dry_run=False))
            self.assertTrue(cold_res.is_success)
            self.assertEqual(cold_res.status, "SUCCESS")

            # Check rendered artifacts on disk
            rendered_app = self.render_dir / "pkg_templated" / "app.conf"
            self.assertTrue(rendered_app.is_file())
            self.assertEqual(rendered_app.read_text(encoding="utf-8"), "setting=phase1_success\n")

            # ---------------------------------------------------------------------
            # Pass 2: Warm Dry-Run Pass (drift render -nv)
            # ---------------------------------------------------------------------
            warm_dry_res = render_package(self.workspace_config, pkg_dir, options=RenderOptions(dry_run=True))
            self.assertTrue(warm_dry_res.is_success)
            self.assertEqual(warm_dry_res.status, "UP_TO_DATE")

            # All actions must be SKIP_IDENTICAL, zero RENDER_ITEM or UPDATE_COPY
            for action in warm_dry_res.actions:
                self.assertEqual(
                    action.action_type,
                    FileActionType.SKIP_IDENTICAL,
                    f"Expected SKIP_IDENTICAL but got {action.action_type} for {action.dst_path}",
                )

            # ---------------------------------------------------------------------
            # Pass 3: Ephemeral Plan Sandbox Pass (drift plan -v)
            # ---------------------------------------------------------------------
            preview = preview_deploy(
                self.workspace_config,
                target_pkgs=["pkg_templated"],
                options=DeployOptions(dry_run=True, verbose=True),
            )
            self.assertEqual(preview.status, "SUCCESS")
            pkg_preview = preview.package_previews.get("pkg_templated")
            self.assertIsNotNone(pkg_preview)
            self.assertIsNotNone(pkg_preview.render_plan)
            self.assertEqual(pkg_preview.render_plan.status, "UP_TO_DATE")

            for action in pkg_preview.render_plan.actions:
                self.assertEqual(
                    action.action_type,
                    FileActionType.SKIP_IDENTICAL,
                    f"Expected SKIP_IDENTICAL in plan render phase but got {action.action_type} for {action.dst_path}",
                )


if __name__ == "__main__":
    unittest.main()

