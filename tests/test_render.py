import os
import tempfile
import unittest
import subprocess
from src.workspace_config import RenderEngineConfig, WorkspaceConfig
from src.render_core import render_template, render_template_to_file
from src.dependency import (
    find_engine_for_file,
    strip_engine_suffix,
    resolve_dependencies,
    check_cyclic_dependencies,
    render_input_templates,
)


class TestRenderEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_template_success_with_input_file(self) -> None:
        # Create an input file (shell script defining a variable)
        input_path = os.path.join(self.temp_dir.name, "env.sh")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='drift_rendered_value'\n")

        # Create a template file
        template_path = os.path.join(self.temp_dir.name, "template.sh")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("echo -n $MY_TEST_VAR")

        # Set up engine config
        engine_config = RenderEngineConfig(
            name="bash_env_engine",
            input_file="env.sh",
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )

        # Call render_template with explicit input file path and drift_root
        output = render_template(
            engine_config=engine_config,
            drift_root=self.temp_dir.name,
            template_file_path=template_path,
            input_file_path=input_path
        )
        self.assertEqual(output, "drift_rendered_value")

    def test_render_template_missing_template_raises_file_not_found(self) -> None:
        engine_config = RenderEngineConfig(
            name="cat_engine",
            input_file="unused.txt",
            suffix="txt",
            # We must include %i and %s to pass validation
            render_command="cat %s # %i"
        )
        non_existent_path = os.path.join(self.temp_dir.name, "non_existent.txt")
        with self.assertRaises(FileNotFoundError):
            render_template(
                engine_config=engine_config,
                drift_root=self.temp_dir.name,
                template_file_path=non_existent_path,
                input_file_path="unused.txt"
            )

    def test_render_template_resolved_input_file_missing_raises_file_not_found(self) -> None:
        template_path = os.path.join(self.temp_dir.name, "template.txt")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("Some template content")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file="non_existent_env.sh",
            suffix="sh",
            render_command="bash -c 'source %i && cat %s'"
        )
        # Calling without passing explicit input_file_path will try to resolve relative to 'config' under drift_root
        with self.assertRaises(FileNotFoundError):
            render_template(
                engine_config=engine_config,
                drift_root=self.temp_dir.name,
                template_file_path=template_path
            )

    def test_render_template_missing_input_raises_value_error_if_unresolved(self) -> None:
        template_path = os.path.join(self.temp_dir.name, "template.txt")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("Some template content")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file="", # Empty input file
            suffix="sh",
            render_command="bash -c 'source %i && cat %s'"
        )
        with self.assertRaises(ValueError):
            render_template(
                engine_config=engine_config,
                drift_root=self.temp_dir.name,
                template_file_path=template_path
            )

    def test_render_template_missing_placeholders_raises_value_error(self) -> None:
        template_path = os.path.join(self.temp_dir.name, "template.txt")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("Some content")

        # Missing %i placeholder
        engine_config_missing_i = RenderEngineConfig(
            name="missing_i",
            input_file="unused.txt",
            suffix="txt",
            render_command="cat %s"
        )
        with self.assertRaises(ValueError) as ctx:
            render_template(
                engine_config=engine_config_missing_i,
                drift_root=self.temp_dir.name,
                template_file_path=template_path,
                input_file_path=template_path
            )
        self.assertIn("must contain '%i' placeholder", str(ctx.exception))

        # Missing %s placeholder
        engine_config_missing_s = RenderEngineConfig(
            name="missing_s",
            input_file="unused.txt",
            suffix="txt",
            render_command="cat %i"
        )
        with self.assertRaises(ValueError) as ctx:
            render_template(
                engine_config=engine_config_missing_s,
                drift_root=self.temp_dir.name,
                template_file_path=template_path,
                input_file_path=template_path
            )
        self.assertIn("must contain '%s' placeholder", str(ctx.exception))

    def test_render_template_failure_raises_runtime_error(self) -> None:
        template_path = os.path.join(self.temp_dir.name, "template.txt")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("Some content")
        input_path = os.path.join(self.temp_dir.name, "unused.sh")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write("echo 'This won't be used'")

        engine_config = RenderEngineConfig(
            name="failing_engine",
            input_file="unused.sh",
            suffix="sh",
            # We must include both placeholders to pass resolve_render_template_args
            render_command="false # %i %s"
        )
        with self.assertRaises(RuntimeError) as ctx:
            render_template(
                engine_config=engine_config,
                drift_root=self.temp_dir.name,
                template_file_path=template_path,
                input_file_path=input_path
            )
        self.assertIn("Render command failed with exit code", str(ctx.exception))

    def test_render_template_input_file_resolution(self) -> None:
        # Create template file
        template_path = os.path.join(self.temp_dir.name, "template.sh")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("echo -n $MY_TEST_VAR")

        # 1. Test resolving directly if input_file exists as absolute path
        input_path = os.path.join(self.temp_dir.name, "my_env_file.sh")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='resolved_value'")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=input_path, # Using absolute path
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )
        # Should auto-resolve because input_file is absolute/exists
        output = render_template(
            engine_config=engine_config,
            drift_root=self.temp_dir.name,
            template_file_path=template_path
        )
        self.assertEqual(output, "resolved_value")

    def test_render_template_relative_input_file_resolution(self) -> None:
        # Create template file
        template_path = os.path.join(self.temp_dir.name, "template.sh")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("echo -n $MY_TEST_VAR")

        # Create temporary config folder and input file inside self.temp_dir.name (representing drift_root)
        config_dir = os.path.join(self.temp_dir.name, "config")
        os.makedirs(config_dir, exist_ok=True)
        config_input_file = "test_env_for_resolution.sh"
        config_input_path = os.path.join(config_dir, config_input_file)
        
        with open(config_input_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='config_resolved_value'")

        engine_config = RenderEngineConfig(
            name="env_engine",
            input_file=config_input_file, # Relative path
            suffix="sh",
            render_command="bash -c 'source %i && source %s'"
        )
        output = render_template(
            engine_config=engine_config,
            drift_root=self.temp_dir.name,
            template_file_path=template_path
        )
        self.assertEqual(output, "config_resolved_value")

    def test_render_template_to_file_creates_output(self) -> None:
        template_path = os.path.join(self.temp_dir.name, "template.txt")
        with open(template_path, "w", encoding="utf-8") as f:
            f.write("Hello World!")

        # Create a dummy input file
        input_path = os.path.join(self.temp_dir.name, "input.txt")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write("unused")

        engine_config = RenderEngineConfig(
            name="cat_engine",
            input_file="unused.txt",
            suffix="txt",
            # Command must contain %i and %s
            render_command="cat %s # %i"
        )

        output_path = os.path.join(self.temp_dir.name, "nested", "output.txt")
        render_template_to_file(
            engine_config=engine_config,
            drift_root=self.temp_dir.name,
            template_file_path=template_path,
            output_file_path=output_path,
            input_file_path=input_path
        )

        # Verify output exists and contains exact content
        self.assertTrue(os.path.exists(output_path))
        with open(output_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content.strip(), "Hello World!")


class TestDependencyResolver(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_find_engine_for_file(self) -> None:
        engines = [
            RenderEngineConfig(name="envsubst", input_file="env.bash", suffix="envst", render_command="cmd"),
            RenderEngineConfig(name="mustache", input_file="mustache.json", suffix="mustache", render_command="cmd")
        ]
        # Match by intermediate segment
        self.assertEqual(find_engine_for_file("mustache.envst.json", engines).name, "envsubst")
        # Match by terminal suffix
        self.assertEqual(find_engine_for_file("mustache.mustache", engines).name, "mustache")
        # No match
        self.assertIsNone(find_engine_for_file("static.json", engines))

    def test_strip_engine_suffix(self) -> None:
        self.assertEqual(strip_engine_suffix("mustache.envst.json", "envst"), "mustache.json")
        self.assertEqual(strip_engine_suffix("settings.mustache.json", "mustache"), "settings.json")
        self.assertEqual(strip_engine_suffix("mustache.envst", "envst"), "mustache")
        self.assertEqual(strip_engine_suffix("no_suffix.json", "envst"), "no_suffix.json")
        # Verify it only replaces the LAST occurrence of ".{suffix}."
        self.assertEqual(
            strip_engine_suffix("file.envst.extra.envst.json", "envst"),
            "file.envst.extra.json"
        )

    def test_resolve_dependencies(self) -> None:
        engines = [
            RenderEngineConfig(name="envsubst", input_file="env.bash", suffix="envst", render_command="cmd"),
            RenderEngineConfig(name="mustache", input_file="mustache.envst.json", suffix="mustache", render_command="cmd")
        ]
        deps = resolve_dependencies(engines)
        self.assertEqual(deps, {"envsubst": None, "mustache": "envsubst"})

        # Self-dependency should be mapped to None (treated as static)
        self_dep_engines = [
            RenderEngineConfig(name="envsubst", input_file="envsubst.envst.bash", suffix="envst", render_command="cmd")
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
        config_dir = os.path.join(self.temp_dir.name, "config")
        os.makedirs(config_dir, exist_ok=True)

        env_file_path = os.path.join(config_dir, "env.sh")
        with open(env_file_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='templated_env_value'")

        mustache_template_path = os.path.join(config_dir, "mustache.envst.json")
        with open(mustache_template_path, "w", encoding="utf-8") as f:
            f.write("echo '{\"var\": \"'$MY_TEST_VAR'\"}'")

        # Define the engines
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="env.sh",
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )

        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file="mustache.envst.json",
            suffix="mustache",
            render_command="cat %s # %i"
        )

        engines = [envsubst_engine, mustache_engine]

        # Call render_input_templates
        render_input_templates(engines, self.temp_dir.name)

        # Verify output file render/config/mustache.json exists and contains correct rendered json
        expected_output_path = os.path.join(self.temp_dir.name, "render", "config", "mustache.json")
        self.assertTrue(os.path.exists(expected_output_path))
        with open(expected_output_path, "r", encoding="utf-8") as f:
            rendered_json = f.read().strip()
        self.assertEqual(rendered_json, '{"var": "templated_env_value"}')

        # Config inputs are updated with the rendered paths
        self.assertEqual(envsubst_engine.input_file, env_file_path)
        self.assertEqual(mustache_engine.input_file, expected_output_path)

    def test_render_input_templates_missing_file_raises_error(self) -> None:
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="non_existent.sh",
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )
        with self.assertRaises(FileNotFoundError):
            render_input_templates([envsubst_engine], self.temp_dir.name)

    def test_multi_level_dependency_raises_error(self) -> None:
        engines = [
            RenderEngineConfig(name="engine_c", input_file="c.sh", suffix="c_suf", render_command="cmd"),
            RenderEngineConfig(name="engine_b", input_file="b.c_suf.sh", suffix="b_suf", render_command="cmd"),
            RenderEngineConfig(name="engine_a", input_file="a.b_suf.sh", suffix="a_suf", render_command="cmd")
        ]
        with self.assertRaises(ValueError) as ctx:
            render_input_templates(engines, self.temp_dir.name)
        self.assertIn("Multi-level dependency chain detected", str(ctx.exception))

    def test_render_input_templates_custom_render_directory(self) -> None:
        # Setup config files:
        config_dir = os.path.join(self.temp_dir.name, "config")
        os.makedirs(config_dir, exist_ok=True)

        env_file_path = os.path.join(config_dir, "env.sh")
        with open(env_file_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='custom_val'")

        mustache_template_path = os.path.join(config_dir, "mustache.envst.json")
        with open(mustache_template_path, "w", encoding="utf-8") as f:
            f.write("echo '{\"var\": \"'$MY_TEST_VAR'\"}'")

        # Engines
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="env.sh",
            suffix="envst",
            render_command="bash -c 'source %i && source %s'"
        )
        mustache_engine = RenderEngineConfig(
            name="mustache",
            input_file="mustache.envst.json",
            suffix="mustache",
            render_command="cat %s # %i"
        )
        engines = [envsubst_engine, mustache_engine]

        # Use WorkspaceConfig with a custom render directory name
        workspace_config = WorkspaceConfig(render_directory="my_custom_render_sandbox")

        render_input_templates(engines, self.temp_dir.name, workspace_config)

        # Expected output should reside inside "my_custom_render_sandbox/config/"
        expected_output_path = os.path.join(self.temp_dir.name, "my_custom_render_sandbox", "config", "mustache.json")
        self.assertTrue(os.path.exists(expected_output_path))
        with open(expected_output_path, "r", encoding="utf-8") as f:
            rendered_json = f.read().strip()
        self.assertEqual(rendered_json, '{"var": "custom_val"}')

    def test_render_input_templates_absolute_template_path(self) -> None:
        config_dir = os.path.join(self.temp_dir.name, "config")
        os.makedirs(config_dir, exist_ok=True)

        env_file_path = os.path.join(config_dir, "env.sh")
        with open(env_file_path, "w", encoding="utf-8") as f:
            f.write("export MY_TEST_VAR='abs_val'")

        # Create the template with an absolute path
        abs_template_path = os.path.join(config_dir, "abs_mustache.envst.json")
        with open(abs_template_path, "w", encoding="utf-8") as f:
            f.write("echo '{\"var\": \"'$MY_TEST_VAR'\"}'")

        # Define the engines, setting mustache engine's input file as an absolute path
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="env.sh",
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
        render_input_templates(engines, self.temp_dir.name)

        # Output should be stripped from abs_mustache.envst.json -> abs_mustache.json
        expected_output_path = os.path.join(self.temp_dir.name, "render", "config", "abs_mustache.json")
        self.assertTrue(os.path.exists(expected_output_path))
        with open(expected_output_path, "r", encoding="utf-8") as f:
            rendered_json = f.read().strip()
        self.assertEqual(rendered_json, '{"var": "abs_val"}')


if __name__ == "__main__":
    unittest.main()
