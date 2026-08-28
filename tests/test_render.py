"""Tests for rendering and compiling engines using pathlib."""

import os
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import patch, MagicMock
from pathlib import Path
from typing import cast, Any, List, Tuple, Union

from drift.constants import (
    CONFIG_DIR_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
)
from drift.workspace_config import RenderEngineConfig, WorkspaceConfig
from drift.render_core import render_template, render_template_to_file, RenderError
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

    def test_env_settings_passed_to_envsubst_render_engine(self) -> None:
        import shutil
        # Check if envsubst is available on the system
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        from drift.workspace_config import load_workspace_config
        from drift.render_core import render_template

        # Create config directory and files
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        drift_toml_path = config_dir / "drift.toml"
        drift_toml_path.write_text("""
            [workspace]
            render_directory = "my_render"

            [packages.enable]
            DEFAULT = false

            [env]
            MY_CUSTOM_ENV_VAR = "hello_from_drift_toml"
            """, encoding="utf-8")

        # Load workspace configuration (this also updates os.environ with env section!)
        workspace_config = load_workspace_config(self.drift_root)

        # Confirm the environment variable is loaded and present in os.environ
        self.assertEqual(os.environ.get("MY_CUSTOM_ENV_VAR"), "hello_from_drift_toml")

        # Create a template file that uses envsubst
        template_path = self.drift_root / "test_envsubst_template.envst"
        template_path.write_text("Greeting: $MY_CUSTOM_ENV_VAR", encoding="utf-8")

        # Create an empty dummy input file to satisfy %i validation
        dummy_input = self.drift_root / "dummy.sh"
        dummy_input.write_text("", encoding="utf-8")

        # Set up an envsubst engine config with both %i and %s
        engine_config = RenderEngineConfig(
            name="envsubst",
            input_file=Path("dummy.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )

        # Call render_template
        output = render_template(
            engine_config=engine_config,
            drift_root=self.drift_root,
            template_file_path=template_path,
            input_file_path=dummy_input
        )

        # Verify that envsubst successfully substituted the variable defined under [env] in drift.toml
        self.assertEqual(output.strip(), "Greeting: hello_from_drift_toml")

        # Clean up os.environ to avoid leaking to other tests
        os.environ.pop("MY_CUSTOM_ENV_VAR", None)

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

    def test_render_template_missing_input_raises_render_error_if_unresolved(self) -> None:
        template_path = self.drift_root / "template.txt"
        template_path.write_text("Some template content", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=Path(""), # Empty input file
            suffix="sh",
            render_command="bash -c 'source %i && cat %s'"
        )
        with self.assertRaises(RenderError):
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

    def test_render_template_failure_raises_render_error(self) -> None:
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
        with self.assertRaises(RenderError) as ctx:
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

    def test_multi_level_dependency_tree(self) -> None:
        # Create config directory
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Write files
        static_file = config_dir / "static.txt"
        static_file.write_text("Hello A", encoding="utf-8")

        template_b = config_dir / "b.suf_a"
        template_b.write_text("Hello B from A", encoding="utf-8")

        template_c = config_dir / "c.suf_a"
        template_c.write_text("Hello C from A", encoding="utf-8")

        template_d = config_dir / "d.suf_c"
        template_d.write_text("Hello D from C", encoding="utf-8")

        # 4 engines: A, B, C, D representing:
        # A -> B
        # A -> C -> D
        engine_a = RenderEngineConfig(name="engine_a", input_file=Path("static.txt"), suffix="suf_a", render_command="cat %s # %i")
        engine_b = RenderEngineConfig(name="engine_b", input_file=Path("b.suf_a"), suffix="suf_b", render_command="cat %s # %i")
        engine_c = RenderEngineConfig(name="engine_c", input_file=Path("c.suf_a"), suffix="suf_c", render_command="cat %s # %i")
        engine_d = RenderEngineConfig(name="engine_d", input_file=Path("d.suf_c"), suffix="suf_d", render_command="cat %s # %i")

        engines = [engine_a, engine_b, engine_c, engine_d]

        # 1. Resolve and check dependencies
        from drift.render_input import resolve_dependencies
        dep_map = resolve_dependencies(engines)

        # Assert correct 1-to-1 dependency resolution mapping
        self.assertEqual(dep_map["engine_a"], None)
        self.assertEqual(dep_map["engine_b"], "engine_a")
        self.assertEqual(dep_map["engine_c"], "engine_a")
        self.assertEqual(dep_map["engine_d"], "engine_c")

        # 2. Run transitive template input rendering
        render_input_templates(engines, self.drift_root)

        # 3. Check that the transitive files compiled successfully inside the sandbox
        rendered_b_input = self.drift_root / "render" / "config" / "b"
        rendered_c_input = self.drift_root / "render" / "config" / "c"
        rendered_d_input = self.drift_root / "render" / "config" / "d"

        self.assertTrue(rendered_b_input.is_file())
        self.assertTrue(rendered_c_input.is_file())
        self.assertTrue(rendered_d_input.is_file())

        self.assertEqual(rendered_b_input.read_text(encoding="utf-8").strip(), "Hello B from A")
        self.assertEqual(rendered_c_input.read_text(encoding="utf-8").strip(), "Hello C from A")
        self.assertEqual(rendered_d_input.read_text(encoding="utf-8").strip(), "Hello D from C")

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
        with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
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

        # Verify drift_package.toml was dumped to drift_drift_package.toml since it is static
        self.assertTrue((render_pkg_dir / PACKAGE_CONFIG_FILE_NAME).is_file())
        rendered_config = parse_toml((render_pkg_dir / PACKAGE_CONFIG_FILE_NAME).read_text(encoding="utf-8"))
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
        with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
            f.write("""
            [package]
            enable_render = false
            """)

        with open(pkg_dir / "static.txt", "w", encoding="utf-8") as f:
            f.write("Static content")

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify render dir contains only the 'drift_drift_package.toml' (loaded from drift_package.toml) and no other files
        render_pkg_dir = drift_root / "render" / "my_pkg" / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(render_pkg_dir.exists())
        rendered_config = parse_toml(render_pkg_dir.read_text(encoding="utf-8"))
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

        with self.assertRaises(FileNotFoundError) as ctx:
            # Run render_package
            render_package(workspace_config, pkg_dir)
        self.assertIn("not found", str(ctx.exception))

    def test_render_package_templated_config_drift_package_toml(self) -> None:
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

        # drift_package.toml was rendered (loaded from package.envst.toml) and renamed to drift_drift_package.toml
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
            with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
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
            with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
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
        (pkg_dir / "drift_package.toml").write_text('[package]\nname = "my_pkg"\nenable_render = true', encoding="utf-8")
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

        # Calling render_template with this engine should raise a RenderError
        with self.assertRaises(RenderError) as ctx:
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

        with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
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

        with open(pkg_dir / "drift_package.toml", "w", encoding="utf-8") as f:
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
        from drift.constants import set_test_mode
        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.render_package", level="INFO") as log_capture:
                render_package(workspace_config, pkg_dir)
        finally:
            set_test_mode(True, enable_logging=False)

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

    def test_secrets_env_load_and_unload_helpers(self) -> None:
        from drift.workspace_config import parse_secrets_env, load_env_settings, unload_env_settings

        # Setup temporary secrets.env file
        config_dir = self.drift_root / CONFIG_DIR_NAME
        config_dir.mkdir(parents=True, exist_ok=True)
        secrets_file = config_dir / SECRETS_ENV_FILE_NAME

        secrets_file.write_text(
            "# This is a comment\n"
            "MY_SECRET_VAR=secret_value\n"
            "PRE_EXISTING_SECRET=new_secret_value\n",
            encoding="utf-8"
        )

        # Set a pre-existing value in environment to check restoration
        os.environ["PRE_EXISTING_SECRET"] = "old_value"

        # 1. Parse secrets file
        secrets = parse_secrets_env(self.drift_root)
        self.assertEqual(len(secrets), 2)
        self.assertEqual(secrets[0], ("MY_SECRET_VAR", "secret_value"))
        self.assertEqual(secrets[1], ("PRE_EXISTING_SECRET", "new_secret_value"))

        # 2. Load env settings
        saved_envs = cast(List[Tuple[str, Union[str, None]]], load_env_settings(secrets))
        self.assertIsNotNone(saved_envs)
        self.assertEqual(len(saved_envs), 2)

        # Verify values loaded into os.environ
        self.assertEqual(os.environ["MY_SECRET_VAR"], "secret_value")
        self.assertEqual(os.environ["PRE_EXISTING_SECRET"], "new_secret_value")

        # 3. Unload env settings
        unload_env_settings(saved_envs)

        # Verify environment is restored
        self.assertNotIn("MY_SECRET_VAR", os.environ)
        self.assertEqual(os.environ["PRE_EXISTING_SECRET"], "old_value")

        # Cleanup pre-existing env
        os.environ.pop("PRE_EXISTING_SECRET", None)

    def test_run_primitive_2_renders_package_with_secrets(self) -> None:
        from drift.render_package import run_primitive_2_render_packages
        from drift.workspace_config import WorkspaceConfig

        # Setup config
        config_dir = self.drift_root / CONFIG_DIR_NAME
        config_dir.mkdir(parents=True, exist_ok=True)
        
        # Write secrets.env
        secrets_file = config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text("PRIMITIVE_SECRET_VAR=ultimate_secret\n", encoding="utf-8")

        # Write a dummy envsubst.sh file so it can be resolved as non-empty
        envsubst_sh = config_dir / "envsubst.sh"
        envsubst_sh.write_text("#!/bin/bash\n", encoding="utf-8")

        # Setup drift.toml configuration
        drift_toml = config_dir / GLOBAL_CONFIG_FILE_NAME
        drift_toml.write_text(
            "[workspace]\n"
            "source_directory = \"src\"\n"
            "render_directory = \"render\"\n",
            encoding="utf-8"
        )

        # Write package config
        pkg_dir = self.drift_root / "src" / "pkg_sec"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_toml = pkg_dir / "drift_package.toml"
        pkg_toml.write_text("[package]\nname = \"pkg_sec\"\n", encoding="utf-8")

        # Write a file that references the secret
        tpl_file = pkg_dir / "secret_file.envst"
        tpl_file.write_text("The secret is: ${PRIMITIVE_SECRET_VAR}\n", encoding="utf-8")

        # Define workspace config
        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render")
        )
        from drift.workspace_config import RenderEngineConfig
        workspace_config.render_engine_config = {
            "envsubst": RenderEngineConfig(
                name="envsubst",
                input_file=Path("envsubst.sh"),
                suffix="envst",
                render_command="bash -c 'envsubst < %s # %i'"
            )
        }

        # Assert environment variable is NOT in current env
        self.assertNotIn("PRIMITIVE_SECRET_VAR", os.environ)

        # Execute render packages primitive 2
        run_primitive_2_render_packages(workspace_config, ["pkg_sec"])

        # Check that the secret was populated inside the rendered file
        rendered_file = self.drift_root / "render" / "pkg_sec" / "secret_file"
        self.assertTrue(rendered_file.is_file())
        self.assertIn("The secret is: ultimate_secret", rendered_file.read_text(encoding="utf-8"))

        # Assert environment variable is cleaned up from current environment
        self.assertNotIn("PRIMITIVE_SECRET_VAR", os.environ)

    def test_render_pre_source_hook_generates_dynamic_files(self) -> None:
        """Verifies that pre_source hook executes in src/pkg before rendering and can dynamically generate source templates."""
        pkg_dir = self.drift_root / "src" / "pkg_dynamic"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir()

        hook_script = scripts_dir / "gen_dynamic.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'DYNAMIC_OUTPUT=123' > dynamic_file.txt\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        pkg_toml = pkg_dir / "drift_package.toml"
        pkg_toml.write_text(
            "[package]\n"
            "name = \"pkg_dynamic\"\n\n"
            "[hooks]\n"
            "pre_source = \"scripts/gen_dynamic.sh\"\n",
            encoding="utf-8"
        )

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render")
        )

        from drift.render_package import run_primitive_2_render_packages
        run_primitive_2_render_packages(workspace_config, ["pkg_dynamic"])

        # Check that the dynamic file was created in src by the hook and then copied/rendered into render
        dynamic_src = pkg_dir / "dynamic_file.txt"
        self.assertTrue(dynamic_src.is_file())
        self.assertEqual(dynamic_src.read_text(encoding="utf-8").strip(), "DYNAMIC_OUTPUT=123")

        dynamic_render = self.drift_root / "render" / "pkg_dynamic" / "dynamic_file.txt"
        self.assertTrue(dynamic_render.is_file())
        self.assertEqual(dynamic_render.read_text(encoding="utf-8").strip(), "DYNAMIC_OUTPUT=123")

    def test_render_pre_source_hook_failure_aborts_render(self) -> None:
        """Verifies that an error in pre_source hook is not suppressed and aborts rendering."""
        pkg_dir = self.drift_root / "src" / "pkg_failing_hook"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_dir / "scripts"
        scripts_dir.mkdir()

        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'Fatal generation error' >&2\n"
            "exit 1\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        pkg_toml = pkg_dir / "drift_package.toml"
        pkg_toml.write_text(
            "[package]\n"
            "name = \"pkg_failing_hook\"\n\n"
            "[hooks]\n"
            "pre_source = \"scripts/failing.sh\"\n",
            encoding="utf-8"
        )

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render")
        )

        from drift.render_package import run_primitive_2_render_packages
        res = run_primitive_2_render_packages(workspace_config, ["pkg_failing_hook"])
        self.assertEqual(res.status, "FAILED")
        self.assertIn("failed with exit code 1", cast(str, res.error_message))

    def test_default_package_envs_available_in_templates(self) -> None:
        """Verifies drift_package_name, drift_package_target_dir, and drift_install_method are available in templates."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst is not available")

        # 1. Setup workspace config
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "envsubst.bash").write_text("# dummy env", encoding="utf-8")

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            render_engine_config={
                "envsubst": RenderEngineConfig(
                    name="envsubst",
                    input_file=Path("envsubst.bash"),
                    suffix="envst",
                    render_command="bash -c 'source %i && envsubst < %s'"
                )
            }
        )

        pkg_dir = self.drift_root / "src" / "pkg_env_test"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        (pkg_dir / "drift_package.toml").write_text("""
            [package]
            name = "pkg_env_test"
            install_method = "copy"
            target_directory = "/custom/target/dir"
        """, encoding="utf-8")

        template_file = pkg_dir / "config.envst.json"
        template_file.write_text(
            '{"name": "$drift_package_name", "target": "$drift_package_target_dir", "method": "$drift_install_method"}',
            encoding="utf-8"
        )

        from drift.render_package import run_primitive_2_render_packages
        run_primitive_2_render_packages(workspace_config, ["pkg_env_test"])

        rendered_output = self.drift_root / "render" / "pkg_env_test" / "config.json"
        self.assertTrue(rendered_output.exists())
        content = rendered_output.read_text(encoding="utf-8")
        self.assertIn('"name": "pkg_env_test"', content)
        self.assertIn('"target": "/custom/target/dir"', content)
        self.assertIn('"method": "copy"', content)

        # Ensure envs are cleaned up after rendering
        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_install_method", os.environ)

    def test_render_input_template_command_failure_disables_engine(self) -> None:
        """Tests that when an input template render command fails (e.g. command not found), the engine is disabled."""
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "env.sh").write_text("export FOO=bar\n", encoding="utf-8")
        (config_dir / "jinja2.mustache.json").write_text('{"foo": "bar"}', encoding="utf-8")

        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && cp %s %s'"
        )
        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file=Path("mustache.json"), # static input
            suffix="mustache",
            render_command="non_existent_command_12345 %i %s" # command will fail
        )
        (config_dir / "mustache.json").write_text("{}", encoding="utf-8")
        jinja2_engine = RenderEngineConfig(
            name="jinja2",
            input_file=Path("jinja2.mustache.json"), # depends on mustache!
            suffix="jinja2",
            render_command="jinja2 %i %s"
        )

        # render_input_templates should not throw, but should gracefully set jinja2.input_file = Path("")
        render_input_templates([envsubst_engine, mustache_engine, jinja2_engine], self.drift_root)
        self.assertEqual(jinja2_engine.input_file, Path(""))
        self.assertTrue(jinja2_engine.is_disabled)

    def test_primitive_2_partial_failure_proceeds_with_other_packages(self) -> None:
        """Tests that Primitive 2 continues rendering remaining packages when one package fails, and returns FAILED status."""
        from drift.workspace_config import WorkspaceConfig
        from drift.render_package import run_primitive_2_render_packages

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            packages_enable={"pkg_good": True, "pkg_broken": True},
            packages_enable_default=False
        )
        disabled_engine = RenderEngineConfig(
            name="jinja2",
            input_file=Path(""), # disabled!
            suffix="jinja2",
            render_command="jinja2 %i %s"
        )
        workspace_config.render_engine_config = {"jinja2": disabled_engine}

        # Setup pkg_good
        pkg_good_dir = self.drift_root / "src" / "pkg_good"
        pkg_good_dir.mkdir(parents=True, exist_ok=True)
        (pkg_good_dir / "drift_package.toml").write_text("[package]\ninstall_method = 'copy'\n", encoding="utf-8")
        (pkg_good_dir / "good_file.txt").write_text("hello world", encoding="utf-8")

        # Setup pkg_broken (contains .jinja2 template relying on disabled engine)
        pkg_broken_dir = self.drift_root / "src" / "pkg_broken"
        pkg_broken_dir.mkdir(parents=True, exist_ok=True)
        (pkg_broken_dir / "drift_package.toml").write_text("[package]\ninstall_method = 'copy'\n", encoding="utf-8")
        (pkg_broken_dir / "broken.jinja2").write_text("template data", encoding="utf-8")

        # Primitive 2 should return RenderResult with status="FAILED"
        res = run_primitive_2_render_packages(workspace_config)
        self.assertEqual(res.status, "FAILED")
        self.assertEqual(res.error_package, "pkg_broken")
        self.assertIn("pkg_broken", cast(str, res.error_message))
        self.assertIn("Render failed", cast(str, res.error_message))

        # But pkg_good should have rendered successfully!
        rendered_good_file = self.drift_root / "render" / "pkg_good" / "good_file.txt"
        self.assertTrue(rendered_good_file.exists())
        self.assertEqual(rendered_good_file.read_text(encoding="utf-8"), "hello world")

    def test_pre_source_hook_static_in_src_copied_and_executed_with_src_cwd(self) -> None:
        """Verifies that a static pre_source hook located inside src/ is copied into render/ before executing with cwd=src/."""
        from drift.lifecycle_hooks import trigger_pre_source_lifecycle_hook

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={"pkg_static_hook": True},
            packages_enable_default=False
        )

        # Setup pkg_static_hook with a static pre_source hook
        pkg_src_dir = self.drift_root / "src" / "pkg_static_hook"
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_src_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "pkg_static_hook"
        install_method = "copy"

        [hooks]
        pre_source = "scripts/generate_static.sh"
        """, encoding="utf-8")

        hook_script = scripts_dir / "generate_static.sh"
        hook_script.write_text("""#!/bin/sh
echo "STATIC_PRE_SOURCE_RAN" > generated_static_file.txt
""", encoding="utf-8")

        # Trigger pre_source hook
        trigger_pre_source_lifecycle_hook(
            workspace_config=workspace_config,
            package_name="pkg_static_hook",
            load_envs=True
        )

        # 1. Copied script should exist in render/pkg_static_hook/scripts/generate_static.sh
        copied_hook = self.drift_root / "render" / "pkg_static_hook" / "scripts" / "generate_static.sh"
        self.assertTrue(copied_hook.is_file())
        self.assertIn("STATIC_PRE_SOURCE_RAN", copied_hook.read_text(encoding="utf-8"))

        # 2. Output file from script execution should exist in src/pkg_static_hook (proving cwd was src/pkg_static_hook)
        created_file = pkg_src_dir / "generated_static_file.txt"
        self.assertTrue(created_file.is_file())
        self.assertEqual(created_file.read_text(encoding="utf-8").strip(), "STATIC_PRE_SOURCE_RAN")

    def test_pre_source_hook_rendered_in_render_dir_and_executed_with_src_cwd(self) -> None:
        """Verifies that a pre_source hook located inside src/ is rendered into render/ before executing with cwd=src/."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        from drift.lifecycle_hooks import trigger_pre_source_lifecycle_hook

        # Setup workspace config with envsubst engine
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "env.sh").write_text("#!/bin/bash\n", encoding="utf-8")

        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )

        workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={"pkg_hook": True},
            packages_enable_default=False
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        # Setup pkg_hook with a templated pre_source hook
        pkg_src_dir = self.drift_root / "src" / "pkg_hook"
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_src_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_hook"
        install_method = "copy"

        [hooks]
        pre_source = "scripts/generate.envst.sh"
        """, encoding="utf-8")

        hook_script = scripts_dir / "generate.envst.sh"
        hook_script.write_text("""#!/bin/sh
echo "CREATED_BY_${drift_package_name}" > generated_file.txt
""", encoding="utf-8")

        # Trigger pre_source hook
        trigger_pre_source_lifecycle_hook(
            workspace_config=workspace_config,
            package_name="pkg_hook",
            load_envs=True
        )

        # 1. Rendered script should exist in render/pkg_hook/scripts/generate.sh
        rendered_hook = self.drift_root / "render" / "pkg_hook" / "scripts" / "generate.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn("CREATED_BY_pkg_hook", rendered_hook.read_text(encoding="utf-8"))

        # 2. Output file from script execution should exist in src/pkg_hook (proving cwd was src/pkg_hook)
        created_file = pkg_src_dir / "generated_file.txt"
        self.assertTrue(created_file.is_file())
        self.assertEqual(created_file.read_text(encoding="utf-8").strip(), "CREATED_BY_pkg_hook")

    def test_python_envsubst_direct(self) -> None:
        from drift.render_core import python_envsubst
        from drift.exceptions import RenderError, ConfigError

        # Valid substitutions with custom env dict
        env = {"USER": "alice", "APP_PORT": "8080"}
        res = python_envsubst("Hello $USER on ${APP_PORT}!", env=env)
        self.assertEqual(res, "Hello alice on 8080!")

        # Missing variable raises RenderError by default
        with self.assertRaises(RenderError) as ctx:
            python_envsubst("Missing: $NOT_SET_VAR", env=env)
        self.assertIn("Environment variable '$NOT_SET_VAR' referenced in template was not found", str(ctx.exception))

        # Missing variable raises ConfigError when error_cls=ConfigError
        with self.assertRaises(ConfigError) as ctx:
            python_envsubst("Missing: ${NOT_SET_VAR}", env=env, error_cls=ConfigError)
        self.assertIn("Environment variable '$NOT_SET_VAR' referenced in template was not found", str(ctx.exception))

    def test_render_template_envsubst_internal_fallback(self) -> None:
        from drift.render_core import render_template

        template_path = self.drift_root / "template.envst.txt"
        template_path.write_text("Value: ${FALLBACK_VAR}", encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="envsubst",
            input_file=Path("envsubst.bash"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )

        with patch.dict(os.environ, {"FALLBACK_VAR": "internal_rendered"}):
            # When shutil.which("bash") or shutil.which("envsubst") returns None
            with patch("shutil.which", return_value=None):
                out = render_template(
                    engine_config=engine_config,
                    drift_root=self.drift_root,
                    template_file_path=template_path
                )
                self.assertEqual(out, "Value: internal_rendered")

            # When missing variable, internal fallback raises RenderError
            with patch("shutil.which", return_value=None):
                bad_template = self.drift_root / "bad.envst.txt"
                bad_template.write_text("Value: ${UNKNOWN_VAR_XYZ}", encoding="utf-8")
                with self.assertRaises(RenderError):
                    render_template(
                        engine_config=engine_config,
                        drift_root=self.drift_root,
                        template_file_path=bad_template
                    )

    def test_render_template_internal_var_engine(self) -> None:
        """Verifies an internal render engine (e.g. [render.var] with render_command='internal')."""
        from drift.render_core import render_template

        template_path = self.drift_root / "config.var.toml"
        template_path.write_text("""
        theme = "$APP_THEME"
        port = ${APP_PORT}
        """, encoding="utf-8")

        engine_config = RenderEngineConfig(
            name="var",
            suffix="var",
            render_command="internal"
        )
        self.assertTrue(engine_config.is_internal)
        self.assertFalse(engine_config.is_disabled)

        with patch.dict(os.environ, {"APP_THEME": "dracula", "APP_PORT": "9000"}):
            out = render_template(
                engine_config=engine_config,
                drift_root=self.drift_root,
                template_file_path=template_path
            )
            self.assertIn('theme = "dracula"', out)
            self.assertIn('port = 9000', out)


if __name__ == "__main__":
    unittest.main()
