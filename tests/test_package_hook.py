"""Tests for the dynamic Python package hook (src/<pkg>/drift_package.py)."""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.constants import (
    set_test_mode,
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    DEFAULT_PACKAGE_HOOK_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
)
from drift.exceptions import ConfigError
from drift.workspace_config import load_workspace_config
from drift.package_config import PackageConfig
from drift.package_hook import (
    PackageHookContext,
    resolve_package_hook_path,
    load_package_hook_module,
    execute_package_hook,
    apply_package_hook,
)
from drift.render_package import render_package


class TestPackageHook(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        # Create standard layout
        (self.drift_root / "src").mkdir(parents=True)
        (self.drift_root / "src" / "pkg1").mkdir()
        (self.drift_root / "src" / "pkg2").mkdir()
        (self.drift_root / CONFIG_DIR_NAME).mkdir(parents=True)

        self.workspace_config_file = self.drift_root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME
        self.workspace_config_file.write_text("""
[workspace]
default_target_directory = "~"
default_install_method = "stow"

[packages.enable]
pkg1 = true
pkg2 = true

[env]
WORKSPACE_BASE = "https://example.com"
""", encoding="utf-8")

        # Standard pkg1 config
        (self.drift_root / "src" / "pkg1" / PACKAGE_CONFIG_FILE_NAME).write_text("""
[package]
install_method = "stow"
target_directory = "~/.config/pkg1"

[env.fallback]
PKG_PORT = "3000"
""", encoding="utf-8")

        self.workspace_config = load_workspace_config(self.drift_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_no_hook_file_loads_normally(self) -> None:
        """Packages without drift_package.py load static TOML normally."""
        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(cfg.name, "pkg1")
        self.assertEqual(cfg.install_method, "stow")
        self.assertEqual(cfg.target_directory, Path("~/.config/pkg1").expanduser())
        self.assertIsNone(cfg.hook_file)

    def test_default_hook_file_transforms_package_config(self) -> None:
        """Default src/<pkg>/drift_package.py can dynamically modify package settings."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    # Dynamically change install_method and target_directory
    pkg["install_method"] = "copy"
    pkg["target_directory"] = "~/custom/pkg1"
    
    # Inject requirements
    reqs = pkg.setdefault("requirements", {})
    reqs["os"] = ["linux", "darwin"]
    reqs["binaries"] = ["git"]
    
    # Inject env override
    env_override = cfg.setdefault("env", {}).setdefault("override", {})
    env_override["DYNAMIC_PKG_VAR"] = "hello_hook"
    
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(cfg.install_method, "copy")
        self.assertEqual(cfg.target_directory, Path("~/custom/pkg1").expanduser())
        self.assertIsNotNone(cfg.requirements)
        self.assertEqual(cfg.requirements.os, ["linux", "darwin"])
        self.assertEqual(cfg.requirements.binaries, ["git"])
        self.assertEqual(cfg.env_override.get("DYNAMIC_PKG_VAR"), "hello_hook")

    def test_hook_controls_enable_install_flag(self) -> None:
        """Hook can set enable_install = False to disable package deployment on incompatible machines."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    # Disable install if os is not windows (test machine is linux/darwin)
    if context.os != "windows":
        pkg["enable_install"] = False
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertFalse(cfg.enable_install)

    def test_hook_accesses_context_properties_and_facts(self) -> None:
        """Hook can inspect package_name, package_dir, drift_root, workspace_config, and system/package facts."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    assert context.package_name == "pkg1"
    assert context.package_dir.name == "pkg1"
    assert context.drift_root is not None
    assert context.workspace_config is not None
    assert isinstance(context.facts, dict)
    assert isinstance(context.package_facts, dict)
    assert context.package_facts["drift_package_name"] == "pkg1"
    
    # Check helper properties
    assert isinstance(context.os, str)
    assert isinstance(context.arch, str)
    assert isinstance(context.distro, str)
    assert isinstance(context.hostname, str)
    assert isinstance(context.user, str)
    
    env_override = cfg.setdefault("env", {}).setdefault("override", {})
    env_override["HOOK_ACCESSED_OS"] = context.os
    env_override["HOOK_ACCESSED_PKG"] = context.package_facts["drift_package_name"]
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("HOOK_ACCESSED_OS", cfg.env_override)
        self.assertEqual(cfg.env_override.get("HOOK_ACCESSED_PKG"), "pkg1")

    def test_custom_hook_file_relative_path(self) -> None:
        """Custom hook_file defined in [package] is resolved relative to package directory."""
        custom_hook_dir = self.drift_root / "src" / "pkg1" / "hooks"
        custom_hook_dir.mkdir(parents=True)
        custom_hook = custom_hook_dir / "custom_setup.py"
        custom_hook.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    pkg["install_method"] = "copy"
    return cfg
""", encoding="utf-8")

        (self.drift_root / "src" / "pkg1" / PACKAGE_CONFIG_FILE_NAME).write_text("""
[package]
install_method = "stow"
target_directory = "~/.config/pkg1"
hook_file = "hooks/custom_setup.py"
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(cfg.install_method, "copy")
        self.assertEqual(cfg.hook_file, (pkg_dir / "hooks/custom_setup.py").resolve())

    def test_custom_hook_file_absolute_path(self) -> None:
        """Custom hook_file specified as an absolute path is resolved properly."""
        custom_hook_file = self.drift_root / "external_hook.py"
        custom_hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    pkg["install_method"] = "copy"
    return cfg
""", encoding="utf-8")

        (self.drift_root / "src" / "pkg1" / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
[package]
install_method = "stow"
target_directory = "~/.config/pkg1"
hook_file = "{custom_hook_file.as_posix()}"
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(cfg.install_method, "copy")

    def test_custom_hook_file_not_found_raises_config_error(self) -> None:
        """Missing custom hook_file raises ConfigError."""
        (self.drift_root / "src" / "pkg1" / PACKAGE_CONFIG_FILE_NAME).write_text("""
[package]
install_method = "stow"
target_directory = "~/.config/pkg1"
hook_file = "non_existent_hook.py"
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("not found", str(ctx.exception))

    def test_missing_configure_package_function_raises_config_error(self) -> None:
        """Hook file without configure_package() function raises ConfigError."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def some_other_function(context):
    return context.config
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("must define a callable 'configure_package(context)'", str(ctx.exception))

    def test_hook_returning_none_raises_config_error(self) -> None:
        """Hook function returning None raises ConfigError."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    pass
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("returned None", str(ctx.exception))

    def test_hook_returning_non_dict_raises_config_error(self) -> None:
        """Hook function returning non-dict raises ConfigError."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    return ["invalid", "return", "type"]
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("must return a dictionary", str(ctx.exception))

    def test_hook_syntax_or_runtime_exception_raises_config_error(self) -> None:
        """Hook syntax errors or runtime exceptions raise ConfigError."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    raise RuntimeError("Intentional hook runtime failure")
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertIn("Intentional hook runtime failure", str(ctx.exception))

    def test_fallback_mode_without_workspace_config(self) -> None:
        """Static loading without workspace_config (fallback mode) executes hook cleanly."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    pkg["install_method"] = "copy"
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, None)
        self.assertEqual(cfg.install_method, "copy")
        self.assertEqual(cfg.name, "pkg1")

    def test_hook_file_registered_in_source_files_and_filtered(self) -> None:
        """Hook files in package source_files are identified by is_package_config_file."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    return context.config
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertTrue(cfg.is_package_config_file(hook_file))
        self.assertTrue(cfg.is_package_config_file(pkg_dir / PACKAGE_CONFIG_FILE_NAME))
        self.assertFalse(cfg.is_package_config_file(pkg_dir / "dotfile.txt"))

    def test_single_compilation_and_rendered_pipeline(self) -> None:
        """Render pipeline compiles hook results into render/<pkg>/drift_package.toml."""
        # Create a dotfile payload in pkg1
        (self.drift_root / "src" / "pkg1" / "app.conf").write_text("setting=1", encoding="utf-8")

        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    pkg = cfg.setdefault("package", {})
    pkg["install_method"] = "copy"
    pkg["target_directory"] = "~/rendered_target"
    return cfg
""", encoding="utf-8")

        # Render package
        pkg_dir = self.drift_root / "src" / "pkg1"
        render_package(
            workspace_config=self.workspace_config,
            package_dir=pkg_dir,
        )

        rendered_pkg_dir = self.drift_root / "render" / "pkg1"
        self.assertTrue((rendered_pkg_dir / "app.conf").is_file())
        self.assertTrue((rendered_pkg_dir / PACKAGE_CONFIG_FILE_NAME).is_file())
        # Hook file itself should NOT be copied into render/ as a deployable payload
        self.assertFalse((rendered_pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME).is_file())

        # Load rendered config
        rendered_cfg = PackageConfig.from_render_dir(self.drift_root / "render" / "pkg1")
        self.assertEqual(rendered_cfg.install_method, "copy")
        self.assertEqual(rendered_cfg.target_directory, Path("~/rendered_target").expanduser())

        # PackageConfig.from_install_dir from install/ state dir should also work once staged
        (self.drift_root / "install" / "pkg1").mkdir(parents=True)
        shutil.copy2(
            rendered_pkg_dir / PACKAGE_CONFIG_FILE_NAME,
            self.drift_root / "install" / "pkg1" / PACKAGE_CONFIG_FILE_NAME
        )
        install_cfg = PackageConfig.from_install_dir(self.drift_root / "install" / "pkg1")
        self.assertEqual(install_cfg.install_method, "copy")
        self.assertEqual(install_cfg.target_directory, Path("~/rendered_target").expanduser())

    def test_hook_with_variable_stitching_and_interpolation(self) -> None:
        """Hook output properly participates in 7-tier variable stitching."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
def configure_package(context):
    cfg = context.config
    # Add override variable referencing workspace base
    env_override = cfg.setdefault("env", {}).setdefault("override", {})
    env_override["DYNAMIC_API"] = "${WORKSPACE_BASE}/v1"
    
    # Target directory referencing dynamic override variable
    pkg = cfg.setdefault("package", {})
    pkg["target_directory"] = "~/.config/app_${drift_package_name}"
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        cfg = PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertEqual(cfg.env_override.get("DYNAMIC_API"), "https://example.com/v1")
        self.assertEqual(cfg.target_directory, Path("~/.config/app_pkg1").expanduser())

    def test_env_scope_isolation_during_hook_execution(self) -> None:
        """Package facts (drift_package_*) are injected into os.environ during hook execution and cleaned up."""
        hook_file = self.drift_root / "src" / "pkg1" / DEFAULT_PACKAGE_HOOK_FILE_NAME
        hook_file.write_text("""
import os

def configure_package(context):
    cfg = context.config
    assert os.environ.get("drift_package_name") == "pkg1"
    assert "drift_package_source_dir" in os.environ
    assert "drift_package_render_dir" in os.environ
    assert "drift_package_install_dir" in os.environ
    return cfg
""", encoding="utf-8")

        pkg_dir = self.drift_root / "src" / "pkg1"
        self.assertNotIn("drift_package_name", os.environ)
        PackageConfig.from_source_dir(pkg_dir, self.workspace_config)
        self.assertNotIn("drift_package_name", os.environ)

    def test_resolve_package_hook_path_helper(self) -> None:
        """resolve_package_hook_path correctly resolves paths or returns None."""
        pkg_dir = self.drift_root / "src" / "pkg1"
        
        # 1. No hook present -> None
        self.assertIsNone(resolve_package_hook_path(pkg_dir, {}))
        
        # 2. Default hook present -> Path
        default_hook = pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        default_hook.write_text("def configure_package(context): return context.config\n", encoding="utf-8")
        self.assertEqual(resolve_package_hook_path(pkg_dir, {}), default_hook)
        
        # 3. Custom hook configured
        custom_hook = pkg_dir / "my_custom.py"
        custom_hook.write_text("def configure_package(context): return context.config\n", encoding="utf-8")
        cfg_dict = {"package": {"hook_file": "my_custom.py"}}
        self.assertEqual(resolve_package_hook_path(pkg_dir, cfg_dict), custom_hook)
        
        # 4. Custom hook missing -> ConfigError
        bad_dict = {"package": {"hook_file": "missing.py"}}
        with self.assertRaises(ConfigError):
            resolve_package_hook_path(pkg_dir, bad_dict)

    def test_apply_package_hook_returns_tuple(self) -> None:
        """apply_package_hook returns a tuple of (config_dict, hook_path)."""
        pkg_dir = self.drift_root / "src" / "pkg1"
        default_hook = pkg_dir / DEFAULT_PACKAGE_HOOK_FILE_NAME
        default_hook.write_text("""
def configure_package(context):
    cfg = context.config
    cfg["package"]["install_method"] = "copy"
    return cfg
""", encoding="utf-8")

        cfg_dict = {"package": {"install_method": "stow"}}
        res_dict, hook_path = apply_package_hook(pkg_dir, cfg_dict, self.workspace_config)
        self.assertEqual(hook_path, default_hook)
        self.assertEqual(res_dict["package"]["install_method"], "copy")


if __name__ == "__main__":
    unittest.main()

