"""Tests for environment variable precedence and configuration loading in drift."""

import os
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

from drift.core.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    SECRETS_ENV_FILE_NAME,
    InstallMethod,
    set_test_mode,
)
from drift.utils.env_utils import (
    EnvConfig,
    EnvImpact,
    EnvResolve,
    resolve_env_configs,
    load_env_settings,
    unload_env_settings,
    parse_secrets_env,
    parse_env_file,
    parse_env_text,
    env_scope,
)
from drift.config.workspace_config import (
    WorkspaceConfig,
)
from drift.config.render_engine_config import RenderEngineRegistry
from drift.render.render_package import run_primitive_2_render_packages


class TestLoadEnvSettingsUnit(unittest.TestCase):
    """Unit tests for load_env_settings and unload_env_settings."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_load_env_settings_empty(self) -> None:
        """Verifies that loading empty envs returns empty dict and modifies nothing."""
        result = load_env_settings([])
        self.assertEqual(result, {})
        result_dict = load_env_settings({})
        self.assertEqual(result_dict, {})

    def test_load_env_settings_overwrite_true(self) -> None:
        """Verifies that overwrite=True (default) updates existing variables and tracks original values."""
        os.environ["TEST_EXISTING"] = "old_value"
        os.environ.pop("TEST_NEW", None)

        # Test with Dict input
        envs = {"TEST_EXISTING": "new_value", "TEST_NEW": "created_value"}
        saved_envs = load_env_settings(envs, overwrite=True)

        self.assertIsInstance(saved_envs, dict)
        self.assertEqual(os.environ["TEST_EXISTING"], "new_value")
        self.assertEqual(os.environ["TEST_NEW"], "created_value")

        # Check saved values
        self.assertEqual(saved_envs["TEST_EXISTING"], "old_value")
        self.assertIsNone(saved_envs["TEST_NEW"])

    def test_load_env_settings_overwrite_false(self) -> None:
        """Verifies that overwrite=False skips existing environment variables."""
        os.environ["TEST_EXISTING"] = "original_value"
        os.environ.pop("TEST_NEW", None)

        envs = {"TEST_EXISTING": "attempted_overwrite", "TEST_NEW": "new_val"}
        saved_envs = load_env_settings(envs, overwrite=False)

        self.assertIsInstance(saved_envs, dict)
        # Existing should NOT be modified
        self.assertEqual(os.environ["TEST_EXISTING"], "original_value")
        # New should be added
        self.assertEqual(os.environ["TEST_NEW"], "new_val")

        # saved_envs should only contain TEST_NEW
        self.assertNotIn("TEST_EXISTING", saved_envs)
        self.assertIn("TEST_NEW", saved_envs)

    def test_load_env_settings_with_env_keep(self) -> None:
        """Verifies that variables in env_keep are protected from being overwritten."""
        os.environ["TEST_KEPT"] = "keep_me"
        os.environ["TEST_OVERWRITABLE"] = "old_val"

        envs = {"TEST_KEPT": "new_val_1", "TEST_OVERWRITABLE": "new_val_2"}
        saved_envs = load_env_settings(envs, overwrite=True, env_keep=["TEST_KEPT"])

        self.assertEqual(os.environ["TEST_KEPT"], "keep_me")
        self.assertEqual(os.environ["TEST_OVERWRITABLE"], "new_val_2")

        self.assertNotIn("TEST_KEPT", saved_envs)
        self.assertEqual(saved_envs["TEST_OVERWRITABLE"], "old_val")

    def test_load_env_settings_duplicate_keys_in_sequence_input(self) -> None:
        """Verifies that duplicate keys in sequence input preserve the true original value."""
        os.environ["TEST_DUP"] = "initial"
        envs = [("TEST_DUP", "first_change"), ("TEST_DUP", "second_change")]
        saved_envs = load_env_settings(envs, overwrite=True)

        self.assertEqual(os.environ["TEST_DUP"], "second_change")
        self.assertEqual(len(saved_envs), 1)
        self.assertEqual(saved_envs["TEST_DUP"], "initial")

        unload_env_settings(saved_envs)
        self.assertEqual(os.environ["TEST_DUP"], "initial")

    def test_unload_env_settings(self) -> None:
        """Verifies that unload_env_settings cleanly restores pre-existing values and pops new ones."""
        os.environ["TEST_RESTORE"] = "before_load"
        os.environ.pop("TEST_POP", None)

        saved = load_env_settings({"TEST_RESTORE": "during_load", "TEST_POP": "during_load"})
        self.assertEqual(os.environ["TEST_RESTORE"], "during_load")
        self.assertEqual(os.environ["TEST_POP"], "during_load")

        unload_env_settings(saved)
        self.assertEqual(os.environ["TEST_RESTORE"], "before_load")
        self.assertNotIn("TEST_POP", os.environ)

        # Unloading empty dict is a no-op
        unload_env_settings()
        unload_env_settings({})

    def test_update_env_dict_pure_dictionary_emits_no_logs(self) -> None:
        """Verifies that update_env_dict and restore_env_dict on pure dictionaries emit no environment logs."""
        set_test_mode(True, enable_logging=True)
        try:
            from drift.utils.env_utils import update_env_dict, restore_env_dict
            dummy_target = {"A": "old_a", "KEEP_VAR": "kept"}
            # Load into dummy target while listening on logger
            with patch("drift.utils.env_utils.logger.debug") as mock_debug:
                _, saved = update_env_dict(dummy_target, {"A": "new_a", "B": "new_b", "KEEP_VAR": "try_overwrite"}, overwrite=True, env_keep={"KEEP_VAR"})
                restore_env_dict(dummy_target, saved)
                mock_debug.assert_not_called()
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_env_settings_logs_only_new_or_overwritten(self) -> None:
        """Verifies that 'Environment variable loaded' is only logged for new or overwritten variables."""
        set_test_mode(True, enable_logging=True)
        try:
            os.environ["TEST_UNCHANGED"] = "same_val"
            os.environ["TEST_OVERWRITTEN"] = "old_val"
            os.environ.pop("TEST_NEW", None)

            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                saved = load_env_settings({
                    "TEST_UNCHANGED": "same_val",
                    "TEST_OVERWRITTEN": "new_val",
                    "TEST_NEW": "brand_new"
                })
                log_output = "\n".join(cm.output)
                self.assertIn("Environment variable loaded: TEST_NEW=brand_new", log_output)
                self.assertIn("Environment variable loaded: TEST_OVERWRITTEN=new_val", log_output)
                self.assertNotIn("TEST_UNCHANGED", log_output)

                # Unload settings
                unload_env_settings(saved)
                log_output_after = "\n".join(cm.output)
                self.assertIn("Environment variable unloaded: popped TEST_NEW", log_output_after)
                self.assertIn("Environment variable unloaded: restored TEST_OVERWRITTEN=old_val", log_output_after)
                self.assertNotIn("restored TEST_UNCHANGED", log_output_after)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_env_settings_masked_values(self) -> None:
        """Verifies that mask_values=True logs 'key=****' instead of plaintext values."""
        set_test_mode(True, enable_logging=True)
        try:
            os.environ["TEST_OVERWRITTEN_SECRET"] = "secret_old"
            os.environ.pop("TEST_NEW_SECRET", None)

            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                saved = load_env_settings(
                    {"TEST_NEW_SECRET": "super_secret_val", "TEST_OVERWRITTEN_SECRET": "new_secret_val"},
                    mask_values=True,
                )
                log_output = "\n".join(cm.output)
                self.assertIn("Environment variable loaded: TEST_NEW_SECRET=****", log_output)
                self.assertIn("Environment variable loaded: TEST_OVERWRITTEN_SECRET=****", log_output)
                self.assertNotIn("super_secret_val", log_output)
                self.assertNotIn("new_secret_val", log_output)

                # Unload with mask_values=True
                unload_env_settings(saved, mask_values=True)
                log_output_after = "\n".join(cm.output)
                self.assertIn("Environment variable unloaded: popped TEST_NEW_SECRET", log_output_after)
                self.assertIn("Environment variable unloaded: restored TEST_OVERWRITTEN_SECRET=****", log_output_after)
                self.assertNotIn("secret_old", log_output_after)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_env_scope_masks_secret_values_in_logs(self) -> None:
        """Verifies that env_scope with mask_values=True automatically masks secret values in debug logs."""
        set_test_mode(True, enable_logging=True)
        try:
            os.environ.pop("DRIFT_API_SECRET", None)
            os.environ["DRIFT_EXISTING_SECRET"] = "old_secret_xyz"

            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                with env_scope(
                    {"DRIFT_API_SECRET": "top_secret_token_123", "DRIFT_EXISTING_SECRET": "updated_secret_456"},
                    overwrite=True,
                    mask_values=True,
                ):
                    self.assertEqual(os.environ["DRIFT_API_SECRET"], "top_secret_token_123")
                    self.assertEqual(os.environ["DRIFT_EXISTING_SECRET"], "updated_secret_456")

                log_output = "\n".join(cm.output)
                # Ensure values are masked as ****
                self.assertIn("Environment variable loaded: DRIFT_API_SECRET=****", log_output)
                self.assertIn("Environment variable loaded: DRIFT_EXISTING_SECRET=****", log_output)
                self.assertIn("Environment variable unloaded: popped DRIFT_API_SECRET", log_output)
                self.assertIn("Environment variable unloaded: restored DRIFT_EXISTING_SECRET=****", log_output)
                # Ensure no secrets leak into logs
                self.assertNotIn("top_secret_token_123", log_output)
                self.assertNotIn("updated_secret_456", log_output)
                self.assertNotIn("old_secret_xyz", log_output)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_parse_env_text_and_file(self) -> None:
        """Verifies parsing .env format text and files."""
        text = """
        # Comment line
        KEY1=val1
        KEY2="quoted_val"
        KEY3='single_quoted'
        KEY4 = spaced_val
        """
        parsed = parse_env_text(text)
        self.assertEqual(parsed["KEY1"], "val1")
        self.assertEqual(parsed["KEY2"], "quoted_val")
        self.assertEqual(parsed["KEY3"], "single_quoted")
        self.assertEqual(parsed["KEY4"], "spaced_val")


class TestStrictVariablePrecedence(unittest.TestCase):
    """Integration tests verifying the strict precedence:

    Host Environment > Secret Vault (secrets.env) > Global Workspace Config ([env.default] in drift_workspace.toml)
    """

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        # Basic workspace layout
        self.config_dir = self.drift_root / CONFIG_DIR_NAME
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.src_dir = self.drift_root / "src"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir = self.drift_root / "render"
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir = self.drift_root / "install"
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # Initialize git repos in render and install
        import subprocess
        for d in (self.drift_root, self.render_dir, self.install_dir):
            subprocess.run(["git", "init"], cwd=d, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "TestUser"], cwd=d, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=d, capture_output=True, check=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)
        self.temp_dir.cleanup()

    def _setup_package_with_template(self, pkg_name: str, template_body: str) -> Path:
        pkg_src = self.src_dir / pkg_name
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname = "{pkg_name}"\ninstall_method = "symlink"\ntarget_directory = "~"\n',
            encoding="utf-8"
        )
        template_file = pkg_src / "dot-config.envst.txt"
        template_file.write_text(template_body, encoding="utf-8")
        return pkg_src

    def test_host_env_overrides_secrets_and_workspace_config(self) -> None:
        """Host environment variable has the highest precedence.

        It must override both secrets.env and drift_workspace.toml [env.default].
        """
        var_name = "DRIFT_PRECEDENCE_VAR_1"
        os.environ[var_name] = "host_wins"

        # Write drift_workspace.toml with [env.default]
        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_test = true

[env.default]
{var_name} = "workspace_toml_value"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        # Write secrets.env
        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secret_vault_value"\n', encoding="utf-8")

        # Setup package template
        self._setup_package_with_template("pkg_test", f"VALUE=${{{var_name}}}\n")

        # Load workspace config and render
        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        # Verify host value wasn't overwritten on load
        self.assertEqual(os.environ[var_name], "host_wins")

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=host_wins")

        # Verify host value is still preserved after rendering
        self.assertEqual(os.environ[var_name], "host_wins")

    def test_secrets_env_overrides_workspace_config(self) -> None:
        """Secret vault (secrets.env) has higher precedence than drift_workspace.toml [env.default]."""
        var_name = "DRIFT_PRECEDENCE_VAR_2"
        os.environ.pop(var_name, None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_test = true

[env.default]
{var_name} = "workspace_toml_value"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secret_vault_value"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"VALUE=${{{var_name}}}\n")

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        # WorkspaceConfig load is in-memory and does not mutate os.environ
        self.assertEqual(ws_config.env_resolve.effective.default[var_name], "workspace_toml_value")
        self.assertNotIn(var_name, os.environ)

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        # Secret value won during render!
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=secret_vault_value")

        # After render, secrets are unloaded, leaving os.environ clean
        self.assertNotIn(var_name, os.environ)

    def test_workspace_config_env_default(self) -> None:
        """Workspace config [env.default] provides defaults when neither host env nor secrets exist."""
        var_name = "DRIFT_PRECEDENCE_VAR_3"
        os.environ.pop(var_name, None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_test = true

[env.default]
{var_name} = "default_from_toml"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"VALUE=${{{var_name}}}\n")

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws_config.env_resolve.effective.default[var_name], "default_from_toml")
        self.assertNotIn(var_name, os.environ)

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=default_from_toml")
        self.assertNotIn(var_name, os.environ)

    def test_secrets_transient_lifecycle(self) -> None:
        """Secrets only present in secrets.env are temporarily loaded during render and popped afterward."""
        var_name = "DRIFT_TRANSIENT_SECRET"
        os.environ.pop(var_name, None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            """
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_test = true
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="temporary_token_123"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"TOKEN=${{{var_name}}}\n")

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertNotIn(var_name, os.environ)

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "TOKEN=temporary_token_123")

        # Verify completely popped from os.environ after render
        self.assertNotIn(var_name, os.environ)

    def test_host_env_overrides_secrets_without_workspace_config(self) -> None:
        """Host env overrides secrets even when the variable is not in drift_workspace.toml."""
        var_name = "DRIFT_HOST_SECRET_VAR"
        os.environ[var_name] = "host_api_key"

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            """
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_test = true
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secrets_api_key"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"KEY=${{{var_name}}}\n")

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "KEY=host_api_key")
        self.assertEqual(os.environ[var_name], "host_api_key")

    def test_local_toml_merging_env(self) -> None:
        """drift_workspace.local.toml overrides drift_workspace.toml [env.default] settings."""
        var_name = "DRIFT_MERGED_VAR"
        os.environ.pop(var_name, None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[packages.enable]
DEFAULT = true

[env.default]
{var_name} = "base_value"
""",
            encoding="utf-8"
        )

        local_toml = self.config_dir / "drift_workspace.local.toml"
        local_toml.write_text(
            f"""
[env.default]
{var_name} = "local_override_value"
""",
            encoding="utf-8"
        )

        ws_cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws_cfg.env_resolve.effective.default[var_name], "local_override_value")
        self.assertEqual(ws_cfg.env_resolve.impact.restricted_env()[var_name], "local_override_value")

    def test_mixed_variable_sources_comprehensive(self) -> None:
        """Simultaneously tests all combinations of sources:

        A: Host + Secrets + TOML -> Host wins
        B: Secrets + TOML -> Secrets wins
        C: TOML only -> TOML wins
        D: Host + TOML -> Host wins
        E: Host + Secrets -> Host wins
        F: Secrets only -> Secrets wins
        """
        os.environ["VAR_A"] = "host_a"
        os.environ["VAR_D"] = "host_d"
        os.environ["VAR_E"] = "host_e"
        os.environ.pop("VAR_B", None)
        os.environ.pop("VAR_C", None)
        os.environ.pop("VAR_F", None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            """
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_mixed = true

[env.default]
VAR_A = "toml_a"
VAR_B = "toml_b"
VAR_C = "toml_c"
VAR_D = "toml_d"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(
            """
VAR_A="secret_a"
VAR_B="secret_b"
VAR_E="secret_e"
VAR_F="secret_f"
""",
            encoding="utf-8"
        )

        template_text = (
            "A=${VAR_A}\n"
            "B=${VAR_B}\n"
            "C=${VAR_C}\n"
            "D=${VAR_D}\n"
            "E=${VAR_E}\n"
            "F=${VAR_F}\n"
        )
        self._setup_package_with_template("pkg_mixed", template_text)

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        run_primitive_2_render_packages(ws_config, ["pkg_mixed"])

        rendered_file = self.render_dir / "pkg_mixed" / "dot-config.txt"
        expected_content = (
            "A=host_a\n"
            "B=secret_b\n"
            "C=toml_c\n"
            "D=host_d\n"
            "E=host_e\n"
            "F=secret_f"
        )
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), expected_content)

        # After render, os.environ is cleanly preserved
        self.assertEqual(os.environ["VAR_A"], "host_a")
        self.assertNotIn("VAR_B", os.environ)
        self.assertNotIn("VAR_C", os.environ)
        self.assertEqual(os.environ["VAR_D"], "host_d")
        self.assertEqual(os.environ["VAR_E"], "host_e")
        self.assertNotIn("VAR_F", os.environ)

    def test_render_exception_restores_environment(self) -> None:
        """Verifies that even if rendering raises an exception, unload_env_settings runs in finally."""
        var_secret = "DRIFT_FAIL_SECRET"
        var_toml = "DRIFT_FAIL_TOML"

        os.environ.pop(var_secret, None)
        os.environ.pop(var_toml, None)

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && exit 1'"

[packages.enable]
pkg_test = true

[env.default]
{var_toml} = "toml_val"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_secret}="secret_val"\n{var_toml}="secret_overwrites_toml"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"FAIL=${{{var_secret}}}\n")

        ws_config = WorkspaceConfig.from_workspace_dir(self.drift_root)
        res = run_primitive_2_render_packages(ws_config, ["pkg_test"])
        self.assertEqual(res.status, "FAILED")

        # Unload should have executed:
        self.assertNotIn(var_secret, os.environ)
        self.assertNotIn(var_toml, os.environ)

    def test_cli_main_with_cmdline_env(self) -> None:
        """Verifies that running CLI main() captures host environment and respects precedence."""
        from drift.cli import main

        var_name = "DRIFT_CLI_TEST_VAR"
        os.environ[var_name] = "cli_host_override"

        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[packages.enable]
pkg_cli = true

[env.default]
{var_name} = "toml_default"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secrets_default"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_cli", f"VAL=${{{var_name}}}\n")

        from drift.primitives.workspace_repair import repair_drift_workspace
        repair_drift_workspace(self.drift_root)

        # Execute CLI render
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", str(self.drift_root), "render", "pkg_cli"])

        rendered_file = self.render_dir / "pkg_cli" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VAL=cli_host_override")
        self.assertEqual(os.environ[var_name], "cli_host_override")


class TestEnvTopologicalResolutionAndInterpolation(unittest.TestCase):
    """Tests for topological sort variable stitching in environment tables and recursive config dictionary interpolation."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        self.drift_root = Path(self.temp_dir).resolve()
        self.config_dir = self.drift_root / CONFIG_DIR_NAME
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_update_env_dict(self) -> None:
        """Verifies update_env_dict with overwrite, non-overwrite, env_keep, and snapshot tracking."""
        from drift.utils.env_utils import update_env_dict, restore_env_dict

        # 1. Basic update with overwrite and snapshot
        target = {"A": "1", "B": "2"}
        res_target, saved = update_env_dict(target, {"B": "20", "C": "30"}, overwrite=True)
        self.assertIs(res_target, target)
        self.assertEqual(target, {"A": "1", "B": "20", "C": "30"})
        self.assertEqual(saved, {"B": "2", "C": None})

        # Restore from snapshot
        restore_env_dict(target, saved)
        self.assertEqual(target, {"A": "1", "B": "2"})

        # 2. Non-overwrite (fill unset blanks only)
        target2 = {"A": "1", "B": "2"}
        _, saved2 = update_env_dict(target2, {"B": "20", "C": "30"}, overwrite=False)
        self.assertEqual(target2, {"A": "1", "B": "2", "C": "30"})
        self.assertEqual(saved2, {"C": None})

        # 3. Protection with env_keep
        target3 = {"CLI_VAR": "cli_value", "OTHER": "old"}
        _, saved3 = update_env_dict(target3, {"CLI_VAR": "new_attempt", "OTHER": "new", "ADDED": "val"}, overwrite=True, env_keep={"CLI_VAR"})
        self.assertEqual(target3["CLI_VAR"], "cli_value")
        self.assertEqual(target3["OTHER"], "new")
        self.assertEqual(target3["ADDED"], "val")
        self.assertEqual(saved3, {"OTHER": "old", "ADDED": None})

        # 4. None / empty source returns target unchanged
        _, saved4 = update_env_dict(target3, None)
        self.assertEqual(target3["CLI_VAR"], "cli_value")
        self.assertEqual(saved4, {})

        # 5. Generator expressions for source pairs and env_keep
        target5 = {"VAR_A": "old_a", "VAR_B": "old_b"}
        source_pairs_gen = ((f"VAR_{x}", f"val_{x}") for x in ["A", "C"])
        env_keep_gen = (k for k in ["VAR_A"])
        _, saved5 = update_env_dict(target5, source_pairs_gen, overwrite=True, env_keep=env_keep_gen)
        self.assertEqual(target5["VAR_A"], "old_a")
        self.assertEqual(target5["VAR_B"], "old_b")
        self.assertEqual(target5["VAR_C"], "val_C")
        self.assertEqual(saved5, {"VAR_C": None})

    def test_topological_sort_generic(self) -> None:
        """Verifies topological_sort handles generic DAGs and catches cycles."""
        from drift.utils.env_utils import topological_sort
        from drift.core.exceptions import ConfigError

        # Integer nodes: 3 depends on 2, 2 depends on 1
        int_graph = {3: {2}, 2: {1}, 1: set()}
        self.assertEqual(topological_sort(int_graph), [1, 2, 3])

        # Diamond DAG with integers
        diamond = {4: {2, 3}, 2: {1}, 3: {1}, 1: set()}
        order = topological_sort(diamond)
        self.assertEqual(order[0], 1)
        self.assertEqual(set(order[1:3]), {2, 3})
        self.assertEqual(order[3], 4)

        # Immediate self-cycle
        with self.assertRaises(ValueError) as ctx:
            topological_sort({"A": {"A"}})
        self.assertIn("Cyclic dependency detected: 'A' references itself.", str(ctx.exception))

        # Mutual cycle
        with self.assertRaises(ValueError) as ctx:
            topological_sort({"A": {"B"}, "B": {"A"}})
        self.assertIn("Cyclic dependency detected among: A, B", str(ctx.exception))

        # Custom error class
        with self.assertRaises(ConfigError):
            topological_sort({"A": {"B"}, "B": {"A"}}, error_cls=ConfigError)

    def test_topological_sort_env(self) -> None:
        """Verifies topological_sort_env computes correct evaluation order and catches cycles."""
        from drift.utils.env_utils import topological_sort_env
        from drift.core.exceptions import ConfigError

        # Independent variables
        order = topological_sort_env({"A": "1", "B": "2"})
        self.assertEqual(set(order), {"A", "B"})

        # Linear chain C depends on B, B depends on A
        raw_env = {
            "C": "${B}_end",
            "B": "${A}_mid",
            "A": "start",
        }
        order = topological_sort_env(raw_env)
        self.assertEqual(order, ["A", "B", "C"])

        # Diamond / multi-dependency: D depends on B and C; B and C depend on A
        diamond_env = {
            "D": "${B}_${C}",
            "B": "${A}_b",
            "C": "${A}_c",
            "A": "base",
        }
        order = topological_sort_env(diamond_env)
        self.assertEqual(order[0], "A")
        self.assertEqual(set(order[1:3]), {"B", "C"})
        self.assertEqual(order[3], "D")

        # Escaped reference should not create dependency
        escaped_env = {
            "A": r"\${B}",
            "B": "val",
        }
        order = topological_sort_env(escaped_env)
        self.assertEqual(set(order), {"A", "B"})

        # Cycle detection
        with self.assertRaises(ConfigError):
            topological_sort_env({"A": "${B}", "B": "${A}"})

    def test_resolve_env_references_linear(self) -> None:
        """Verifies that linear dependencies A -> B -> C resolve in correct topological order."""
        from drift.utils.env_utils import resolve_env_references

        raw_env = {
            "A": "root_val",
            "B": "${A}_layer2",
            "C": "${B}_layer3",
        }
        resolved = resolve_env_references(raw_env, base_env={})
        self.assertEqual(resolved["A"], "root_val")
        self.assertEqual(resolved["B"], "root_val_layer2")
        self.assertEqual(resolved["C"], "root_val_layer2_layer3")

    def test_resolve_env_references_multi_dep(self) -> None:
        """Verifies that multiple variable references in a single string interpolate correctly."""
        from drift.utils.env_utils import resolve_env_references

        raw_env = {
            "HOST": "127.0.0.1",
            "PORT": "1080",
            "SOCKS_PROXY": "socks5h://${HOST}:${PORT}",
            "ALL_PROXY": "${SOCKS_PROXY}",
        }
        resolved = resolve_env_references(raw_env, base_env={})
        self.assertEqual(resolved["HOST"], "127.0.0.1")
        self.assertEqual(resolved["PORT"], "1080")
        self.assertEqual(resolved["SOCKS_PROXY"], "socks5h://127.0.0.1:1080")
        self.assertEqual(resolved["ALL_PROXY"], "socks5h://127.0.0.1:1080")

    def test_resolve_env_references_with_base_env(self) -> None:
        """Verifies that external variables from base_env (e.g. os.environ or host facts) are resolved."""
        from drift.utils.env_utils import resolve_env_references

        base_env = {"HOME": "/home/tester", "drift_host_os": "linux"}
        raw_env = {
            "APP_DIR": "${HOME}/.local/share/app",
            "OS_NAME": "os_${drift_host_os}",
            "FULL_PATH": "${APP_DIR}/${OS_NAME}/bin",
        }
        resolved = resolve_env_references(raw_env, base_env=base_env)
        self.assertEqual(resolved["APP_DIR"], "/home/tester/.local/share/app")
        self.assertEqual(resolved["OS_NAME"], "os_linux")
        self.assertEqual(resolved["FULL_PATH"], "/home/tester/.local/share/app/os_linux/bin")

    def test_resolve_env_references_self_cycle(self) -> None:
        """Verifies that immediate self-references raise ConfigError."""
        from drift.utils.env_utils import resolve_env_references
        from drift.core.exceptions import ConfigError

        raw_env = {"LOOP": "${LOOP}"}
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_references(raw_env, base_env={})
        self.assertIn("Cyclic dependency", str(ctx.exception))

    def test_resolve_env_references_mutual_cycle(self) -> None:
        """Verifies that cyclic dependencies (A -> B -> A) raise ConfigError."""
        from drift.utils.env_utils import resolve_env_references
        from drift.core.exceptions import ConfigError

        raw_env = {
            "A": "start_${B}",
            "B": "mid_${C}",
            "C": "end_${A}",
        }
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_references(raw_env, base_env={})
        self.assertIn("Cyclic dependency", str(ctx.exception))

    def test_resolve_env_references_missing_var(self) -> None:
        """Verifies that referencing a non-existent variable raises ConfigError."""
        from drift.utils.env_utils import resolve_env_references
        from drift.core.exceptions import ConfigError

        raw_env = {"A": "${UNKNOWN_VAR}"}
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_references(raw_env, base_env={})
        self.assertIn("UNKNOWN_VAR", str(ctx.exception))

    def test_interpolate_config_dict(self) -> None:
        """Verifies recursive interpolation of strings across nested dicts, lists, and tuples."""
        from drift.utils.env_utils import interpolate_config_dict

        env = {"BASE": "/opt/app", "PORT": "8080"}
        data = {
            "target": "${BASE}/dest",
            "port_num": 8080,
            "is_enabled": True,
            "hooks": {
                "pre": "${BASE}/scripts/pre.sh",
                "timeout": 30,
            },
            "files": ["${BASE}/f1.txt", "${BASE}/f2.txt"],
            "env": {
                "raw_text": "${DONT_TOUCH_ME}",
            }
        }
        result = interpolate_config_dict(data, env=env, exclude_keys={"env"})
        self.assertEqual(result["target"], "/opt/app/dest")
        self.assertEqual(result["port_num"], 8080)
        self.assertEqual(result["is_enabled"], True)
        self.assertEqual(result["hooks"]["pre"], "/opt/app/scripts/pre.sh")
        self.assertEqual(result["hooks"]["timeout"], 30)
        self.assertEqual(result["files"], ["/opt/app/f1.txt", "/opt/app/f2.txt"])
        # Excluded key 'env' was not interpolated
        self.assertEqual(result["env"]["raw_text"], "${DONT_TOUCH_ME}")

    def test_workspace_config_with_stitched_env_and_field_interpolation(self) -> None:
        """Verifies that workspace drift_workspace.toml resolves [env.default] stitching and interpolates fields."""
        drift_toml = self.config_dir / WORKSPACE_CONFIG_FILE_NAME
        drift_toml.write_text(
            f"""
[workspace]
source_directory = "${{SRC_SUBDIR}}"
default_target_directory = "${{TARGET_ROOT}}/user_home"

[packages.enable]
default = true

[env.default]
ROOT_DIR = "{(self.drift_root / 'custom' / 'base').as_posix()}"
SRC_SUBDIR = "src_custom"
TARGET_ROOT = "${{ROOT_DIR}}/dest"
SocksProxyHost = "127.0.0.1"
SocksProxyPort = "9050"
SOCKS_PROXY = "socks5h://${{SocksProxyHost}}:${{SocksProxyPort}}"
ALL_PROXY = "${{SOCKS_PROXY}}"
""",
            encoding="utf-8"
        )
        ws = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws.env_resolve.effective.default["SOCKS_PROXY"], "socks5h://127.0.0.1:9050")
        self.assertEqual(ws.env_resolve.effective.default["ALL_PROXY"], "socks5h://127.0.0.1:9050")
        self.assertEqual(ws.env_resolve.impact.restricted_env()["SOCKS_PROXY"], "socks5h://127.0.0.1:9050")
        self.assertEqual(ws.workspace.source_directory, Path("src_custom"))
        self.assertEqual(ws.workspace.default_target_directory, (self.drift_root / "custom" / "base" / "dest" / "user_home").resolve())

    def test_workspace_config_with_direct_env_raises_error(self) -> None:
        """Verifies that direct key-value pairs in workspace [env] raise ConfigError."""
        from drift.config.workspace_config import WorkspaceConfig
        from drift.core.exceptions import ConfigError

        ws_dict = {
            "workspace": {},
            "packages": {"enable": {}},
            "env": {
                "LEGACY_VAR": "legacy_val",
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            WorkspaceConfig.from_dict(ws_dict, drift_root=self.drift_root)
        self.assertIn("Unsupported key 'LEGACY_VAR' in [env] in workspace configuration", str(ctx.exception))
        self.assertIn("Expected [env.override], [env.secrets], [env.default], or [env.fallback]", str(ctx.exception))

    def test_workspace_config_with_unknown_env_subtable_raises_error(self) -> None:
        """Verifies that unknown sub-tables under workspace [env] raise ConfigError."""
        from drift.config.workspace_config import WorkspaceConfig
        from drift.core.exceptions import ConfigError

        ws_dict = {
            "workspace": {},
            "packages": {"enable": {}},
            "env": {
                "invalid_subtable": {"VAR": "val"},
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            WorkspaceConfig.from_dict(ws_dict, drift_root=self.drift_root)
        self.assertIn("Unsupported key 'invalid_subtable' in [env] in workspace configuration", str(ctx.exception))
        self.assertIn("Expected [env.override], [env.secrets], [env.default], or [env.fallback]", str(ctx.exception))

    def test_package_config_with_env_override_and_field_interpolation(self) -> None:
        """Verifies that package drift_package.toml resolves [env.override] and interpolates package fields."""
        from drift.config.package_config import PackageConfig

        pkg_dict = {
            "package": {
                "name": "my_daemon",
                "target_directory": "${APP_ROOT}/${drift_package_name}",
            },
            "env": {
                "override": {
                    "APP_ROOT": "/var/lib",
                    "PORT": "8000",
                    "SERVICE_URL": "http://127.0.0.1:${PORT}",
                }
            },
            "hooks": {
                "post_install": "drift_hooks/start_${drift_package_name}.sh",
                "timeout": 45,
            }
        }
        from drift.config.package_loader import resolve_and_interpolate_package_config
        base_dir = Path("/mock/src/my_daemon")
        stitched, env_res = resolve_and_interpolate_package_config(pkg_dict, package_name="my_daemon")
        pkg_cfg = PackageConfig.from_dict(stitched, package_name="my_daemon", base_dir=base_dir)
        self.assertEqual(pkg_cfg.name, "my_daemon")
        self.assertEqual(str(pkg_cfg.package.target_directory), "/var/lib/my_daemon")
        self.assertEqual(pkg_cfg.env_resolve.effective.override["SERVICE_URL"], "http://127.0.0.1:8000")
        self.assertEqual(pkg_cfg.hooks.post_install, base_dir / ".drift/hooks/start_my_daemon.sh")
        self.assertEqual(pkg_cfg.hooks.timeout, 45)

    def test_package_config_with_direct_env_raises_error(self) -> None:
        """Verifies that direct key-value pairs in package [env] raise ConfigError."""
        from drift.config.package_config import PackageConfig
        from drift.core.exceptions import ConfigError

        pkg_dict = {
            "package": {
                "name": "legacy_pkg",
            },
            "env": {
                "LEGACY_VAR": "legacy_val",
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_dict, package_name="legacy_pkg", base_dir=Path("/test/legacy_pkg"))
        self.assertIn("Unsupported key 'LEGACY_VAR' in [env] in package 'legacy_pkg'", str(ctx.exception))
        self.assertIn("Expected [env.override], [env.secrets], [env.default], or [env.fallback]", str(ctx.exception))

    def test_escaped_variable_stitching(self) -> None:
        """Verifies that \\$VAR and \\${VAR} escape variable stitching in environment tables and config fields."""
        from drift.utils.env_utils import resolve_env_references, interpolate_config_dict

        raw_env = {
            "REAL_VAR": "actual_val",
            "ESCAPED_ONE": r"\${REAL_VAR}",
            "ESCAPED_TWO": r"\$REAL_VAR",
            "ESCAPED_UNDEFINED": r"\${NOT_A_REAL_VAR}",
        }
        # Escaped variables must not be treated as graph dependencies
        resolved = resolve_env_references(raw_env, base_env={})
        self.assertEqual(resolved["REAL_VAR"], "actual_val")
        self.assertEqual(resolved["ESCAPED_ONE"], "${REAL_VAR}")
        self.assertEqual(resolved["ESCAPED_TWO"], "$REAL_VAR")
        self.assertEqual(resolved["ESCAPED_UNDEFINED"], "${NOT_A_REAL_VAR}")

        data = {
            "target": r"\${ESCAPED_PATH}/app",
            "expanded": "${REAL_VAR}/app",
        }
        interpolated = interpolate_config_dict(data, env=resolved)
        self.assertEqual(interpolated["target"], "${ESCAPED_PATH}/app")
        self.assertEqual(interpolated["expanded"], "actual_val/app")

    def test_package_config_facts_and_precedence(self) -> None:
        """Verifies that all four package facts are available and 6-tier precedence is respected in package config."""
        from drift.config.package_config import PackageConfig
        from drift.config.package_loader import resolve_and_interpolate_package_config
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        # Test CLI environment presence in ambient os.environ (Tier 3)
        os.environ["CLI_OVERRIDE_VAR"] = "cli_val"

        ws = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=Path("/target"),
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs=RenderEngineRegistry(),
            env_resolve=EnvResolve(),
        )

        pkg_toml_path = self.drift_root / "src" / "my_pkg" / "drift_package.toml"
        pkg_dict = {
            "package": {
                "name": "my_pkg",
                "target_directory": "${APP_INSTALL_DIR}/target",
            },
            "env": {
                "fallback": {
                    "FALLBACK_VAR": "fallback_val",
                    "OVERRIDDEN_BY_WORKSPACE": "should_be_overridden",
                },
                "override": {
                    "SRC_DIR_REF": "${drift_package_source_dir}",
                    "RENDER_DIR_REF": "${drift_package_render_dir}",
                    "INSTALL_DIR_REF": "${drift_package_install_dir}",
                    "APP_INSTALL_DIR": "${drift_package_install_dir}",
                    "CLI_OVERRIDE_VAR": "attempted_pkg_override",
                }
            },
            "hooks": {
                "post_install": "${SRC_DIR_REF}/drift_hooks/post.sh",
            }
        }
        # Simulate workspace environment variable in os.environ (Tier 6)
        os.environ["OVERRIDDEN_BY_WORKSPACE"] = "workspace_val"

        stitched, env_res = resolve_and_interpolate_package_config(pkg_dict, package_name="my_pkg", workspace_config=ws)
        pkg_cfg = PackageConfig.from_dict(stitched,
                                          package_name="my_pkg",
                                          base_dir=self.drift_root / "src" / "my_pkg",
                                          source_files=[pkg_toml_path],
                                          workspace_config=ws)
        self.assertEqual(pkg_cfg.name, "my_pkg")
        self.assertEqual(str(pkg_cfg.package.target_directory), str(self.drift_root / "install" / "my_pkg" / "target"))
        self.assertEqual(pkg_cfg.env_resolve.effective.override["SRC_DIR_REF"], str(self.drift_root / "src" / "my_pkg"))
        self.assertEqual(pkg_cfg.env_resolve.effective.override["RENDER_DIR_REF"], str(self.drift_root / "render" / "my_pkg"))
        self.assertEqual(pkg_cfg.env_resolve.effective.override["INSTALL_DIR_REF"], str(self.drift_root / "install" / "my_pkg"))
        # Fallback filled unset blanks
        self.assertEqual(pkg_cfg.env_resolve.effective.fallback["FALLBACK_VAR"], "fallback_val")
        # Hook path was interpolated and normalized to stage base (install directory for post_install)
        self.assertEqual(str(pkg_cfg.hooks.post_install), str(self.drift_root / "install" / "my_pkg" / ".drift" / "hooks" / "post.sh"))
        self.assertEqual(pkg_cfg.hooks.get_relative_path("post_install"), Path("drift_hooks/post.sh"))

    def test_package_config_facts_with_custom_workspace_config(self) -> None:
        """Verifies that custom workspace paths (e.g. custom_src, custom_render, custom_install) populate package facts."""
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
        from drift.config.package_config import PackageConfig
        from drift.config.package_loader import resolve_and_interpolate_package_config

        ws = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                source_directory=Path("custom_src"),
                render_directory=Path("custom_render"),
                install_directory=Path("custom_install"),
                backup_directory=Path("custom_backup"),
                default_target_directory=Path("/target"),
                default_install_method=InstallMethod.SYMLINK,
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs=RenderEngineRegistry(),
            env_resolve=EnvResolve(),
        )
        pkg_dict = {
            "package": {
                "name": "custom_pkg",
            },
            "env": {
                "override": {
                    "SRC": "${drift_package_source_dir}",
                    "RENDER": "${drift_package_render_dir}",
                    "INSTALL": "${drift_package_install_dir}",
                }
            }
        }
        stitched, env_res = resolve_and_interpolate_package_config(pkg_dict, package_name="custom_pkg", workspace_config=ws)
        pkg_cfg = PackageConfig.from_dict(stitched,
                                          package_name="custom_pkg",
                                          base_dir=self.drift_root / "custom_src" / "custom_pkg",
                                          workspace_config=ws)
        self.assertEqual(pkg_cfg.env_resolve.effective.override["SRC"], str(self.drift_root / "custom_src" / "custom_pkg"))
        self.assertEqual(pkg_cfg.env_resolve.effective.override["RENDER"], str(self.drift_root / "custom_render" / "custom_pkg"))
        self.assertEqual(pkg_cfg.env_resolve.effective.override["INSTALL"], str(self.drift_root / "custom_install" / "custom_pkg"))

    def test_package_config_without_workspace_config_leaves_dir_facts_unset(self) -> None:
        """Verifies that when workspace_config is not provided, 'dir' facts are unset."""
        from drift.config.package_config import PackageConfig
        from drift.config.package_loader import resolve_and_interpolate_package_config
        from drift.core.exceptions import ConfigError

        # drift_package_name is always set
        pkg_dict_name_only = {
            "package": {
                "name": "my_pkg",
            },
            "env": {
                "override": {
                    "NAME_REF": "${drift_package_name}",
                }
            }
        }
        stitched, env_res = resolve_and_interpolate_package_config(pkg_dict_name_only, package_name="my_pkg", workspace_config=None)
        pkg_cfg = PackageConfig.from_dict(stitched,
                                          package_name="my_pkg",
                                          base_dir=self.drift_root / "src" / "my_pkg")
        self.assertEqual(pkg_cfg.env_resolve.effective.override["NAME_REF"], "my_pkg")

        # Referencing dir facts without workspace_config raises ConfigError
        pkg_dict_dir_ref = {
            "package": {
                "name": "my_pkg",
            },
            "env": {
                "override": {
                    "SRC_REF": "${drift_package_source_dir}",
                }
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            resolve_and_interpolate_package_config(pkg_dict_dir_ref, package_name="my_pkg", workspace_config=None)
        self.assertIn("drift_package_source_dir", str(ctx.exception))

    def test_package_config_fallback_references_drift_package_source_dir(self) -> None:
        """Verifies that [env.fallback] can reference ${drift_package_source_dir} and respect precedence."""
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
        from drift.config.package_config import PackageConfig
        from drift.config.package_loader import resolve_and_interpolate_package_config
        from drift.config.render_engine_config import RenderEngineRegistry

        ws = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                source_directory=Path("src"),
                render_directory=Path("render"),
                install_directory=Path("install"),
                backup_directory=Path("backup"),
                default_target_directory=Path("/target"),
                default_install_method=InstallMethod.SYMLINK,
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs=RenderEngineRegistry(),
            env_resolve=EnvResolve(),
        )

        pkg_dict = {
            "package": {
                "name": "pkg_fallback_test",
                "target_directory": "${FALLBACK_SRC_DIR}/subtarget",
            },
            "env": {
                "fallback": {
                    "FALLBACK_SRC_DIR": "${drift_package_source_dir}",
                    "EXTERNAL_VAR": "${drift_package_source_dir}/fallback_ext",
                }
            }
        }

        # 1. When EXTERNAL_VAR is unset in os.environ, fallback takes effect
        stitched, env_res = resolve_and_interpolate_package_config(pkg_dict, package_name="pkg_fallback_test", workspace_config=ws)
        pkg_cfg = PackageConfig.from_dict(stitched,
                                          package_name="pkg_fallback_test",
                                          base_dir=self.drift_root / "src" / "pkg_fallback_test",
                                          workspace_config=ws)
        expected_src = str(self.drift_root / "src" / "pkg_fallback_test")
        self.assertEqual(pkg_cfg.env_resolve.effective.fallback["FALLBACK_SRC_DIR"], expected_src)
        self.assertEqual(pkg_cfg.env_resolve.effective.fallback["EXTERNAL_VAR"], f"{expected_src}/fallback_ext")
        self.assertEqual(str(pkg_cfg.package.target_directory), f"{expected_src}/subtarget")

        # 2. When EXTERNAL_VAR is already set in outer environment, outer value takes precedence over [env.fallback]
        with patch.dict(os.environ, {"EXTERNAL_VAR": "/custom/external/path"}):
            with pkg_cfg.package_envs():
                self.assertEqual(os.environ.get("EXTERNAL_VAR"), "/custom/external/path")
                self.assertEqual(os.environ.get("FALLBACK_SRC_DIR"), expected_src)

    def test_load_package_config_from_source_dir_writes_stitched_toml_and_renders(self) -> None:
        """Verifies that PackageConfig.from_source_dir writes out stitched TOML and PackageConfig.from_rendered_file reads it."""
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
        from drift.config.package_config import PackageConfig
        from drift.config.render_engine_config import RenderEngineRegistry

        ws = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                source_directory=Path("src"),
                render_directory=Path("render"),
                install_directory=Path("install"),
                backup_directory=Path("backup"),
                default_target_directory=Path("/target"),
                default_install_method=InstallMethod.SYMLINK,
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs=RenderEngineRegistry(),
            env_resolve=EnvResolve(),
        )

        pkg_src_dir = self.drift_root / "src" / "pkg_stitched_test"
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f"""
        [package]
        name = "pkg_stitched_test"
        target_directory = "${{drift_package_source_dir}}/my_target"

        [env.fallback]
        FALLBACK_SRC = "${{drift_package_source_dir}}"
        """, encoding="utf-8")

        # 1. Load from source dir with workspace config
        loaded_cfg = PackageConfig.from_source_dir(pkg_src_dir, ws)
        expected_src = str(self.drift_root / "src" / "pkg_stitched_test")
        self.assertEqual(str(loaded_cfg.package.target_directory), f"{expected_src}/my_target")
        self.assertEqual(loaded_cfg.env_resolve.effective.fallback["FALLBACK_SRC"], expected_src)

        # 2. Verify rendered file on disk in render/
        rendered_toml_path = self.drift_root / "render" / "pkg_stitched_test" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_toml_path.exists())
        rendered_content = rendered_toml_path.read_text(encoding="utf-8")
        self.assertNotIn("${drift_package_source_dir}", rendered_content)
        self.assertIn(f"{expected_src}/my_target", rendered_content)

        # 3. Load from rendered file directly (without workspace config)
        rendered_cfg = PackageConfig.from_rendered_file(
            rendered_toml_path,
            package_name="pkg_stitched_test",
            package_dir=self.drift_root / "render" / "pkg_stitched_test",
            workspace_config=ws,
        )
        self.assertEqual(str(rendered_cfg.package.target_directory), f"{expected_src}/my_target")
        self.assertEqual(rendered_cfg.env_resolve.effective.fallback["FALLBACK_SRC"], expected_src)

        # 4. Load from rendered package directory
        rendered_dir_cfg = PackageConfig.from_render_dir(
            self.drift_root / "render" / "pkg_stitched_test",
            workspace_config=ws,
        )
        self.assertEqual(str(rendered_dir_cfg.package.target_directory), f"{expected_src}/my_target")
        self.assertEqual(rendered_dir_cfg.env_resolve.effective.fallback["FALLBACK_SRC"], expected_src)

    def test_workspace_config_secrets_dict_and_scope(self) -> None:
        """Verifies that secrets are loaded into WorkspaceConfig.env_resolve.effective.secrets."""
        from drift.config.workspace_config import WorkspaceConfig
        from drift.utils.env_utils import resolve_env_configs

        # Write secrets.env and workspace config
        (self.config_dir / WORKSPACE_CONFIG_FILE_NAME).write_text("[workspace]\n[packages.enable]\n", encoding="utf-8")
        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text("MY_SECRET_KEY=\"my_secret_val\"\n", encoding="utf-8")

        # 1. Test via WorkspaceConfig.from_workspace_dir
        ws = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws.env_resolve.effective.secrets, {"MY_SECRET_KEY": "my_secret_val"})

        # Verify secrets are NOT leaked in os.environ outside scope
        self.assertNotIn("MY_SECRET_KEY", os.environ)

        # 2. Test env_scope with secrets dict directly
        with env_scope(ws.env_resolve.effective.secrets, mask_values=True):
            self.assertEqual(os.environ["MY_SECRET_KEY"], "my_secret_val")
        self.assertNotIn("MY_SECRET_KEY", os.environ)

        # 3. Test direct WorkspaceConfig instantiation with manual secrets dict
        manual_ws = WorkspaceConfig(
            drift_root=self.drift_root,
            env_resolve=resolve_env_configs(EnvConfig(secrets={"CUSTOM_SEC": "custom_val"})),
        )
        self.assertEqual(manual_ws.env_resolve.effective.secrets, {"CUSTOM_SEC": "custom_val"})


class TestEnvSecretsHierarchy(unittest.TestCase):
    """Comprehensive test suite for hierarchical [env.secrets], topological resolution, and 6-tier precedence."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name)
        self.config_dir = self.drift_root / CONFIG_DIR_NAME
        self.config_dir.mkdir(parents=True, exist_ok=True)
        (self.drift_root / "src").mkdir(parents=True, exist_ok=True)
        (self.drift_root / "render").mkdir(parents=True, exist_ok=True)
        (self.drift_root / "install").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_resolve_and_interpolate_workspace_config_pure_in_memory(self) -> None:
        """Verifies resolve_and_interpolate_workspace_config resolves secrets topologically without modifying os.environ."""
        from drift.config.workspace_loader import resolve_and_interpolate_workspace_config

        with patch.dict(os.environ, {"HOST_CLI_VAR": "cli_val", "drift_os": "linux"}, clear=False):
            initial_environ_snapshot = dict(os.environ)

            data = {
                "workspace": {
                    "name": "sec_ws",
                    "target_directory": "/tmp/${DERIVED_VAR}",
                },
                "env": {
                    "default": {
                        "DERIVED_VAR": "derived_${SECRET_TOKEN}",
                    },
                    "secrets": {
                        "FILE_BASE_SEC": "${SECRETS_FILE_KEY}_extended",
                        "SECRET_TOKEN": "${FILE_BASE_SEC}_token",
                        "drift_os": "malicious_os_override",  # Tier 4 attempting to overwrite Tier 3 facts
                        "HOST_CLI_VAR": "secret_cli_override", # Tier 4 attempting to overwrite Tier 1 CLI
                    }
                }
            }
            secrets_file = {
                "SECRETS_FILE_KEY": "raw_secret",
            }

            interpolated_dict, env_res = resolve_and_interpolate_workspace_config(
                data,
                secrets_file=secrets_file,
            )
            effective_secrets = env_res.effective.secrets
            current_secrets = env_res.current.secrets

            # 1. Verify os.environ was NOT mutated
            self.assertEqual(dict(os.environ), initial_environ_snapshot)

            # 2. Verify effective secrets were resolved topologically
            self.assertEqual(effective_secrets["SECRETS_FILE_KEY"], "raw_secret")
            self.assertEqual(effective_secrets["FILE_BASE_SEC"], "raw_secret_extended")
            self.assertEqual(effective_secrets["SECRET_TOKEN"], "raw_secret_extended_token")
            self.assertEqual(current_secrets["FILE_BASE_SEC"], "raw_secret_extended")
            self.assertEqual(current_secrets["SECRET_TOKEN"], "raw_secret_extended_token")

            # 3. Verify Tier 2 facts and Tier 3 ambient context: drift_os and HOST_CLI_VAR are preserved in base
            self.assertEqual(os.environ["drift_os"], "linux")
            self.assertEqual(os.environ["HOST_CLI_VAR"], "cli_val")

            # 4. Verify regular [env.default] was resolved against secrets
            self.assertEqual(env_res.effective.default["DERIVED_VAR"], "derived_raw_secret_extended_token")
            self.assertEqual(interpolated_dict["workspace"]["target_directory"], "/tmp/derived_raw_secret_extended_token")

    def test_interpolate_workspace_config_uses_ambient_env_without_fallback(self) -> None:
        """Verifies ambient environment variables not declared in [env] can be interpolated in workspace paths."""
        from drift.config.workspace_loader import resolve_and_interpolate_workspace_config

        with patch.dict(os.environ, {"UNDECLARED_AMBIENT_VAR": "ambient_path_val"}):
            data = {
                "workspace": {
                    "name": "ambient_ws",
                    "target_directory": "/opt/${UNDECLARED_AMBIENT_VAR}/app",
                }
            }
            interpolated_dict, env_res = resolve_and_interpolate_workspace_config(data)
            self.assertEqual(
                interpolated_dict["workspace"]["target_directory"],
                "/opt/ambient_path_val/app",
            )

    def test_workspace_secrets_precedence_and_python_hook(self) -> None:
        """Verifies workspace secret precedence: hook > local.toml > toml > secrets.env."""
        from drift.config.workspace_config import WorkspaceConfig

        # 1. secrets.env
        secrets_env = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_env.write_text("SHARED_KEY=from_env_file\nENV_ONLY=env_val\n", encoding="utf-8")

        # 2. drift_workspace.toml
        (self.config_dir / WORKSPACE_CONFIG_FILE_NAME).write_text(
            """[workspace]
[packages.enable]
[env.secrets]
SHARED_KEY = "from_workspace_toml"
TOML_ONLY = "toml_val"
""",
            encoding="utf-8",
        )

        # 3. drift_workspace.local.toml
        (self.config_dir / WORKSPACE_CONFIG_LOCAL_FILE_NAME).write_text(
            """[env.secrets]
SHARED_KEY = "from_workspace_local"
LOCAL_ONLY = "local_val"
""",
            encoding="utf-8",
        )

        # 4. drift_workspace.py hook injecting a secret
        hook_py = self.config_dir / "drift_workspace.py"
        hook_py.write_text(
            """def configure_workspace(context):
    cfg = context.config
    secrets = cfg.setdefault("env", {}).setdefault("secrets", {})
    secrets["HOOK_KEY"] = "hook_val"
    secrets["SHARED_KEY"] = "from_python_hook"
    return cfg
""",
            encoding="utf-8",
        )

        ws = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws.env_resolve.effective.secrets["SHARED_KEY"], "from_python_hook")
        self.assertEqual(ws.env_resolve.effective.secrets["HOOK_KEY"], "hook_val")
        self.assertEqual(ws.env_resolve.effective.secrets["LOCAL_ONLY"], "local_val")
        self.assertEqual(ws.env_resolve.effective.secrets["TOML_ONLY"], "toml_val")
        self.assertEqual(ws.env_resolve.effective.secrets["ENV_ONLY"], "env_val")

    def test_package_secrets_three_tier_precedence_and_render_staging(self) -> None:
        """Verifies Package [env.secrets] > Workspace [env.secrets] > secrets.env, render staging, and package_envs."""
        from drift.config.workspace_config import WorkspaceConfig
        from drift.config.package_config import PackageConfig
        from drift.utils.toml_utils import parse_toml

        # Setup secrets.env
        (self.config_dir / SECRETS_ENV_FILE_NAME).write_text(
            "TIER5_OVERRIDE=level_1_file\nFILE_SECRET=file_val\n",
            encoding="utf-8"
        )

        # Setup drift_workspace.toml with [env.secrets]
        (self.config_dir / WORKSPACE_CONFIG_FILE_NAME).write_text(
            """[workspace]
[packages.enable]
pkg_a = true

[env.secrets]
TIER5_OVERRIDE = "level_2_workspace"
WS_SECRET = "ws_val"
""",
            encoding="utf-8"
        )

        # Setup src/pkg_a/drift_package.toml with [env.secrets], [env.fallback], [env.override]
        pkg_dir = self.drift_root / "src" / "pkg_a"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            """[package]
name = "pkg_a"

[env.secrets]
TIER5_OVERRIDE = "level_3_package"
PKG_SECRET = "${WS_SECRET}_derived_${drift_package_name}"

[env.fallback]
MY_FALLBACK = "fallback_with_${PKG_SECRET}"

[env.override]
MY_OVERRIDE = "override_with_${TIER5_OVERRIDE}"
""",
            encoding="utf-8"
        )

        ws = WorkspaceConfig.from_workspace_dir(self.drift_root)
        pkg_cfg = PackageConfig.from_source_dir(pkg_dir, ws)

        # 1. Verify package_config.env.secrets contains resolved package secrets
        self.assertEqual(pkg_cfg.env_resolve.effective.secrets["TIER5_OVERRIDE"], "level_3_package")
        self.assertEqual(pkg_cfg.env_resolve.effective.secrets["PKG_SECRET"], "ws_val_derived_pkg_a")
        self.assertEqual(pkg_cfg.env_resolve.current.secrets["TIER5_OVERRIDE"], "level_3_package")
        self.assertEqual(pkg_cfg.env_resolve.current.secrets["PKG_SECRET"], "ws_val_derived_pkg_a")

        # 2. Verify fallback and override used resolved secrets
        self.assertEqual(pkg_cfg.env_resolve.effective.fallback["MY_FALLBACK"], "fallback_with_ws_val_derived_pkg_a")
        self.assertEqual(pkg_cfg.env_resolve.effective.override["MY_OVERRIDE"], "override_with_level_3_package")

        # 3. Verify render/pkg_a/.drift/drift_package.toml on disk contains fully resolved [env.secrets]
        rendered_pkg_dir = self.drift_root / "render" / "pkg_a"
        rendered_toml_path = rendered_pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME
        self.assertTrue(rendered_toml_path.exists())
        disk_data = parse_toml(rendered_toml_path.read_text(encoding="utf-8"))
        self.assertIn("env", disk_data)
        self.assertIn("secrets", disk_data["env"])
        self.assertEqual(disk_data["env"]["secrets"], {
            "TIER5_OVERRIDE": "level_3_package",
            "PKG_SECRET": "ws_val_derived_pkg_a",
        })
        self.assertIn("fallback", disk_data["env"])
        self.assertIn("override", disk_data["env"])

        # 4. Verify package_envs puts merged secrets in os.environ (Tier 4 precedence) and unloads cleanly
        self.assertNotIn("TIER5_OVERRIDE", os.environ)
        self.assertNotIn("PKG_SECRET", os.environ)
        self.assertNotIn("WS_SECRET", os.environ)

        with pkg_cfg.package_envs():
            self.assertEqual(os.environ["TIER5_OVERRIDE"], "level_3_package")
            self.assertEqual(os.environ["PKG_SECRET"], "ws_val_derived_pkg_a")
            self.assertEqual(os.environ["WS_SECRET"], "ws_val")
            self.assertEqual(os.environ["FILE_SECRET"], "file_val")
            self.assertEqual(os.environ["MY_FALLBACK"], "fallback_with_ws_val_derived_pkg_a")
            self.assertEqual(os.environ["MY_OVERRIDE"], "override_with_level_3_package")

        self.assertNotIn("TIER5_OVERRIDE", os.environ)
        self.assertNotIn("PKG_SECRET", os.environ)
        self.assertNotIn("WS_SECRET", os.environ)
        self.assertNotIn("FILE_SECRET", os.environ)

        # 5. Verify that loading from render/ preserves full secret execution in downstream lifecycle hooks
        rendered_cfg = PackageConfig.from_render_dir(
                rendered_pkg_dir,
                ws
        )
        with rendered_cfg.package_envs():
            self.assertEqual(os.environ["TIER5_OVERRIDE"], "level_3_package")
            self.assertEqual(os.environ["PKG_SECRET"], "ws_val_derived_pkg_a")
            self.assertEqual(os.environ["WS_SECRET"], "ws_val")
            self.assertEqual(os.environ["FILE_SECRET"], "file_val")

    def test_package_python_hook_injects_secrets(self) -> None:
        """Verifies that dynamic Python package hook (drift_package.py) can inject [env.secrets]."""
        from drift.config.workspace_config import WorkspaceConfig
        from drift.config.package_config import PackageConfig

        (self.config_dir / WORKSPACE_CONFIG_FILE_NAME).write_text(
            """[workspace]
[packages.enable]
hook_pkg = true
""",
            encoding="utf-8"
        )

        pkg_dir = self.drift_root / "src" / "hook_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            """[package]
name = "hook_pkg"
""",
            encoding="utf-8"
        )

        # Create drift_package.py hook
        (pkg_dir / "drift_package.py").write_text(
            """def configure_package(context):
    cfg = context.config
    cfg.setdefault("env", {}).setdefault("secrets", {})["DYNAMIC_PKG_SECRET"] = "dyn_secret_123"
    return cfg
""",
            encoding="utf-8"
        )

        ws = WorkspaceConfig.from_workspace_dir(self.drift_root)
        pkg_cfg = PackageConfig.from_source_dir(pkg_dir, ws)

        self.assertEqual(pkg_cfg.env_resolve.effective.secrets.get("DYNAMIC_PKG_SECRET"), "dyn_secret_123")
        with pkg_cfg.package_envs():
            self.assertEqual(os.environ.get("DYNAMIC_PKG_SECRET"), "dyn_secret_123")
        self.assertNotIn("DYNAMIC_PKG_SECRET", os.environ)
        with pkg_cfg.package_envs():
            self.assertEqual(os.environ.get("DYNAMIC_PKG_SECRET"), "dyn_secret_123")
        self.assertNotIn("DYNAMIC_PKG_SECRET", os.environ)


class TestMergeKvPairs(unittest.TestCase):
    """Unit tests for the generic merge_kvpairs functional utility."""

    def test_merge_kvpairs_empty_inputs(self) -> None:
        from drift.utils.env_utils import merge_kvpairs
        self.assertEqual(merge_kvpairs([]), {})

    def test_merge_kvpairs_string_mappings(self) -> None:
        from drift.utils.env_utils import merge_kvpairs
        m1 = {"a": "1", "b": "2"}
        m2 = {"b": "override", "c": "3"}
        self.assertEqual(merge_kvpairs([m1, m2]), {"a": "1", "b": "override", "c": "3"})




class TestEnvDagResolutionOrder(unittest.TestCase):
    """Unit tests validating the unidirectional DAG resolution barrier (secret >> fallback >> default >> override)."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_secret_referencing_default_raises_config_error(self) -> None:
        """Secrets (Tier 4) resolves first and must NOT be able to reference [env.default] (Tier 5)."""
        from drift.utils.env_utils import resolve_env_configs
        from drift.core.exceptions import ConfigError

        current = EnvConfig(
            secrets={"MY_SECRET": "secret_${DEFAULT_VAR}"},
            default={"DEFAULT_VAR": "default_val"},
        )
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_configs(current)
        self.assertIn("DEFAULT_VAR", str(ctx.exception))

    def test_secret_referencing_override_raises_config_error(self) -> None:
        """Secrets (Tier 4) resolves first and must NOT be able to reference [env.override] (Tier 2)."""
        from drift.utils.env_utils import resolve_env_configs
        from drift.core.exceptions import ConfigError

        current = EnvConfig(
            secrets={"MY_SECRET": "secret_${OVERRIDE_VAR}"},
            override={"OVERRIDE_VAR": "override_val"},
        )
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_configs(current)
        self.assertIn("OVERRIDE_VAR", str(ctx.exception))

    def test_fallback_referencing_default_raises_config_error(self) -> None:
        """Fallback (Tier 6) resolves second and must NOT be able to reference [env.default] (Tier 5)."""
        from drift.utils.env_utils import resolve_env_configs
        from drift.core.exceptions import ConfigError

        current = EnvConfig(
            fallback={"MY_FALLBACK": "fb_${DEFAULT_VAR}"},
            default={"DEFAULT_VAR": "default_val"},
        )
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_configs(current)
        self.assertIn("DEFAULT_VAR", str(ctx.exception))

    def test_fallback_referencing_override_raises_config_error(self) -> None:
        """Fallback (Tier 6) resolves second and must NOT be able to reference [env.override] (Tier 2)."""
        from drift.utils.env_utils import resolve_env_configs
        from drift.core.exceptions import ConfigError

        current = EnvConfig(
            fallback={"MY_FALLBACK": "fb_${OVERRIDE_VAR}"},
            override={"OVERRIDE_VAR": "override_val"},
        )
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_configs(current)
        self.assertIn("OVERRIDE_VAR", str(ctx.exception))

    def test_default_referencing_override_raises_config_error(self) -> None:
        """Default (Tier 5) resolves third and must NOT be able to reference [env.override] (Tier 2)."""
        from drift.utils.env_utils import resolve_env_configs
        from drift.core.exceptions import ConfigError

        current = EnvConfig(
            default={"MY_DEFAULT": "def_${OVERRIDE_VAR}"},
            override={"OVERRIDE_VAR": "override_val"},
        )
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_configs(current)
        self.assertIn("OVERRIDE_VAR", str(ctx.exception))

    def test_dag_valid_forward_cascade(self) -> None:
        """Valid forward references along the DAG (secret -> fallback -> default -> override) resolve seamlessly."""
        from drift.utils.env_utils import resolve_env_configs

        current = EnvConfig(
            secrets={"SEC": "sec_val"},
            fallback={"FB": "fb_${SEC}"},
            default={"DEF": "def_${SEC}_${FB}"},
            override={"OVR": "ovr_${SEC}_${FB}_${DEF}"},
        )
        res = resolve_env_configs(current)
        self.assertEqual(res.effective.secrets["SEC"], "sec_val")
        self.assertEqual(res.effective.fallback["FB"], "fb_sec_val")
        self.assertEqual(res.effective.default["DEF"], "def_sec_val_fb_sec_val")
        self.assertEqual(res.effective.override["OVR"], "ovr_sec_val_fb_sec_val_def_sec_val_fb_sec_val")


class TestEnvPrecedenceLadder(unittest.TestCase):
    """Unit tests asserting the exact 6-tier precedence ladder on individual variables."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_complete_6_tier_precedence_cascade_on_single_key(self) -> None:
        """Tests that resolution strictly follows Tier 1 (Override) > Tier 2 (Facts) > Tier 3 (CLI/Ambient) > Tier 4 (Secrets) > Tier 5 (Default) > Tier 6 (Fallback)."""
        # 1. All 6 tiers defined -> Tier 1 (Override) wins over all tiers including ambient os.environ
        os.environ["LADDER_KEY"] = "tier3_cli"
        config_all = EnvConfig(
            override={"LADDER_KEY": "tier1_override"},
            secrets={"LADDER_KEY": "tier4_secrets"},
            default={"LADDER_KEY": "tier5_default"},
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_all = resolve_env_configs(config_all, package_facts={"LADDER_KEY": "tier2_facts"})
        self.assertEqual(res_all.impact.restricted_env()["LADDER_KEY"], "tier1_override")
        with res_all.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier1_override")

        # 2. Tier 1 absent -> Tier 2 (Facts) wins over CLI, Secrets, Default, Fallback
        config_no_t1 = EnvConfig(
            secrets={"LADDER_KEY": "tier4_secrets"},
            default={"LADDER_KEY": "tier5_default"},
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_t2 = resolve_env_configs(config_no_t1, package_facts={"LADDER_KEY": "tier2_facts"})
        self.assertEqual(res_t2.impact.restricted_env()["LADDER_KEY"], "tier2_facts")
        with res_t2.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier2_facts")

        # 3. Tier 1 & Tier 2 absent -> Tier 3 (CLI/Ambient) wins over Secrets, Default, Fallback
        config_no_t1_t2 = EnvConfig(
            secrets={"LADDER_KEY": "tier4_secrets"},
            default={"LADDER_KEY": "tier5_default"},
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_t3 = resolve_env_configs(config_no_t1_t2, package_facts={})
        self.assertEqual(res_t3.impact.restricted_env()["LADDER_KEY"], "tier3_cli")
        with res_t3.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier3_cli")

        # 4. Tier 1, 2, 3 absent -> Tier 4 (Secrets) wins over Default, Fallback
        os.environ.pop("LADDER_KEY", None)
        config_no_t3 = EnvConfig(
            secrets={"LADDER_KEY": "tier4_secrets"},
            default={"LADDER_KEY": "tier5_default"},
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_t4 = resolve_env_configs(config_no_t3, package_facts={})
        self.assertEqual(res_t4.impact.restricted_env()["LADDER_KEY"], "tier4_secrets")
        with res_t4.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier4_secrets")

        # 5. Tier 1, 2, 3, 4 absent -> Tier 5 (Default) wins over Fallback
        config_no_t4 = EnvConfig(
            default={"LADDER_KEY": "tier5_default"},
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_t5 = resolve_env_configs(config_no_t4, package_facts={})
        self.assertEqual(res_t5.impact.restricted_env()["LADDER_KEY"], "tier5_default")
        with res_t5.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier5_default")

        # 6. Only Tier 6 (Fallback) defined -> Fallback provides the value
        config_only_t6 = EnvConfig(
            fallback={"LADDER_KEY": "tier6_fallback"},
        )
        res_t6 = resolve_env_configs(config_only_t6, package_facts={})
        self.assertEqual(res_t6.impact.restricted_env()["LADDER_KEY"], "tier6_fallback")
        with res_t6.impact.scope():
            self.assertEqual(os.environ["LADDER_KEY"], "tier6_fallback")

    def test_resolve_env_configs_uses_system_facts_without_mutating_os_environ(self) -> None:
        """Verifies resolve_env_configs incorporates system facts purely in-memory without polluting os.environ."""
        from drift.utils.host_facts import get_cached_system_facts
        cached_facts = get_cached_system_facts()
        self.assertIn("drift_os", cached_facts)

        # Clear drift_* from os.environ to ensure no ambient pollution
        for k in cached_facts:
            os.environ.pop(k, None)

        initial_env = dict(os.environ)

        config = EnvConfig(
            default={"MY_OS": "${drift_os}_custom"},
            override={"REF_ARCH": "${drift_arch}_target"},
        )
        package_facts = {"drift_package_name": "test_pkg", "drift_os": "virtual_os"}

        res = resolve_env_configs(config, package_facts=package_facts)

        # 1. Package facts override system facts (Tier 2 intra-tier precedence)
        self.assertEqual(res.effective.default["MY_OS"], "virtual_os_custom")
        self.assertEqual(res.effective.override["REF_ARCH"], f"{cached_facts['drift_arch']}_target")

        # 2. Both system facts and package facts are included in the impact overrides
        self.assertEqual(res.impact.overrides["drift_package_name"], "test_pkg")
        self.assertEqual(res.impact.overrides["drift_os"], "virtual_os")
        self.assertEqual(res.impact.overrides["drift_arch"], cached_facts["drift_arch"])

        # 3. os.environ was NOT mutated
        self.assertEqual(dict(os.environ), initial_env)


class TestSystemFactsHierarchy(unittest.TestCase):
    """Exhaustive tests verifying the exact precedence hierarchy of System Facts across all tiers.

    Hierarchy Specification:
    1. Tier 1 (Override): Package [env.override] > Workspace [env.override] > Tier 2 Facts
    2. Tier 2 (Facts): Package Facts (Authoritative) > System Facts (Auto-detected)
    3. Tier 2 Facts > Tier 3 (Ambient os.environ / CLI flags)
    4. Tier 2 Facts > Tier 4 (Secrets: Package > Workspace > secrets.env)
    5. Tier 2 Facts > Tier 5 (Default: Package > Workspace)
    6. Tier 2 Facts > Tier 6 (Fallback: Package > Workspace)
    """

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        from drift.utils.host_facts import get_cached_system_facts
        self.cached_facts = dict(get_cached_system_facts())
        self.system_os = self.cached_facts["drift_os"]
        self.system_arch = self.cached_facts["drift_arch"]

        # Ensure clean os.environ without pre-existing drift_*
        for k in self.cached_facts:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_tier1_override_beats_system_facts(self) -> None:
        """Tier 1 [env.override] takes precedence over auto-probed system facts."""
        config = EnvConfig(override={"drift_os": "forced_override_os"})
        res = resolve_env_configs(config)
        self.assertEqual(res.impact.overrides["drift_os"], "forced_override_os")
        self.assertEqual(res.impact.restricted_env()["drift_os"], "forced_override_os")
        with res.impact.scope():
            self.assertEqual(os.environ["drift_os"], "forced_override_os")

    def test_tier2_package_facts_beats_system_facts(self) -> None:
        """Tier 2 Package facts take precedence over System facts within Tier 2."""
        config = EnvConfig()
        package_facts = {"drift_os": "package_specific_os"}
        res = resolve_env_configs(config, package_facts=package_facts)
        # drift_os is overridden by package fact
        self.assertEqual(res.impact.overrides["drift_os"], "package_specific_os")
        # other system facts remain intact
        self.assertEqual(res.impact.overrides["drift_arch"], self.system_arch)

    def test_system_facts_beats_tier3_ambient_environ(self) -> None:
        """Tier 2 System facts overwrite polluted ambient os.environ."""
        os.environ["drift_os"] = "ambient_polluted_os"
        os.environ["drift_arch"] = "ambient_polluted_arch"

        config = EnvConfig()
        res = resolve_env_configs(config)
        # Auto-probed system facts must overwrite the ambient polluted values
        self.assertEqual(res.impact.overrides["drift_os"], self.system_os)
        self.assertEqual(res.impact.overrides["drift_arch"], self.system_arch)
        with res.impact.scope():
            self.assertEqual(os.environ["drift_os"], self.system_os)
            self.assertEqual(os.environ["drift_arch"], self.system_arch)

    def test_system_facts_beats_tier4_secrets(self) -> None:
        """Tier 2 System facts cannot be shadowed by Tier 4 secrets."""
        config = EnvConfig(secrets={"drift_os": "secret_os", "drift_arch": "secret_arch"})
        lower = EnvConfig(secrets={"drift_distro": "lower_secret_distro"})
        res = resolve_env_configs(config, lower_layer=lower)
        # System facts win over secrets in both current and lower layers
        self.assertEqual(res.impact.overrides["drift_os"], self.system_os)
        self.assertEqual(res.impact.overrides["drift_arch"], self.system_arch)
        self.assertEqual(res.impact.overrides["drift_distro"], self.cached_facts["drift_distro"])

    def test_system_facts_beats_tier5_default(self) -> None:
        """Tier 2 System facts take precedence over Tier 5 default."""
        config = EnvConfig(default={"drift_os": "default_os"})
        lower = EnvConfig(default={"drift_arch": "lower_default_arch"})
        res = resolve_env_configs(config, lower_layer=lower)
        self.assertEqual(res.impact.overrides["drift_os"], self.system_os)
        self.assertEqual(res.impact.overrides["drift_arch"], self.system_arch)

    def test_system_facts_beats_tier6_fallback(self) -> None:
        """Tier 2 System facts take precedence over Tier 6 fallback."""
        config = EnvConfig(fallback={"drift_os": "fallback_os"})
        lower = EnvConfig(fallback={"drift_arch": "lower_fallback_arch"})
        res = resolve_env_configs(config, lower_layer=lower)
        self.assertEqual(res.impact.overrides["drift_os"], self.system_os)
        self.assertEqual(res.impact.overrides["drift_arch"], self.system_arch)

    def test_full_system_facts_hierarchy_ladder_on_single_fact(self) -> None:
        """Verifies full cascade when drift_os is defined simultaneously across all 6 tiers."""
        # 1. All defined: Tier 1 Override wins
        os.environ["drift_os"] = "tier3_ambient_os"
        config_all = EnvConfig(
            override={"drift_os": "tier1_override_os"},
            secrets={"drift_os": "tier4_secret_os"},
            default={"drift_os": "tier5_default_os"},
            fallback={"drift_os": "tier6_fallback_os"},
        )
        res_t1 = resolve_env_configs(config_all, package_facts={"drift_os": "tier2_package_os"})
        self.assertEqual(res_t1.impact.overrides["drift_os"], "tier1_override_os")

        # 2. Tier 1 absent: Tier 2 Package Fact wins over System Fact, Ambient, Secrets, Default, Fallback
        config_no_t1 = EnvConfig(
            secrets={"drift_os": "tier4_secret_os"},
            default={"drift_os": "tier5_default_os"},
            fallback={"drift_os": "tier6_fallback_os"},
        )
        res_t2_pkg = resolve_env_configs(config_no_t1, package_facts={"drift_os": "tier2_package_os"})
        self.assertEqual(res_t2_pkg.impact.overrides["drift_os"], "tier2_package_os")

        # 3. Package Fact absent: Tier 2 System Fact wins over Ambient, Secrets, Default, Fallback
        res_t2_sys = resolve_env_configs(config_no_t1, package_facts={})
        self.assertEqual(res_t2_sys.impact.overrides["drift_os"], self.system_os)

    def test_system_facts_reference_expansion_across_all_tiers(self) -> None:
        """Verifies that all tiers (Fallback, Default, Secrets, Override) can reference system facts."""
        config = EnvConfig(
            fallback={"FB_REF": "fallback_${drift_os}"},
            secrets={"SEC_REF": "secret_${drift_arch}"},
            default={"DEF_REF": "default_${drift_distro}"},
            override={"OVR_REF": "override_${drift_hostname}_${drift_user}"},
        )
        res = resolve_env_configs(config)
        self.assertEqual(res.effective.fallback["FB_REF"], f"fallback_{self.system_os}")
        self.assertEqual(res.effective.secrets["SEC_REF"], f"secret_{self.system_arch}")
        self.assertEqual(res.effective.default["DEF_REF"], f"default_{self.cached_facts['drift_distro']}")
        self.assertEqual(
            res.effective.override["OVR_REF"],
            f"override_{self.cached_facts['drift_hostname']}_{self.cached_facts['drift_user']}"
        )


class TestEnvParsingAndAliasing(unittest.TestCase):
    """Unit tests for parse_env_dict, subtable aliasing, and EnvConfig serialization."""

    def test_parse_env_dict_supports_subtable_aliases(self) -> None:
        """[env.override] and alias [env.overwrite] in the same dict are merged seamlessly."""
        from drift.utils.env_utils import parse_env_dict

        data = {
            "override": {"KEY_A": "from_override", "SHARED": "override_val"},
            "overwrite": {"KEY_B": "from_overwrite", "SHARED": "overwrite_val"},
            "secrets": {"SEC": "secret_val"},
            "default": {"DEF": "default_val"},
            "fallback": {"FB": "fallback_val"},
        }
        cfg = parse_env_dict(data)
        self.assertEqual(cfg.override["KEY_A"], "from_override")
        self.assertEqual(cfg.override["KEY_B"], "from_overwrite")
        self.assertEqual(cfg.override["SHARED"], "overwrite_val")
        self.assertEqual(cfg.secrets["SEC"], "secret_val")
        self.assertEqual(cfg.default["DEF"], "default_val")
        self.assertEqual(cfg.fallback["FB"], "fallback_val")

    def test_parse_env_dict_rejects_non_dict_subtable(self) -> None:
        """Passing a non-dict to a subtable (e.g. override = 'string') raises ConfigError."""
        from drift.utils.env_utils import parse_env_dict
        from drift.core.exceptions import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            parse_env_dict({"override": "not_a_dict"})
        self.assertIn("[env.override] must be a table of key-value pairs", str(ctx.exception))

    def test_parse_env_dict_rejects_unknown_subtables(self) -> None:
        """Unknown keys under [env] raise ConfigError."""
        from drift.utils.env_utils import parse_env_dict
        from drift.core.exceptions import ConfigError

        with self.assertRaises(ConfigError) as ctx:
            parse_env_dict({"unknown_tier": {"A": "1"}})
        self.assertIn("Unsupported key 'unknown_tier' in [env]", str(ctx.exception))

    def test_parse_env_dict_empty_or_none(self) -> None:
        """Empty or None input returns default empty EnvConfig."""
        from drift.utils.env_utils import parse_env_dict

        self.assertEqual(parse_env_dict({}), EnvConfig())
        self.assertEqual(parse_env_dict(None), EnvConfig())

    def test_env_config_to_env_dict_serialization(self) -> None:
        """EnvConfig.to_env_dict drops empty tables and round-trips with parse_env_dict."""
        from drift.utils.env_utils import parse_env_dict

        empty_cfg = EnvConfig()
        self.assertEqual(empty_cfg.to_env_dict(), {})
        self.assertEqual(parse_env_dict(empty_cfg.to_env_dict()), empty_cfg)

        partial_cfg = EnvConfig(override={"O": "1"}, default={"D": "2"})
        serialized = partial_cfg.to_env_dict()
        self.assertEqual(serialized, {"override": {"O": "1"}, "default": {"D": "2"}})
        self.assertNotIn("secrets", serialized)
        self.assertNotIn("fallback", serialized)
        self.assertEqual(parse_env_dict(serialized), partial_cfg)


class TestSecretsMaskingInLogs(unittest.TestCase):
    """Unit tests verifying secret value masking in debug logs."""

    def test_env_resolve_secret_keys_property(self) -> None:
        """Verifies EnvResolve.secret_keys returns the set of effective secret keys."""
        res = EnvResolve(
            current=EnvConfig(secrets={"LOCAL_KEY": "loc_val"}),
            effective=EnvConfig(secrets={"GLOBAL_KEY": "glob_val", "LOCAL_KEY": "loc_val"}),
        )
        self.assertEqual(res.secret_keys, {"GLOBAL_KEY", "LOCAL_KEY"})

    def test_env_scope_masks_secret_values_in_logger(self) -> None:
        """env_scope with mask_values=True masks secret values with **** in logger output."""
        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                with env_scope({"TOP_SECRET_PASSWORD": "super_secret_value_12345"}, mask_values=True):
                    pass

            log_output = "\n".join(cm.output)
            self.assertIn("TOP_SECRET_PASSWORD=****", log_output)
            self.assertNotIn("super_secret_value_12345", log_output)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_env_scope_selective_masking_with_iterable(self) -> None:
        """env_scope with mask_values=Iterable[str] masks only specified secret keys in logger."""
        set_test_mode(True, enable_logging=True)
        try:
            os.environ["EXISTING_SECRET"] = "old_secret_val"
            os.environ["EXISTING_NORMAL"] = "old_normal_val"

            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                with env_scope(
                    {
                        "NEW_SECRET": "secret_abc",
                        "NEW_NORMAL": "normal_xyz",
                        "EXISTING_SECRET": "updated_secret_def",
                        "EXISTING_NORMAL": "updated_normal_uvw",
                    },
                    overwrite=True,
                    mask_values={"NEW_SECRET", "EXISTING_SECRET"},
                ):
                    pass

            log_output = "\n".join(cm.output)
            # Loaded logs: secrets are masked, non-secrets are in clear text
            self.assertIn("Environment variable loaded: NEW_SECRET=****", log_output)
            self.assertIn("Environment variable loaded: EXISTING_SECRET=****", log_output)
            self.assertIn("Environment variable loaded: NEW_NORMAL=normal_xyz", log_output)
            self.assertIn("Environment variable loaded: EXISTING_NORMAL=updated_normal_uvw", log_output)

            # Restored logs: restored secrets are masked, restored non-secrets are in clear text
            self.assertIn("Environment variable unloaded: popped NEW_SECRET", log_output)
            self.assertIn("Environment variable unloaded: popped NEW_NORMAL", log_output)
            self.assertIn("Environment variable unloaded: restored EXISTING_SECRET=****", log_output)
            self.assertIn("Environment variable unloaded: restored EXISTING_NORMAL=old_normal_val", log_output)

            # Confirm raw secret values never appeared in logs
            self.assertNotIn("secret_abc", log_output)
            self.assertNotIn("updated_secret_def", log_output)
            self.assertNotIn("old_secret_val", log_output)
        finally:
            os.environ.pop("EXISTING_SECRET", None)
            os.environ.pop("EXISTING_NORMAL", None)
            set_test_mode(True, enable_logging=False)

    def test_env_impact_scope_granular_masking(self) -> None:
        """EnvImpact.scope automatically extracts secret_keys and masks only secrets in debug logs."""
        set_test_mode(True, enable_logging=True)
        try:
            os.environ["PRE_EXISTING_SECRET"] = "pre_secret_123"
            os.environ["AMBIENT_OVERRIDE_SECRET"] = "ambient_val_456"
            env_res = EnvResolve(
                effective=EnvConfig(
                    override={
                        "OVERRIDE_VAR": "my_override",
                        "PRE_EXISTING_SECRET": "new_secret_888",
                    },
                    secrets={
                        "MY_TOKEN": "secret_token_999",
                        "PRE_EXISTING_SECRET": "new_secret_888",
                        "AMBIENT_OVERRIDE_SECRET": "secret_fallback_val",
                    },
                    default={"APP_HOST": "localhost"},
                ),
            )

            with patch("sys.stderr", StringIO()), self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                with env_res.impact.scope():
                    self.assertEqual(os.environ["MY_TOKEN"], "secret_token_999")
                    self.assertEqual(os.environ["PRE_EXISTING_SECRET"], "new_secret_888")
                    # Ambient env (Tier 3) preserves ambient value against default secrets (Tier 4)
                    self.assertEqual(os.environ["AMBIENT_OVERRIDE_SECRET"], "ambient_val_456")
                    self.assertEqual(os.environ["APP_HOST"], "localhost")

            log_output = "\n".join(cm.output)
            # Secrets masked
            self.assertIn("Environment variable loaded: MY_TOKEN=****", log_output)
            self.assertIn("Environment variable loaded: PRE_EXISTING_SECRET=****", log_output)
            # Ambient env skipped for default secret
            self.assertIn("Environment variable skipped (already set and overwrite=False): AMBIENT_OVERRIDE_SECRET", log_output)
            # Non-secrets in clear text
            self.assertIn("Environment variable loaded: OVERRIDE_VAR=my_override", log_output)
            self.assertIn("Environment variable loaded: APP_HOST=localhost", log_output)

            # Restored secret is masked
            self.assertIn("Environment variable unloaded: restored PRE_EXISTING_SECRET=****", log_output)

            # No raw secrets leaked
            self.assertNotIn("secret_token_999", log_output)
            self.assertNotIn("new_secret_888", log_output)
            self.assertNotIn("pre_secret_123", log_output)
            self.assertNotIn("secret_fallback_val", log_output)
        finally:
            os.environ.pop("PRE_EXISTING_SECRET", None)
            os.environ.pop("AMBIENT_OVERRIDE_SECRET", None)
            set_test_mode(True, enable_logging=False)

    def test_package_envs_selective_secret_masking(self) -> None:
        """PackageConfig.package_envs masks package/workspace secrets while keeping facts/defaults in clear text."""
        from drift.config.package_config import PackageConfig, PackageSectionConfig
        from drift.config.workspace_config import WorkspaceConfig

        set_test_mode(True, enable_logging=True)
        try:
            ws = WorkspaceConfig(
                drift_root=Path("/mock/root"),
                env_resolve=EnvResolve(
                    effective=EnvConfig(
                        secrets={"WS_API_KEY": "super_secret_ws_key"},
                        default={"GLOBAL_SETTING": "enabled"},
                    )
                ),
            )
            pkg = PackageConfig(
                PackageSectionConfig(name="mask_pkg"),
                env_resolve=EnvResolve(
                    current=EnvConfig(
                        secrets={"PKG_SECRET_TOKEN": "vault_token_777"},
                        default={"LOCAL_SETTING": "custom_val"},
                    )
                ),
            )
            pkg.compute_effective_envs(ws)

            with self.assertLogs("drift.utils.env_utils", level="DEBUG") as cm:
                with pkg.package_envs():
                    self.assertEqual(os.environ["PKG_SECRET_TOKEN"], "vault_token_777")
                    self.assertEqual(os.environ["WS_API_KEY"], "super_secret_ws_key")
                    self.assertEqual(os.environ["LOCAL_SETTING"], "custom_val")
                    self.assertEqual(os.environ["drift_package_name"], "mask_pkg")

            log_output = "\n".join(cm.output)
            # Secrets masked
            self.assertIn("PKG_SECRET_TOKEN=****", log_output)
            self.assertIn("WS_API_KEY=****", log_output)
            # Non-secrets in clear text
            self.assertIn("LOCAL_SETTING=custom_val", log_output)
            self.assertIn("GLOBAL_SETTING=enabled", log_output)
            self.assertIn("drift_package_name=mask_pkg", log_output)

            # Zero leakage
            self.assertNotIn("vault_token_777", log_output)
            self.assertNotIn("super_secret_ws_key", log_output)
        finally:
            set_test_mode(True, enable_logging=False)


class TestPackageConfigEnvResolveEdgeCases(unittest.TestCase):
    """Unit tests for PackageConfig environment resolution edge cases."""

    def test_package_facts_reflection_overrides_workspace_target_and_install_method(self) -> None:
        """PackageConfig.get_drift_package_facts reflects package-level custom target and install_method."""
        from drift.config.package_config import PackageConfig
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        ws = WorkspaceConfig(
            drift_root=Path("/mock/drift"),
            workspace=WorkspaceSectionConfig(
                default_target_directory=Path("/default/target"),
                default_install_method=InstallMethod.SYMLINK,
            ),
        )

        from drift.config.package_config import PackageConfig, PackageSectionConfig
        pkg = PackageConfig(
            PackageSectionConfig(
                name="custom_pkg",
                target_directory=Path("/custom/pkg/target"),
                install_method=InstallMethod.COPY,
            )
        )

        facts = pkg.get_drift_package_facts(ws)
        self.assertEqual(facts["drift_package_name"], "custom_pkg")
        self.assertEqual(facts["drift_package_target_dir"], "/custom/pkg/target")
        self.assertEqual(facts["drift_package_install_method"], "copy")
        self.assertEqual(facts["drift_package_source_dir"], "/mock/drift/src/custom_pkg")

    def test_package_compute_effective_envs_standalone_vs_workspace(self) -> None:
        """compute_effective_envs resolves against workspace effective envs when provided and standalone when None."""
        from drift.config.package_config import PackageConfig, PackageSectionConfig
        from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        ws = WorkspaceConfig(
            drift_root=Path("/mock/drift"),
            workspace=WorkspaceSectionConfig(
                default_target_directory=Path("/default/target"),
            ),
            env_resolve=resolve_env_configs(EnvConfig(default={"WS_GLOBAL_VAR": "ws_val"})),
        )

        pkg = PackageConfig(
            PackageSectionConfig(name="demo_pkg"),
            env_resolve=EnvResolve(current=EnvConfig(override={"PKG_VAR": "${WS_GLOBAL_VAR}_extended"})),
        )

        # 1. With workspace_config -> WS_GLOBAL_VAR is resolved
        pkg.compute_effective_envs(ws)
        self.assertEqual(pkg.env_resolve.effective.override["PKG_VAR"], "ws_val_extended")
        self.assertEqual(pkg.env_resolve.effective.default["WS_GLOBAL_VAR"], "ws_val")

        # 2. Standalone without workspace_config -> referencing WS_GLOBAL_VAR raises ConfigError
        from drift.core.exceptions import ConfigError
        standalone_pkg = PackageConfig(
            PackageSectionConfig(name="demo_pkg"),
            env_resolve=EnvResolve(current=EnvConfig(override={"PKG_VAR": "${WS_GLOBAL_VAR}_extended"})),
        )
        with self.assertRaises(ConfigError):
            standalone_pkg.compute_effective_envs(None)


class TestEnvImpactUnit(unittest.TestCase):
    """Unit tests for EnvImpact data model: full_env, restricted_env, and scope."""

    def test_env_impact_full_env_precedence(self) -> None:
        """full_env implements overrides > base_env > defaults."""
        impact = EnvImpact(
            overrides={"O": "override_val", "SHARED": "override_shared"},
            defaults={"D": "default_val", "SHARED": "default_shared", "CLI_WIN": "default_cli"},
        )
        base = {
            "CLI_WIN": "ambient_cli_val",
            "SHARED": "ambient_shared_val",
            "HOST_ONLY": "host_val",
        }
        res = impact.full_env(base)
        # 1. Overrides win over base and defaults
        self.assertEqual(res["O"], "override_val")
        self.assertEqual(res["SHARED"], "override_shared")
        # 2. Base wins over defaults
        self.assertEqual(res["CLI_WIN"], "ambient_cli_val")
        # 3. Defaults fill in missing base keys
        self.assertEqual(res["D"], "default_val")
        # 4. Host variables preserved
        self.assertEqual(res["HOST_ONLY"], "host_val")

    def test_env_impact_restricted_env_zero_noise(self) -> None:
        """restricted_env projects only declared keys and strictly filters out ambient noise."""
        impact = EnvImpact(
            overrides={"OVERRIDE_KEY": "over_val"},
            defaults={"DEFAULT_KEY": "def_val", "CLI_OVERRIDDEN_KEY": "def_base"},
        )
        base = {
            "CLI_OVERRIDDEN_KEY": "custom_cli_val",
            "SHLVL": "2",
            "SSH_AUTH_SOCK": "/tmp/ssh.sock",
            "OLDPWD": "/var/tmp",
            "WINDOWID": "12345",
        }
        restricted = impact.restricted_env(base)
        # Declared keys present
        self.assertEqual(restricted["OVERRIDE_KEY"], "over_val")
        self.assertEqual(restricted["DEFAULT_KEY"], "def_val")
        self.assertEqual(restricted["CLI_OVERRIDDEN_KEY"], "custom_cli_val")
        # Zero session noise leakage
        self.assertNotIn("SHLVL", restricted)
        self.assertNotIn("SSH_AUTH_SOCK", restricted)
        self.assertNotIn("OLDPWD", restricted)
        self.assertNotIn("WINDOWID", restricted)
        self.assertEqual(set(restricted.keys()), impact.declared_keys)

    def test_env_impact_scope_two_phase_and_restoration(self) -> None:
        """scope performs two-phase activation (defaults conditional, overrides forced) and restores state."""
        impact = EnvImpact(
            overrides={"OVER_KEY": "new_over", "STOMP_KEY": "over_wins"},
            defaults={"DEF_KEY": "new_def", "PRE_KEY": "def_loses"},
        )
        target = {
            "PRE_KEY": "orig_pre",
            "STOMP_KEY": "orig_stomp",
            "UNCHANGED": "orig_unchanged",
        }

        with impact.scope(target):
            # Overrides overwrite unconditionally
            self.assertEqual(target["OVER_KEY"], "new_over")
            self.assertEqual(target["STOMP_KEY"], "over_wins")
            # Defaults do NOT overwrite existing keys
            self.assertEqual(target["PRE_KEY"], "orig_pre")
            # Defaults populate unset keys
            self.assertEqual(target["DEF_KEY"], "new_def")
            # Unrelated keys preserved
            self.assertEqual(target["UNCHANGED"], "orig_unchanged")

        # After exiting scope, state is completely restored
        self.assertEqual(target["PRE_KEY"], "orig_pre")
        self.assertEqual(target["STOMP_KEY"], "orig_stomp")
        self.assertEqual(target["UNCHANGED"], "orig_unchanged")
        self.assertNotIn("OVER_KEY", target)
        self.assertNotIn("DEF_KEY", target)


class TestEnvResolveUnit(unittest.TestCase):
    """Unit tests for EnvResolve data model: initialization, build_impact, and secret_keys."""

    def test_env_resolve_default_initialization(self) -> None:
        """Default initialization builds empty EnvConfigs and an empty EnvImpact."""
        res = EnvResolve()
        self.assertEqual(res.current, EnvConfig())
        self.assertEqual(res.effective, EnvConfig())
        self.assertEqual(res.impact.overrides, {})
        self.assertEqual(res.impact.defaults, {})
        self.assertEqual(res.secret_keys, set())

    def test_env_resolve_build_impact_from_effective(self) -> None:
        """build_impact compiles effective tables into an EnvImpact following 6-tier precedence."""
        res = EnvResolve(
            effective=EnvConfig(
                override={"O": "override_val", "SHARED": "override_shared"},
                secrets={"SEC": "secret_val"},
                default={"D": "default_val", "SHARED": "default_shared"},
                fallback={"FB": "fallback_val"},
            )
        )
        impact = res.build_impact()
        # Overrides win over defaults
        self.assertEqual(impact.overrides["O"], "override_val")
        self.assertEqual(impact.overrides["SHARED"], "override_shared")
        # Defaults merge fallback + default + secrets (disjoint from overrides)
        self.assertEqual(impact.defaults["FB"], "fallback_val")
        self.assertEqual(impact.defaults["D"], "default_val")
        self.assertEqual(impact.defaults["SEC"], "secret_val")
        self.assertNotIn("SHARED", impact.defaults)
        # Secrets are captured in secret_keys
        self.assertIn("SEC", impact.secret_keys)
        self.assertEqual(res.secret_keys, {"SEC"})

    def test_env_resolve_build_impact_with_all_facts(self) -> None:
        """build_impact merges all_facts into overrides with Tier 1 beating Tier 2 facts."""
        res = EnvResolve(
            effective=EnvConfig(
                override={"drift_os": "custom_os", "OTHER": "val"},
                default={"FB_KEY": "def"},
            )
        )
        facts = {
            "drift_os": "system_probed_os",
            "drift_arch": "x86_64",
            "drift_package_name": "my_pkg",
        }
        impact = res.build_impact(all_facts=facts)
        # Tier 1 override beats Tier 2 fact
        self.assertEqual(impact.overrides["drift_os"], "custom_os")
        # Non-colliding facts are included in overrides
        self.assertEqual(impact.overrides["drift_arch"], "x86_64")
        self.assertEqual(impact.overrides["drift_package_name"], "my_pkg")
        self.assertEqual(impact.overrides["OTHER"], "val")
        self.assertEqual(impact.defaults["FB_KEY"], "def")

    def test_env_resolve_init_with_all_facts_automatically_populates_impact(self) -> None:
        """Passing all_facts to EnvResolve.__init__ automatically compiles impact."""
        facts = {"drift_os": "auto_os", "drift_user": "tester"}
        res = EnvResolve(
            current=EnvConfig(override={"LOCAL": "curr_val"}),
            effective=EnvConfig(override={"LOCAL": "curr_val", "drift_os": "forced_os"}),
            all_facts=facts,
        )
        self.assertEqual(res.impact.overrides["LOCAL"], "curr_val")
        self.assertEqual(res.impact.overrides["drift_os"], "forced_os")
        self.assertEqual(res.impact.overrides["drift_user"], "tester")

    def test_env_resolve_explicit_impact_override(self) -> None:
        """Passing explicit impact overrides automatic build_impact."""
        custom_impact = EnvImpact(overrides={"EXPLICIT": "yes"})
        res = EnvResolve(
            effective=EnvConfig(override={"SHOULD_BE_IGNORED": "no"}),
            impact=custom_impact,
        )
        self.assertEqual(res.impact, custom_impact)
        self.assertEqual(res.impact.overrides, {"EXPLICIT": "yes"})


if __name__ == "__main__":
    unittest.main()



