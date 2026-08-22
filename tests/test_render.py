"""Tests for rendering and compiling engines using pathlib."""

import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from typing import cast

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import RenderEngineConfig, WorkspaceConfig
from drift.render_core import render_template, render_template_to_file
from drift.render_input import (
    find_engine_for_file,
    strip_engine_suffix,
    resolve_dependencies,
    check_cyclic_dependencies,
    render_input_templates,
)
from drift.toml_utils import parse_toml


class TestRenderEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_template_success_with_input_file(self) -> None:
        # Create an input file (shell script defining a variable)
        input_path = self.drift_root / "env.sh"
        input_path.write_text("export MY_TEST_VAR='drift_rendered_value'\n", encoding="utf-8")

        # Create a template file
        template_path = self.drift_root / "template.sh"
        template_path.write_text("echo -n $MY_TEST_VAR", encoding="utf-8")

        # Set up engine config
        engine_config = RenderEngineConfig(
            name="bash_env_engine",
            input_file=Path("env.sh"),
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )

        # Call render_template with explicit input file path and drift_root
        output = render_template(
            engine_config=engine_config,
            drift_root=self.drift_root,
            template_file_path=template_path,
            input_file_path=input_path
        )
        self.assertEqual(output, "drift_rendered_value")

    def test_render_template_missing_template_raises_file_not_found(self) -> None:
        engine_config = RenderEngineConfig(
            name="cat_engine",
            input_file=Path("unused.txt"),
            suffix="txt",
            # We must include %i and %s to pass validation
            render_command="cat %s # %i"
        )
        non_existent_path = self.drift_root / "non_existent.txt"
        with self.assertRaises(FileNotFoundError):
            render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=non_existent_path,
                input_file_path=Path("unused.txt")
            )

    def test_render_template_resolved_input_file_missing_raises_file_not_found(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Some template content", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=Path("non_existent_env.sh"),
            suffix="sh",
            render_command="bash -c 'source %i && cat %s'"
        )
        # Calling without passing explicit input_file_path will try to resolve relative to 'config' under drift_root
        with self.assertRaises(FileNotFoundError):
            render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=template_path
            )

    def test_render_template_missing_input_raises_value_error_if_unresolved(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Some template content", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=Path(""), # Empty input file
            suffix="sh",
            render_command="bash -c 'source %i && cat %s'"
        )
        with self.assertRaises(ValueError):
            render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=template_path
            )

    def test_render_template_missing_placeholders_raises_value_error(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Some content", encoding="utf-8")

        # Missing %i placeholder
        engine_config_missing_i = RenderEngineConfig(
            name="missing_i",
            input_file=Path("unused.txt"),
            suffix="txt",
            render_command="cat %s"
        )
        with self.assertRaises(ValueError) as ctx:
            render_template(
                engine_config=engine_config_missing_i,
                drift_root=self.drift_root,
                template_file_path=template_path,
                input_file_path=template_path
            )
        self.assertIn("must contain '%i' placeholder", str(ctx.exception))

        # Missing %s placeholder
        engine_config_missing_s = RenderEngineConfig(
            name="missing_s",
            input_file=Path("unused.txt"),
            suffix="txt",
            render_command="cat %i"
        )
        with self.assertRaises(ValueError) as ctx:
            render_template(
                engine_config=engine_config_missing_s,
                drift_root=self.drift_root,
                template_file_path=template_path,
                input_file_path=template_path
            )
        self.assertIn("must contain '%s' placeholder", str(ctx.exception))

    def test_render_template_failure_raises_runtime_error(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Some content", encoding="utf-8")
        input_path = self.drift_root / "unused.sh"
        input_path.write_text("echo 'This won't be used'", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="failing_engine",
            input_file=Path("unused.sh"),
            suffix="sh",
            # We must include both placeholders to pass resolve_render_template_args
            render_command="false # %i %s"
        )
        with self.assertRaises(RuntimeError) as ctx:
            render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=template_path,
                input_file_path=input_path
            )
        self.assertIn("Render command failed with exit code", str(ctx.exception))

    def test_render_template_input_file_resolution(self) -> None:
        # Create template file
        template_path = self.drift_root / "template.sh"
        template_path.write_text("echo -n $MY_TEST_VAR", encoding="utf-8")

        # 1. Test resolving directly if input_file exists as absolute path
        input_path = self.drift_root / "my_env_file.sh"
        input_path.write_text("export MY_TEST_VAR='resolved_value'", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=input_path, # Using absolute path
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )
        # Should auto-resolve because input_file is absolute/exists
        output = render_template(
            engine_config=engine_config,
            drift_root=self.drift_root,
            template_file_path=template_path
        )
        self.assertEqual(output, "resolved_value")

    def test_render_template_relative_input_file_resolution(self) -> None:
        # Create template file
        template_path = self.drift_root / "template.sh"
        template_path.write_text("echo -n $MY_TEST_VAR", encoding="utf-8")

        # Create temporary config folder and input file inside self.drift_root (representing drift_root)
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_input_file = "test_env_for_resolution.sh"
        config_input_path = config_dir / config_input_file
        config_input_path.write_text("export MY_TEST_VAR='config_resolved_value'", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=Path(config_input_file), # Relative path
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )
        output = render_template(
            engine_config=engine_config,
            drift_root=self.drift_root,
            template_file_path=template_path
        )
        self.assertEqual(output, "config_resolved_value")

    def test_render_template_to_file_creates_output(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Hello World!", encoding="utf-8")

        # Create a dummy input file
        input_path = self.drift_root / "input.txt"
        input_path.write_text("unused", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="cat_engine",
            input_file=Path("unused.txt"),
            suffix="txt",
            # Command must contain %i and %s
            render_command="cat %s # %i"
        )

        output_path = self.drift_root / "nested" / "output.txt"
        render_template_to_file(
            engine_config=engine_config,
            drift_root=self.drift_root,
            template_file_path=template_path,
            output_file_path=output_path,
            input_file_path=input_path
        )

        # Verify output exists and contains exact content
        self.assertTrue(output_path.is_file())
        self.assertEqual(output_path.read_text(encoding="utf-8").strip(), "Hello World!")


class TestDependencyResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_find_engine_for_file(self) -> None:
        engines = [
            RenderEngineConfig(name="envsubst", input_file=Path("env.bash"), suffix="envst", render_command="cmd"),
            RenderEngineConfig(name="mustache", input_file=Path("mustache.json"), suffix="mustache", render_command="cmd")
        ]
        # Match by intermediate segment
        engine1: RenderEngineConfig = cast(RenderEngineConfig,
                                           find_engine_for_file("mustache.envst.json", engines))
        self.assertEqual(engine1.name, "envsubst")
        # Match by terminal suffix
        engine2: RenderEngineConfig = cast(RenderEngineConfig,
                                           find_engine_for_file("mustache.mustache", engines))
        self.assertEqual(engine2.name, "mustache")
        # No match
        self.assertIsNone(find_engine_for_file("static.json", engines))

    def test_strip_engine_suffix(self) -> None:
        # 1. Test legacy strip_engine_suffix wrapper
        self.assertEqual(strip_engine_suffix("mustache.envst.json", "envst"), "mustache.json")
        self.assertEqual(strip_engine_suffix("settings.mustache.json", "mustache"), "settings.json")
        self.assertEqual(strip_engine_suffix("mustache.envst", "envst"), "mustache")
        self.assertEqual(strip_engine_suffix("no_suffix.json", "envst"), "no_suffix.json")
        self.assertEqual(
            strip_engine_suffix("file.envst.extra.envst.json", "envst"),
            "file.envst.extra.json"
        )

        # 2. Test new member method RenderEngineConfig.strip_suffix
        envst_engine = RenderEngineConfig(name="envsubst", input_file=Path(""), suffix="envst", render_command="")
        mustache_engine = RenderEngineConfig(name="mustache", input_file=Path(""), suffix="mustache", render_command="")
        
        self.assertEqual(envst_engine.strip_suffix("mustache.envst.json"), "mustache.json")
        self.assertEqual(mustache_engine.strip_suffix("settings.mustache.json"), "settings.json")
        self.assertEqual(envst_engine.strip_suffix("mustache.envst"), "mustache")
        self.assertEqual(envst_engine.strip_suffix("no_suffix.json"), "no_suffix.json")
        self.assertEqual(
            envst_engine.strip_suffix("file.envst.extra.envst.json"),
            "file.envst.extra.json"
        )

    def test_resolve_dependencies(self) -> None:
        engines = [
            RenderEngineConfig(name="envsubst", input_file=Path("env.bash"), suffix="envst", render_command="cmd"),
            RenderEngineConfig(name="mustache", input_file=Path("mustache.envst.json"), suffix="mustache", render_command="cmd")
        ]
        deps = resolve_dependencies(engines)
        self.assertEqual(deps, {"envsubst": None, "mustache": "envsubst"})

        # Self-dependency should be mapped to None (treated as static)
        self_dep_engines = [
            RenderEngineConfig(name="envsubst", input_file=Path("envsubst.envst.bash"), suffix="envst", render_command="cmd")
        ]
        self_deps = resolve_dependencies(self_dep_engines)
        self.assertEqual(self_deps, {"envsubst": None})

    def test_check_cyclic_dependencies(self) -> None:
        # No cycle
        clean_deps = {"envsubst": None, "mustache": "envsubst"}
        check_cyclic_dependencies(clean_deps) # should not raise error

        # Direct cycle
        cyclic_deps = {"envsubst": "mustache", "mustache": "envsubst"}
        with self.assertRaises(ValueError) as ctx:
            check_cyclic_dependencies(cyclic_deps)
        self.assertIn("Cyclic dependency detected", str(ctx.exception))

        # Self cycle
        self_cycle = {"self_engine": "self_engine"}
        with self.assertRaises(ValueError) as ctx:
            check_cyclic_dependencies(self_cycle)
        self.assertIn("Cyclic dependency detected", str(ctx.exception))

    def test_render_input_templates_success(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        env_file_path = config_dir / "env.sh"
        env_file_path.write_text("export MY_TEST_VAR='templated_env_value'", encoding="utf-8")

        mustache_template_path = config_dir / "mustache.envst.json"
        mustache_template_path.write_text("echo '{\"var\": \"'$MY_TEST_VAR'\"}'", encoding="utf-8")

        # Define the engines
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )

        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=Path("mustache.envst.json"),
            suffix="mustache",
            render_command="cat %s # %i"
        )

        engines = [envsubst_engine, mustache_engine]

        # Call render_input_templates
        render_input_templates(engines, self.drift_root)

        # Verify output file render/config/mustache.json exists and contains correct rendered json
        expected_output_path = self.drift_root / "render" / "config" / "mustache.json"
        self.assertTrue(expected_output_path.is_file())
        self.assertEqual(expected_output_path.read_text(encoding="utf-8").strip(), '{"var": "templated_env_value"}')

        # Config inputs are updated with the rendered paths
        self.assertEqual(envsubst_engine.input_file, env_file_path)
        self.assertEqual(mustache_engine.input_file, expected_output_path)

    def test_render_input_templates_missing_file_raises_error(self) -> None:
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("non_existent.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )
        # Calling render_input_templates should not raise FileNotFoundError anymore.
        # It logs a warning and updates input_file to Path("").
        render_input_templates([envsubst_engine], self.drift_root)
        self.assertEqual(envsubst_engine.input_file, Path(""))

    def test_multi_level_dependency_raises_error(self) -> None:
        engines = [
            RenderEngineConfig(name="engine_c", input_file=Path("c.sh"), suffix="c_suf", render_command="cmd"),
            RenderEngineConfig(name="engine_b", input_file=Path("b.c_suf.sh"), suffix="b_suf", render_command="cmd"),
            RenderEngineConfig(name="engine_a", input_file=Path("a.b_suf.sh"), suffix="a_suf", render_command="cmd")
        ]
        with self.assertRaises(ValueError) as ctx:
            render_input_templates(engines, self.drift_root)
        self.assertIn("Multi-level dependency chain detected", str(ctx.exception))

    def test_render_input_templates_custom_render_directory(self) -> None:
        # Setup config files:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        env_file_path = config_dir / "env.sh"
        env_file_path.write_text("export MY_TEST_VAR='custom_val'", encoding="utf-8")

        mustache_template_path = config_dir / "mustache.envst.json"
        mustache_template_path.write_text("echo '{\"var\": \"'$MY_TEST_VAR'\"}'", encoding="utf-8")

        # Engines
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )
        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=Path("mustache.envst.json"),
            suffix="mustache",
            render_command="cat %s # %i"
        )
        engines = [envsubst_engine, mustache_engine]

        # Use WorkspaceConfig with a custom render directory name
        workspace_config = WorkspaceConfig(render_directory=Path("my_custom_render_sandbox"))

        render_input_templates(engines, self.drift_root, workspace_config)

        # Expected output should reside inside "my_custom_render_sandbox/config/"
        expected_output_path = self.drift_root / "my_custom_render_sandbox" / "config" / "mustache.json"
        self.assertTrue(expected_output_path.is_file())
        self.assertEqual(expected_output_path.read_text(encoding="utf-8").strip(), '{"var": "custom_val"}')

    def test_render_input_templates_absolute_template_path(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        env_file_path = config_dir / "env.sh"
        env_file_path.write_text("export MY_TEST_VAR='abs_val'", encoding="utf-8")

        # Create the template with an absolute path
        abs_template_path = config_dir / "abs_mustache.envst.json"
        abs_template_path.write_text("echo '{\"var\": \"'$MY_TEST_VAR'\"}'", encoding="utf-8")

        # Define the engines, setting mustache engine's input file as an absolute path
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )

        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=abs_template_path, # Absolute path template file
            suffix="mustache",
            render_command="cat %s # %i"
        )

        engines = [envsubst_engine, mustache_engine]

        # Render
        render_input_templates(engines, self.drift_root)

        # Output should be stripped from abs_mustache.envst.json -> abs_mustache.json
        expected_output_path = self.drift_root / "render" / "config" / "abs_mustache.json"
        self.assertTrue(expected_output_path.is_file())
        self.assertEqual(expected_output_path.read_text(encoding="utf-8").strip(), '{"var": "abs_val"}')


class TestRenderPackage(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_package_success_static_config(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.drift_root

        # 1. Create config and env file
        config_dir = drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_dir / "env.sh", "w", encoding="utf-8") as f:
            f.write("export MY_ENV_VAR='drift_render_test'\n")

        # 2. Setup WorkspaceConfig
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        # 3. Create a package src directory
        pkg_dir = drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # 4. Write static package config
        with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_pkg"
            enable_render = true
            """)

        # 5. Write static file and template file
        with open(pkg_dir / "static.txt", "w", encoding="utf-8") as f:
            f.write("Static content")

        with open(pkg_dir / "templated.envst.txt", "w", encoding="utf-8") as f:
            f.write("Rendered: $MY_ENV_VAR")

        # 6. Run render_package
        render_package(workspace_config, pkg_dir)

        # 7. Verify outputs in render/my_pkg/
        render_pkg_dir = drift_root / "render" / "my_pkg"

        # Verify static file was copied
        self.assertTrue((render_pkg_dir / "static.txt").is_file())
        self.assertEqual((render_pkg_dir / "static.txt").read_text(encoding="utf-8"), "Static content")

        # Verify template was rendered
        self.assertTrue((render_pkg_dir / "templated.txt").is_file())
        self.assertEqual((render_pkg_dir / "templated.txt").read_text(encoding="utf-8"), "Rendered: drift_render_test")

        # Verify package.toml was dumped to drift_package.toml since it is static
        self.assertTrue((render_pkg_dir / PACKAGE_CONFIG_FILE_NAME).is_file())
        rendered_config = parse_toml((render_pkg_dir / PACKAGE_CONFIG_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(rendered_config.get("package", {}).get("name"), "my_pkg")
        self.assertEqual(rendered_config.get("package", {}).get("enable_render"), True)

    def test_render_package_disabled(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig

        drift_root = self.drift_root
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )

        pkg_dir = drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # package config with enable_render = false
        with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_pkg"
            enable_render = false
            """)

        with open(pkg_dir / "static.txt", "w", encoding="utf-8") as f:
            f.write("Static content")

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify render dir contains only the 'drift_package.toml' (loaded from package.toml) and no other files
        render_pkg_dir = drift_root / "render" / "my_pkg" / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(render_pkg_dir.exists())
        rendered_config = parse_toml(render_pkg_dir.read_text(encoding="utf-8"))
        self.assertEqual(rendered_config.get("package", {}).get("name"), "my_pkg")
        self.assertEqual(rendered_config.get("package", {}).get("enable_render"), False)

    def test_render_package_templated_config_package_toml(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.drift_root

        config_dir = drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_dir / "env.sh", "w", encoding="utf-8") as f:
            f.write("export PKG_NAME='rendered_pkg_name'\n")

        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        pkg_dir = drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Write templated package config
        with open(pkg_dir / "package.envst.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "$PKG_NAME"
            enable_render = true
            """)

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify output in render/my_pkg/
        render_pkg_dir = drift_root / "render" / "my_pkg"

        # package.toml was rendered (loaded from package.envst.toml) and renamed to drift_package.toml
        rendered_config_path = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_config_path.is_file())
        content = rendered_config_path.read_text(encoding="utf-8")
        self.assertIn('name = "rendered_pkg_name"', content)

        # There shouldn't be any package.envst.toml in render dir
        self.assertFalse((render_pkg_dir / "package.envst.toml").exists())
        self.assertFalse((render_pkg_dir / "drift_package.envst.toml").exists())

    def test_render_package_templated_config_package_toml(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.drift_root

        config_dir = drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_dir / "env.sh", "w", encoding="utf-8") as f:
            f.write("export PKG_NAME='rendered_pkg_name'\n")

        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        pkg_dir = drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Write templated package config
        with open(pkg_dir / "drift_package.envst.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "$PKG_NAME"
            enable_render = true
            """)

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify output in render/my_pkg/
        render_pkg_dir = drift_root / "render" / "my_pkg"

        # package.toml was rendered (loaded from package.envst.toml) and renamed to drift_package.toml
        rendered_config_path = render_pkg_dir / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_config_path.is_file())
        content = rendered_config_path.read_text(encoding="utf-8")
        self.assertIn('name = "rendered_pkg_name"', content)

        # There shouldn't be any package.envst.toml in render dir
        self.assertFalse((render_pkg_dir / "package.envst.toml").exists())
        self.assertFalse((render_pkg_dir / "drift_package.envst.toml").exists())

    def test_render_all_packages(self) -> None:
        from drift.render_package import run_primitive_2_render_packages
        from drift.workspace_config import WorkspaceConfig

        drift_root = self.drift_root

        # Setup WorkspaceConfig
        # pkg_a is explicitly enabled (True)
        # pkg_b is explicitly disabled (False)
        # pkg_c is not listed, but packages_enable_default is True
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            packages_enable={
                "pkg_a": True,
                "pkg_b": False,
            },
            packages_enable_default=True
        )

        # Create package source folders under src/
        for pkg_name in ("pkg_a", "pkg_b", "pkg_c"):
            pkg_dir = drift_root / "src" / pkg_name
            pkg_dir.mkdir(parents=True, exist_ok=True)
            with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
                f.write(f"""
                [package]
                name = "{pkg_name}"
                enable_render = true
                """)
            with open(pkg_dir / "file.txt", "w", encoding="utf-8") as f:
                f.write(f"Content for {pkg_name}")

        # Run run_primitive_2_render_packages
        run_primitive_2_render_packages(workspace_config)

        # Verify pkg_a is rendered
        self.assertTrue((drift_root / "render" / "pkg_a" / "file.txt").is_file())
        # Verify pkg_b is NOT rendered
        self.assertFalse((drift_root / "render" / "pkg_b" / "file.txt").exists())
        # Verify pkg_c is rendered (due to default=True)
        self.assertTrue((drift_root / "render" / "pkg_c" / "file.txt").is_file())

    def test_run_primitive_3_commit_render_repo(self) -> None:
        from drift.render_package import run_primitive_3_commit_render_repo
        from drift.workspace_config import WorkspaceConfig
        import subprocess

        drift_root = self.drift_root
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )

        # 1. Create render directory
        render_dir = drift_root / "render"
        render_dir.mkdir(parents=True, exist_ok=True)

        # 2. Initialize a git repository inside the render directory
        # Also, configure standard dummy git user for testing environments to avoid commit issues
        subprocess.run(["git", "init"], cwd=str(render_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(render_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(render_dir), capture_output=True, check=True)

        # 3. Write a file inside render directory
        test_file_path = render_dir / "test.txt"
        test_file_path.write_text("Hello render", encoding="utf-8")

        # 4. Commit using run_primitive_3_commit_render_repo (unscoped)
        msg = "Test dynamic commit message"
        run_primitive_3_commit_render_repo(workspace_config, msg)

        # 5. Verify the commit message
        log_res = subprocess.run(
            ["git", "-C", str(render_dir), "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(log_res.stdout.strip(), msg)

        # 6. Run again on a clean repo (should return gracefully without error)
        run_primitive_3_commit_render_repo(workspace_config, "Should not commit anything")

        # 7. Test scoped commit to a specific package
        pkg_a_dir = render_dir / "pkg_a"
        pkg_b_dir = render_dir / "pkg_b"
        pkg_a_dir.mkdir(parents=True, exist_ok=True)
        pkg_b_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_a_dir / "file_a.txt", "w", encoding="utf-8") as f:
            f.write("pkg_a file")
        with open(pkg_b_dir / "file_b.txt", "w", encoding="utf-8") as f:
            f.write("pkg_b file")

        # Commit pkg_a specifically
        scoped_msg = "Commit pkg_a changes"
        run_primitive_3_commit_render_repo(workspace_config, scoped_msg, ["pkg_a"])

        # Verify only pkg_a was committed
        status_res = subprocess.run(
            ["git", "-C", str(render_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        status_output = status_res.stdout.strip()
        # pkg_b should still be untracked (marked as ??)
        self.assertTrue("?? pkg_b/" in status_output or "?? pkg_b/file_b.txt" in status_output)
        # pkg_a should NOT be in the status output because it is clean
        self.assertNotIn("pkg_a/", status_output)

        # Verify the commit message of the scoped commit
        log_scoped = subprocess.run(
            ["git", "-C", str(render_dir), "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(log_scoped.stdout.strip(), scoped_msg)

    def test_render_and_commit_multiple_packages(self) -> None:
        """Verifies rendering and committing multiple packages specifically."""
        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.workspace_config import WorkspaceConfig
        import subprocess

        drift_root = self.drift_root
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )

        # 1. Create package source directories under src/
        for pkg_name in ("pkg_one", "pkg_two", "pkg_three"):
            pkg_dir = drift_root / "src" / pkg_name
            pkg_dir.mkdir(parents=True, exist_ok=True)
            with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
                f.write(f"""
                [package]
                name = "{pkg_name}"
                enable_render = true
                """)
            with open(pkg_dir / "file.txt", "w", encoding="utf-8") as f:
                f.write(f"Content for {pkg_name}")

        # 2. Render only pkg_one and pkg_two specifically
        run_primitive_2_render_packages(workspace_config, ["pkg_one", "pkg_two"])

        # Verify pkg_one and pkg_two are rendered, but pkg_three is NOT
        self.assertTrue((drift_root / "render" / "pkg_one" / "file.txt").is_file())
        self.assertTrue((drift_root / "render" / "pkg_two" / "file.txt").is_file())
        self.assertFalse((drift_root / "render" / "pkg_three" / "file.txt").exists())

        # 3. Setup git repo in render directory
        render_dir = drift_root / "render"
        subprocess.run(["git", "init"], cwd=str(render_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(render_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(render_dir), capture_output=True, check=True)

        # 4. Commit only pkg_one specifically
        run_primitive_3_commit_render_repo(workspace_config, "Commit pkg_one specifically", ["pkg_one"])

        # Verify pkg_one is committed and clean, but pkg_two is still untracked
        status_res = subprocess.run(
            ["git", "-C", str(render_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        status_output = status_res.stdout.strip()
        self.assertNotIn("pkg_one/", status_output)
        self.assertTrue("?? pkg_two/" in status_output or "?? pkg_two/file.txt" in status_output)

    def test_render_engine_input_dependency(self) -> None:
        """Verifies that engine input templates are rendered before package rendering."""
        from drift.render_package import run_primitive_2_render_packages
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.drift_root

        # 1. Setup config files:
        config_dir = drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        env_file_path = config_dir / "env.sh"
        env_file_path.write_text("export MY_VAL='orchestrated_value'", encoding="utf-8")

        mustache_template_path = config_dir / "mustache.envst.json"
        # Mustache input template that uses envsubst
        mustache_template_path.write_text('{"the_value": "$MY_VAL"}', encoding="utf-8")

        # 2. Setup WorkspaceConfig with engines
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            packages_enable={"my_pkg": True}
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        # Mustache engine depends on rendered mustache.json (from mustache.envst.json)
        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=Path("mustache.envst.json"),
            suffix="mustache",
            # We'll use a simple python command to 'render' mustache-like if mustache is not installed
            # But here we just want to verify the orchestration.
            # Using cat and some markers to simulate mustache.
            render_command="bash -c 'cat %i %s'"
        )
        workspace_config.render_engine_config = {
            "envsubst": envsubst_engine,
            "mustache": mustache_engine
        }

        # 3. Create a package that uses mustache
        pkg_dir = drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "package.toml").write_text('[package]\nname = "my_pkg"\nenable_render = true', encoding="utf-8")
        (pkg_dir / "file.mustache.txt").write_text("Template content", encoding="utf-8")

        # 4. Run rendering
        run_primitive_2_render_packages(workspace_config)

        # 5. Verify engine input was rendered
        rendered_mustache_json = drift_root / "render" / "config" / "mustache.json"
        self.assertTrue(rendered_mustache_json.is_file())
        self.assertIn('"the_value": "orchestrated_value"', rendered_mustache_json.read_text(encoding="utf-8"))

        # 6. Verify package file was rendered using the rendered input
        rendered_pkg_file = drift_root / "render" / "my_pkg" / "file.txt"
        self.assertTrue(rendered_pkg_file.is_file())
        content = rendered_pkg_file.read_text(encoding="utf-8")
        # Since our simulated mustache command was 'cat %i %s', it should contain both
        self.assertIn('"the_value": "orchestrated_value"', content)
        self.assertIn("Template content", content)

    def test_render_input_templates_graceful_missing_static_input(self) -> None:
        """Tests that a missing static input file results in a warning, sets input_file to empty path, and subsequent rendering raises ValueError."""
        # Setup config directory
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Create an engine config pointing to a non-existent input file
        engine_config = RenderEngineConfig(
            name="missing_static_engine",
            input_file=Path("non_existent_env.sh"),
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )

        # Calling render_input_templates should not raise FileNotFoundError anymore
        # It logs a warning and updates engine_config.input_file to Path("")
        render_input_templates([engine_config], self.drift_root)
        self.assertEqual(engine_config.input_file, Path(""))

        # Create a mock template
        template_path = self.drift_root / "template.sh"
        template_path.write_text("echo -n 'hello'", encoding="utf-8")

        # Calling render_template with this engine should raise a ValueError
        with self.assertRaises(ValueError) as ctx:
            render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=template_path
            )
        self.assertIn("is disabled or has an invalid/empty input file", str(ctx.exception))

    def test_render_input_templates_graceful_missing_templated_input(self) -> None:
        """Tests that a missing input template file for a dependent engine results in a warning and updates input_file to empty path."""
        # Setup config directory
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Create a dependent engine where its input template file is missing
        dep_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"), # we can let env.sh exist or not
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=Path("non_existent_mustache.envst.json"), # Missing template input!
            suffix="mustache",
            render_command="mustache %i %s"
        )

        render_input_templates([dep_engine, mustache_engine], self.drift_root)
        self.assertEqual(mustache_engine.input_file, Path(""))

    def test_render_package_name_starts_with_dot_dash(self) -> None:
        """Verifies that rendering a package whose name starts with 'dot-' preserves the name exactly."""
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig

        # Setup WorkspaceConfig
        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )

        # Create package dir starting with dot-
        pkg_dir = self.drift_root / "src" / "dot-my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "dot-my_pkg"
            enable_render = true
            """)

        with open(pkg_dir / "file.txt", "w", encoding="utf-8") as f:
            f.write("static content")

        render_package(workspace_config, pkg_dir)

        # Verify output directory is 'render/dot-my_pkg' (name starting with 'dot-' is preserved)
        render_pkg_dir = self.drift_root / "render" / "dot-my_pkg"
        self.assertTrue(render_pkg_dir.is_dir())
        self.assertTrue((render_pkg_dir / "file.txt").is_file())
        self.assertEqual((render_pkg_dir / "file.txt").read_text(encoding="utf-8"), "static content")

    def test_render_package_skips_raw_hidden_files(self) -> None:
        """Verifies that render_package skips raw hidden files (starting with '.') except for .drift_ignore."""
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig

        # Setup WorkspaceConfig
        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
        )

        # Create package dir
        pkg_dir = self.drift_root / "src" / "pkg_h"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        with open(pkg_dir / "package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "pkg_h"
            enable_render = true
            """)

        # Add a valid .drift_ignore file
        with open(pkg_dir / ".drift_ignore", "w", encoding="utf-8") as f:
            f.write("# ignore config")

        # Add a raw hidden file (should be skipped)
        with open(pkg_dir / ".hidden_file.txt", "w", encoding="utf-8") as f:
            f.write("hidden content")

        # Add a normal file (should not be skipped)
        with open(pkg_dir / "normal.txt", "w", encoding="utf-8") as f:
            f.write("normal content")

        # Setup logger spy to verify print info
        with self.assertLogs("drift.render_package", level="INFO") as log_capture:
            render_package(workspace_config, pkg_dir)

        # Verify output directory
        render_pkg_dir = self.drift_root / "render" / "pkg_h"
        self.assertTrue(render_pkg_dir.is_dir())

        # Verify .drift_ignore was processed/copied
        self.assertTrue((render_pkg_dir / ".drift_ignore").is_file())

        # Verify normal file was processed/copied
        self.assertTrue((render_pkg_dir / "normal.txt").is_file())

        # Verify .hidden_file.txt was skipped
        self.assertFalse((render_pkg_dir / ".hidden_file.txt").exists())

        # Verify SKIP warning message was logged
        self.assertTrue(any("Skipping hidden file" in log_msg for log_msg in log_capture.output))


if __name__ == "__main__":
    unittest.main()
