import os
import tempfile
import unittest
from drift.toml_parser import (
    parse_toml,
    _parse_toml_fallback,
    parse_toml_value,
    split_array_elements,
)
from drift.workspace_config import (
    WorkspaceConfig,
   load_workspace_config,
)
from drift.package_config import (
    PackageConfig,
    locate_package_config_file_static,
    load_package_config_static,
    load_package_config_from_dir,
    get_package_config_file_info,
    PackageConfigFileInfo,
)


class TestConfigParser(unittest.TestCase):
    def setUp(self) -> None:
        # We will run standard parser tests on both the main parse_toml
        # and explicitly on the fallback parser to guarantee both work identically.
        self.parsers = [parse_toml, _parse_toml_fallback]

    def test_parse_toml_value(self) -> None:
        # String
        self.assertEqual(parse_toml_value('"hello"'), "hello")
        self.assertEqual(parse_toml_value("'world'"), "world")
        self.assertEqual(parse_toml_value('"escaped \\" quote"'), 'escaped " quote')
        self.assertEqual(parse_toml_value('"new\\nline"'), "new\nline")

        # Boolean
        self.assertEqual(parse_toml_value('true'), True)
        self.assertEqual(parse_toml_value('FALSE'), False)

        # Integer & Float
        self.assertEqual(parse_toml_value('42'), 42)
        self.assertEqual(parse_toml_value('-12'), -12)
        self.assertEqual(parse_toml_value('3.14'), 3.14)
        self.assertEqual(parse_toml_value('-0.5'), -0.5)

        # Fallback
        self.assertEqual(parse_toml_value('unquoted_str'), 'unquoted_str')

    def test_split_array_elements(self) -> None:
        self.assertEqual(split_array_elements('"a", "b", "c"'), ['"a"', '"b"', '"c"'])
        self.assertEqual(split_array_elements('"a, b", "c"'), ['"a, b"', '"c"'])
        self.assertEqual(split_array_elements("'a', 'b'"), ["'a'", "'b'"])

    def test_parse_toml_array_value(self) -> None:
        self.assertEqual(parse_toml_value('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_toml_value('[]'), [])

    def test_parse_toml_simple(self) -> None:
        toml_str = """
        # Global Comment
        key = "value"  # Inline comment
        number = 42
        enabled = true
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertEqual(data.get("key"), "value")
            self.assertEqual(data.get("number"), 42)
            self.assertEqual(data.get("enabled"), True)

    def test_parse_toml_with_tables(self) -> None:
        toml_str = """
        [workspace]
        render_directory = "my_render"
        install_directory = "my_install"

        [packages.enable]
        shell = true
        nvim = false
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("workspace", data)
            self.assertEqual(data["workspace"]["render_directory"], "my_render")
            self.assertEqual(data["workspace"]["install_directory"], "my_install")
            self.assertIn("packages", data)
            self.assertEqual(data["packages"]["enable"]["shell"], True)
            self.assertEqual(data["packages"]["enable"]["nvim"], False)

    def test_parse_toml_multiline_array(self) -> None:
        toml_str = """
        [package]
        name = "test_pkg"
        fully_controlled_dirs = [
            "dir1", # Comment inside
            "dir2"  # Another comment
        ]
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("package", data)
            self.assertEqual(data["package"]["name"], "test_pkg")
            self.assertEqual(data["package"]["fully_controlled_dirs"], ["dir1", "dir2"])

    def test_parse_toml_dotted_tables(self) -> None:
        toml_str = """
        [table.subtable]
        val = "nested"
        """
        for parser in self.parsers:
            data = parser(toml_str)
            self.assertIn("table", data)
            self.assertIn("subtable", data["table"])
            self.assertEqual(data["table"]["subtable"]["val"], "nested")


class TestConfigClasses(unittest.TestCase):
    def test_workspace_config_defaults(self) -> None:
        config = WorkspaceConfig()
        self.assertEqual(config.render_directory, "render")
        self.assertEqual(config.install_directory, "install")
        self.assertEqual(config.backup_directory, "backup")
        self.assertEqual(config.default_target_directory, os.path.expanduser("~"))
        self.assertEqual(config.packages, {})

    def test_workspace_config_from_dict(self) -> None:
        data = {
            "workspace": {
                "render_directory": "custom_render",
                "install_directory": "custom_install",
                "backup_directory": "custom_backup",
                "default_target_directory": "/etc"
            },
            "packages": {
                "enable": {
                    "shell": "true",
                    "nvim": True,
                    "emacs": "false"
                }
             }
        }
        config = WorkspaceConfig.from_dict(data)
        self.assertEqual(config.render_directory, "custom_render")
        self.assertEqual(config.install_directory, "custom_install")
        self.assertEqual(config.backup_directory, "custom_backup")
        self.assertEqual(config.default_target_directory, "/etc")
        self.assertEqual(config.packages, {"shell": True, "nvim": True, "emacs": False})

    def test_workspace_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            WorkspaceConfig(render_directory="").validate()
        with self.assertRaises(TypeError):
            WorkspaceConfig(packages="not_a_dict").validate() # type: ignore

    def test_package_config_from_dict(self) -> None:
        data = {
            "package": {
                "name": "my_pkg",
                "install_method": "stow"
            }
        }
        config = PackageConfig.from_dict(data, default_name="fallback_name")
        self.assertEqual(config.name, "my_pkg")

        data_no_name = {
            "package": {
                "install_method": "stow"
            }
        }
        config = PackageConfig.from_dict(data_no_name, default_name="fallback_name")
        self.assertEqual(config.name, "fallback_name")

        with self.assertRaises(ValueError):
            PackageConfig.from_dict(data_no_name)

    def test_package_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            PackageConfig(name="").validate()
        with self.assertRaises(ValueError):
            PackageConfig(name="foo", install_method="invalid").validate()
        with self.assertRaises(TypeError):
            PackageConfig(name="foo", enable_render="yes").validate() # type: ignore


class TestConfigLoaders(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_workspace_config(self) -> None:
        os.makedirs(os.path.join(self.temp_dir.name, "config"), exist_ok=True)
        config_path = os.path.join(self.temp_dir.name, "config/drift.toml")
        
        # Test nonexistent file (raises FileNotFoundError)
        with self.assertRaises(FileNotFoundError):
            load_workspace_config(config_path)

        # Test valid file
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            render_directory = "sandbox"
            """)
        config = load_workspace_config(config_path)
        self.assertEqual(config.render_directory, "sandbox")
        # Verify absolute drift_root_path computation
        self.assertEqual(config.drift_root_path, os.path.abspath(self.temp_dir.name))

    def test_load_package_config(self) -> None:
        pkg_config_path = os.path.join(self.temp_dir.name, "package.toml")

        # Nonexistent without default_name raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_package_config_static(pkg_config_path)

        # Nonexistent with default_name raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_package_config_static(pkg_config_path, default_name="my_default")

        # Valid file
        with open(pkg_config_path, "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_actual_package"
            install_method = "copy"
            """)
        config = load_package_config_static(pkg_config_path)
        self.assertEqual(config.name, "my_actual_package")
        self.assertEqual(config.install_method, "copy")

    def test_find_package_config_file_and_load_from_dir(self) -> None:
        pkg_dir = os.path.join(self.temp_dir.name, "my_pkg_folder")
        os.makedirs(pkg_dir)

        # No config file exists yet (raises FileNotFoundError)
        self.assertIsNone(locate_package_config_file_static(pkg_dir))
        with self.assertRaises(FileNotFoundError):
            load_package_config_from_dir(pkg_dir, "my_pkg_folder")

        # Creating drift_package.toml (alternative name)
        alt_config_path = os.path.join(pkg_dir, "drift_package.toml")
        with open(alt_config_path, "w", encoding="utf-8") as f:
            f.write("""
            [package]
            install_method = "copy"
            """)
        self.assertEqual(locate_package_config_file_static(pkg_dir), alt_config_path)
        
        config = load_package_config_from_dir(pkg_dir, "my_pkg_folder")
        self.assertEqual(config.name, "my_pkg_folder")
        self.assertEqual(config.install_method, "copy")
        # Static path fields are identical
        self.assertEqual(config.config_template_path, alt_config_path)
        self.assertEqual(config.config_rendered_path, alt_config_path)

    def test_get_package_config_file_info(self) -> None:
        from drift.workspace_config import RenderEngineConfig
        pkg_dir = os.path.join(self.temp_dir.name, "test_find_info")
        os.makedirs(pkg_dir)

        # Create WorkspaceConfig
        workspace_config = WorkspaceConfig(drift_root_path=self.temp_dir.name)
        engine = RenderEngineConfig(name="envsubst", input_file="env.sh", suffix="envst", render_command="cmd")
        workspace_config.render_engine_config = {"envsubst": engine}

        # 1. No files exist - should return None
        res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNone(res)

        # 2. package.envst.toml exists
        template_pkg_path = os.path.join(pkg_dir, "package.envst.toml")
        with open(template_pkg_path, "w", encoding="utf-8") as f:
            f.write("")
        res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "template")
        self.assertEqual(res.path, template_pkg_path)
        self.assertEqual(res.engine, engine)
        self.assertEqual(res.target_name, "package.toml")

        # 3. drift_package.envst.toml exists (takes precedence over package.envst.toml)
        template_drift_path = os.path.join(pkg_dir, "drift_package.envst.toml")
        with open(template_drift_path, "w", encoding="utf-8") as f:
            f.write("")
        res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "template")
        self.assertEqual(res.path, template_drift_path)
        self.assertEqual(res.engine, engine)
        self.assertEqual(res.target_name, "drift_package.toml")

        # 4. package.toml exists (takes precedence over templates)
        pkg_toml_path = os.path.join(pkg_dir, "package.toml")
        with open(pkg_toml_path, "w", encoding="utf-8") as f:
            f.write("")
        res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "static")
        self.assertEqual(res.path, pkg_toml_path)
        self.assertIsNone(res.engine)
        self.assertEqual(res.target_name, "package.toml")

        # 5. drift_package.toml exists (takes precedence over package.toml)
        drift_package_toml_path = os.path.join(pkg_dir, "drift_package.toml")
        with open(drift_package_toml_path, "w", encoding="utf-8") as f:
            f.write("")
        res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "static")
        self.assertEqual(res.path, drift_package_toml_path)
        self.assertIsNone(res.engine)
        self.assertEqual(res.target_name, "drift_package.toml")

    def test_package_toml_template_rendering(self) -> None:
        drift_root = self.temp_dir.name

        # 1. Create config/drift.toml
        config_dir = os.path.join(drift_root, "config")
        os.makedirs(config_dir, exist_ok=True)
        drift_toml_path = os.path.join(config_dir, "drift.toml")
        with open(drift_toml_path, "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            render_directory = "my_render"

            [render.envsubst]
            input_file = "env.sh"
            suffix = "envst"
            render_command = "bash -c 'source %i && envsubst < %s'"
            """)

        # 2. Create env.sh input file
        env_sh_path = os.path.join(config_dir, "env.sh")
        with open(env_sh_path, "w", encoding="utf-8") as f:
            f.write("export MY_PKG_METHOD='copy'\nexport MY_PKG_SUDO='true'")

        # Load WorkspaceConfig
        workspace_config = load_workspace_config(drift_toml_path)
        self.assertEqual(workspace_config.drift_root_path, os.path.abspath(drift_root))

        # 3. Create package template: src/my_pkg/package.envst.toml
        pkg_dir = os.path.join(drift_root, "src", "my_pkg")
        os.makedirs(pkg_dir, exist_ok=True)
        pkg_template_path = os.path.join(pkg_dir, "package.envst.toml")
        with open(pkg_template_path, "w", encoding="utf-8") as f:
            f.write("""
            [package]
            name = "my_pkg"
            install_method = "$MY_PKG_METHOD"
            sudo = $MY_PKG_SUDO
            """)

        # 4. Resolve engines input file dependencies first (which resolves envsubst input_file to absolute env.sh path)
        from drift.dependency import render_input_templates
        render_input_templates(list(workspace_config.render_engine_configs.values()), workspace_config.drift_root_path)

        # 5. Load package config from directory (which should render package.envst.toml -> render/my_pkg/package.toml)
        pkg_config = load_package_config_from_dir(pkg_dir, "my_pkg", workspace_config)

        # Verify fields and values
        self.assertEqual(pkg_config.name, "my_pkg")
        self.assertEqual(pkg_config.install_method, "copy")
        self.assertEqual(pkg_config.sudo, True)

        # Verify path trackers are set correctly (and are different since it was rendered)
        expected_rendered_path = os.path.join(drift_root, "my_render", "my_pkg", "package.toml")
        self.assertEqual(pkg_config.config_template_path, pkg_template_path)
        self.assertEqual(pkg_config.config_rendered_path, expected_rendered_path)
        self.assertTrue(os.path.exists(expected_rendered_path))


class TestRenderEngineAndWorkspaceTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_engine_config_validation(self) -> None:
        from drift.workspace_config import RenderEngineConfig
        config = RenderEngineConfig(
            name="envsubst",
            input_file="envsubst.bash",
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        config.validate()

        with self.assertRaises(ValueError):
            RenderEngineConfig(name="", input_file="a", suffix="b", render_command="c").validate()

    def test_workspace_config_with_render_engines(self) -> None:
        from drift.workspace_config import WorkspaceConfig
        data = {
            "workspace": {
                "render_directory": "custom_render",
            },
            "render": {
                "envsubst": {
                    "input_file": "envsubst.bash",
                    "suffix": "envst",
                    "render_command": "bash -c 'source %i && envsubst < %s'"
                },
                "mustache": {
                    "input_file": "mustache.envst.json",
                    "suffix": "mustache",
                    "render_command": "mustache %i %s"
                }
            }
        }
        config = WorkspaceConfig.from_dict(data)
        self.assertIn("envsubst", config.render_engine_config)
        self.assertEqual(config.render_engine_config["envsubst"].suffix, "envst")
        self.assertIn("mustache", config.render_engine_configs)
        self.assertEqual(config.render_engine_configs["mustache"].input_file, "mustache.envst.json")

    def test_meta_rendering_drift_envst_toml(self) -> None:
        from drift.workspace_config import load_workspace_config
        # We set an env variable
        os.environ["MY_TEST_RENDER_DIR"] = "templated_render"
        os.environ["MY_TEST_INSTALL_DIR"] = "templated_install"

        os.makedirs(os.path.join(self.temp_dir.name, "config"), exist_ok=True)
        envst_toml_path = os.path.join(self.temp_dir.name, "config/drift.envst.toml")
        with open(envst_toml_path, "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            render_directory = "$MY_TEST_RENDER_DIR"
            install_directory = "${MY_TEST_INSTALL_DIR}"
            """)

        # Call load_workspace_config on the non-existent .toml, which should trigger rendering of .envst.toml
        toml_path = os.path.join(self.temp_dir.name, "config/drift.toml")
        config = load_workspace_config(toml_path)

        self.assertEqual(config.drift_root_path, os.path.abspath(self.temp_dir.name))
        self.assertEqual(config.render_directory, "templated_render")
        self.assertEqual(config.install_directory, "templated_install")


if __name__ == "__main__":
    unittest.main()
