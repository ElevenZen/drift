import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from typing import cast, Any
from drift.core.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME_LIST,
    DRIFT_INTERNAL_DIR_NAME,
    DEFAULT_HOOK_TIMEOUT,
    InstallMethod,
    set_test_mode,
)
from drift.utils.toml_utils import (
    parse_toml,
    _parse_toml_fallback,
    parse_toml_value,
    split_array_elements,
    get_first_from,
    get_nested_from,
    validate_known_keys,
)
from drift.core.exceptions import ConfigError
from drift.config.workspace_config import (
    WorkspaceConfig,
    WorkspaceSectionConfig,
    SettingsConfig,
    RenderEngineConfig,
    RenderEngineRegistry,
    RenderSourceMatch,
    load_workspace_config,
)
from drift.config.package_config import (
    PackageConfig,
    PackageHooks,
    PackageRequirements,
    load_package_config_rendered,
    load_package_config_from_source_dir,
    load_package_config_from_render_dir,
    load_package_config_for_install,
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

    def test_get_first_from(self) -> None:
        data = {"alias_b": "value_b", "disabled_flag": False}
        self.assertEqual(get_first_from(data, ["alias_a", "alias_b", "alias_c"]), "value_b")
        self.assertEqual(get_first_from(data, ["disabled_flag", "other"]), False)
        self.assertIsNone(get_first_from(data, ["nonexistent_a", "nonexistent_b"]))
        self.assertEqual(get_first_from(data, ["nonexistent"], default="fallback"), "fallback")
        self.assertEqual(get_first_from(None, ["key"], default="fallback"), "fallback")

        # Generator expression support (lazy, unmaterialized)
        key_gen = (k for k in ["missing_1", "alias_b", "unreachable"])
        self.assertEqual(get_first_from(data, key_gen), "value_b")

        # Verify lazy short-circuiting: generator is not consumed past the first match
        consumed = []
        def track_gen():
            for k in ["missing_1", "alias_b", "never_reached"]:
                consumed.append(k)
                yield k

        val = get_first_from(data, track_gen())
        self.assertEqual(val, "value_b")
        self.assertEqual(consumed, ["missing_1", "alias_b"])

    def test_get_nested_from(self) -> None:
        data = {
            "packages": {
                "enable": {
                    "pkg_a": True,
                    "pkg_b": False,
                },
                "flat_str": "value",
            }
        }

        # Basic retrieval with string dot-path and sequence path
        self.assertEqual(get_nested_from(data, "packages.enable.pkg_a"), True)
        self.assertEqual(get_nested_from(data, ["packages", "enable", "pkg_b"]), False)
        self.assertEqual(get_nested_from(data, "packages.enable"), {"pkg_a": True, "pkg_b": False})

        # Missing path with fallback default
        self.assertIsNone(get_nested_from(data, "packages.missing"))
        self.assertEqual(get_nested_from(data, "packages.missing", default="custom"), "custom")
        self.assertEqual(get_nested_from(None, "packages.enable", default="fallback"), "fallback")
        self.assertEqual(get_nested_from("not_a_dict", "packages.enable", default="fallback"), "fallback")

        # Missing required path raises ConfigError with default or custom context
        with self.assertRaises(ConfigError) as ctx:
            get_nested_from(data, "packages.nonexistent", required=True)
        self.assertIn("Missing '[packages.nonexistent]' section in configuration.", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            get_nested_from(data, "packages.nonexistent", required=True, context="custom target config")
        self.assertIn("Missing '[packages.nonexistent]' section in custom target config.", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            get_nested_from(None, "packages.enable", required=True, context="workspace configuration")
        self.assertIn("Missing '[packages.enable]' section in workspace configuration.", str(ctx.exception))

        # is_table validation
        self.assertEqual(get_nested_from(data, "packages.enable", is_table=True), {"pkg_a": True, "pkg_b": False})
        with self.assertRaises(ConfigError) as ctx:
            get_nested_from(data, "packages.flat_str", is_table=True)
        self.assertIn("'[packages.flat_str]' must be a TOML table", str(ctx.exception))

    def test_validate_known_keys(self) -> None:
        # None or non-mapping data does not raise
        validate_known_keys(None, ["a", "b"])
        validate_known_keys({}, ["a", "b"])
        validate_known_keys("not_a_dict", ["a", "b"])  # type: ignore[arg-type]

        # Valid keys do not raise
        validate_known_keys({"a": 1, "b": 2}, ["a", "b", "c"])

        # Single unknown key raises with default prefix
        with self.assertRaises(ConfigError) as ctx:
            validate_known_keys({"a": 1, "bad_key": 2}, ["a", "b"])
        self.assertIn("Unknown option: 'bad_key'", str(ctx.exception))

        # Single unknown key with context
        with self.assertRaises(ConfigError) as ctx:
            validate_known_keys({"bad_key": 1}, ["a"], context="[settings]")
        self.assertIn("Unknown option under [settings]: 'bad_key'", str(ctx.exception))

        # Multiple unknown keys reports all unknown keys
        with self.assertRaises(ConfigError) as ctx:
            validate_known_keys({"k1": 1, "k2": 2, "valid": 3}, ["valid"], context="[settings]")
        self.assertIn("'k1'", str(ctx.exception))
        self.assertIn("'k2'", str(ctx.exception))
        self.assertIn("Unknown option under [settings]:", str(ctx.exception))

        # Custom message_prefix and suffix
        with self.assertRaises(ConfigError) as ctx:
            validate_known_keys(
                {"bad1": 1, "bad2": 2},
                ["valid"],
                message_prefix="Unknown hook option in package [hooks]",
                suffix=" for package 'my_pkg'",
            )
        self.assertIn("Unknown hook option in package [hooks]:", str(ctx.exception))
        self.assertIn("'bad1'", str(ctx.exception))
        self.assertIn("'bad2'", str(ctx.exception))
        self.assertTrue(str(ctx.exception).endswith(" for package 'my_pkg'"))

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
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.drift_root = Path(cls.temp_dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_workspace_config_defaults(self) -> None:
        config = WorkspaceConfig(drift_root=self.drift_root)
        self.assertEqual(config.workspace.render_directory, Path("render"))
        self.assertEqual(config.workspace.install_directory, Path("install"))
        self.assertEqual(config.workspace.backup_directory, Path("backup"))
        self.assertEqual(config.workspace.default_target_directory, Path("~").expanduser())
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
        config = WorkspaceConfig.from_dict(data, drift_root=self.drift_root)
        self.assertEqual(config.workspace.render_directory, Path("custom_render"))
        self.assertEqual(config.workspace.install_directory, Path("custom_install"))
        self.assertEqual(config.workspace.backup_directory, Path("custom_backup"))
        self.assertEqual(config.workspace.default_target_directory, Path("/etc"))
        self.assertEqual(config.packages, {"shell": True, "nvim": True, "emacs": False})

    def test_workspace_config_validation(self) -> None:
        with self.assertRaises(ConfigError):
            WorkspaceConfig(drift_root=self.drift_root, workspace=WorkspaceSectionConfig(render_directory=Path(""))).validate()
        with self.assertRaises(ConfigError):
            WorkspaceConfig(drift_root=self.drift_root, workspace="not_a_workspace_config").validate() # type: ignore
        with self.assertRaises(ConfigError):
            WorkspaceConfig(drift_root=self.drift_root, packages_enable="not_a_dict").validate() # type: ignore

    def test_workspace_config_missing_packages_enable_raises(self) -> None:
        # 1. Missing [packages] entirely
        with self.assertRaises(ValueError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}}, drift_root=self.drift_root)
        self.assertIn("Missing '[packages.enable]'", str(cm.exception))

        # 2. Obsolete flat [packages] without nested enable
        with self.assertRaises(ValueError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}, "packages": {"pkg_a": True}}, drift_root=self.drift_root)
        self.assertIn("Missing '[packages.enable]'", str(cm.exception))

        # 3. [packages.enable] is not a dict
        with self.assertRaises(ConfigError) as cm:
            WorkspaceConfig.from_dict({"workspace": {}, "packages": {"enable": "not_a_table"}}, drift_root=self.drift_root)
        self.assertIn("'[packages.enable]' must be a TOML table", str(cm.exception))

    def test_find_source_file_for_rendered_names(self) -> None:
        """Verifies find_source_file_for_rendered_names correctly identifies static and template source files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir).resolve()
            
            # Setup engines
            engine = RenderEngineConfig(name="envsubst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
            config = WorkspaceConfig(drift_root=directory, render_engine_configs=RenderEngineRegistry({"envsubst": engine}))
            
            targets = ["config.toml", "settings.json"]
            
            # 1. Neither exists
            self.assertIsNone(config.render_engine_configs.find_source_file_for_rendered_names(directory, targets))
            
            # 2. Template form 1 exists (config.toml.envst)
            p1 = directory / "config.toml.envst"
            p1.touch()
            match = config.render_engine_configs.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p1, engine=engine, target_name="config.toml"))
            p1.unlink()

            # 3. Template form 2 exists (config.envst.toml)
            p2 = directory / "config.envst.toml"
            p2.touch()
            match = config.render_engine_configs.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p2, engine=engine, target_name="config.toml"))

            # 4. Static exists (takes precedence over template)
            p_static = directory / "config.toml"
            p_static.touch()
            match = config.render_engine_configs.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=p_static, engine=None, target_name="config.toml"))

    def test_find_source_file_for_targets_with_directories(self) -> None:
        """Verifies find_source_file_for_rendered_names correctly identifies directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir).resolve()
            config = WorkspaceConfig(drift_root=directory, render_engine_configs=RenderEngineRegistry())
            
            targets = ["my_folder", "other_folder"]
            
            # 1. Directory exists
            d1 = directory / "my_folder"
            d1.mkdir()
            match = config.render_engine_configs.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=d1, engine=None, target_name="my_folder"))
            
            # 2. File with same name takes precedence
            shutil.rmtree(d1)
            f1 = directory / "my_folder"
            f1.touch()
            match = config.render_engine_configs.find_source_file_for_rendered_names(directory, targets)
            self.assertEqual(match, RenderSourceMatch(path=f1, engine=None, target_name="my_folder"))

    def test_find_conflict_in_source_dir(self) -> None:
        """Verifies find_conflict_in_source_dir correctly identifies matches and blocks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            src_pkg_dir = root / "src" / "pkg"
            src_pkg_dir.mkdir(parents=True)
            
            engine = RenderEngineConfig(name="envst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
            config = WorkspaceConfig(drift_root=root, render_engine_configs=RenderEngineRegistry({"envst": engine}))
            
            # 1. Exact match (static file)
            f1 = src_pkg_dir / "dot-bashrc"
            f1.touch()
            match: Any = config.render_engine_configs.find_conflict_in_source_dir(src_pkg_dir, Path(".bashrc"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, f1)
            self.assertEqual(match.status, "match")
            f1.unlink()
            
            # 2. Exact match (template)
            t1 = src_pkg_dir / "dot-bashrc.envst"
            t1.touch()
            match: Any = config.render_engine_configs.find_conflict_in_source_dir(src_pkg_dir, Path(".bashrc"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, t1)
            self.assertEqual(match.status, "match")
            t1.unlink()
            
            # 3. Block (intermediate file)
            # We want to render to .config/nvim/init.vim
            # But src/pkg/dot-config is a file
            b1 = src_pkg_dir / "dot-config"
            b1.touch()
            match = config.render_engine_configs.find_conflict_in_source_dir(src_pkg_dir, Path(".config/nvim/init.vim"))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, b1)
            self.assertEqual(match.status, "block")
            b1.unlink()
            
            # 4. No conflict
            match = config.render_engine_configs.find_conflict_in_source_dir(src_pkg_dir, Path(".config/nvim/init.vim"))
            self.assertIsNone(match)


    def test_package_config_from_dict(self) -> None:
        base_dir = Path("/mock/src/my_pkg")
        data = {
            "package": {
                "install_method": "stow"
            },
            "hooks": {
                "pre_source": "drift_hooks/gen.sh",
                "timeout": 60
            }
        }
        config = PackageConfig.from_dict(data, base_dir=base_dir, package_name="my_pkg")
        self.assertEqual(config.name, "my_pkg")
        self.assertEqual(config.hooks.pre_source, base_dir / ".drift/hooks/gen.sh")
        self.assertEqual(config.hooks.timeout, 60)

        # Test string casting for timeout
        data_str_timeout = {
            "package": {
                "install_method": "stow"
            },
            "hooks": {
                "timeout": "45"
            }
        }
        config_str = PackageConfig.from_dict(data_str_timeout, package_name="my_pkg", base_dir=base_dir)
        self.assertEqual(config_str.hooks.timeout, 45)

        data_no_name = {
            "package": {
                "install_method": "stow"
            }
        }
        config = PackageConfig.from_dict(data_no_name, package_name="fallback_name", base_dir=base_dir)
        self.assertEqual(config.name, "fallback_name")
        self.assertEqual(config.hooks.timeout, DEFAULT_HOOK_TIMEOUT)

        # base_dir is required in PackageConfig.from_dict
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict(data_no_name, package_name="fallback_name", base_dir=None)  # type: ignore

    def test_package_config_validation(self) -> None:
        with self.assertRaises(ConfigError):
            PackageConfig(name="").validate()
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", install_method="invalid").validate()
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", enable_render="yes").validate() # type: ignore
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", hooks=PackageHooks(timeout="not_an_int")).validate() # type: ignore
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", hooks=PackageHooks(timeout=0)).validate()
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", hooks=PackageHooks(timeout=-10)).validate()
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", hooks=PackageHooks(pre_source=123)).validate() # type: ignore
        with self.assertRaises(ConfigError):
            PackageConfig(name="foo", hook_file=123).validate() # type: ignore

    def test_package_hooks_dataclass(self) -> None:
        base = Path("/workspace/test_pkg")
        hooks = PackageHooks(
            pre_source=base / ".drift/hooks/gen.sh",
            pre_install=base / ".drift/hooks/pre.sh",
            post_install=base / ".drift/hooks/post.sh",
            timeout=30
        )
        config = PackageConfig(name="test_pkg", hooks=hooks)
        self.assertEqual(config.hooks.pre_source, base / ".drift/hooks/gen.sh")
        self.assertEqual(config.hooks.timeout, 30)
        self.assertIs(config.hooks.package_config, config)

        # Direct property modification on hooks
        config.hooks.post_render = base / ".drift/hooks/render.sh"
        self.assertEqual(config.hooks.post_render, base / ".drift/hooks/render.sh")

    def test_package_hooks_from_dict_and_validation(self) -> None:
        """Verifies PackageHooks.from_dict method, validation, and error handling."""
        # 1. Valid dict parsing
        raw = {
            "pre_source": "drift_hooks/gen.sh",
            "post_install": "drift_hooks/post.sh",
            "timeout": "60"
        }
        base = Path("/workspace/my_pkg")
        hooks = PackageHooks.from_dict(raw, package_name="my_pkg", base_dir=base)
        self.assertEqual(hooks.pre_source, base / ".drift/hooks/gen.sh")
        self.assertEqual(hooks.post_install, base / ".drift/hooks/post.sh")
        self.assertEqual(hooks.timeout, 60)

        # 2. Non-string hook value raises ConfigError
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"pre_source": 12345}, package_name="bad_pkg", base_dir=base)

        # 3. Non-dict Windows subtable raises ConfigError
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"windows": "not_a_dict"}, package_name="bad_pkg", base_dir=base)

        # 4. Invalid timeout raises ConfigError
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"timeout": "abc"}, package_name="bad_pkg", base_dir=base)
        with self.assertRaises(ConfigError):
            PackageHooks.from_dict({"timeout": -10}, package_name="bad_pkg", base_dir=base)

    def test_load_package_config_with_hooks_table(self) -> None:
        """Verifies parsing package configuration with dedicated [hooks] table."""
        toml_dict = {
            "package": {
                "install_method": "copy",
                "target_directory": "~/.config/test"
            },
            "hooks": {
                "pre_source": "drift_hooks/gen.sh",
                "pre_install": "drift_hooks/pre_install.sh",
                "post_install": "drift_hooks/post_install.sh",
                "pre_update": "drift_hooks/pre_update.sh",
                "post_update": "drift_hooks/post_update.sh",
                "pre_uninstall": "drift_hooks/pre_uninstall.sh",
                "post_uninstall": "drift_hooks/post_uninstall.sh",
                "post_render": "drift_hooks/post_render.sh",
                "health": "drift_hooks/health_check.sh",
                "timeout": 45
            }
        }
        base = Path("/workspace/pkg_with_hooks")
        config = PackageConfig.from_dict(toml_dict, package_name="pkg_with_hooks", base_dir=base)
        self.assertEqual(config.name, "pkg_with_hooks")
        self.assertEqual(config.install_method, "copy")
        self.assertEqual(config.hooks.pre_source, base / ".drift/hooks/gen.sh")
        self.assertEqual(config.hooks.pre_install, base / ".drift/hooks/pre_install.sh")
        self.assertEqual(config.hooks.post_install, base / ".drift/hooks/post_install.sh")
        self.assertEqual(config.hooks.pre_update, base / ".drift/hooks/pre_update.sh")
        self.assertEqual(config.hooks.post_update, base / ".drift/hooks/post_update.sh")
        self.assertEqual(config.hooks.pre_uninstall, base / ".drift/hooks/pre_uninstall.sh")
        self.assertEqual(config.hooks.post_uninstall, base / ".drift/hooks/post_uninstall.sh")
        self.assertEqual(config.hooks.post_render, base / ".drift/hooks/post_render.sh")
        self.assertEqual(config.hooks.health, base / ".drift/hooks/health_check.sh")
        self.assertEqual(config.hooks.timeout, 45)

    def test_load_package_config_with_hooks_windows_and_aliases(self) -> None:
        """Verifies [hooks.windows] and alias sub-tables (win32, winos, win) override default hooks on Windows."""
        for alias in ["windows", "win32", "winos", "win"]:
            toml_dict = {
                "package": {
                    "install_method": "copy",
                },
                "hooks": {
                    "pre_install": "drift_hooks/bootstrap.sh",
                    "post_install": "drift_hooks/setup.sh",
                    alias: {
                        "pre_install": "drift_hooks/bootstrap.ps1",
                        "post_install": "drift_hooks/setup.ps1",
                        "post_update": "drift_hooks/update.bat",
                    }
                }
            }
            base = Path("/workspace/my_pkg")
            # On POSIX (Linux/macOS), windows subtable is ignored
            with patch("sys.platform", "linux"):
                config_linux = PackageConfig.from_dict(toml_dict, package_name="my_pkg", base_dir=base)
                self.assertEqual(config_linux.hooks.pre_install, base / ".drift/hooks/bootstrap.sh")
                self.assertEqual(config_linux.hooks.post_install, base / ".drift/hooks/setup.sh")
                self.assertIsNone(config_linux.hooks.post_update)

            # On Windows, windows subtable overrides default hooks
            with patch("sys.platform", "win32"):
                config_win = PackageConfig.from_dict(toml_dict, package_name="my_pkg", base_dir=base)
                self.assertEqual(config_win.hooks.pre_install, base / ".drift/hooks/bootstrap.ps1")
                self.assertEqual(config_win.hooks.post_install, base / ".drift/hooks/setup.ps1")
                self.assertEqual(config_win.hooks.post_update, base / ".drift/hooks/update.bat")

    def test_package_hooks_disabled_values_in_base_and_subtables(self) -> None:
        """Verifies that 'disable' and 'disabled' (case-insensitive) in base [hooks] or platform tables turn off hooks."""
        # 1. Base [hooks] with "disable" and "disabled"
        toml_dict = {
            "package": {
                "install_method": "copy",
            },
            "hooks": {
                "pre_source": "disable",
                "pre_install": "disabled",
                "post_install": "Disabled",
                "pre_update": "DISABLE",
                "post_update": "",
                "pre_uninstall": "   ",
                "post_uninstall": "drift_hooks/uninstall.sh",
            }
        }
        base = Path("/workspace/pkg_disabled")
        config = PackageConfig.from_dict(toml_dict, package_name="pkg_disabled", base_dir=base)
        self.assertIsNone(config.hooks.pre_source)
        self.assertIsNone(config.hooks.pre_install)
        self.assertIsNone(config.hooks.post_install)
        self.assertIsNone(config.hooks.pre_update)
        self.assertIsNone(config.hooks.post_update)
        self.assertIsNone(config.hooks.pre_uninstall)
        self.assertEqual(config.hooks.post_uninstall, base / ".drift/hooks/uninstall.sh")

        # 2. [hooks.windows] disabling a base hook on Windows
        override_dict = {
            "package": {
                "install_method": "copy",
            },
            "hooks": {
                "post_install": "drift_hooks/posix_post.sh",
                "windows": {
                    "post_install": "disable",
                }
            }
        }
        with patch("sys.platform", "linux"):
            config_linux = PackageConfig.from_dict(override_dict, package_name="pkg_override", base_dir=base)
            self.assertEqual(config_linux.hooks.post_install, base / ".drift/hooks/posix_post.sh")

        with patch("sys.platform", "win32"):
            config_win = PackageConfig.from_dict(override_dict, package_name="pkg_override", base_dir=base)
            self.assertIsNone(config_win.hooks.post_install)

    def test_package_hooks_property_setters_with_disabled(self) -> None:
        """Verifies that setting a hook property to 'disable' or 'disabled' normalizes to None."""
        base = Path("/workspace/my_pkg")
        hooks = PackageHooks(post_install=base / ".drift/hooks/post.sh")
        self.assertEqual(hooks.post_install, base / ".drift/hooks/post.sh")

        hooks.post_install = Path("disabled")
        self.assertIsNone(hooks.post_install)

        hooks.pre_install = Path("disable")
        self.assertIsNone(hooks.pre_install)

    def test_package_source_directory_parsing_and_validation(self) -> None:
        """Verifies parsing, normalization, and validation of package source_directory."""
        base = Path("/workspace/my_pkg")

        # 1. Default (omitted) defaults to Path(".")
        cfg_default = PackageConfig.from_dict({"package": {"install_method": "stow"}}, base_dir=base, package_name="my_pkg")
        self.assertEqual(cfg_default.source_directory, Path("."))
        self.assertEqual(cfg_default.get_source_directory_to_render(base), base)

        # 2. Valid relative subfolder
        cfg_sub = PackageConfig.from_dict({"package": {"source_directory": "dotfiles"}}, base_dir=base, package_name="my_pkg")
        self.assertEqual(cfg_sub.source_directory, Path("dotfiles"))
        self.assertEqual(cfg_sub.get_source_directory_to_render(base), base / "dotfiles")

        # 3. Nested subfolder with trailing slash normalization
        cfg_nested = PackageConfig.from_dict({"package": {"source_directory": "src/nested/"}}, base_dir=base, package_name="my_pkg")
        self.assertEqual(cfg_nested.source_directory, Path("src/nested"))
        self.assertEqual(cfg_nested.get_source_directory_to_render(base), base / "src/nested")

        # 4. Dot or empty string resolves to Path(".")
        cfg_dot = PackageConfig.from_dict({"package": {"source_directory": "."}}, base_dir=base, package_name="my_pkg")
        self.assertEqual(cfg_dot.source_directory, Path("."))

        # 5. Escaping package root raises ConfigError
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {"source_directory": "../outside"}}, base_dir=base, package_name="my_pkg")

        # 6. Absolute path raises ConfigError
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {"source_directory": "/etc/shadow"}}, base_dir=base, package_name="my_pkg")

    def test_package_hooks_drift_hooks_isolation_and_symlink_guidance(self) -> None:
        """Verifies hook path resolution enforcing drift_hooks/ and error advice."""
        base = Path("/workspace/my_pkg")

        # 1. Hook outside drift_hooks/ raises ConfigError with symlink guidance
        with self.assertRaises(ConfigError) as ctx:
            PackageHooks.from_dict({"pre_install": "scripts/install.sh"}, package_name="my_pkg", base_dir=base)
        self.assertIn("must be located within 'drift_hooks/' directory", str(ctx.exception))
        self.assertIn("create a symlink inside 'drift_hooks/'", str(ctx.exception))

        # 2. External absolute hook executable is allowed without drift_hooks/ restriction
        ext_path = Path("/usr/local/bin/my_hook")
        hooks_ext = PackageHooks.from_dict({"post_install": str(ext_path)}, package_name="my_pkg", base_dir=base)
        self.assertEqual(hooks_ext.post_install, ext_path.resolve())
        self.assertIsNone(hooks_ext.get_relative_path("post_install"))

    def test_build_hook_execution_command(self) -> None:
        """Verifies cross-platform command building for lifecycle hook dispatch."""
        from drift.hooks.lifecycle_hooks import (
            build_hook_execution_command,
            build_hook_execution_command_win32,
            build_hook_execution_command_posix,
        )

        # 1. Windows direct builder
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.ps1")),
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"C:\scripts\install.ps1"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.bat")),
            ["cmd.exe", "/c", r"C:\scripts\install.bat"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.cmd")),
            ["cmd.exe", "/c", r"C:\scripts\install.cmd"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.py")),
            [sys.executable, r"C:\scripts\install.py"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.sh")),
            ["bash.exe", r"C:\scripts\install.sh"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\install.exe")),
            [r"C:\scripts\install.exe"]
        )
        self.assertEqual(
            build_hook_execution_command_win32(Path(r"C:\scripts\custom.bin")),
            [r"C:\scripts\custom.bin"]
        )

        # 2. POSIX direct builder with temp files
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Executable file
            exec_file = tmppath / "exec.sh"
            exec_file.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
            exec_file.chmod(0o755)
            self.assertEqual(build_hook_execution_command_posix(exec_file), [str(exec_file)])

            # Non-executable .sh
            non_exec_sh = tmppath / "hook.sh"
            non_exec_sh.write_text("echo ok\n", encoding="utf-8")
            non_exec_sh.chmod(0o644)
            self.assertEqual(build_hook_execution_command_posix(non_exec_sh), ["/bin/bash", str(non_exec_sh)])

            # Non-executable .py
            non_exec_py = tmppath / "hook.py"
            non_exec_py.write_text("print('ok')\n", encoding="utf-8")
            non_exec_py.chmod(0o644)
            self.assertEqual(build_hook_execution_command_posix(non_exec_py), [sys.executable, str(non_exec_py)])

            # Non-executable with custom shebang
            shebang_file = tmppath / "hook.custom"
            shebang_file.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
            shebang_file.chmod(0o644)
            self.assertEqual(build_hook_execution_command_posix(shebang_file), ["/usr/bin/env", "python3", str(shebang_file)])

            # Non-executable without extension or shebang -> fallback to /bin/bash
            plain_file = tmppath / "plain"
            plain_file.write_text("echo plain\n", encoding="utf-8")
            plain_file.chmod(0o644)
            self.assertEqual(build_hook_execution_command_posix(plain_file), ["/bin/bash", str(plain_file)])

        # 3. Cross-platform dispatcher
        with patch("sys.platform", "win32"):
            self.assertEqual(
                build_hook_execution_command(Path(r"C:\scripts\install.ps1")),
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", r"C:\scripts\install.ps1"]
            )
        with patch("sys.platform", "linux"):
            with patch("drift.hooks.lifecycle_hooks.build_hook_execution_command_posix") as mock_posix:
                mock_posix.return_value = ["/bin/bash", "/path/hook.sh"]
                self.assertEqual(build_hook_execution_command(Path("/path/hook.sh")), ["/bin/bash", "/path/hook.sh"])

    def test_execute_hook_command(self) -> None:
        """Verifies execute_hook_command runs in user space across platforms."""
        from drift.hooks.lifecycle_hooks import execute_hook_command

        with patch("drift.hooks.lifecycle_hooks.run_command") as mock_run:
            mock_run.return_value = MagicMock()

            # POSIX execution in user space
            with patch("sys.platform", "linux"):
                execute_hook_command(
                    cmd=["/scripts/setup.sh"],
                    cwd=Path("/opt/app"),
                    timeout_seconds=60
                )
                mock_run.assert_called_with(
                    ["/scripts/setup.sh"],
                    cwd="/opt/app",
                    text=True,
                    timeout=60,
                    streaming=True
                )

            mock_run.reset_mock()

            # Windows execution in user space
            with patch("sys.platform", "win32"):
                execute_hook_command(
                    cmd=["powershell.exe", "-File", r"C:\scripts\setup.ps1"],
                    cwd=Path(r"C:\app"),
                    timeout_seconds=60,
                    streaming=False
                )
                mock_run.assert_called_with(
                    ["powershell.exe", "-File", r"C:\scripts\setup.ps1"],
                    cwd=r"C:\app",
                    text=True,
                    timeout=60,
                    streaming=False
                )

    def test_check_sudo_and_root_windows(self) -> None:
        """Verifies check_sudo_and_root passes unconditionally on Windows."""
        from drift.cli import check_sudo_and_root

        with patch("sys.platform", "win32"):
            # Should not raise or exit
            check_sudo_and_root(Path.cwd())

    def test_package_hooks_check_hook_files(self) -> None:
        """Verifies check_hook_files validates existence and regular file status of configured hook files."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            scripts_dir = base / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "pre_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            (scripts_dir / "post_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            hooks = PackageHooks(
                pre_install=base / "scripts/pre_install.sh",
                post_install=base / "scripts/post_install.sh"
            )
            # 1. Valid hook files pass
            hooks.check_hook_files(base, is_source=False)

            # 2. Missing hook file raises FileNotFoundError
            hooks.post_update = base / "scripts/missing.sh"
            with self.assertRaises(FileNotFoundError) as cm:
                hooks.check_hook_files(base, is_source=False)
            self.assertIn("missing.sh", str(cm.exception))

            # 3. Hook path pointing to directory raises ValueError
            (scripts_dir / "dir_hook").mkdir()
            hooks.post_update = base / "scripts/dir_hook"
            with self.assertRaises(ValueError) as cm:
                hooks.check_hook_files(base, is_source=False)
            self.assertIn("not a regular file", str(cm.exception))

            # 4. Filtered hook_names ignores unrequested broken hooks
            (scripts_dir / "pre_uninstall.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            hooks.pre_uninstall = base / "scripts/pre_uninstall.sh"
            # Checking only pre_uninstall passes even though post_update is broken
            hooks.check_hook_files(base, is_source=False, hook_names=["pre_uninstall"])

            # 5. Check drift_hooks/ routing for is_source=True vs is_source=False
            drift_hooks_src = base / "src_pkg" / "drift_hooks"
            drift_hooks_src.mkdir(parents=True)
            (drift_hooks_src / "post_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            drift_hooks_rendered = base / "install_pkg" / ".drift" / "hooks"
            drift_hooks_rendered.mkdir(parents=True)
            (drift_hooks_rendered / "post_install.sh").write_text("#!/bin/bash\n", encoding="utf-8")

            hooks_dh = PackageHooks.from_dict(
                {"post_install": "drift_hooks/post_install.sh"},
                package_name="test_pkg",
                base_dir=base / "src_pkg"
            )
            # is_source=True checks src_pkg/drift_hooks/post_install.sh
            hooks_dh.check_hook_files(base / "src_pkg", is_source=True)
            # is_source=False checks install_pkg/.drift/hooks/post_install.sh
            hooks_dh.check_hook_files(base / "install_pkg", is_source=False)

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
            
            # Create directories            # pkg_a has drift_package.toml
            pkg_a_dir = root_path / "pkg_a"
            (pkg_a_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_a_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()

            # pkg_b has drift_package.toml
            pkg_b_dir = root_path / "pkg_b"
            (pkg_b_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_b_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()

            # pkg_c has no config file, should not be discovered
            pkg_c_dir = root_path / "pkg_c"
            pkg_c_dir.mkdir()

            config = WorkspaceConfig(
                drift_root=root_path,
                packages_enable={"pkg_a": True, "pkg_b": False},
                packages_enable_default=False
            )

            # 1. No target_pkgs - should return only enabled discovered packages (pkg_a)
            discovered = config.filter_custom_dir_packages_by_target(root_path)
            self.assertEqual(discovered, ["pkg_a"])

            # 2. Target packages explicitly specified (even disabled pkg_b is returned)
            discovered_targets = config.filter_custom_dir_packages_by_target(root_path, target_packages=["pkg_a", "pkg_b"])
            self.assertEqual(discovered_targets, ["pkg_a", "pkg_b"])

            # 3. Missing target package (raises ValueError)
            with self.assertRaises(ValueError):
                config.filter_custom_dir_packages_by_target(root_path, target_packages=["pkg_a", "pkg_c"])

    def test_get_source_packages_and_rendered_installed(self) -> None:
        """Verifies filter_source_packages_by_target finds all packages in src/, and filter_render/install find packages with config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            src_dir = root / "src"
            render_dir = root / "render"
            install_dir = root / "install"
            src_dir.mkdir()
            render_dir.mkdir()
            install_dir.mkdir()

            # In src: pkg_a has drift_package.toml, pkg_b has drift_package.envst.toml, pkg_c has no config
            (src_dir / "pkg_a").mkdir()
            (src_dir / "pkg_a" / PACKAGE_CONFIG_FILE_NAME).touch()

            (src_dir / "pkg_b").mkdir()
            (src_dir / "pkg_b" / "drift_package.envst.toml").touch()

            (src_dir / "pkg_c").mkdir()

            # In render: pkg_a and pkg_b compiled with drift_package.toml in .drift/
            (render_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (render_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()
            (render_dir / "pkg_b" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (render_dir / "pkg_b" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()

            # In install: pkg_a staged with drift_package.toml in .drift/
            (install_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (install_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()

            config = WorkspaceConfig(
                drift_root=root,
                workspace=WorkspaceSectionConfig(
                    source_directory=Path("src"),
                    render_directory=Path("render"),
                    install_directory=Path("install"),
                ),
                packages_enable={"pkg_a": True, "pkg_b": True, "pkg_c": False},
                packages_enable_default=False
            )

            # filter_source_packages_by_target discovers all subdirs in src/ (pkg_a, pkg_b, pkg_c)
            # When target_packages is None, returns enabled: pkg_a, pkg_b
            self.assertEqual(config.filter_source_packages_by_target(), ["pkg_a", "pkg_b"])

            # Explicit target_packages includes disabled pkg_c
            self.assertEqual(config.filter_source_packages_by_target(target_packages=["pkg_c"]), ["pkg_c"])

            # filter_render_packages_by_target discovers pkg_a and pkg_b from render/
            self.assertEqual(config.filter_render_packages_by_target(), ["pkg_a", "pkg_b"])

            # filter_install_packages_by_target discovers pkg_a from install/
            self.assertEqual(config.filter_install_packages_by_target(), ["pkg_a"])

    def test_workspace_config_absolute_target_dir(self) -> None:
        """Verifies that WorkspaceConfig.validate raises ValueError if default_target_directory is relative."""
        # Using an absolute directory is valid
        WorkspaceConfig(drift_root=self.drift_root, workspace=WorkspaceSectionConfig(default_target_directory=Path("/absolute/path"))).validate()
        
        # Using a relative directory raises ValueError
        with self.assertRaises(ValueError) as ctx:
            WorkspaceConfig(drift_root=self.drift_root, workspace=WorkspaceSectionConfig(default_target_directory=Path("relative/path"))).validate()
        self.assertIn("default_target_directory must be an absolute path", str(ctx.exception))

    def test_unknown_option_raises_config_error(self) -> None:
        """Verifies that unknown configuration options and sections raise ConfigError."""
        # 1. Workspace unknown top-level section
        data_unknown_top = {
            "workspace": {},
            "packages": {"enable": {}},
            "unknown_top_section": {"foo": "bar"}
        }
        with self.assertRaises(ConfigError) as ctx:
            WorkspaceConfig.from_dict(data_unknown_top, drift_root=self.drift_root)
        self.assertIn("Unknown top-level config section: 'unknown_top_section'", str(ctx.exception))

        # 2. Workspace unknown option in [workspace]
        data_unknown_workspace = {
            "workspace": {
                "unknown_workspace_opt": "random_val"
            },
            "packages": {"enable": {}}
        }
        with self.assertRaises(ConfigError) as ctx:
            WorkspaceConfig.from_dict(data_unknown_workspace, drift_root=self.drift_root)
        self.assertIn("Unknown workspace option: 'unknown_workspace_opt'", str(ctx.exception))

        # 3. Workspace unknown option in [render.<engine>]
        data_unknown_render = {
            "workspace": {},
            "packages": {"enable": {}},
            "render": {
                "mustache": {
                    "input_file": "input.json",
                    "suffix": "mustache",
                    "render_command": "mustache %i %s",
                    "unknown_render_opt": "blah"
                }
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            WorkspaceConfig.from_dict(data_unknown_render, drift_root=self.drift_root)
        self.assertIn("Unknown option under render.mustache: 'unknown_render_opt'", str(ctx.exception))

        # 4. PackageConfig unknown top-level section
        pkg_data_unknown_top = {
            "package": {"name": "my_pkg"},
            "another_unknown_top_section": {"baz": "qux"}
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_data_unknown_top, package_name="my_pkg", base_dir=Path("/test/pkg"))
        self.assertIn("Unknown top-level package config section: 'another_unknown_top_section'", str(ctx.exception))

        # 5. PackageConfig unknown package option
        pkg_data_unknown_opt = {
            "package": {
                "name": "my_pkg",
                "unknown_pkg_opt": "something"
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_data_unknown_opt, package_name="my_pkg", base_dir=Path("/test/pkg"))
        self.assertIn("Unknown package option: 'unknown_pkg_opt'", str(ctx.exception))

        # 6. PackageHooks unknown hook option
        pkg_data_unknown_hook = {
            "package": {"name": "my_pkg"},
            "hooks": {
                "unknown_hook_opt": "script.sh"
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_data_unknown_hook, package_name="my_pkg", base_dir=Path("/test/pkg"))
        self.assertIn("Unknown hook option in package [hooks]: 'unknown_hook_opt'", str(ctx.exception))

        # 7. PackageHooks unknown platform sub-table hook option
        pkg_data_unknown_subtable_hook = {
            "package": {"name": "my_pkg"},
            "hooks": {
                "windows": {
                    "unknown_win_hook": "script.ps1"
                }
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_data_unknown_subtable_hook, package_name="my_pkg", base_dir=Path("/test/pkg"))
        self.assertIn("Unknown hook option in platform hooks sub-table: 'unknown_win_hook'", str(ctx.exception))

    def test_package_config_get_install_method(self) -> None:
        ws_config = WorkspaceConfig(
            drift_root=Path("/test"),
            workspace=WorkspaceSectionConfig(default_install_method=InstallMethod.STOW),
        )
        pkg_config = PackageConfig(name="test_pkg", install_method=InstallMethod.STOW)

        # On non-Windows, returns stow
        with patch("sys.platform", "linux"):
            self.assertEqual(pkg_config.get_install_method(ws_config), InstallMethod.STOW)

        # On Windows (win32), always forces copy
        with patch("sys.platform", "win32"):
            self.assertEqual(pkg_config.get_install_method(ws_config), InstallMethod.COPY)

    def test_package_config_target_directory_windows_and_aliases(self) -> None:
        ws_config = WorkspaceConfig(
            drift_root=Path("/test"),
            workspace=WorkspaceSectionConfig(default_target_directory=Path("/default/target")),
        )
        home = Path.home()

        for alias in ["windows", "win32", "winos", "win"]:
            data = {
                "package": {
                    "name": "nvim",
                    "target_directory": "~/.config/nvim",
                    f"target_directory_{alias}": "%LOCALAPPDATA%/nvim"
                }
            }
            pkg_config = PackageConfig.from_dict(data, package_name="nvim", base_dir=Path("/test/nvim"))

            # On Linux/POSIX, returns standard target_directory
            with patch("sys.platform", "linux"):
                self.assertEqual(pkg_config.get_target_directory(ws_config), home / ".config" / "nvim")

            # On Windows, returns target_directory_<alias> expanded
            with patch("sys.platform", "win32"):
                with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/testuser/AppData/Local"}):
                    self.assertEqual(pkg_config.get_target_directory(ws_config), Path("C:/Users/testuser/AppData/Local/nvim"))

    def test_target_directory_windows_forward_and_mixed_slashes(self) -> None:
        """Verifies that forward slashes '/' and mixed slashes in target_directory_windows are supported and parsed correctly."""
        ws_config = WorkspaceConfig(
            drift_root=Path("/test"),
            workspace=WorkspaceSectionConfig(default_target_directory=Path("/default/target")),
        )
        home = Path.home()

        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {
                "LOCALAPPDATA": "C:/Users/testuser/AppData/Local",
                "USERPROFILE": "C:/Users/testuser",
                "APPDATA": "C:/Users/testuser/AppData/Roaming",
            }):
                # 1. Pure forward slashes with %VAR%
                data1 = {
                    "package": {
                        "name": "pkg1",
                        "target_directory_windows": "%LOCALAPPDATA%/my_app/config"
                    }
                }
                cfg1 = PackageConfig.from_dict(data1, package_name="pkg1", base_dir=Path("/test/pkg1"))
                self.assertEqual(cfg1.get_target_directory(ws_config), Path("C:/Users/testuser/AppData/Local/my_app/config"))

                # 2. Pure forward slashes with drive letter
                data2 = {
                    "package": {
                        "name": "pkg2",
                        "target_directory_windows": "C:/Custom/Path/To/App"
                    }
                }
                cfg2 = PackageConfig.from_dict(data2, package_name="pkg2", base_dir=Path("/test/pkg2"))
                self.assertEqual(cfg2.get_target_directory(ws_config), Path("C:/Custom/Path/To/App"))

                # 3. Pure forward slashes with ~ (tilde)
                data3 = {
                    "package": {
                        "name": "pkg3",
                        "target_directory_windows": "~/AppData/Local/nvim"
                    }
                }
                cfg3 = PackageConfig.from_dict(data3, package_name="pkg3", base_dir=Path("/test/pkg3"))
                self.assertEqual(cfg3.get_target_directory(ws_config), home / "AppData" / "Local" / "nvim")

                # 4. Mixed slashes
                data4 = {
                    "package": {
                        "name": "pkg4",
                        "target_directory_windows": "%USERPROFILE%\\AppData/Roaming\\alacritty/nested"
                    }
                }
                cfg4 = PackageConfig.from_dict(data4, package_name="pkg4", base_dir=Path("/test/pkg4"))
                self.assertEqual(cfg4.get_target_directory(ws_config), Path("C:/Users/testuser/AppData/Roaming/alacritty/nested"))


class TestConfigLoaders(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_workspace_config(self) -> None:
        # Test nonexistent file (raises ConfigError)
        with self.assertRaises(ConfigError):
            load_workspace_config(self.drift_root)

        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / WORKSPACE_CONFIG_FILE_NAME

        # Test valid file
        config_path.write_text("""
            [workspace]
            render_directory = "sandbox"

            [packages.enable]
            DEFAULT = false
            """, encoding="utf-8")
        config = load_workspace_config(self.drift_root)
        self.assertEqual(config.workspace.render_directory, Path("sandbox"))
        # Verify absolute drift_root and drift_root_path computation
        self.assertEqual(config.drift_root, self.drift_root)
        self.assertEqual(config.drift_root_path, self.drift_root)

        # Invalid default_install_method raises ConfigError
        config_path.write_text("""
            [workspace]
            default_install_method = "invalid_method"
            [packages.enable]
            DEFAULT = true
            """, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_workspace_config(self.drift_root)

    def test_load_package_config(self) -> None:
        pkg_config_path = self.drift_root / PACKAGE_CONFIG_FILE_NAME

        # Nonexistent file raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            PackageConfig.from_rendered_file(pkg_config_path, package_name="my_default", package_dir=self.drift_root)

        # Valid file without name field (derived from package_name parameter)
        pkg_config_path.write_text("""
            [package]
            install_method = "copy"
            """, encoding="utf-8")
        config = PackageConfig.from_rendered_file(pkg_config_path, package_name="my_actual_package", package_dir=self.drift_root)
        self.assertEqual(config.name, "my_actual_package")
        self.assertEqual(config.install_method, "copy")

        # Invalid hook type raises ConfigError
        pkg_config_path.write_text("""
            [package]
            install_method = "copy"
            [hooks]
            pre_source = 12345
            """, encoding="utf-8")
        with self.assertRaises(ConfigError):
            PackageConfig.from_rendered_file(pkg_config_path, package_name="my_actual_package", package_dir=self.drift_root)

    def test_locate_package_config_file_and_load_from_dir(self) -> None:
        pkg_dir = self.drift_root / "my_pkg_folder"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # No config file exists yet (raises FileNotFoundError)
        with self.assertRaises(FileNotFoundError):
            PackageConfig.from_source_dir(pkg_dir)

        # Creating drift_package.toml
        alt_config_path = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        alt_config_path.write_text("""
            [package]
            install_method = "copy"
            """, encoding="utf-8")
        
        config = PackageConfig.from_source_dir(pkg_dir)
        self.assertEqual(config.name, "my_pkg_folder")
        self.assertEqual(config.install_method, "copy")

        # Invalid config raises ConfigError
        alt_config_path.write_text("""
            [package]
            install_method = "copy"
            [hooks]
            timeout = "not_an_int"
            """, encoding="utf-8")
        with self.assertRaises(ConfigError):
            PackageConfig.from_source_dir(pkg_dir)

    def test_get_package_config_file_info(self) -> None:
        from drift.config.workspace_config import RenderEngineConfig
        pkg_dir = self.drift_root / "test_find_info"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        # Create WorkspaceConfig
        workspace_config = WorkspaceConfig(drift_root=self.drift_root)
        engine = RenderEngineConfig(name="envsubst", input_file=Path("env.sh"), suffix="envst", render_command="cmd")
        workspace_config.render_engine_configs = RenderEngineRegistry({"envsubst": engine})

        config_files = [
            pkg_dir / PACKAGE_CONFIG_FILE_NAME,
            pkg_dir / "drift_package.local.toml",
        ]

        # 1. No files exist - should return []
        res = get_package_config_file_info(config_files, workspace_config.render_engine_configs)
        self.assertEqual(res, [])

        # 2. drift_package.envst.toml exists
        template_drift_path = pkg_dir / package_config_template_name
        template_drift_path.write_text("", encoding="utf-8")
        res = get_package_config_file_info(config_files, workspace_config.render_engine_configs)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].type, "template")
        self.assertEqual(res[0].path, template_drift_path)
        self.assertEqual(res[0].engine, engine)

        # 3. drift_package.toml exists (takes precedence over drift_package.envst.toml)
        drift_package_toml_path = pkg_dir / PACKAGE_CONFIG_FILE_NAME
        drift_package_toml_path.write_text("", encoding="utf-8")
        res = get_package_config_file_info(config_files, workspace_config.render_engine_configs)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].type, "static")
        self.assertEqual(res[0].path, drift_package_toml_path)
        self.assertIsNone(res[0].engine)

        # 4. drift_package.local.toml also exists
        local_path = pkg_dir / "drift_package.local.toml"
        local_path.write_text("", encoding="utf-8")
        res = get_package_config_file_info(config_files, workspace_config.render_engine_configs)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].path, drift_package_toml_path)
        self.assertEqual(res[1].path, local_path)

        # 5. Lazy generator expression
        config_gen = (f for f in config_files)
        res_gen = get_package_config_file_info(config_gen, workspace_config.render_engine_configs)
        self.assertEqual(len(res_gen), 2)
        self.assertEqual(res_gen[0].path, drift_package_toml_path)
        self.assertEqual(res_gen[1].path, local_path)

    def test_package_toml_template_rendering(self) -> None:
        # 1. Create config/drift_workspace.toml
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        drift_toml_path = config_dir / WORKSPACE_CONFIG_FILE_NAME
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
        self.assertEqual(workspace_config.drift_root, self.drift_root)
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
        from drift.render.render_input import render_input_templates
        from drift.core.constants import DRIFT_INTERNAL_DIR_NAME, DRIFT_INTERNAL_RENDER_DIR_NAME
        render_input_templates(
            workspace_config.render_engine_configs,
            workspace_config.drift_root,
            workspace_config.render_path / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_RENDER_DIR_NAME
        )

        # 5. Load package config from directory (which should render package.envst.toml -> render/my_pkg/drift_package.toml)
        pkg_config = PackageConfig.from_source_dir(pkg_dir, workspace_config)

        # Verify fields and values
        self.assertEqual(pkg_config.name, "my_pkg")
        self.assertEqual(pkg_config.install_method, "copy")
        self.assertEqual(pkg_config.sudo, True)

        # Verify that the expected rendered config file exists inside the render/ sandbox
        expected_rendered_path = self.drift_root / "my_render" / "my_pkg" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(expected_rendered_path.is_file())

    def test_workspace_local_config_merge(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / WORKSPACE_CONFIG_FILE_NAME
        local_path = config_dir / "drift_workspace.local.toml"

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
        self.assertEqual(config.workspace.render_directory, Path("my_render"))
        self.assertEqual(config.workspace.install_directory, Path("overridden_install"))

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
        pkg_config = PackageConfig.from_source_dir(pkg_dir)
        self.assertEqual(pkg_config.name, "my_pkg_merge")
        self.assertEqual(pkg_config.install_method, "stow")
        self.assertEqual(pkg_config.sudo, True)

    def test_package_local_config_merge_with_workspace(self) -> None:
        # Create workspace config structure
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / WORKSPACE_CONFIG_FILE_NAME
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

        pkg_config = PackageConfig.from_source_dir(pkg_dir, workspace_config)
        self.assertEqual(pkg_config.name, "my_pkg_merge_ws")
        self.assertEqual(pkg_config.install_method, "stow")
        self.assertEqual(pkg_config.sudo, True)

        # Ensure the combined file gets rendered correctly in render/ sandbox
        expected_rendered_path = self.drift_root / "my_render" / "my_pkg_merge_ws" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(expected_rendered_path.is_file())

    def test_workspace_config_env_loading(self) -> None:
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / WORKSPACE_CONFIG_FILE_NAME
        local_path = config_dir / "drift_workspace.local.toml"

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
        from drift.config.workspace_config import RenderEngineConfig
        config = RenderEngineConfig(
            name="envsubst",
            input_file=Path("envsubst.bash"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        config.validate()

        with self.assertRaises(ConfigError):
            RenderEngineConfig(name="", input_file=Path("a"), suffix="b", render_command="c").validate()

        # Suffix cannot contain dots ('.')
        with self.assertRaises(ConfigError) as ctx:
            RenderEngineConfig(
                name="invalid_suffix",
                input_file=Path("envsubst.bash"),
                suffix="envst.sh",
                render_command="bash -c 'source %i && envsubst < %s'"
            ).validate()
        self.assertIn("cannot contain dots", str(ctx.exception))

    def test_workspace_config_with_render_engines(self) -> None:
        from drift.config.workspace_config import WorkspaceConfig
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
                },
                "var": {
                    "suffix": "var",
                    "render_command": "internal"
                }
            }
        }
        dummy_root = Path("/workspace_test")
        config = WorkspaceConfig.from_dict(data, drift_root=dummy_root)
        self.assertIn("envsubst", config.render_engine_config)
        self.assertEqual(config.render_engine_config["envsubst"].suffix, "envst")
        self.assertIn("mustache", config.render_engine_configs)
        self.assertEqual(config.render_engine_configs["mustache"].input_file, (dummy_root / "config" / "mustache.envst.json").resolve())
        self.assertIn("var", config.render_engine_configs)
        self.assertTrue(config.render_engine_configs["var"].is_internal)
        self.assertFalse(config.render_engine_configs["var"].is_disabled)
        self.assertEqual(config.render_engine_configs["var"].input_file, Path(""))

    def test_render_engine_registry_class(self) -> None:
        from drift.config.render_engine_config import RenderEngineConfig, RenderEngineRegistry, RenderSourceMatch
        from drift.core.exceptions import ConfigError

        dummy_base = Path("/workspace_test/config")
        # Test from_dict empty / non-dict
        self.assertEqual(len(RenderEngineRegistry.from_dict({}, base_dir=dummy_base)), 0)
        with self.assertRaises(ConfigError):
            RenderEngineRegistry.from_dict("invalid", base_dir=dummy_base)
        with self.assertRaises(ConfigError):
            RenderEngineRegistry.from_dict({"test": {"invalid_key": 123}}, base_dir=dummy_base)

        # Test valid instantiation and mapping operations
        engine = RenderEngineConfig(
            name="envst",
            input_file=Path("input.env"),
            suffix="envst",
            render_command="render %i %s"
        )
        registry = RenderEngineRegistry({"envst": engine})
        registry.validate()

        # Test mapping protocol: getitem, setitem, delitem, len, contains, iter, keys, values, items, get, repr, eq
        self.assertEqual(len(registry), 1)
        self.assertTrue("envst" in registry)
        self.assertFalse("nonexistent" in registry)
        self.assertEqual(registry["envst"], engine)
        self.assertEqual(registry.get("envst"), engine)
        self.assertIsNone(registry.get("missing"))
        self.assertEqual(list(registry.keys()), ["envst"])
        self.assertEqual(list(registry.values()), [engine])
        self.assertEqual(list(registry.items()), [("envst", engine)])
        self.assertEqual(list(iter(registry)), ["envst"])
        self.assertIn("RenderEngineRegistry", repr(registry))
        self.assertEqual(registry, {"envst": engine})
        self.assertEqual(registry, RenderEngineRegistry({"envst": engine}))
        self.assertNotEqual(registry, "not_a_registry")

        # Test copy
        copied = registry.copy()
        self.assertEqual(copied, registry)
        self.assertIsNot(copied, registry)

        # Test overlay (field-level inheritance)
        pkg_engine_override = RenderEngineConfig(
            name="envst",
            input_file=Path("/pkg/input.env"),
            suffix="",
            render_command=""
        )
        pkg_registry = RenderEngineRegistry({"envst": pkg_engine_override})
        merged = registry.overlay(pkg_registry)
        self.assertEqual(merged["envst"].input_file, Path("/pkg/input.env"))
        self.assertEqual(merged["envst"].suffix, "envst")  # Inherited
        self.assertEqual(merged["envst"].render_command, "render %i %s")  # Inherited

        # Test __setitem__ type validation
        with self.assertRaises(ConfigError):
            registry["bad"] = "not_an_engine"  # type: ignore

        # Test __delitem__
        del registry["envst"]
        self.assertEqual(len(registry), 0)
        self.assertNotIn("envst", registry)

        # Re-add engine
        registry["envst"] = engine

        # Test make_new_template_name
        self.assertEqual(registry.make_new_template_name("file.envst", "new_file"), "new_file.envst")
        self.assertEqual(registry.make_new_template_name("dot-old.envst.sh", "dot-new.sh"), "dot-new.envst.sh")
        self.assertEqual(registry.make_new_template_name("plain.txt", "target.txt"), "target.txt")

        # Test find_source_file_for_rendered_names & find_conflict_in_source_dir
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "hello.envst.txt").write_text("content", encoding="utf-8")
            (p / "plain.txt").write_text("plain", encoding="utf-8")
            (p / "subdir").mkdir()

            # Static match
            match = cast(RenderSourceMatch, registry.find_source_file_for_rendered_names(p, ["plain.txt"]))
            self.assertIsNotNone(match)
            self.assertEqual(match.path, p / "plain.txt")
            self.assertIsNone(match.engine)

            # Template match form 2
            match_tmpl = cast(RenderSourceMatch, registry.find_source_file_for_rendered_names(p, ["hello.txt"]))
            self.assertIsNotNone(match_tmpl)
            self.assertEqual(match_tmpl.path, p / "hello.envst.txt")
            self.assertEqual(match_tmpl.engine, engine)

            # Conflict in source dir
            conflict = cast(RenderSourceMatch, registry.find_conflict_in_source_dir(p, Path("hello.txt")))
            self.assertIsNotNone(conflict)
            self.assertEqual(conflict.status, "match")

            # Blocking conflict (file blocking directory path)
            block_conflict = cast(RenderSourceMatch,
                                  registry.find_conflict_in_source_dir(p, Path("plain.txt/nested/file.txt")))
            self.assertIsNotNone(block_conflict)
            self.assertEqual(block_conflict.status, "block")



    def test_meta_rendering_drift_envst_toml(self) -> None:
        from drift.config.workspace_config import load_workspace_config
        # We set an env variable
        os.environ["MY_TEST_RENDER_DIR"] = "templated_render"
        os.environ["MY_TEST_INSTALL_DIR"] = "templated_install"

        os.makedirs(os.path.join(self.temp_dir.name, "config"), exist_ok=True)

        base, ext = os.path.splitext(WORKSPACE_CONFIG_FILE_NAME)
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

        self.assertEqual(config.drift_root, Path(self.temp_dir.name).resolve())
        self.assertEqual(config.drift_root_path, Path(self.temp_dir.name).resolve())
        self.assertEqual(config.workspace.render_directory, Path("templated_render"))
        self.assertEqual(config.workspace.install_directory, Path("templated_install"))

    def test_meta_rendering_drift_envst_toml_missing_var_raises_config_error(self) -> None:
        from drift.config.workspace_config import load_workspace_config
        from drift.core.exceptions import ConfigError

        os.makedirs(os.path.join(self.temp_dir.name, "config"), exist_ok=True)
        base, ext = os.path.splitext(WORKSPACE_CONFIG_FILE_NAME)
        config_envst_name = base + ".envst" + ext
        envst_toml_path = os.path.join(self.temp_dir.name, os.path.join(CONFIG_DIR_NAME, config_envst_name))
        with open(envst_toml_path, "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            render_directory = "$UNSET_TEST_RENDER_DIR_XYZ"
            """)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                load_workspace_config(Path(self.temp_dir.name))
            self.assertIn("Environment variable '$UNSET_TEST_RENDER_DIR_XYZ' referenced in template was not found", str(ctx.exception))

    def test_package_discovery_methods(self) -> None:
        """Verifies package discovery methods on WorkspaceConfig correctly find folders from source, render, and install dirs."""
        from drift.config.workspace_config import WorkspaceConfig
        with tempfile.TemporaryDirectory() as root_path:
            # Setup directories
            os.makedirs(os.path.join(root_path, "src", "pkg_src_a"), exist_ok=True)
            os.makedirs(os.path.join(root_path, "src", "pkg_src_b"), exist_ok=True)
            
            config = WorkspaceConfig(
                drift_root=Path(root_path),
                workspace=WorkspaceSectionConfig(
                    source_directory=Path("src"),
                    render_directory=Path("render"),
                    install_directory=Path("install"),
                ),
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

    def test_package_filtering_methods(self) -> None:
        """Verifies filter_*_by_target methods correctly handle target_packages=None vs () vs explicit lists."""
        with tempfile.TemporaryDirectory() as root_path:
            root = Path(root_path)
            # Setup source, render, install dirs
            (root / "src" / "pkg_a").mkdir(parents=True)
            (root / "src" / "pkg_b").mkdir(parents=True)
            (root / "src" / "pkg_c").mkdir(parents=True)

            (root / "render" / "pkg_a" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True)
            (root / "render" / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).touch()
            (root / "render" / "pkg_b").mkdir(parents=True)
            (root / "render" / "pkg_b" / PACKAGE_CONFIG_FILE_NAME).touch()
            # pkg_c has no config file in render/
            (root / "render" / "pkg_c").mkdir(parents=True)

            (root / "install" / "pkg_a").mkdir(parents=True)
            (root / "install" / "pkg_a" / PACKAGE_CONFIG_FILE_NAME).touch()

            config = WorkspaceConfig(
                drift_root=root,
                packages_enable={"pkg_a": True, "pkg_b": False, "pkg_c": True},
                packages_enable_default=False,
            )

            # 1. filter_source_packages_by_target
            # None -> returns enabled packages
            self.assertEqual(config.filter_source_packages_by_target(target_packages=None), ["pkg_a", "pkg_c"])
            # () -> returns empty list
            self.assertEqual(config.filter_source_packages_by_target(target_packages=()), [])
            # Explicit list -> returns matching target packages in order
            self.assertEqual(config.filter_source_packages_by_target(target_packages=["pkg_b"]), ["pkg_b"])
            self.assertEqual(config.filter_source_packages_by_target(target_packages=["pkg_c", "pkg_a"]), ["pkg_c", "pkg_a"])
            # Missing package in target -> raises ValueError
            with self.assertRaises(ValueError) as ctx:
                config.filter_source_packages_by_target(target_packages=["pkg_missing"])
            self.assertIn("Given target packages not found in directory", str(ctx.exception))
            self.assertIn("pkg_missing", str(ctx.exception))

            # 2. filter_render_packages_by_target
            # render/ only recognizes packages with config file (pkg_a, pkg_b)
            self.assertEqual(config.filter_render_packages_by_target(target_packages=None), ["pkg_a"])
            self.assertEqual(config.filter_render_packages_by_target(target_packages=()), [])
            self.assertEqual(config.filter_render_packages_by_target(target_packages=["pkg_b"]), ["pkg_b"])
            with self.assertRaises(ValueError):
                config.filter_render_packages_by_target(target_packages=["pkg_c"])
            self.assertEqual(config.filter_render_packages_by_target(target_packages=["pkg_b", "pkg_c"], missing_ok=True), ["pkg_b"])

            # 3. filter_install_packages_by_target
            # install/ only has pkg_a
            self.assertEqual(config.filter_install_packages_by_target(target_packages=None), ["pkg_a"])
            self.assertEqual(config.filter_install_packages_by_target(target_packages=()), [])
            with self.assertRaises(ValueError):
                config.filter_install_packages_by_target(target_packages=["pkg_b"])
            self.assertEqual(config.filter_install_packages_by_target(target_packages=["pkg_a", "pkg_b"], missing_ok=True), ["pkg_a"])
            self.assertEqual(config.filter_install_packages_by_target(target_packages=["pkg_b"], missing_ok=True), [])

            # 4. filter_custom_dir_packages_by_target
            custom_dir = root / "custom"
            (custom_dir / "pkg_x").mkdir(parents=True)
            (custom_dir / "pkg_x" / PACKAGE_CONFIG_FILE_NAME).touch()
            self.assertEqual(config.filter_custom_dir_packages_by_target(custom_dir, target_packages=None), [])
            config.packages_enable_default = True
            self.assertEqual(config.filter_custom_dir_packages_by_target(custom_dir, target_packages=None), ["pkg_x"])
            self.assertEqual(config.filter_custom_dir_packages_by_target(custom_dir, target_packages=()), [])
            self.assertEqual(config.filter_custom_dir_packages_by_target(custom_dir, target_packages=["pkg_x", "pkg_non_existent"], missing_ok=True), ["pkg_x"])

            # 5. filter_given_packages_by_target without error_context_dir
            with self.assertRaises(ValueError) as ctx:
                config.filter_given_packages_by_target(["pkg_1"], ["pkg_2"])
            self.assertEqual(str(ctx.exception), "Given target packages not found: ['pkg_2']")
            self.assertEqual(config.filter_given_packages_by_target(["pkg_1"], ["pkg_1", "pkg_2"], missing_ok=True), ["pkg_1"])
            self.assertEqual(config.filter_given_packages_by_target(["pkg_1"], ["pkg_2"], missing_ok=True), [])

    def test_load_workspace_config_layered_functions(self) -> None:
        """Verifies load_workspace_config_file_with_render and load_workspace_config_files_layered functions."""
        from drift.config.workspace_config import (
            render_workspace_config,
            load_workspace_config_file_with_render,
            load_workspace_config_files_layered,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg_dir = root / "config"
            cfg_dir.mkdir(parents=True)

            base_file = cfg_dir / "drift_workspace.toml"
            override_file = cfg_dir / "drift_workspace.local.toml"
            tmpl_file = cfg_dir / "drift_workspace.envst.toml"

            # 1. Test load_workspace_config_file_with_render on non-existent file returns None
            self.assertIsNone(load_workspace_config_file_with_render(base_file))

            # 2. Test render_workspace_config and template loading
            tmpl_file.write_text("""
            [workspace]
            source_directory = "$TEST_SRC_DIR"
            """, encoding="utf-8")
            with patch.dict(os.environ, {"TEST_SRC_DIR": "my_src"}):
                rendered_str = render_workspace_config(tmpl_file)
                self.assertIn('source_directory = "my_src"', rendered_str)
                loaded_dict = load_workspace_config_file_with_render(base_file)
                self.assertIsNotNone(loaded_dict)
                self.assertEqual(loaded_dict["workspace"]["source_directory"], "my_src")

            # 3. Static base file takes precedence over template
            base_file.write_text("""
            [workspace]
            source_directory = "static_src"
            render_directory = "render"
            """, encoding="utf-8")
            loaded_static = load_workspace_config_file_with_render(base_file)
            self.assertEqual(loaded_static["workspace"]["source_directory"], "static_src")

            # 4. Layered loading merges base and override
            override_file.write_text("""
            [workspace]
            render_directory = "custom_render"
            """, encoding="utf-8")
            merged = load_workspace_config_files_layered([base_file, override_file])
            self.assertEqual(merged["workspace"]["source_directory"], "static_src")
            self.assertEqual(merged["workspace"]["render_directory"], "custom_render")

            # 5. Layered loading raises ConfigError when none exist
            non_existent = root / "non_existent.toml"
            with self.assertRaises(ConfigError) as ctx:
                load_workspace_config_files_layered([non_existent])
            self.assertIn("Workspace configuration file not found", str(ctx.exception))

    def test_render_engine_strip_suffix(self) -> None:
        """Verifies RenderEngineConfig.strip_suffix strips engine suffix segment correctly from the filename."""
        from drift.config.workspace_config import RenderEngineConfig
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
        from drift.config.package_config import PackageConfig
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        config = WorkspaceConfig(
            drift_root=Path("/dummy/root"),
            workspace=WorkspaceSectionConfig(default_target_directory=Path("/global/target")),
        )
        pkg = PackageConfig(
            name="my_pkg",
            target_directory=Path("/custom/target"),
            install_method=InstallMethod.COPY
        )

        # 1. Load with workspace config
        saved = pkg.load_package_envs(config)
        self.assertEqual(os.environ.get("drift_package_name"), "my_pkg")
        self.assertEqual(os.environ.get("drift_package_target_dir"), "/custom/target")
        self.assertEqual(os.environ.get("drift_package_source_dir"), str(config.source_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_package_render_dir"), str(config.render_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_package_install_dir"), str(config.install_path / "my_pkg"))
        self.assertEqual(os.environ.get("drift_package_install_method"), "copy")

        # 2. Unload
        pkg.unload_package_envs(saved)
        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_source_dir", os.environ)
        self.assertNotIn("drift_package_render_dir", os.environ)
        self.assertNotIn("drift_package_install_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

        # 4. Context manager usage with 'with'
        with pkg.package_envs(config):
            self.assertEqual(os.environ.get("drift_package_name"), "my_pkg")
            self.assertEqual(os.environ.get("drift_package_target_dir"), "/custom/target")
            self.assertEqual(os.environ.get("drift_package_source_dir"), str(config.source_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_package_render_dir"), str(config.render_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_package_install_dir"), str(config.install_path / "my_pkg"))
            self.assertEqual(os.environ.get("drift_package_install_method"), "copy")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_source_dir", os.environ)
        self.assertNotIn("drift_package_render_dir", os.environ)
        self.assertNotIn("drift_package_install_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

    def test_package_envs_resolution_with_custom_workspace_target_and_install_method(self) -> None:
        """Verifies environment variable resolution when workspace target != '~' and package has/has not explicit target."""
        from drift.config.package_config import PackageConfig
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        # Workspace with non-default target directory != '~' and non-default install method
        custom_global_target = Path("/opt/custom_drift_target")
        workspace_config = WorkspaceConfig(
            drift_root=Path("/dummy/root"),
            workspace=WorkspaceSectionConfig(
                default_target_directory=custom_global_target,
                default_install_method=InstallMethod.COPY,
            ),
        )

        # 1. Package WITHOUT explicit target_directory and WITHOUT explicit install_method
        pkg_inherited = PackageConfig(name="pkg_inherited")
        with pkg_inherited.package_envs(workspace_config):
            self.assertEqual(os.environ.get("drift_package_name"), "pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_target_dir"), str(custom_global_target.expanduser()))
            self.assertEqual(os.environ.get("drift_package_source_dir"), "/dummy/root/src/pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_render_dir"), "/dummy/root/render/pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_install_dir"), "/dummy/root/install/pkg_inherited")
            self.assertEqual(os.environ.get("drift_package_install_method"), "copy")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

        # 2. Package WITH explicit target_directory and explicit install_method
        pkg_overridden = PackageConfig(
            name="pkg_overridden",
            target_directory=Path("/etc/custom_pkg_target"),
            install_method=InstallMethod.STOW
        )
        with pkg_overridden.package_envs(workspace_config):
            self.assertEqual(os.environ.get("drift_package_name"), "pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_target_dir"), "/etc/custom_pkg_target")
            self.assertEqual(os.environ.get("drift_package_source_dir"), "/dummy/root/src/pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_render_dir"), "/dummy/root/render/pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_install_dir"), "/dummy/root/install/pkg_overridden")
            self.assertEqual(os.environ.get("drift_package_install_method"), "stow")

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

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
            self.assertEqual(os.environ.get("drift_package_install_method"), "copy")  # Inherited copy

        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

    def test_package_source_directory_config(self) -> None:
        """Verifies parsing, defaults, validation, and resolution of source_directory."""
        # 1. Default source_directory is Path(".")
        pkg_default = PackageConfig(name="default_pkg")
        self.assertEqual(pkg_default.source_directory, Path("."))
        base_dir = Path("/home/user/workspace/src/default_pkg")
        self.assertEqual(pkg_default.get_source_directory_to_render(base_dir), base_dir)

        # 2. Custom relative source_directory
        pkg_custom = PackageConfig(name="custom_pkg", source_directory=Path("dotfiles/config"))
        self.assertEqual(pkg_custom.source_directory, Path("dotfiles/config"))
        self.assertEqual(pkg_custom.get_source_directory_to_render(base_dir), base_dir / "dotfiles/config")

        # 3. from_dict parsing
        data = {
            "package": {
                "source_directory": "src_subfolder"
            }
        }
        pkg_from_dict = PackageConfig.from_dict(data, package_name="parsed_pkg", base_dir=base_dir)
        self.assertEqual(pkg_from_dict.source_directory, Path("src_subfolder"))
        self.assertEqual(pkg_from_dict.get_source_directory_to_render(base_dir), base_dir / "src_subfolder")

        # 4. Type validation error on from_dict
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {"source_directory": 123}}, package_name="bad_pkg", base_dir=base_dir)

        # 5. Absolute path rejected
        with self.assertRaises(ConfigError):
            pkg_abs = PackageConfig(name="abs_pkg", source_directory=Path("/absolute/path"))
            pkg_abs.validate()

        # 6. Path traversal escaping package dir rejected
        with self.assertRaises(ConfigError):
            pkg_escape = PackageConfig(name="escape_pkg", source_directory=Path("../other_pkg"))
            pkg_escape.validate()

    def test_package_env_override_and_fallback_parsing(self) -> None:
        """Verifies parsing of [env.override], [env.overwrite], [env.fallback] and flat [env] tables."""
        # 1. Parsing [env.override] and [env.fallback]
        data = {
            "package": {"name": "test_pkg"},
            "env": {
                "override": {"THEME": "catppuccin", "DEBUG": "1"},
                "fallback": {"FALLBACK_KEY": "default_val"}
            }
        }
        pkg = PackageConfig.from_dict(data, package_name="test_pkg", base_dir=Path("/test/pkg"))
        self.assertEqual(pkg.env_override, {"THEME": "catppuccin", "DEBUG": "1"})
        self.assertEqual(pkg.env_fallback, {"FALLBACK_KEY": "default_val"})

        # 2. Parsing alias [env.overwrite]
        data_alias = {
            "package": {"name": "test_pkg"},
            "env": {
                "overwrite": {"THEME": "nord"}
            }
        }
        pkg_alias = PackageConfig.from_dict(data_alias, package_name="test_pkg", base_dir=Path("/test/pkg"))
        self.assertEqual(pkg_alias.env_override, {"THEME": "nord"})

        # 3. Flat [env] keys raise ConfigError
        data_flat = {
            "package": {"name": "test_pkg"},
            "env": {
                "CUSTOM_KEY": "custom_val"
            }
        }
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict(data_flat, package_name="test_pkg", base_dir=Path("/test/pkg"))

        # 4. Error on non-dict sub-tables
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {}, "env": {"override": "not_a_dict"}}, package_name="err_pkg", base_dir=Path("/test/pkg"))
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {}, "env": {"fallback": "not_a_dict"}}, package_name="err_pkg", base_dir=Path("/test/pkg"))
        with self.assertRaises(ConfigError):
            PackageConfig.from_dict({"package": {}, "env": {"unknown_subtable": {"k": "v"}}}, package_name="err_pkg", base_dir=Path("/test/pkg"))

    def test_seven_tier_variable_preemption_order(self) -> None:
        """Verifies the complete 7-tier environment variable preemption hierarchy."""
        from drift.core.constants import set_initial_env, update_initial_env
        from drift.config.workspace_config import WorkspaceConfig

        # Setup workspace config
        workspace_config = WorkspaceConfig(
            drift_root=Path("/test/workspace"),
        )

        # Base system fact is present
        os.environ["drift_os"] = "linux"
        os.environ["GLOBAL_VAR"] = "from_workspace"
        os.environ["CLI_VAR"] = "from_cli"
        os.environ["OVERRIDDEN_BY_PACKAGE"] = "from_workspace"
        os.environ["FALLBACK_TEST"] = "from_workspace"

        # Mark CLI_VAR as coming from CLI invocation
        set_initial_env(["CLI_VAR"])

        pkg = PackageConfig(
            name="demo_pkg",
            env_override={
                "OVERRIDDEN_BY_PACKAGE": "package_override_value",
                "CLI_VAR": "attempted_pkg_override",
            },
            env_fallback={
                "FALLBACK_TEST": "fallback_should_not_overwrite",
                "NEW_FALLBACK_VAR": "fallback_activated",
            }
        )

        with pkg.package_envs(workspace_config):
            # Tier 1: CLI variable wins over package [env.override]
            self.assertEqual(os.environ.get("CLI_VAR"), "from_cli")

            # Tier 2: Package [env.override] wins over workspace
            self.assertEqual(os.environ.get("OVERRIDDEN_BY_PACKAGE"), "package_override_value")

            # Tier 3: Package facts are loaded
            self.assertEqual(os.environ.get("drift_package_name"), "demo_pkg")
            self.assertEqual(os.environ.get("drift_package_install_method"), "copy" if sys.platform == "win32" else "stow")

            # Tier 4: System facts are preserved
            self.assertEqual(os.environ.get("drift_os"), "linux")

            # Tier 6: Workspace env remains if not overridden
            self.assertEqual(os.environ.get("GLOBAL_VAR"), "from_workspace")

            # Tier 7: Package fallback does NOT overwrite existing workspace env, but fills new var
            self.assertEqual(os.environ.get("FALLBACK_TEST"), "from_workspace")
            self.assertEqual(os.environ.get("NEW_FALLBACK_VAR"), "fallback_activated")

        # After context exit: package variables are cleanly restored
        self.assertEqual(os.environ.get("OVERRIDDEN_BY_PACKAGE"), "from_workspace")
        self.assertNotIn("NEW_FALLBACK_VAR", os.environ)
        self.assertNotIn("drift_package_name", os.environ)

        # Cleanup
        os.environ.pop("GLOBAL_VAR", None)
        os.environ.pop("CLI_VAR", None)
        os.environ.pop("OVERRIDDEN_BY_PACKAGE", None)
        os.environ.pop("FALLBACK_TEST", None)
        update_initial_env()


class TestSettingsConfig(unittest.TestCase):
    """Tests for SettingsConfig and [settings] in drift_workspace.toml."""

    def test_settings_config_defaults(self) -> None:
        from drift.config.workspace_config import SettingsConfig
        settings = SettingsConfig()
        self.assertTrue(settings.hook_inject_non_interactive_envs)

    def test_settings_config_from_dict(self) -> None:
        from drift.config.workspace_config import SettingsConfig
        s1 = SettingsConfig.from_dict({"hook_inject_non_interactive_envs": False})
        self.assertFalse(s1.hook_inject_non_interactive_envs)

    def test_settings_config_from_dict_aliases(self) -> None:
        from drift.config.workspace_config import SettingsConfig
        s2 = SettingsConfig.from_dict({"hook_inject_non_interactive_env": False})
        self.assertFalse(s2.hook_inject_non_interactive_envs)

        s3 = SettingsConfig.from_dict({"inject_hook_non_interactive_envs": False})
        self.assertFalse(s3.hook_inject_non_interactive_envs)

        s4 = SettingsConfig.from_dict({})
        self.assertTrue(s4.hook_inject_non_interactive_envs)

    def test_settings_config_validation(self) -> None:
        from drift.config.workspace_config import SettingsConfig
        from drift.core.exceptions import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            SettingsConfig.from_dict({"unknown_setting": True})
        self.assertIn("Unknown option under [settings]: 'unknown_setting'", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            SettingsConfig.from_dict({"unknown_1": 1, "unknown_2": 2})
        self.assertIn("Unknown option under [settings]:", str(ctx.exception))
        self.assertIn("'unknown_1'", str(ctx.exception))
        self.assertIn("'unknown_2'", str(ctx.exception))

        with self.assertRaises(ConfigError):
            SettingsConfig.from_dict({"hook_inject_non_interactive_envs": "not_a_bool"})

    def test_workspace_config_with_settings(self) -> None:
        toml_content = """
        [workspace]
        source_directory = "src"
        render_directory = "render"
        install_directory = "install"
        backup_directory = "backup"
        default_target_directory = "~"
        default_install_method = "stow"

        [packages.enable]
        DEFAULT = true

        [settings]
        hook_inject_non_interactive_envs = false
        """
        data = parse_toml(toml_content)
        ws_cfg = WorkspaceConfig.from_dict(data, drift_root=Path("/tmp/workspace"))
        self.assertFalse(ws_cfg.settings.hook_inject_non_interactive_envs)

    def test_class_constants(self) -> None:
        """Verifies schema and key ClassVars on configuration classes."""
        self.assertEqual(WorkspaceConfig.PACKAGES_ENABLE_DEFAULT_KEY, "DEFAULT")
        self.assertEqual(WorkspaceConfig.WORKSPACE_PACKAGES_DEFAULT_KEY, "DEFAULT")
        self.assertIn("settings", WorkspaceConfig.KNOWN_TOP_SECTIONS)
        self.assertIn("workspace", WorkspaceConfig.KNOWN_TOP_SECTIONS)

        self.assertIn("source_directory", WorkspaceSectionConfig.KNOWN_KEYS)
        self.assertIn("hook_inject_non_interactive_envs", SettingsConfig.HOOK_INJECT_NON_INTERACTIVE_ENVS_KEYS)
        self.assertIn("hook_inject_non_interactive_envs", SettingsConfig.KNOWN_KEYS)

        self.assertIn("input_file", RenderEngineConfig.KNOWN_KEYS)
        self.assertIn("os", PackageRequirements.KNOWN_KEYS)
        self.assertIn("ip", PackageRequirements.IP_KEYS)
        self.assertIn("package", PackageConfig.KNOWN_TOP_SECTIONS)
        self.assertIn("source_directory", PackageConfig.KNOWN_PACKAGE_KEYS)


class TestWorkspaceSectionConfig(unittest.TestCase):
    """Tests for WorkspaceSectionConfig and [workspace] in drift_workspace.toml."""

    def test_workspace_section_defaults(self) -> None:
        ws_sec = WorkspaceSectionConfig()
        self.assertEqual(ws_sec.source_directory, Path("src"))
        self.assertEqual(ws_sec.render_directory, Path("render"))
        self.assertEqual(ws_sec.install_directory, Path("install"))
        self.assertEqual(ws_sec.backup_directory, Path("backup"))
        self.assertEqual(ws_sec.default_target_directory, Path("~").expanduser())
        self.assertEqual(ws_sec.default_install_method, "stow")
        self.assertIsNone(ws_sec.hook_file)

    def test_workspace_section_from_dict(self) -> None:
        data = {
            "source_directory": "custom_src",
            "render_directory": "custom_render",
            "install_directory": "custom_install",
            "backup_directory": "custom_backup",
            "default_target_directory": "/tmp/custom_target",
            "default_install_method": "copy",
            "hook_file": "custom_hook.py",
        }
        ws_sec = WorkspaceSectionConfig.from_dict(data)
        self.assertEqual(ws_sec.source_directory, Path("custom_src"))
        self.assertEqual(ws_sec.render_directory, Path("custom_render"))
        self.assertEqual(ws_sec.install_directory, Path("custom_install"))
        self.assertEqual(ws_sec.backup_directory, Path("custom_backup"))
        self.assertEqual(ws_sec.default_target_directory, Path("/tmp/custom_target"))
        self.assertEqual(ws_sec.default_install_method, "copy")
        self.assertEqual(ws_sec.hook_file, Path("custom_hook.py"))

    def test_workspace_section_validation(self) -> None:
        with self.assertRaises(ConfigError):
            WorkspaceSectionConfig(source_directory=123).validate()  # type: ignore
        with self.assertRaises(ConfigError):
            WorkspaceSectionConfig(source_directory=Path("")).validate()
        with self.assertRaises(ConfigError):
            WorkspaceSectionConfig(default_target_directory=Path("relative/path")).validate()
        with self.assertRaises(ConfigError):
            WorkspaceSectionConfig(default_install_method="invalid_method").validate()

    def test_get_package_names_from_dir_filtering_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            # Create valid packages
            (base_dir / "zsh").mkdir()
            (base_dir / "nvim").mkdir()

            # Create hidden directory and files
            (base_dir / ".git").mkdir()
            (base_dir / ".hidden_pkg").mkdir()
            (base_dir / ".gitignore").write_text("# ignore")
            (base_dir / "state.toml").write_text("# state")

            # Create reserved/forbidden directories
            (base_dir / "config").mkdir()
            (base_dir / "install").mkdir()

            with patch("drift.config.workspace_config.logger.warning") as mock_warn:
                packages = WorkspaceConfig.get_package_names_from_dir(base_dir)
                self.assertEqual(packages, ["nvim", "zsh"])

                # Check that warnings were emitted for forbidden folders
                warn_calls = [call[0][0] for call in mock_warn.call_args_list]
                self.assertTrue(any("config" in msg for msg in warn_calls))
                self.assertTrue(any("install" in msg for msg in warn_calls))

    def test_get_package_names_from_source_dir_skips_dot_folders(self) -> None:
        """Verifies that all dot-named folders in src/ and render/.config are skipped cleanly without warnings."""
        with tempfile.TemporaryDirectory() as td:
            drift_root = Path(td)
            src_dir = drift_root / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "pkg_one").mkdir()
            (src_dir / "pkg_two").mkdir()
            (src_dir / ".git").mkdir()
            (src_dir / ".cache").mkdir()
            (src_dir / ".hidden_folder").mkdir()
            (src_dir / ".config").mkdir()

            render_dir = drift_root / "render"
            render_dir.mkdir(parents=True)
            (render_dir / ".config").mkdir()
            (render_dir / ".git").mkdir()
            (render_dir / "pkg_one").mkdir()
            (render_dir / "pkg_one" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (render_dir / "pkg_one" / DRIFT_INTERNAL_DIR_NAME / "drift_package.toml").write_text("[package]\nname = 'pkg_one'\n")

            ws_cfg = WorkspaceConfig(drift_root=drift_root, packages_enable_default=True)
            with patch("drift.config.workspace_config.logger.warning") as mock_warn:
                src_pkgs = ws_cfg.get_package_names_from_source_dir()
                self.assertEqual(src_pkgs, ["pkg_one", "pkg_two"])

                rendered_pkgs = ws_cfg.filter_render_packages_by_target()
                self.assertEqual(rendered_pkgs, ["pkg_one"])

                # No warnings should be emitted for any dot-named folders
                self.assertEqual(mock_warn.call_count, 0)


class TestPathSuffixHelpers(unittest.TestCase):
    def test_path_and_string_suffix_helpers(self) -> None:
        from drift.core.constants import (
            add_suffix_path,
            add_local_path,
            add_envst_path,
            add_suffix_str,
            add_local_str,
            add_envst_str,
        )

        p = Path("config/drift_workspace.toml")
        self.assertEqual(add_suffix_path(p, "local"), Path("config/drift_workspace.local.toml"))
        self.assertEqual(add_local_path(p), Path("config/drift_workspace.local.toml"))
        self.assertEqual(add_envst_path(p), Path("config/drift_workspace.envst.toml"))
        self.assertEqual(add_envst_path(Path("config/drift.local.toml")), Path("config/drift.local.envst.toml"))

        self.assertEqual(add_suffix_str("drift_workspace.toml", "local"), "drift_workspace.local.toml")
        self.assertEqual(add_local_str("drift_workspace.toml"), "drift_workspace.local.toml")
        self.assertEqual(add_envst_str("drift_workspace.toml"), "drift_workspace.envst.toml")
        self.assertEqual(
            add_envst_str("drift_root/config/drift.local.toml"),
            "drift_root/config/drift.local.envst.toml"
        )


class TestPackageHooksConfiguredPaths(unittest.TestCase):
    def test_package_hooks_get_configured_hook_paths(self) -> None:
        from drift.config.package_config import PackageHooks

        base1 = Path("/workspace/src/pkg1")
        base2 = Path("/workspace/render/pkg1")
        external = Path("/opt/shared/hooks/global.sh")

        hooks = PackageHooks(
            pre_source=base1 / "scripts/pre_source.sh",
            post_render=base2 / "scripts/post_render.sh",
            post_install=external,
        )

        # 1. Without relative_to, returns absolute POSIX paths
        abs_paths = hooks.get_configured_hook_paths()
        self.assertEqual(abs_paths, {
            "/workspace/src/pkg1/scripts/pre_source.sh",
            "/workspace/render/pkg1/scripts/post_render.sh",
            "/opt/shared/hooks/global.sh",
        })

        # 2. With relative_to bases, returns relative paths for matched bases and absolute for external
        rel_paths = hooks.get_configured_hook_paths(relative_to=[base1, base2])
        self.assertEqual(rel_paths, {
            "scripts/pre_source.sh",
            "scripts/post_render.sh",
            "/opt/shared/hooks/global.sh",
        })


class TestLegacyPackageConfigFallback(unittest.TestCase):
    """Tests backward-compatibility fallback for legacy root drift_package.toml in render/ and install/."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        self.pkg_dir = self.base_path / "my_pkg"
        self.pkg_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_from_render_dir_canonical_dot_drift(self) -> None:
        """Loads canonical .drift/drift_package.toml without warnings."""
        dot_drift = self.pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'my_pkg'\n", encoding="utf-8")

        cfg = load_package_config_from_render_dir(self.pkg_dir)
        self.assertEqual(cfg.name, "my_pkg")

    def test_load_from_render_dir_legacy_root_fallback_with_warning(self) -> None:
        """Loads legacy root drift_package.toml with deprecation warning."""
        (self.pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'my_pkg'\n", encoding="utf-8")

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.config.package_config", level="WARNING") as cm:
                cfg = load_package_config_from_render_dir(self.pkg_dir)
                self.assertEqual(cfg.name, "my_pkg")
                self.assertTrue(any("DEPRECATION" in msg and "render" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_from_render_dir_missing_raises(self) -> None:
        """Raises RuntimeError when drift_package.toml is missing in both locations."""
        with self.assertRaises(RuntimeError) as ctx:
            load_package_config_from_render_dir(self.pkg_dir)
        self.assertIn("Failed to find", str(ctx.exception))

    def test_load_for_install_canonical_dot_drift(self) -> None:
        """Loads canonical .drift/drift_package.toml without warnings."""
        dot_drift = self.pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'my_pkg'\n", encoding="utf-8")

        cfg = load_package_config_for_install(self.pkg_dir)
        self.assertEqual(cfg.name, "my_pkg")

    def test_load_for_install_legacy_root_fallback_with_warning(self) -> None:
        """Loads legacy root drift_package.toml with deprecation warning."""
        (self.pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\nname = 'my_pkg'\n", encoding="utf-8")

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.config.package_config", level="WARNING") as cm:
                cfg = load_package_config_for_install(self.pkg_dir)
                self.assertEqual(cfg.name, "my_pkg")
                self.assertTrue(any("DEPRECATION" in msg and "install" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_for_install_missing_raises(self) -> None:
        """Raises FileNotFoundError or RuntimeError when drift_package.toml is missing in both locations."""
        with self.assertRaises((FileNotFoundError, RuntimeError)) as ctx:
            load_package_config_for_install(self.pkg_dir)
        self.assertIn("Missing required", str(ctx.exception))


class TestDumpToml(unittest.TestCase):
    """Unit tests for dump_toml serialization, multiline escaping, and roundtrip parsing."""

    def test_dump_toml_multiline_string_escaping(self) -> None:
        from drift.utils.toml_utils import dump_toml, parse_toml

        data = {
            "package": {
                "name": "my_pkg",
                "description": "Line 1\nLine 2\nLine 3",
                "notes": "Tab:\t, Windows Line:\r\n, Backslash: \\, Quotes: \"Hello\"",
            },
            "env": {
                "override": {
                    "SCRIPT": "echo 'hello'\necho 'world'\n",
                }
            }
        }

        toml_str = dump_toml(data)
        # Ensure raw literal line breaks are NOT in the serialized string literals
        for line in toml_str.splitlines():
            if line.startswith("description ="):
                self.assertIn(r"\n", line)
                self.assertNotIn("\n", line[len("description ="):])

        parsed = parse_toml(toml_str)
        self.assertEqual(parsed["package"]["name"], "my_pkg")
        self.assertEqual(parsed["package"]["description"], "Line 1\nLine 2\nLine 3")
        self.assertEqual(parsed["package"]["notes"], "Tab:\t, Windows Line:\r\n, Backslash: \\, Quotes: \"Hello\"")
        self.assertEqual(parsed["env"]["override"]["SCRIPT"], "echo 'hello'\necho 'world'\n")

    def test_dump_toml_roundtrip_data_types(self) -> None:
        from drift.utils.toml_utils import dump_toml, parse_toml

        data = {
            "version": 1,
            "debug": True,
            "ratio": 3.14,
            "tags": ["a\nb", "c", "d"],
            "empty": None,
            "package": {
                "enable_render": False,
                "count": 42,
            }
        }

        toml_str = dump_toml(data)
        parsed = parse_toml(toml_str)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["debug"], True)
        self.assertEqual(parsed["ratio"], 3.14)
        self.assertEqual(parsed["tags"], ["a\nb", "c", "d"])
        self.assertNotIn("empty", parsed)
        self.assertEqual(parsed["package"]["enable_render"], False)
        self.assertEqual(parsed["package"]["count"], 42)


if __name__ == "__main__":
    unittest.main()


