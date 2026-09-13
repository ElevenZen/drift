"""Unit and integration tests for package-level render engine configuration,
multi-stage compilation pipelines, and engine overlay semantics.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import cast, Dict, Any

from drift.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
)
from drift.workspace_config import WorkspaceConfig, load_workspace_config
from drift.package_config import PackageConfig
from drift.render_engine_config import RenderEngineConfig, RenderEngineRegistry
from drift.render_package import render_package, run_primitive_2_render_packages
from drift.reverse_sync import run_primitive_1_reverse_sync
from drift.adopt_repo import resolve_source_file_path, adopt_one_package_drifts
from drift.toml_utils import parse_toml


class TestPackageRenderEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()
        self.config_dir = self.drift_root / CONFIG_DIR_NAME
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.src_dir = self.drift_root / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.system_target_dir = self.drift_root / "system_target"
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_field_level_inheritance(self) -> None:
        """Verifies that package [render.<name>] inherits unspecified fields from workspace config."""
        # 1. Setup workspace with global mustache engine
        global_json = self.config_dir / "global.json"
        global_json.write_text('{"site": "global_site"}', encoding="utf-8")

        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            pkg_override = true
            pkg_default = true

            [render.mustache]
            input_file = "global.json"
            suffix = "mustache"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        # 2. Package 1 overrides only input_file
        pkg1_dir = self.src_dir / "pkg_override"
        pkg1_dir.mkdir(parents=True, exist_ok=True)
        pkg1_json = pkg1_dir / "pkg.json"
        pkg1_json.write_text('{"site": "package_site"}', encoding="utf-8")

        (pkg1_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "pkg_override"
            enable_render = true

            [render.mustache]
            input_file = "pkg.json"
        """, encoding="utf-8")
        (pkg1_dir / "app.mustache.txt").write_text("Hello from app", encoding="utf-8")

        # 3. Package 2 uses workspace default mustache engine without override
        pkg2_dir = self.src_dir / "pkg_default"
        pkg2_dir.mkdir(parents=True, exist_ok=True)
        (pkg2_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "pkg_default"
            enable_render = true
        """, encoding="utf-8")
        (pkg2_dir / "default.mustache.txt").write_text("Hello from default", encoding="utf-8")

        # 4. Load workspace config and run Primitive 2
        workspace_config = load_workspace_config(self.drift_root)
        res = run_primitive_2_render_packages(workspace_config)
        self.assertEqual(res.status, "SUCCESS")

        # 5. Verify pkg_override rendered with package_site (inherited command and suffix)
        rendered_pkg1 = self.drift_root / "render" / "pkg_override" / "app.txt"
        self.assertTrue(rendered_pkg1.is_file())
        content1 = rendered_pkg1.read_text(encoding="utf-8")
        self.assertIn("package_site", content1)
        self.assertIn("Hello from app", content1)

        # 6. Verify pkg_default rendered with global_site
        rendered_pkg2 = self.drift_root / "render" / "pkg_default" / "default.txt"
        self.assertTrue(rendered_pkg2.is_file())
        content2 = rendered_pkg2.read_text(encoding="utf-8")
        self.assertIn("global_site", content2)
        self.assertIn("Hello from default", content2)

    def test_new_package_render_engine_definition(self) -> None:
        """Verifies defining a brand new render engine in drift_package.toml."""
        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            custom_pkg = true
        """, encoding="utf-8")

        pkg_dir = self.src_dir / "custom_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        data_file = pkg_dir / "custom_data.txt"
        data_file.write_text("CUSTOM_EXTRA_HEADER", encoding="utf-8")

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "custom_pkg"
            enable_render = true

            [render.custom]
            input_file = "custom_data.txt"
            suffix = "custom"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        (pkg_dir / "output.custom.txt").write_text("BODY_CONTENT", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        res = run_primitive_2_render_packages(workspace_config, ["custom_pkg"])
        self.assertEqual(res.status, "SUCCESS")

        rendered_file = self.drift_root / "render" / "custom_pkg" / "output.txt"
        self.assertTrue(rendered_file.is_file())
        content = rendered_file.read_text(encoding="utf-8")
        self.assertIn("CUSTOM_EXTRA_HEADER", content)
        self.assertIn("BODY_CONTENT", content)

    def test_package_level_input_template_chaining_in_drift_sandbox(self) -> None:
        """Verifies package-level input template rendering into render/<pkg>/.drift/ sandbox."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst is not available on this system")

        # 1. Setup workspace with global envsubst engine
        global_env = self.config_dir / "global_env.sh"
        global_env.write_text("export GLOBAL_KEY='workspace_val'\n", encoding="utf-8")

        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            pkg_chain = true

            [render.envsubst]
            input_file = "global_env.sh"
            suffix = "envst"
            render_command = "bash -c 'source %i && envsubst < %s'"
        """, encoding="utf-8")

        # 2. Package defines mustache engine whose input is a template (pkg_data.envst.json)
        pkg_dir = self.src_dir / "pkg_chain"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        pkg_template_input = pkg_dir / "pkg_data.envst.json"
        pkg_template_input.write_text('{"title": "$DRIFT_PKG_TITLE", "scope": "$GLOBAL_KEY"}', encoding="utf-8")

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "pkg_chain"
            enable_render = true

            [env.override]
            DRIFT_PKG_TITLE = "Chained Package Title"

            [render.mustache]
            input_file = "pkg_data.envst.json"
            suffix = "mustache"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        (pkg_dir / "page.mustache.html").write_text("<h1>Rendered Page</h1>", encoding="utf-8")

        # 3. Render package
        workspace_config = load_workspace_config(self.drift_root)
        res = run_primitive_2_render_packages(workspace_config, ["pkg_chain"])
        self.assertEqual(res.status, "SUCCESS")

        # 4. Check intermediate rendered input in render/pkg_chain/.drift/pkg_data.json
        pkg_internal_drift_dir = self.drift_root / "render" / "pkg_chain" / DRIFT_INTERNAL_DIR_NAME
        rendered_input_json = pkg_internal_drift_dir / "pkg_data.json"
        self.assertTrue(rendered_input_json.is_file())
        input_content = rendered_input_json.read_text(encoding="utf-8")
        self.assertIn("Chained Package Title", input_content)
        self.assertIn("workspace_val", input_content)

        # 5. Check final rendered package output
        rendered_page = self.drift_root / "render" / "pkg_chain" / "page.html"
        self.assertTrue(rendered_page.is_file())
        page_content = rendered_page.read_text(encoding="utf-8")
        self.assertIn("Chained Package Title", page_content)
        self.assertIn("workspace_val", page_content)
        self.assertIn("<h1>Rendered Page</h1>", page_content)

    def test_package_level_cycle_detection(self) -> None:
        """Verifies that cyclic input file dependencies defined within package engines raise ValueError."""
        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            cycle_pkg = true
        """, encoding="utf-8")

        pkg_dir = self.src_dir / "cycle_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "a.suf_b").write_text("cycle a", encoding="utf-8")
        (pkg_dir / "b.suf_a").write_text("cycle b", encoding="utf-8")

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "cycle_pkg"
            enable_render = true

            [render.engine_a]
            input_file = "a.suf_b"
            suffix = "suf_a"
            render_command = "cat %s # %i"

            [render.engine_b]
            input_file = "b.suf_a"
            suffix = "suf_b"
            render_command = "cat %s # %i"
        """, encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        with self.assertRaises(ValueError) as ctx:
            render_package(workspace_config, pkg_dir)
        self.assertIn("Cyclic dependency detected", str(ctx.exception))

    def test_multi_stage_drift_package_toml_template_compilation(self) -> None:
        """Verifies 2-stage compilation: global engines render drift_package.envst.toml, then package engines compile files."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst is not available on this system")

        # 1. Setup workspace with global env and envsubst engine
        global_env = self.config_dir / "env.sh"
        global_env.write_text("export PKG_SUB_DIR='custom_subdir'\n", encoding="utf-8")

        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            tmpl_pkg = true

            [render.envsubst]
            input_file = "env.sh"
            suffix = "envst"
            render_command = "bash -c 'source %i && envsubst < %s'"
        """, encoding="utf-8")

        # 2. Package has templated config: drift_package.envst.toml
        pkg_dir = self.src_dir / "tmpl_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        (pkg_dir / "data.txt").write_text("DYNAMIC_STAGE_2_DATA", encoding="utf-8")

        (pkg_dir / "drift_package.envst.toml").write_text("""
            [package]
            name = "tmpl_pkg"
            enable_render = true
            target_directory = "~/$PKG_SUB_DIR"

            [render.pkg_engine]
            input_file = "data.txt"
            suffix = "custom"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        (pkg_dir / "file.custom.txt").write_text("CONTENT_HERE", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        res = run_primitive_2_render_packages(workspace_config, ["tmpl_pkg"])
        self.assertEqual(res.status, "SUCCESS")

        # 3. Check rendered drift_package.toml in render/tmpl_pkg/
        rendered_pkg_toml = self.drift_root / "render" / "tmpl_pkg" / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_pkg_toml.is_file())
        parsed = parse_toml(rendered_pkg_toml.read_text(encoding="utf-8"))
        self.assertEqual(parsed.get("package", {}).get("target_directory"), "~/custom_subdir")

        # 4. Check rendered package file using package engine
        rendered_file = self.drift_root / "render" / "tmpl_pkg" / "file.txt"
        self.assertTrue(rendered_file.is_file())
        file_content = rendered_file.read_text(encoding="utf-8")
        self.assertIn("DYNAMIC_STAGE_2_DATA", file_content)
        self.assertIn("CONTENT_HERE", file_content)

    def test_reverse_sync_with_package_level_render_engine(self) -> None:
        """Verifies that reverse-sync correctly recognizes package-level template suffixes."""
        # 1. Setup workspace config
        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            pkg_rev = true
        """, encoding="utf-8")

        # 2. Setup package with custom render engine
        pkg_dir = self.src_dir / "pkg_rev"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "dummy.txt").write_text("HEADER", encoding="utf-8")

        pkg_target = self.system_target_dir / "pkg_rev"
        pkg_target.mkdir(parents=True, exist_ok=True)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "pkg_rev"
            enable_render = true
            target_directory = "{pkg_target.as_posix()}"

            [render.custom]
            input_file = "dummy.txt"
            suffix = "custom"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        # Source template file in package
        (pkg_dir / "config.custom.ini").write_text("[section]\nkey=val\n", encoding="utf-8")

        # Setup install directory as a git repository
        install_base = self.drift_root / "install"
        install_base.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init"], cwd=str(install_base), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(install_base), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(install_base), check=True, capture_output=True)

        install_pkg_dir = install_base / "pkg_rev"
        install_pkg_dir.mkdir(parents=True, exist_ok=True)
        (install_pkg_dir / "config.ini").write_text("[section]\nkey=val\n", encoding="utf-8")
        (install_pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            (pkg_dir / PACKAGE_CONFIG_FILE_NAME).read_text(encoding="utf-8"),
            encoding="utf-8"
        )
        subprocess.run(["git", "add", "."], cwd=str(install_base), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install commit"], cwd=str(install_base), check=True, capture_output=True)

        # Deployed file on host is modified
        host_file = pkg_target / "config.ini"
        host_file.write_text("[section]\nkey=modified_on_host\n", encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        # Run reverse-sync primitive 1
        res = run_primitive_1_reverse_sync(workspace_config, ["pkg_rev"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(
            (install_pkg_dir / "config.ini").read_text(encoding="utf-8"),
            "[section]\nkey=modified_on_host\n"
        )

        # Verify resolve_source_file_path uses overlaid package engines
        pkg_config = PackageConfig.from_source_dir(pkg_dir, workspace_config)
        effective_engines = pkg_config.package_render_engines(workspace_config)
        src_path = resolve_source_file_path(effective_engines, pkg_dir, Path("config.ini"))
        self.assertIsNotNone(src_path)
        self.assertEqual(src_path, pkg_dir / "config.custom.ini")

        # Verify adopt_one_package_drifts works with effective_engines
        pkg_adopt_res = adopt_one_package_drifts(
            workspace_config=workspace_config,
            pkg=pkg_config.name,
            dry_run=True,
        )
        self.assertEqual(pkg_adopt_res.status, "SUCCESS")
    def test_lifecycle_hook_with_package_render_engine_input_chaining(self) -> None:
        """Verifies lifecycle hooks resolve package-level engines with chained input templates."""
        from drift.lifecycle_hooks import trigger_package_hook_with_render, HookExecFlags

        if not shutil.which("envsubst"):
            self.skipTest("envsubst is not available on this system")

        # 1. Setup workspace with global envsubst engine
        global_env = self.config_dir / "env.sh"
        global_env.write_text("export HOOK_VAL='HOOK_CHAINED_SUCCESS'\n", encoding="utf-8")

        workspace_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        workspace_toml.write_text("""
            [workspace]
            render_directory = "render"

            [packages.enable]
            pkg_hook_chain = true

            [render.envsubst]
            input_file = "env.sh"
            suffix = "envst"
            render_command = "bash -c 'source %i && envsubst < %s'"
        """, encoding="utf-8")

        # 2. Setup package with custom mustache engine whose input is a template
        pkg_dir = self.src_dir / "pkg_hook_chain"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # Template input for custom engine with shebang
        (pkg_dir / "hook_data.envst.txt").write_text("""#!/bin/bash
# DATA: $HOOK_VAL
""", encoding="utf-8")

        # Templated hook script
        hook_script = scripts_dir / "setup.custom.sh"
        hook_script.write_text("""
echo "EXECUTED" >> "$DRIFT_HOOK_OUT"
grep "HOOK_CHAINED_SUCCESS" "$0" >> "$DRIFT_HOOK_OUT"
""", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
            [package]
            name = "pkg_hook_chain"
            enable_render = true

            [hooks]
            pre_source = "scripts/setup.sh"

            [render.custom]
            input_file = "hook_data.envst.txt"
            suffix = "custom"
            render_command = "bash -c 'cat %i %s'"
        """, encoding="utf-8")

        workspace_config = load_workspace_config(self.drift_root)
        pkg_config = PackageConfig.from_source_dir(pkg_dir, workspace_config)

        output_log = self.drift_root / "hook_output.log"
        with patch.dict(os.environ, {"DRIFT_HOOK_OUT": str(output_log)}):
            res = trigger_package_hook_with_render(
                workspace_config=workspace_config,
                package_name="pkg_hook_chain",
                hook_name="pre_source",
                flags=HookExecFlags(load_envs=True),
                pkg_config_override=pkg_config,
            )
            self.assertEqual(res.status, "SUCCESS")

        self.assertTrue(output_log.is_file())
        log_content = output_log.read_text(encoding="utf-8")
        self.assertIn("HOOK_CHAINED_SUCCESS", log_content)
        self.assertIn("EXECUTED", log_content)


if __name__ == "__main__":
    unittest.main()

