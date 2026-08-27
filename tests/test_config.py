import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import cast, Any
from drift.constants import (
    CONFIG_DIR_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME_LIST,
    set_test_mode,
)
from drift.toml_utils import (
    parse_toml,
    _parse_toml_fallback,
    parse_toml_value,
    split_array_elements,
)
from drift.workspace_config import (
    WorkspaceConfig,
    RenderEngineConfig,
    RenderSourceMatch,
    load_workspace_config,
)
from drift.package_config import (
    PackageConfig,
    PackageHooks,
    locate_load_package_config_file_static,
    load_package_config_rendered,
    load_package_config_from_source_dir,
    get_package_config_file_info,
    PackageConfigFileInfo,
)
from tests.test_utils import add_envst

package_config_template_name = add_envst(PACKAGE_CONFIG_FILE_NAME)

# Enable test mode for the duration of these tests
set_test_mode(True)

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
        self.assertEqual(config.render_directory, Path("render"))
        self.assertEqual(config.install_directory, Path("install"))
        self.assertEqual(config.backup_directory, Path("backup"))
        self.assertEqual(config.default_target_directory, Path("~").expanduser())
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
        self.assertEqual(config.render_directory, Path("custom_render"))
        self.assertEqual(config.install_directory, Path("custom_install"))
        self.assertEqual(config.backup_directory, Path("custom_backup"))
        self.assertEqual(config.default_target_directory, Path("/etc"))
        self.assertEqual(config.packages, {"shell": True, "nvim": True, "emacs": False})

    def test_workspace_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            WorkspaceConfig(render_directory=Path("")).validate()
        with self.assertRaises(TypeError):
            WorkspaceConfig(packages_enable="not_a_dict").validate() # type: ignore

    def test_workspace_config_missing_packages_enable_raises(self) -> None:
        # 1. Missing [packages] entirely
        with self.assertRaises(ValueError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}})
        self.assertIn("Missing '[packages.enable]'", str(cm.exception))

        # 2. Obsolete flat [packages] without nested enable
        with self.assertRaises(ValueError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}, "packages": {"pkg_a": True}})
        self.assertIn("Missing '[packages.enable]'", str(cm.exception))

        # 3. [packages.enable] is not a dict
        with self.assertRaises(TypeError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}, "packages": {"enable": "not_a_table"}})
        self.assertIn("'[packages.enable]' must be a TOML table", str(cm.exception))

    def test_find_source_file_for_rendered_names(self) -> None:
        """Verifies find_source_file_for_rendered_names correctly identifies static and template source files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir).resolve()
            
            # Setup engines
            engine = RenderEngineConfig(name="envsubst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
            config = WorkspaceConfig(render_engine_config={"envsubst": engine})
            
            targets = ["config.toml", "settings.json"]
            
            # 1. Neither exists
            self.assertIsNone(config.find_source_file_for_rendered_names(directory, targets))
            
            # 2. Template form 1 exists (config.toml.envst)
            p1 = directory / "config.toml.envst"
            p1.touch()
            match = config.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p1, engine=engine, target_name="config.toml"))
            p1.unlink()

            # 3. Template form 2 exists (config.envst.toml)
            p2 = directory / "config.envst.toml"
            p2.touch()
            match = config.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p2, engine=engine, target_name="config.toml"))

            # 4. Static exists (takes precedence over template)
            p_static = directory / "config.toml"
            p_static.touch()
            match = config.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p_static, engine=None, target_name="config.toml"))

    def test_find_source_file_for_targets_with_directories(self) -> None:
        """Verifies find_source_file_for_rendered_names correctly identifies directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir).resolve()
            config = WorkspaceConfig(render_engine_config={})
            
            targets = ["my_folder", "other_folder"]
            
            # 1. Directory exists
            d1 = directory / "my_folder"
            d1.mkdir()
            match = config.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=d1, engine=None, target_name="my_folder"))
            
            # 2. File with same name takes precedence
            shutil.rmtree(d1)
            f1 = directory / "my_folder"
            f1.touch()
            match = config.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=f1, engine=None, target_name="my_folder"))

    def test_find_conflict_in_source_dir(self) -> None:
        """Verifies find_conflict_in_source_dir correctly identifies matches and blocks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            src_pkg_dir = root / "src" / "pkg"
            src_pkg_dir.mkdir(parents=True)
            
            engine = RenderEngineConfig(name="envst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
            config = WorkspaceConfig(render_engine_config={"envst": engine})
            
            # 1. Exact match (static file)
            f1 = src_pkg_dir / "dot-bashrc"
            f1.touch()
            match: Any = config.find_conflict_in_source_dir(src_pkg_dir, Path(".bashrc"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, f1)
            self.assertEqual(match.status, "match")
            f1.unlink()
            
            # 2. Exact match (template)
            t1 = src_pkg_dir / "dot-bashrc.envst"
            t1.touch()
            match: Any = config.find_conflict_in_source_dir(src_pkg_dir, Path(".bashrc"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, t1)
            self.assertEqual(match.status, "match")
            t1.unlink()
            
            # 3. Block (intermediate file)
            # We want to render to .config/nvim/init.vim
            # But src/pkg/dot-config is a file
            b1 = src_pkg_dir / "dot-config"
            b1.touch()
            match = config.find_conflict_in_source_dir(src_pkg_dir, Path(".config/nvim/init.vim"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, b1)
            self.assertEqual(match.status, "block")
            b1.unlink()
            
            # 4. No conflict
            match = config.find_conflict_in_source_dir(src_pkg_dir, Path(".config/nvim/init.vim"))
            self.assertIsNone(match)


    def test_package_config_from_dict(self) -> None:
        data = {
            "package": {
                "install_method": "stow"
            },
            "hooks": {
                "pre_source": "scripts/gen.sh",
                "timeout": 60
            }
        }
        config = PackageConfig.from_dict(data, package_name="my_pkg")
        self.assertEqual(config.name, "my_pkg")
        self.assertEqual(config.pre_source, "scripts/gen.sh")
        self.assertEqual(config.hook_timeout, 60)

        # Test string casting for timeout
        data_str_timeout = {
            "package": {
                "install_method": "stow"
            },
            "hooks": {
                "timeout": "45"
            }
        }
        config_str = PackageConfig.from_dict(data_str_timeout, package_name="my_pkg")
        self.assertEqual(config_str.hook_timeout, 45)

        data_no_name = {
            "package": {
                "install_method": "stow"
            }
        }
        config = PackageConfig.from_dict(data_no_name, package_name="fallback_name")
        self.assertEqual(config.name, "fallback_name")
        self.assertEqual(config.hook_timeout, 120)  # Default value

    def test_package_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            PackageConfig(name="").validate()
        with self.assertRaises(ValueError):
            PackageConfig(name="foo", install_method="invalid").validate()
        with self.assertRaises(TypeError):
            PackageConfig(name="foo", enable_render="yes").validate() # type: ignore
        with self.assertRaises(TypeError):
            PackageConfig(name="foo", hook_timeout="not_an_int").validate() # type: ignore
        with self.assertRaises(ValueError):
            PackageConfig(name="foo", hook_timeout=0).validate()
        with self.assertRaises(ValueError):
            PackageConfig(name="foo", hook_timeout=-10).validate()
        with self.assertRaises(TypeError):
            PackageConfig(name="foo", pre_source=123).validate() # type: ignore

    def test_package_hooks_dataclass(self) -> None:
        hooks = PackageHooks(
            pre_source="scripts/gen.sh",
            pre_install="scripts/pre.sh",
            post_install="scripts/post.sh",
            timeout=30
        )
        config = PackageConfig(name="test_pkg", hooks=hooks)
        self.assertEqual(config.hooks.pre_source, "scripts/gen.sh")
        self.assertEqual(config.pre_source, "scripts/gen.sh")
        self.assertEqual(config.hook_timeout, 30)
        self.assertIs(config.hooks.package_config, config)

        # Direct property modification forwards to hooks
        config.post_render = "scripts/render.sh"
        self.assertEqual(config.hooks.post_render, "scripts/render.sh")

    def test_load_package_config_with_hooks_table(self) -> None:
        """Verifies parsing package configuration with dedicated [hooks] table."""
        toml_dict = {
            "package": {
                "install_method": "copy",
                "target_directory": "~/.config/test"
            },
            "hooks": {
                "pre_source": "scripts/gen.sh",
                "pre_install": "scripts/pre_install.sh",
                "post_install": "scripts/post_install.sh",
                "pre_update": "scripts/pre_update.sh",
                "post_update": "scripts/post_update.sh",
                "pre_uninstall": "scripts/pre_uninstall.sh",
                "post_uninstall": "scripts/post_uninstall.sh",
                "post_render": "scripts/post_render.sh",
                "health": "scripts/health_check.sh",
                "timeout": 45
            }
        }
        config = PackageConfig.from_dict(toml_dict, package_name="pkg_with_hooks")
        self.assertEqual(config.name, "pkg_with_hooks")
        self.assertEqual(config.install_method, "copy")
        self.assertEqual(config.hooks.pre_source, "scripts/gen.sh")
        self.assertEqual(config.hooks.pre_install, "scripts/pre_install.sh")
        self.assertEqual(config.hooks.post_install, "scripts/post_install.sh")
        self.assertEqual(config.hooks.pre_update, "scripts/pre_update.sh")
        self.assertEqual(config.hooks.post_update, "scripts/post_update.sh")
        self.assertEqual(config.hooks.pre_uninstall, "scripts/pre_uninstall.sh")
        self.assertEqual(config.hooks.post_uninstall, "scripts/post_uninstall.sh")
        self.assertEqual(config.hooks.post_render, "scripts/post_render.sh")
        self.assertEqual(config.hooks.health, "scripts/health_check.sh")
        self.assertEqual(config.hooks.timeout, 45)
        self.assertEqual(config.hook_timeout, 45)

    def test_package_hooks_check_hook_files(self) -> None:
        """Verifies check_hook_files validates existence and regular file status of configured hook files."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            scripts_dir = base / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "pre_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (scripts_dir / "post_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            hooks = PackageHooks(
                pre_install="scripts/pre_install.sh",
                post_install="scripts/post_install.sh"
            )
            # 1. Valid hook files pass
            hooks.check_hook_files(base)

            # 2. Missing hook file raises FileNotFoundError
            hooks.post_update = "scripts/missing.sh"
            with self.assertRaises(FileNotFoundError) as cm:
                hooks.check_hook_files(base)
            self.assertIn("missing.sh", str(cm.exception))

            # 3. Hook path pointing to directory raises ValueError
            (scripts_dir / "dir_hook").mkdir()
            hooks.post_update = "scripts/dir_hook"
            with self.assertRaises(ValueError) as cm:
                hooks.check_hook_files(base)
            self.assertIn("not a regular file", str(cm.exception))

            # 4. Filtered hook_names ignores unrequested broken hooks
            (scripts_dir / "pre_uninstall.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            hooks.pre_uninstall = "scripts/pre_uninstall.sh"
            # Checking only pre_uninstall passes even though post_update is broken
            hooks.check_hook_files(base, hook_names=["pre_uninstall"])

    def test_is_package_config_file(self) -> None:
        """Verifies PackageConfig.is_package_config_file checks template or rendered path correctly."""
        config = PackageConfig(name="my_pkg",
                               source_files=[
                                   Path("/src/my_pkg/drift_package.toml"),
                                   Path("/src/my_pkg/drift_package.local.toml"),
                               ])
        self.assertTrue(config.is_package_config_file(Path("/src/my_pkg/drift_package.toml")))
        self.assertFalse(config.is_package_config_file(Path("/render/my_pkg/package.toml")))
        self.assertTrue(config.is_package_config_file(Path("/src/my_pkg/drift_package.local.toml")))
        self.assertFalse(config.is_package_config_file(Path("/other/file.toml")))

    def test_get_discovered_packages(self) -> None:
        """Verifies WorkspaceConfig.get_discovered_packages discovers, validates, and filters packages correctly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root_path = Path(temp_dir).resolve()
            
            # Create directories
            pkg_a_dir = root_path / "pkg_a"
            pkg_a_dir.mkdir()
            (pkg_a_dir / PACKAGE_CONFIG_FILE_NAME).touch()

            pkg_b_dir = root_path / "pkg_b"
            pkg_b_dir.mkdir()
            (pkg_b_dir / PACKAGE_CONFIG_FILE_NAME).touch()

            # pkg_c has no config file, should not be discovered
            pkg_c_dir = root_path / "pkg_c"
            pkg_c_dir.mkdir()

            config = WorkspaceConfig(
                packages_enable={"pkg_a": True, "pkg_b": False},
                packages_enable_default=False
            )

            # 1. No target_pkgs - should return only enabled discovered packages (pkg_a)
            discovered = config.get_discovered_packages(root_path, target_pkgs=None)
            self.assertEqual(discovered, ["pkg_a"])

            # 2. Target packages explicitly specified (even disabled pkg_b is returned)
            discovered_targets = config.get_discovered_packages(root_path, target_pkgs=["pkg_a", "pkg_b"])
            self.assertEqual(discovered_targets, ["pkg_a", "pkg_b"])

            # 3. Missing target package (raises ValueError)
            with self.assertRaises(ValueError):
                config.get_discovered_packages(root_path, target_pkgs=["pkg_a", "pkg_c"])

    def test_workspace_config_absolute_target_dir(self) -> None:
        """Verifies that WorkspaceConfig.validate raises ValueError if default_target_directory is relative."""
        # Using an absolute directory is valid
        WorkspaceConfig(default_target_directory=Path("/absolute/path")).validate()
        
        # Using a relative directory raises ValueError
        with self.assertRaises(ValueError) as ctx:
            WorkspaceConfig(default_target_directory=Path("relative/path")).validate()
        self.assertIn("default_target_directory must be an absolute path", str(ctx.exception))

    def test_unknown_option_warnings(self) -> None:
        """Verifies that unknown configuration options and sections trigger warnings."""
        from unittest.mock import patch

        # 1. Workspace unknown option warnings
        workspace_data_with_warnings = {
            "workspace": {
                "render_directory": "custom_render",
                "unknown_workspace_opt": "random_val"
            },
            "packages": {
                "enable": {}
            },
            "unknown_top_section": {
                "foo": "bar"
            },
            "render": {
                "mustache": {
                    "input_file": "input.json",
                    "suffix": "mustache",
                    "render_command": "mustache %i %s",
                    "unknown_render_opt": "blah"
                }
            }
        }
        
        with patch("drift.workspace_config.logger.warning") as mock_warn:
            WorkspaceConfig.from_dict(workspace_data_with_warnings)
            
            # Extract actual warning calls
            warn_messages = [call[0][0] for call in mock_warn.call_args_list]
            self.assertTrue(any("Unknown top-level config section: 'unknown_top_section'" in msg for msg in warn_messages))
            self.assertTrue(any("Unknown workspace option: 'unknown_workspace_opt'" in msg for msg in warn_messages))
            self.assertTrue(any("Unknown option under render.mustache: 'unknown_render_opt'" in msg for msg in warn_messages))

        # 2. PackageConfig unknown option warnings
        package_data_with_warnings = {
            "package": {
                "unknown_pkg_opt": "something"
            },
            "another_unknown_top_section": {
                "baz": "qux"
            }
        }

        with patch("drift.package_config.logger.warning") as mock_package_warn:
            PackageConfig.from_dict(package_data_with_warnings, package_name="my_pkg")
            
            package_warn_messages = [call[0][0] for call in mock_package_warn.call_args_list]
            self.assertTrue(any("Unknown top-level package config section: 'another_unknown_top_section'" in msg for msg in package_warn_messages))
            self.assertTrue(any("Unknown package option: 'unknown_pkg_opt'" in msg for msg in package_warn_messages))


class TestConfigLoaders(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_workspace_config(self) -> None:
        # Test nonexistent file (raises FileNotFoundError)
        with self.assertRaises(FileNotFoundError):
            load_workspace_config(self.drift_root)

        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / GLOBAL_CONFIG_FILE_NAME

        # Test valid file
        config_path.write_text("""
            [workspace]
            render_directory = "sandbox"

            [packages.enable]
            DEFAULT = false
            """, encoding="utf-8")
        config = load_workspace_config(self.drift_root)
        self.assertEqual(config.render_directory, Path("sandbox"))
        # Verify absolute drift_root_path computation
        self.assertEqual(config.drift_root_path, self.drift_root)

    def test_load_package_config(self) -> None:
        pkg_config_path = self.drift_root / PACKAGE_CONFIG_FILE_NAME

        # Nonexistent without package_name raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_package_config_rendered(pkg_config_path)

        # Nonexistent with package_name raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            load_package_config_rendered(pkg_config_path, package_name_override="my_default")

        # Valid file without name field (derived from package_name parameter)
        pkg_config_path.write_text("""
            [package]
            install_method = "copy"
            """, encoding="utf-8")
        config = load_package_config_rendered(pkg_config_path, package_name_override="my_actual_package")
        self.assertEqual(config.name, "my_actual_package")
        self.assertEqual(config.install_method, "copy")

    def test_locate_package_config_file_and_load_from_dir(self) -> None:
        pkg_dir = self.drift_root / "my_pkg_folder"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # No config file exists yet (raises FileNotFoundError)
        self.assertEqual(locate_load_package_config_file_static(pkg_dir, PACKAGE_CONFIG_FILE_NAME_LIST),
                         ({}, None))
        with self.assertRaises(FileNotFoundError):
            load_package_config_from_source_dir(pkg_dir)

        # Creating drift_package.toml (alternative name)
        alt_config_path = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        alt_config_path.write_text("""
            [package]
            install_method = "copy"
            """, encoding="utf-8")
        self.assertEqual(locate_load_package_config_file_static(pkg_dir, PACKAGE_CONFIG_FILE_NAME_LIST),
                        ({ "package" : { "install_method": "copy" } }, alt_config_path))
        
        config = load_package_config_from_source_dir(pkg_dir)
        self.assertEqual(config.name, "my_pkg_folder")
        self.assertEqual(config.install_method, "copy")

    def test_get_package_config_file_info(self) -> None:
        from drift.workspace_config import RenderEngineConfig
        pkg_dir = self.drift_root / "test_find_info"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Create WorkspaceConfig
        workspace_config = WorkspaceConfig(drift_root_path=self.drift_root)
        engine = RenderEngineConfig(name="envsubst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
        workspace_config.render_engine_config = {"envsubst": engine}

        # 1. No files exist - should return (None, None)
        base_res, local_res = get_package_config_file_info(pkg_dir, workspace_config)
        self.assertIsNone(base_res)
        self.assertIsNone(local_res)

        # 2. drift_package.envst.toml exists
        template_drift_path = pkg_dir / package_config_template_name
        template_drift_path.write_text("", encoding="utf-8")
        base_res, local_res = get_package_config_file_info(pkg_dir, workspace_config)
        res = cast(PackageConfigFileInfo, base_res)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "template")
        self.assertEqual(res.path, template_drift_path)
        self.assertEqual(res.engine, engine)
        self.assertIsNone(local_res)

        # 3. drift_package.toml exists (takes precedence over drift_package.envst.toml)
        drift_package_toml_path = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        drift_package_toml_path.write_text("", encoding="utf-8")
        base_res, local_res = get_package_config_file_info(pkg_dir, workspace_config)
        res = cast(PackageConfigFileInfo, base_res)
        self.assertIsNotNone(res)
        self.assertEqual(res.type, "static")
        self.assertEqual(res.path, drift_package_toml_path)
        self.assertIsNone(res.engine)
        self.assertIsNone(local_res)

    def test_package_toml_template_rendering(self) -> None:
        # 1. Create config/drift.toml
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        drift_toml_path = config_dir / GLOBAL_CONFIG_FILE_NAME
        drift_toml_path.write_text("""
            [workspace]
            render_directory = "my_render"

            [packages.enable]
            DEFAULT = true

            [render.envsubst]
            input_file = "env.sh"
            suffix = "envst"
            render_command = "bash -c 'source %i && envsubst < %s'"
            """, encoding="utf-8")

        # 2. Create env.sh input file
        env_sh_path = config_dir / "env.sh"
        env_sh_path.write_text("export MY_PKG_METHOD='copy'\nexport MY_PKG_SUDO='true'", encoding="utf-8")

        # Load WorkspaceConfig
        workspace_config = load_workspace_config(self.drift_root)
        self.assertEqual(workspace_config.drift_root_path, self.drift_root)

        # 3. Create package template: src/my_pkg/package.envst.toml
        pkg_dir = self.drift_root / "src" / "my_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_template_path = pkg_dir / package_config_template_name
        pkg_template_path.write_text("""
            [package]
            name = "my_pkg"
            install_method = "$MY_PKG_METHOD"
            sudo = $MY_PKG_SUDO
            """, encoding="utf-8")

        # 4. Resolve engines input file dependencies first (which resolves envsubst input_file to absolute env.sh path)
        from drift.render_input import render_input_templates
        render_input_templates(list(workspace_config.render_engine_configs.values()), workspace_config.drift_root_path)

        # 5. Load package config from directory (which should render package.envst.toml -> render/my_pkg/drift_package.toml)
        pkg_config = load_package_config_from_source_dir(pkg_dir, workspace_config)

        # Verify fields and values
        self.assertEqual(pkg_config.name, "my_pkg")
        self.assertEqual(pkg_config.install_method, "copy")
        self.assertEqual(pkg_config.sudo, True)

        # Verify that the expected rendered config file exists inside the render/ sandbox
        expected_rendered_path = self.drift_root / "my_render" / "my_pkg" / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(expected_rendered_path.is_file())

    def test_workspace_local_config_merge(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / GLOBAL_CONFIG_FILE_NAME
        local_path = config_dir / "drift.local.toml"

        config_path.write_text("""
            [workspace]
            render_directory = "my_render"
            install_directory = "my_install"

            [packages.enable]
            DEFAULT = false
            """, encoding="utf-8")

        local_path.write_text("""
            [workspace]
            install_directory = "overridden_install"
            """, encoding="utf-8")

        config = load_workspace_config(self.drift_root)
        self.assertEqual(config.render_directory, Path("my_render"))
        self.assertEqual(config.install_directory, Path("overridden_install"))

    def test_package_local_config_merge_without_workspace(self) -> None:
        pkg_dir = self.drift_root / "my_pkg_merge"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        base_config_path = pkg_dir / "drift_package.toml"
        base_config_path.write_text("""
            [package]
            name = "my_pkg_merge"
            install_method = "copy"
            sudo = false
            """, encoding="utf-8")

        local_config_path = pkg_dir / "drift_package.local.toml"
        local_config_path.write_text("""
            [package]
            install_method = "stow"
            sudo = true
            """, encoding="utf-8")

        # Passing workspace_config=None triggers static loading path
        pkg_config = load_package_config_from_source_dir(pkg_dir)
        self.assertEqual(pkg_config.name, "my_pkg_merge")
        self.assertEqual(pkg_config.install_method, "stow")
        self.assertEqual(pkg_config.sudo, True)

    def test_package_local_config_merge_with_workspace(self) -> None:
        # Create workspace config structure
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / GLOBAL_CONFIG_FILE_NAME
        config_path.write_text("""
            [workspace]
            render_directory = "my_render"

            [packages.enable]
            DEFAULT = true
            """, encoding="utf-8")
        workspace_config = load_workspace_config(self.drift_root)

        pkg_dir = self.drift_root / "src" / "my_pkg_merge_ws"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        base_config_path = pkg_dir / "drift_package.toml"
        base_config_path.write_text("""
            [package]
            name = "my_pkg_merge_ws"
            install_method = "copy"
            sudo = false
            """, encoding="utf-8")

        local_config_path = pkg_dir / "drift_package.local.toml"
        local_config_path.write_text("""
            [package]
            install_method = "stow"
            sudo = true
            """, encoding="utf-8")

        pkg_config = load_package_config_from_source_dir(pkg_dir, workspace_config)
        self.assertEqual(pkg_config.name, "my_pkg_merge_ws")
        self.assertEqual(pkg_config.install_method, "stow")
        self.assertEqual(pkg_config.sudo, True)

        # Ensure the combined file gets rendered correctly in render/ sandbox
        expected_rendered_path = self.drift_root / "my_render" / "my_pkg_merge_ws" / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(expected_rendered_path.is_file())

    def test_workspace_config_env_loading(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / GLOBAL_CONFIG_FILE_NAME
        local_path = config_dir / "drift.local.toml"

        config_path.write_text("""
            [workspace]
            render_directory = "my_render"

            [packages.enable]
            DEFAULT = false

            [env]
            TEST_DRIFT_VAR = "hello"
            TEST_DRIFT_OVERRIDE = "from_base"
            """, encoding="utf-8")

        local_path.write_text("""
            [env]
            TEST_DRIFT_OVERRIDE = "from_local"
            TEST_DRIFT_LOCAL_ONLY = "local_only"
            """, encoding="utf-8")

        # Ensure they are not in os.environ initially (or clean them up first)
        for var in ["TEST_DRIFT_VAR", "TEST_DRIFT_OVERRIDE", "TEST_DRIFT_LOCAL_ONLY"]:
            os.environ.pop(var, None)

        config = load_workspace_config(self.drift_root)
        
        # Verify stored in WorkspaceConfig object
        self.assertEqual(config.env.get("TEST_DRIFT_VAR"), "hello")
        self.assertEqual(config.env.get("TEST_DRIFT_OVERRIDE"), "from_local")
        self.assertEqual(config.env.get("TEST_DRIFT_LOCAL_ONLY"), "local_only")

        # Verify propagated to os.environ immediately
        self.assertEqual(os.environ.get("TEST_DRIFT_VAR"), "hello")
        self.assertEqual(os.environ.get("TEST_DRIFT_OVERRIDE"), "from_local")
        self.assertEqual(os.environ.get("TEST_DRIFT_LOCAL_ONLY"), "local_only")

        # Clean up
        for var in ["TEST_DRIFT_VAR", "TEST_DRIFT_OVERRIDE", "TEST_DRIFT_LOCAL_ONLY"]:
            os.environ.pop(var, None)


class TestRenderEngineAndWorkspaceTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_engine_config_validation(self) -> None:
        from drift.workspace_config import RenderEngineConfig
        config = RenderEngineConfig(
            name="envsubst",
            input_file=Path("envsubst.bash"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        config.validate()

        with self.assertRaises(ValueError):
            RenderEngineConfig(name="", input_file=Path("a"), suffix="b", render_command="c").validate()

        # Suffix cannot contain dots ('.')
        with self.assertRaises(ValueError) as ctx:
            RenderEngineConfig(
                name="invalid_suffix",
                input_file=Path("envsubst.bash"),
                suffix="envst.sh",
                render_command="bash -c 'source %i && envsubst < %s'"
            ).validate()
        self.assertIn("cannot contain dots", str(ctx.exception))

    def test_workspace_config_with_render_engines(self) -> None:
        from drift.workspace_config import WorkspaceConfig
        data = {
            "workspace": {
                "render_directory": "custom_render",
            },
            "packages": {
                "enable": {}
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
        self.assertEqual(config.render_engine_configs["mustache"].input_file, Path("mustache.envst.json"))

    def test_meta_rendering_drift_envst_toml(self) -> None:
        from drift.workspace_config import load_workspace_config
        # We set an env variable
        os.environ["MY_TEST_RENDER_DIR"] = "templated_render"
        os.environ["MY_TEST_INSTALL_DIR"] = "templated_install"

        os.makedirs(os.path.join(self.temp_dir.name, "config"), exist_ok=True)

        base, ext = os.path.splitext(GLOBAL_CONFIG_FILE_NAME)
        config_envst_name = base + ".envst" + ext
        envst_toml_path = os.path.join(self.temp_dir.name, os.path.join(CONFIG_DIR_NAME, config_envst_name))
        with open(envst_toml_path, "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            render_directory = "$MY_TEST_RENDER_DIR"
            install_directory = "${MY_TEST_INSTALL_DIR}"

            [packages.enable]
            DEFAULT = false
            """)

        # Call load_workspace_config on the non-existent .toml, which should trigger rendering of .envst.toml
        config = load_workspace_config(Path(self.temp_dir.name))

        self.assertEqual(config.drift_root_path, Path(self.temp_dir.name).resolve())
        self.assertEqual(config.render_directory, Path("templated_render"))
        self.assertEqual(config.install_directory, Path("templated_install"))

    def test_package_discovery_methods(self) -> None:
        """Verifies package discovery methods on WorkspaceConfig correctly find folders from source, render, and install dirs."""
        from drift.workspace_config import WorkspaceConfig
        with tempfile.TemporaryDirectory() as root_path:
            # Setup directories
            os.makedirs(os.path.join(root_path, "src", "pkg_src_a"), exist_ok=True)
            os.makedirs(os.path.join(root_path, "src", "pkg_src_b"), exist_ok=True)
            
            config = WorkspaceConfig(
                drift_root_path=Path(root_path),
                source_directory=Path("src"),
                render_directory=Path("render"),
                install_directory=Path("install")
            )

            # Test source dir discovery
            self.assertEqual(config.get_package_names_from_source_dir(), ["pkg_src_a", "pkg_src_b"])

            # Test is_package_enabled
            config.packages_enable = {"pkg_src_a": True, "pkg_src_b": False}
            config.packages_enable_default = False
            self.assertTrue(config.is_package_enabled("pkg_src_a"))
            self.assertFalse(config.is_package_enabled("pkg_src_b"))
            self.assertFalse(config.is_package_enabled("pkg_unlisted"))

            config.packages_enable_default = True
            self.assertTrue(config.is_package_enabled("pkg_unlisted"))

    def test_render_engine_strip_suffix(self) -> None:
        """Verifies RenderEngineConfig.strip_suffix strips engine suffix segment correctly from the filename."""
        from drift.workspace_config import RenderEngineConfig
        engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("env.sh"),
            suffix="envst",
            render_command="cmd"
        )
        # Ends with .envst
        self.assertEqual(engine.strip_suffix("dot-bashrc.envst"), "dot-bashrc")
        # In the middle (replaces only the last occurrence)
        self.assertEqual(engine.strip_suffix("all_proxy.envst.conf"), "all_proxy.conf")
        self.assertEqual(engine.strip_suffix("file.envst.envst.txt"), "file.envst.txt")
        # Non-matching remains unchanged
        self.assertEqual(engine.strip_suffix("normal_file.conf"), "normal_file.conf")

    def test_package_config_load_unload_package_envs(self) -> None:
        """Verifies PackageConfig.load_package_envs and unload_package_envs."""
        from drift.package_config import PackageConfig
        from drift.workspace_config import WorkspaceConfig

        config = WorkspaceConfig(drift_root_path=Path("/dummy/root"), default_target_directory=Path("/global/target"))
        pkg = PackageConfig(
            name="my_pkg",
            target_directory=Path("/custom/target"),
            install_method="copy"
        )

        # 1. Load with workspace config
        saved = pkg.load_package_envs(config)
        self.assertEqual(os.environ.get("drift_package_name"), "my_pkg")
        self.assertEqual(os.environ.get("drift_package_target_dir"), "/custom/target")
        self.assertEqual(os.environ.get("drift_package_source_dir"), str(config.source_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_package_render_dir"), str(config.render_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_package_install_dir"), str(config.install_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_install_method"), "copy")

        # 2. Unload
        pkg.unload_package_envs(saved)
        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_source_dir", os.environ)
        self.assertNotIn("drift_package_render_dir", os.environ)
        self.assertNotIn("drift_package_install_dir", os.environ)
        self.assertNotIn("drift_install_method", os.environ)

        # 4. Context manager usage with 'with'
        with pkg.package_envs(config):
            self.assertEqual(os.environ.get("drift_package_name"), "my_pkg")
            self.assertEqual(os.environ.get("drift_package_target_dir"), "/custom/target")
            self.assertEqual(os.environ.get("drift_package_source_dir"), str(config.source_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_package_render_dir"), str(config.render_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_package_install_dir"), str(config.install_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_install_method"), "copy")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_source_dir", os.environ)
        self.assertNotIn("drift_package_render_dir", os.environ)
        self.assertNotIn("drift_package_install_dir", os.environ)
        self.assertNotIn("drift_install_method", os.environ)

    def test_package_envs_resolution_with_custom_workspace_target_and_install_method(self) -> None:
        """Verifies environment variable resolution when workspace target != '~' and package has/has not explicit target."""
        from drift.package_config import PackageConfig
        from drift.workspace_config import WorkspaceConfig

        # Workspace with non-default target directory != '~' and non-default install method
        custom_global_target = Path("/opt/custom_drift_target")
        workspace_config = WorkspaceConfig(
            drift_root_path=Path("/dummy/root"),
            default_target_directory=custom_global_target,
            default_install_method="copy"
        )

        # 1. Package WITHOUT explicit target_directory and WITHOUT explicit install_method
        pkg_inherited = PackageConfig(name="pkg_inherited")
        with pkg_inherited.package_envs(workspace_config):
            self.assertEqual(os.environ.get("drift_package_name"), "pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_target_dir"), str(custom_global_target.expanduser()))
            self.assertEqual(os.environ.get("drift_package_source_dir"), "/dummy/root/src/pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_render_dir"), "/dummy/root/render/pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_install_dir"), "/dummy/root/install/pkg_inherited")
            self.assertEqual(os.environ.get("drift_install_method"), "copy")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_install_method", os.environ)

        # 2. Package WITH explicit target_directory and explicit install_method
        pkg_overridden = PackageConfig(
            name="pkg_overridden",
            target_directory=Path("/etc/custom_pkg_target"),
            install_method="stow"
        )
        with pkg_overridden.package_envs(workspace_config):
            self.assertEqual(os.environ.get("drift_package_name"), "pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_target_dir"), "/etc/custom_pkg_target")
            self.assertEqual(os.environ.get("drift_package_source_dir"), "/dummy/root/src/pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_render_dir"), "/dummy/root/render/pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_install_dir"), "/dummy/root/install/pkg_overridden")
            self.assertEqual(os.environ.get("drift_install_method"), "stow")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_install_method", os.environ)

        # 3. Package WITH explicit target_directory using home expansion (~)
        pkg_home = PackageConfig(
            name="pkg_home",
            target_directory=Path("~/.config/my_app")
        )
        with pkg_home.package_envs(workspace_config):
            self.assertEqual(os.environ.get("drift_package_name"), "pkg_home")
            self.assertEqual(os.environ.get("drift_package_target_dir"), str(Path("~/.config/my_app").expanduser()))
            self.assertEqual(os.environ.get("drift_package_source_dir"), "/dummy/root/src/pkg_home")
            self.assertEqual(os.environ.get("drift_package_render_dir"), "/dummy/root/render/pkg_home")
            self.assertEqual(os.environ.get("drift_package_install_dir"), "/dummy/root/install/pkg_home")
            self.assertEqual(os.environ.get("drift_install_method"), "copy")  # Inherited copy

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)


if __name__ == "__main__":
    unittest.main()
