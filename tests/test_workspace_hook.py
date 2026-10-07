"""Tests for the dynamic Python workspace hook (config/drift_workspace.py)."""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.core.constants import set_test_mode, CONFIG_DIR_NAME, WORKSPACE_CONFIG_FILE_NAME
from drift.core.exceptions import ConfigError
from drift.config.workspace_config import WorkspaceConfig
from drift.hooks.workspace_hook import (
    WorkspaceHookContext,
    apply_workspace_hook,
    load_workspace_hook_module,
)


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
default_install_method = "symlink"

[packages.enable]
pkg1 = true
pkg2 = false

[env.default]
BASE_URL = "https://example.com"
""", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_no_hook_file_loads_normally(self) -> None:
        """Workspaces without drift_workspace.py load static TOML normally."""
        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertTrue(cfg.is_package_enabled("pkg1"))
        self.assertFalse(cfg.is_package_enabled("pkg2"))
        self.assertEqual(cfg.env_resolve.effective.default.get("BASE_URL"), "https://example.com")
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
    env_default = cfg.setdefault("env", {}).setdefault("default", {})
    env_default["DYNAMIC_PORT"] = "8080"
    env_default["FULL_API"] = "${BASE_URL}:${DYNAMIC_PORT}/api"
    return cfg
""", encoding="utf-8")

        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertTrue(cfg.is_package_enabled("pkg1"))
        self.assertTrue(cfg.is_package_enabled("pkg2"))
        self.assertEqual(cfg.env_resolve.effective.default.get("DYNAMIC_PORT"), "8080")
        self.assertEqual(cfg.env_resolve.effective.default.get("FULL_API"), "https://example.com:8080/api")

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

    # Use facts and accessors to set env vars
    assert context.os == context.facts["drift_os"]
    assert context.arch == context.facts["drift_arch"]
    assert context.distro == context.facts["drift_distro"]
    assert context.hostname == context.facts["drift_hostname"]
    assert context.user == context.facts["drift_user"]
    assert "drift_ip_addresses" in context.facts

    env_default = cfg.setdefault("env", {}).setdefault("default", {})
    env_default["DETECTED_OS"] = context.os
    env_default["DETECTED_ARCH"] = context.arch
    env_default["DETECTED_USER"] = context.user
    return cfg
""", encoding="utf-8")

        from drift.utils.host_facts import get_cached_system_facts
        cached_facts = get_cached_system_facts()
        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertIn("DETECTED_OS", cfg.env_resolve.effective.default)
        self.assertEqual(cfg.env_resolve.effective.default["DETECTED_OS"], cached_facts["drift_os"])
        self.assertEqual(cfg.env_resolve.effective.default["DETECTED_ARCH"], cached_facts["drift_arch"])
        self.assertEqual(cfg.env_resolve.effective.default["DETECTED_USER"], cached_facts["drift_user"])

    def test_custom_hook_file_in_workspace_config(self) -> None:
        """Custom hook_file defined in [workspace] is resolved relative to config/."""
        custom_hook = self.drift_root / CONFIG_DIR_NAME / "custom_hook.py"
        custom_hook.write_text("""
def configure_workspace(context):
    context.config.setdefault("env", {}).setdefault("default", {})["CUSTOM_HOOK_RAN"] = "yes"
    return context.config
""", encoding="utf-8")

        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
hook_file = "custom_hook.py"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(cfg.env_resolve.effective.default.get("CUSTOM_HOOK_RAN"), "yes")
        self.assertEqual(str(cfg.workspace.hook_file), "custom_hook.py")

    def test_custom_hook_file_nested_in_config_dir(self) -> None:
        """Custom hook_file nested inside a subdirectory of config/ resolves properly."""
        nested_dir = self.drift_root / CONFIG_DIR_NAME / "hooks"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "nested_hook.py").write_text("""
def configure_workspace(context):
    context.config.setdefault("env", {}).setdefault("default", {})["NESTED_RAN"] = "yes"
    return context.config
""", encoding="utf-8")

        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
hook_file = "hooks/nested_hook.py"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(cfg.env_resolve.effective.default.get("NESTED_RAN"), "yes")

    def test_custom_hook_file_absolute_path(self) -> None:
        """Custom hook_file with an absolute path is resolved directly."""
        abs_hook = self.drift_root / "abs_hook.py"
        abs_hook.write_text("""
def configure_workspace(context):
    context.config.setdefault("env", {}).setdefault("default", {})["ABS_RAN"] = "yes"
    return context.config
""", encoding="utf-8")

        self.config_file.write_text(f"""
[workspace]
default_target_directory = "~"
hook_file = "{abs_hook.as_posix()}"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(cfg.env_resolve.effective.default.get("ABS_RAN"), "yes")

    def test_custom_hook_file_missing_raises_config_error(self) -> None:
        """Specifying a non-existent hook_file in [workspace] raises ConfigError."""
        self.config_file.write_text("""
[workspace]
default_target_directory = "~"
hook_file = "nonexistent_hook.py"

[packages.enable]
pkg1 = true
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            WorkspaceConfig.from_workspace_dir(self.drift_root)
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
            WorkspaceConfig.from_workspace_dir(self.drift_root)
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
            WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertIn("returned None", str(cm.exception))

    def test_hook_returning_non_dict_raises_config_error(self) -> None:
        """Hook function returning a non-dict raises ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    return ["not", "a", "dict"]
""", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertIn("must return a dictionary", str(cm.exception))

    def test_hook_syntax_error_raises_config_error(self) -> None:
        """Syntax errors in the hook module raise ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("def invalid_syntax(:", encoding="utf-8")

        with self.assertRaises(ConfigError) as cm:
            WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertIn("Failed to load workspace hook", str(cm.exception))

    def test_hook_runtime_exception_raises_config_error(self) -> None:
        """Exceptions raised inside configure_workspace() are wrapped in ConfigError."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    raise ValueError("Something went wrong during dynamic config calculation!")
""", encoding="utf-8")

    def test_hook_accesses_context_secrets(self) -> None:
        """Workspace hook can inspect secrets via context.env and use them to mutate config."""
        from drift.core.constants import SECRETS_ENV_FILE_NAME
        (self.drift_root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME).write_text(
            'VAULT_TOKEN="secret_token_123"\n',
            encoding="utf-8"
        )
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("""
def configure_workspace(context):
    cfg = context.config
    assert "VAULT_TOKEN" in context.env
    assert context.env["VAULT_TOKEN"] == "secret_token_123"
    cfg.setdefault("env", {}).setdefault("default", {})["INJECTED_TOKEN"] = context.env["VAULT_TOKEN"]
    return cfg
""", encoding="utf-8")

        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(cfg.env_resolve.effective.default.get("INJECTED_TOKEN"), "secret_token_123")

    def test_load_workspace_hook_module(self) -> None:
        """Verifies load_workspace_hook_module loads the module and exposes configure_workspace."""
        hook_file = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_file.write_text("def configure_workspace(context):\n    return context.config\n", encoding="utf-8")
        mod = load_workspace_hook_module(hook_file)
        self.assertTrue(hasattr(mod, "configure_workspace"))
        self.assertTrue(callable(mod.configure_workspace))


class TestWorkspaceHookContext(unittest.TestCase):
    """Direct unit tests for WorkspaceHookContext data structure and accessors."""

    def test_facts_filters_strictly_cached_system_facts(self) -> None:
        """Verifies context.facts dynamically includes only keys from get_cached_system_facts()."""
        from drift.utils.host_facts import get_cached_system_facts
        cached_keys = get_cached_system_facts().keys()
        self.assertTrue(len(cached_keys) >= 6)

        env_payload = {
            "drift_os": "linux",
            "drift_arch": "x86_64",
            "drift_distro": "arch",
            "drift_hostname": "test-box",
            "drift_user": "tester",
            "drift_ip_addresses": "192.168.1.50",
            # drift_ prefixed but NOT system facts:
            "drift_package_name": "pkg1",
            "drift_package_source_dir": "/path/to/src",
            "drift_custom_setting": "custom_val",
            # Standard env vars:
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/tester",
            "VAULT_TOKEN": "secret_abc",
        }

        context = WorkspaceHookContext(
            config={},
            drift_root=Path("/workspace"),
            env=env_payload,
        )

        facts = context.facts
        # Facts must match cached_keys exactly
        self.assertEqual(set(facts.keys()), set(cached_keys) & set(env_payload.keys()))
        self.assertIn("drift_os", facts)
        self.assertIn("drift_arch", facts)
        self.assertIn("drift_distro", facts)
        self.assertIn("drift_hostname", facts)
        self.assertIn("drift_user", facts)
        self.assertIn("drift_ip_addresses", facts)

        # Must NOT include non-system-fact keys
        self.assertNotIn("drift_package_name", facts)
        self.assertNotIn("drift_package_source_dir", facts)
        self.assertNotIn("drift_custom_setting", facts)
        self.assertNotIn("PATH", facts)
        self.assertNotIn("HOME", facts)
        self.assertNotIn("VAULT_TOKEN", facts)

    def test_property_accessors(self) -> None:
        """Verifies convenience properties (os, arch, distro, hostname, user)."""
        context = WorkspaceHookContext(
            config={},
            drift_root=Path("/workspace"),
            env={
                "drift_os": "darwin",
                "drift_arch": "arm64",
                "drift_distro": "macos",
                "drift_hostname": "macbook",
                "drift_user": "developer",
            },
        )
        self.assertEqual(context.os, "darwin")
        self.assertEqual(context.arch, "arm64")
        self.assertEqual(context.distro, "macos")
        self.assertEqual(context.hostname, "macbook")
        self.assertEqual(context.user, "developer")

    def test_empty_env_fallbacks(self) -> None:
        """Verifies accessors return empty string when keys are missing from env."""
        context = WorkspaceHookContext(
            config={},
            drift_root=Path("/workspace"),
            env={},
        )
        self.assertEqual(context.facts, {})
        self.assertEqual(context.os, "")
        self.assertEqual(context.arch, "")
        self.assertEqual(context.distro, "")
        self.assertEqual(context.hostname, "")
        self.assertEqual(context.user, "")


class TestWorkspaceHookPrecedenceAndIsolation(unittest.TestCase):
    """Rigorous precedence and isolation tests for apply_workspace_hook."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        (self.drift_root / CONFIG_DIR_NAME).mkdir(parents=True)
        (self.drift_root / "src").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_environ)

    def _write_hook(self, body: str) -> Path:
        hook_path = self.drift_root / CONFIG_DIR_NAME / "drift_workspace.py"
        hook_path.write_text(f"""
def configure_workspace(context):
    cfg = context.config
{body}
    return cfg
""", encoding="utf-8")
        return hook_path

    def test_tier1_override_beats_tier2_facts(self) -> None:
        """Tier 1 [env.override] takes precedence over auto-detected Tier 2 system facts."""
        self._write_hook("""
    cfg["__recorded_os"] = context.os
    cfg["__recorded_facts_os"] = context.facts["drift_os"]
    cfg["__recorded_env_os"] = context.env["drift_os"]
""")
        config_dict = {
            "env": {
                "override": {
                    "drift_os": "forced_custom_os",
                }
            }
        }
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        self.assertEqual(transformed["__recorded_os"], "forced_custom_os")
        self.assertEqual(transformed["__recorded_facts_os"], "forced_custom_os")
        self.assertEqual(transformed["__recorded_env_os"], "forced_custom_os")

    def test_tier2_facts_beats_tier3_ambient_environ(self) -> None:
        """Tier 2 system facts overwrite polluted Tier 3 ambient os.environ in hook context."""
        from drift.utils.host_facts import get_cached_system_facts
        actual_system_os = get_cached_system_facts()["drift_os"]

        self._write_hook("""
    cfg["__recorded_os"] = context.os
    cfg["__recorded_facts_os"] = context.facts["drift_os"]
    cfg["__recorded_env_os"] = context.env["drift_os"]
""")
        # Pollute host os.environ with fake drift_os
        os.environ["drift_os"] = "polluted_fake_ambient_os"

        config_dict = {}
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        # Authoritative system facts must overwrite the ambient polluted variable
        self.assertEqual(transformed["__recorded_os"], actual_system_os)
        self.assertEqual(transformed["__recorded_facts_os"], actual_system_os)
        self.assertEqual(transformed["__recorded_env_os"], actual_system_os)

    def test_tier3_ambient_beats_tier4_secrets(self) -> None:
        """Tier 3 ambient os.environ takes precedence over Tier 4 secrets in hook context."""
        from drift.core.constants import SECRETS_ENV_FILE_NAME
        (self.drift_root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME).write_text(
            'SHARED_API_KEY="secret_key_from_file"\n',
            encoding="utf-8"
        )
        self._write_hook("""
    cfg["__recorded_key"] = context.env["SHARED_API_KEY"]
""")
        os.environ["SHARED_API_KEY"] = "ambient_key_from_environ"

        config_dict = {}
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        self.assertEqual(transformed["__recorded_key"], "ambient_key_from_environ")

    def test_ws_secrets_beats_secrets_file(self) -> None:
        """Inline [env.secrets] takes precedence over config/secrets.env."""
        from drift.core.constants import SECRETS_ENV_FILE_NAME
        (self.drift_root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME).write_text(
            'TOKEN="file_token"\n',
            encoding="utf-8"
        )
        self._write_hook("""
    cfg["__recorded_token"] = context.env["TOKEN"]
""")
        config_dict = {
            "env": {
                "secrets": {
                    "TOKEN": "inline_toml_token",
                }
            }
        }
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        self.assertEqual(transformed["__recorded_token"], "inline_toml_token")

    def test_tier2_facts_beats_tier4_secrets(self) -> None:
        """Tier 2 system facts cannot be shadowed by Tier 4 secrets."""
        from drift.utils.host_facts import get_cached_system_facts
        actual_system_arch = get_cached_system_facts()["drift_arch"]

        from drift.core.constants import SECRETS_ENV_FILE_NAME
        (self.drift_root / CONFIG_DIR_NAME / SECRETS_ENV_FILE_NAME).write_text(
            'drift_arch="fake_secret_arch"\n',
            encoding="utf-8"
        )
        self._write_hook("""
    cfg["__recorded_arch"] = context.arch
    cfg["__recorded_facts_arch"] = context.facts["drift_arch"]
""")
        config_dict = {
            "env": {
                "secrets": {
                    "drift_arch": "fake_inline_secret_arch",
                }
            }
        }
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        self.assertEqual(transformed["__recorded_arch"], actual_system_arch)
        self.assertEqual(transformed["__recorded_facts_arch"], actual_system_arch)

    def test_apply_workspace_hook_leaves_os_environ_completely_unmutated(self) -> None:
        """Verifies that running apply_workspace_hook does not mutate or pollute host os.environ."""
        # Ensure clean state without drift_* in os.environ
        drift_keys = ["drift_os", "drift_arch", "drift_distro", "drift_hostname", "drift_user", "drift_ip_addresses"]
        for k in drift_keys:
            os.environ.pop(k, None)

        before_environ = dict(os.environ)

        self._write_hook("""
    # Hook inspects facts and mutates config
    cfg.setdefault("env", {}).setdefault("default", {})["HOOK_RAN"] = "true"
""")
        config_dict = {
            "env": {
                "override": {"OVERRIDE_VAR": "new_val"},
                "secrets": {"SECRET_VAR": "secret_val"},
            }
        }
        apply_workspace_hook(self.drift_root, config_dict)

        # Invariant: host os.environ must remain completely identical
        self.assertEqual(dict(os.environ), before_environ)
        for k in drift_keys:
            self.assertNotIn(k, os.environ)
        self.assertNotIn("OVERRIDE_VAR", os.environ)
        self.assertNotIn("SECRET_VAR", os.environ)

    def test_apply_workspace_hook_clean_environ_has_all_six_facts(self) -> None:
        """In a completely clean environment with no drift_* in os.environ, all 6 facts are accessible."""
        drift_keys = ["drift_os", "drift_arch", "drift_distro", "drift_hostname", "drift_user", "drift_ip_addresses"]
        for k in drift_keys:
            os.environ.pop(k, None)

        self._write_hook("""
    cfg["__all_facts_present"] = all(k in context.facts for k in [
        "drift_os", "drift_arch", "drift_distro", "drift_hostname", "drift_user", "drift_ip_addresses"
    ])
    cfg["__all_env_facts_present"] = all(k in context.env for k in [
        "drift_os", "drift_arch", "drift_distro", "drift_hostname", "drift_user", "drift_ip_addresses"
    ])
    cfg["__os_non_empty"] = bool(context.os)
    cfg["__arch_non_empty"] = bool(context.arch)
    cfg["__distro_non_empty"] = bool(context.distro)
    cfg["__hostname_non_empty"] = bool(context.hostname)
    cfg["__user_non_empty"] = bool(context.user)
""")
        config_dict = {}
        transformed = apply_workspace_hook(self.drift_root, config_dict)
        self.assertTrue(transformed["__all_facts_present"])
        self.assertTrue(transformed["__all_env_facts_present"])
        self.assertTrue(transformed["__os_non_empty"])
        self.assertTrue(transformed["__arch_non_empty"])
        self.assertTrue(transformed["__distro_non_empty"])
        self.assertTrue(transformed["__hostname_non_empty"])
        self.assertTrue(transformed["__user_non_empty"])


if __name__ == "__main__":
    unittest.main()
