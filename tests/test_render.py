import os
import tempfile
import unittest
from typing import cast

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import RenderEngineConfig, WorkspaceConfig
from drift.render_core import render_template, render_template_to_file
from drift.dependency import (
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


class TestRenderPackage(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_package_success_static_config(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.temp_dir.name

        # 1. Create config and env file
        config_dir = os.path.join(drift_root, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "env.sh"), "w", encoding="utf-8") as f:
            f.write("export MY_ENV_VAR='drift_render_test'\n")

        # 2. Setup WorkspaceConfig
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory="src",
            render_directory="render",
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="env.sh",
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        # 3. Create a package src directory
        pkg_dir = os.path.join(drift_root, "src", "my_pkg")
        os.makedirs(pkg_dir, exist_ok=True)

        # 4. Write static package config
        with open(os.path.join(pkg_dir, "package.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_pkg"
            enable_render = true
            """)

        # 5. Write static file and template file
        with open(os.path.join(pkg_dir, "static.txt"), "w", encoding="utf-8") as f:
            f.write("Static content")

        with open(os.path.join(pkg_dir, "templated.envst.txt"), "w", encoding="utf-8") as f:
            f.write("Rendered: $MY_ENV_VAR")

        # 6. Run render_package
        render_package(workspace_config, pkg_dir)

        # 7. Verify outputs in render/my_pkg/
        render_pkg_dir = os.path.join(drift_root, "render", "my_pkg")

        # Verify static file was copied
        self.assertTrue(os.path.exists(os.path.join(render_pkg_dir, "static.txt")))
        with open(os.path.join(render_pkg_dir, "static.txt"), "r") as f:
            self.assertEqual(f.read(), "Static content")

        # Verify template was rendered
        self.assertTrue(os.path.exists(os.path.join(render_pkg_dir, "templated.txt")))
        with open(os.path.join(render_pkg_dir, "templated.txt"), "r") as f:
            self.assertEqual(f.read(), "Rendered: drift_render_test")

        # Verify package.toml was copied to drift_package.toml since it is static
        self.assertTrue(os.path.exists(os.path.join(render_pkg_dir, PACKAGE_CONFIG_FILE_NAME)))

    def test_render_package_disabled(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig

        drift_root = self.temp_dir.name
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory="src",
            render_directory="render",
        )

        pkg_dir = os.path.join(drift_root, "src", "my_pkg")
        os.makedirs(pkg_dir, exist_ok=True)

        # package config with enable_render = false
        with open(os.path.join(pkg_dir, "package.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_pkg"
            enable_render = false
            """)

        with open(os.path.join(pkg_dir, "static.txt"), "w", encoding="utf-8") as f:
            f.write("Static content")

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify render dir does not contain the package since rendering is disabled
        render_pkg_dir = os.path.join(drift_root, "render", "my_pkg")
        self.assertFalse(os.path.exists(render_pkg_dir))

    def test_render_package_templated_config(self) -> None:
        from drift.render_package import render_package
        from drift.workspace_config import WorkspaceConfig, RenderEngineConfig

        drift_root = self.temp_dir.name

        config_dir = os.path.join(drift_root, "config")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "env.sh"), "w", encoding="utf-8") as f:
            f.write("export PKG_NAME='rendered_pkg_name'\n")

        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory="src",
            render_directory="render",
        )
        envsubst_engine = RenderEngineConfig(
            name="envsubst",
            input_file="env.sh",
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        workspace_config.render_engine_config = {"envsubst": envsubst_engine}

        pkg_dir = os.path.join(drift_root, "src", "my_pkg")
        os.makedirs(pkg_dir, exist_ok=True)

        # Write templated package config
        with open(os.path.join(pkg_dir, "package.envst.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "$PKG_NAME"
            enable_render = true
            """)

        # Run render_package
        render_package(workspace_config, pkg_dir)

        # Verify output in render/my_pkg/
        render_pkg_dir = os.path.join(drift_root, "render", "my_pkg")

        # package.toml was rendered (loaded from package.envst.toml) and renamed to drift_package.toml
        rendered_config_path = os.path.join(render_pkg_dir, PACKAGE_CONFIG_FILE_NAME)
        self.assertTrue(os.path.exists(rendered_config_path))
        with open(rendered_config_path, "r") as f:
            content = f.read()
        self.assertIn('name = "rendered_pkg_name"', content)

        # There shouldn't be any package.envst.toml in render dir
        self.assertFalse(os.path.exists(os.path.join(render_pkg_dir, "package.envst.toml")))

    def test_render_all_packages(self) -> None:
        from drift.render_package import render_all_packages
        from drift.workspace_config import WorkspaceConfig

        drift_root = self.temp_dir.name

        # Setup WorkspaceConfig
        # pkg_a is explicitly enabled (True)
        # pkg_b is explicitly disabled (False)
        # pkg_c is not listed, but packages_enable_default is True
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory="src",
            render_directory="render",
            packages_enable={
                "pkg_a": True,
                "pkg_b": False,
            },
            packages_enable_default=True
        )

        # Create package source folders under src/
        for pkg_name in ("pkg_a", "pkg_b", "pkg_c"):
            pkg_dir = os.path.join(drift_root, "src", pkg_name)
            os.makedirs(pkg_dir, exist_ok=True)
            with open(os.path.join(pkg_dir, "package.toml"), "w", encoding="utf-8") as f:
                f.write(f"""
                [package]
                name = "{pkg_name}"
                enable_render = true
                """)
            with open(os.path.join(pkg_dir, "file.txt"), "w", encoding="utf-8") as f:
                f.write(f"Content for {pkg_name}")

        # Run render_all_packages
        render_all_packages(workspace_config)

        # Verify pkg_a is rendered
        self.assertTrue(os.path.exists(os.path.join(drift_root, "render", "pkg_a", "file.txt")))
        # Verify pkg_b is NOT rendered
        self.assertFalse(os.path.exists(os.path.join(drift_root, "render", "pkg_b", "file.txt")))
        # Verify pkg_c is rendered (due to default=True)
        self.assertTrue(os.path.exists(os.path.join(drift_root, "render", "pkg_c", "file.txt")))

    def test_commit_render_repo(self) -> None:
        from drift.render_package import commit_render_repo
        from drift.workspace_config import WorkspaceConfig
        import subprocess

        drift_root = self.temp_dir.name
        workspace_config = WorkspaceConfig(
            drift_root_path=drift_root,
            source_directory="src",
            render_directory="render",
        )

        # 1. Create render directory
        render_dir = os.path.join(drift_root, "render")
        os.makedirs(render_dir, exist_ok=True)

        # 2. Initialize a git repository inside the render directory
        # Also, configure standard dummy git user for testing environments to avoid commit issues
        subprocess.run(["git", "init"], cwd=render_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=render_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=render_dir, capture_output=True, check=True)

        # 3. Write a file inside render directory
        test_file_path = os.path.join(render_dir, "test.txt")
        with open(test_file_path, "w", encoding="utf-8") as f:
            f.write("Hello render")

        # 4. Commit using commit_render_repo (unscoped)
        msg = "Test dynamic commit message"
        commit_render_repo(workspace_config, msg)

        # 5. Verify the commit message
        log_res = subprocess.run(
            ["git", "-C", render_dir, "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(log_res.stdout.strip(), msg)

        # 6. Run again on a clean repo (should return gracefully without error)
        commit_render_repo(workspace_config, "Should not commit anything")

        # 7. Test scoped commit to a specific package
        pkg_a_dir = os.path.join(render_dir, "pkg_a")
        pkg_b_dir = os.path.join(render_dir, "pkg_b")
        os.makedirs(pkg_a_dir, exist_ok=True)
        os.makedirs(pkg_b_dir, exist_ok=True)

        with open(os.path.join(pkg_a_dir, "file_a.txt"), "w", encoding="utf-8") as f:
            f.write("pkg_a file")
        with open(os.path.join(pkg_b_dir, "file_b.txt"), "w", encoding="utf-8") as f:
            f.write("pkg_b file")

        # Commit pkg_a specifically
        scoped_msg = "Commit pkg_a changes"
        commit_render_repo(workspace_config, scoped_msg, "pkg_a")

        # Verify only pkg_a was committed
        status_res = subprocess.run(
            ["git", "-C", render_dir, "status", "--porcelain"],
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
            ["git", "-C", render_dir, "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(log_scoped.stdout.strip(), scoped_msg)


if __name__ == "__main__":
    unittest.main()
