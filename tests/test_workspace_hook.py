"""Tests for the dynamic Python workspace hook (config/drift_workspace.py)."""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.constants import set_test_mode, CONFIG_DIR_NAME, WORKSPACE_CONFIG_FILE_NAME
from drift.exceptions import ConfigError
from drift.workspace_config import load_workspace_config
from drift.workspace_hook import WorkspaceHookContext, apply_workspace_hook


class TestWorkspaceHook(unittest.TestCase):
    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        # Create standard layout
        (self.drift_root / "src").mkdir(parents=True)
        (self.drift_root / "src" / "pkg1").mkdir()
        (self.drift_root / "src" / "pkg2").mkdir()
        (self.drift_root / CONFIG_DIR_NAME).mkdir(parents=True)

        self.config_file = self.drift_root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME
        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
default_install_method = "stow"

[packages.enable]
pkg1 = true
pkg2 = false

[env]
BASE_URL = "https://example.com"
""", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_no_hook_file_loads_normally(self) -> None:
        """Workspaces without drift_workspace.py load static TOML normally."""
        cfg = load_workspace_config(self.drift_root)
        self.assertTrue(cfg.is_package_enabled("pkg1"))
        self.assertFalse(cfg.is_package_enabled("pkg2"))
        self.assertEqual(cfg.env.get("BASE_URL"), "https://example.com")
        self.assertIsNone(cfg.workspace.hook_file)

    def test_default_hook_file_transforms_packages_and_env(self) -> None:
        """Default config/drift_workspace.py can dynamically modify packages and env."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    cfg = context.config
    # Enable pkg2 conditionally
    cfg["packages"]["enable"]["pkg2"] = True
    # Inject dynamic env
    cfg["env"]["DYNAMIC_PORT"] = "8080"
    cfg["env"]["FULL_API"] = "${BASE_URL}:${DYNAMIC_PORT}/api"
    return cfg
""", encoding="utf-8")

        cfg = load_workspace_config(self.drift_root)
        self.assertTrue(cfg.is_package_enabled("pkg1"))
        self.assertTrue(cfg.is_package_enabled("pkg2"))
        self.assertEqual(cfg.env.get("DYNAMIC_PORT"), "8080")
        self.assertEqual(cfg.env.get("FULL_API"), "https://example.com:8080/api")

    def test_hook_accesses_context_properties(self) -> None:
        """Hook can inspect context.drift_root, context.facts, context.env, and context.discovered_packages."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    cfg = context.config
    assert str(context.drift_root) in str(cfg) or True
    assert isinstance(context.discovered_packages, list)
    assert "pkg1" in context.discovered_packages
    assert "pkg2" in context.discovered_packages
    assert isinstance(context.facts, dict)

    # Use facts to set an env var
    os_name = context.facts.get("drift_os", "unknown")
    cfg["env"]["DETECTED_OS"] = os_name
    return cfg
""", encoding="utf-8")

        cfg = load_workspace_config(self.drift_root)
        self.assertIn("DETECTED_OS", cfg.env)
        self.assertTrue(len(cfg.env["DETECTED_OS"]) > 0)

    def test_custom_hook_file_in_workspace_config(self) -> None:
        """Custom hook_file defined in [workspace] is executed."""
        custom_hook = self.drift_root / CONFIG_DIR_NAME / "custom_hook.py"
        custom_hook.write_text("""
def configure_workspace(context):
    context.config.setdefault("env", {})["CUSTOM_HOOK_RAN"] = "yes"
    return context.config
""", encoding="utf-8")

        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
hook_file = "config/custom_hook.py"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        cfg = load_workspace_config(self.drift_root)
        self.assertEqual(cfg.env.get("CUSTOM_HOOK_RAN"), "yes")
        self.assertEqual(str(cfg.workspace.hook_file), "config/custom_hook.py")

    def test_custom_hook_file_missing_raises_config_error(self) -> None:
        """Specifying a non-existent hook_file in [workspace] raises ConfigError."""
        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
hook_file = "config/nonexistent_hook.py"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("nonexistent_hook.py", str(cm.exception))

    def test_hook_missing_configure_workspace_func_raises_config_error(self) -> None:
        """Hook file without configure_workspace() raises ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
# Missing configure_workspace
def some_other_function():
    pass
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("must define a callable 'configure_workspace(context)'", str(cm.exception))

    def test_hook_returning_none_raises_config_error(self) -> None:
        """Hook function returning None raises ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    # Forgot return
    context.config["packages"]["enable"]["pkg2"] = True
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("returned None", str(cm.exception))

    def test_hook_returning_non_dict_raises_config_error(self) -> None:
        """Hook function returning a non-dict raises ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    return ["not", "a", "dict"]
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("must return a dictionary", str(cm.exception))

    def test_hook_syntax_error_raises_config_error(self) -> None:
        """Syntax errors in the hook module raise ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("def invalid_syntax(:", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("Failed to load workspace hook", str(cm.exception))

    def test_hook_runtime_exception_raises_config_error(self) -> None:
        """Exceptions raised inside configure_workspace() are wrapped in ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    raise ValueError("Something went wrong during dynamic config calculation!")
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            load_workspace_config(self.drift_root)
        self.assertIn("Error executing workspace hook", str(cm.exception))
        self.assertIn("Something went wrong", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
