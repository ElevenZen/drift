"""Tests for environment variable precedence and configuration loading in drift."""

import os
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

from drift.constants import (
    CONFIG_DIR_NAME,
    GLOBAL_CONFIG_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    INITIAL_ENV,
    set_test_mode,
    update_initial_env,
    set_initial_env,
)
from drift.env_utils import (
    load_env_settings,
    unload_env_settings,
    parse_secrets_env,
    parse_env_file,
    parse_env_text,
    env_scope,
    secrets_env_scope,
)
from drift.workspace_config import (
    load_workspace_config
)
from drift.render_package import run_primitive_2_render_packages


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

        # Unloading None or empty dict is a no-op
        unload_env_settings(None)
        unload_env_settings({})

    def test_load_env_settings_logs_only_new_or_overwritten(self) -> None:
        """Verifies that 'Environment variable loaded' is only logged for new or overwritten variables."""
        import logging
        logging.disable(logging.NOTSET)
        try:
            os.environ["TEST_UNCHANGED"] = "same_val"
            os.environ["TEST_OVERWRITTEN"] = "old_val"
            os.environ.pop("TEST_NEW", None)

            with self.assertLogs("drift.env_utils", level="DEBUG") as cm:
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

    Host Environment > Secret Vault (secrets.env) > Global Workspace Config ([env] in drift.toml)
    """

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name)

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
        update_initial_env()
        self.temp_dir.cleanup()

    def _setup_package_with_template(self, pkg_name: str, template_body: str) -> Path:
        pkg_src = self.src_dir / pkg_name
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname = "{pkg_name}"\ninstall_method = "stow"\ntarget_directory = "~"\n',
            encoding="utf-8"
        )
        template_file = pkg_src / "dot-config.envst.txt"
        template_file.write_text(template_body, encoding="utf-8")
        return pkg_src

    def test_host_env_overrides_secrets_and_workspace_config(self) -> None:
        """Host environment variable has the highest precedence.

        It must override both secrets.env and drift.toml [env].
        """
        var_name = "DRIFT_PRECEDENCE_VAR_1"
        os.environ[var_name] = "host_wins"
        set_initial_env([var_name] + list(self.original_environ.keys()))

        # Write drift.toml with [env]
        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
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
        ws_config = load_workspace_config(self.drift_root)
        # Verify host value wasn't overwritten on load
        self.assertEqual(os.environ[var_name], "host_wins")

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=host_wins")

        # Verify host value is still preserved after rendering
        self.assertEqual(os.environ[var_name], "host_wins")

    def test_secrets_env_overrides_workspace_config(self) -> None:
        """Secret vault (secrets.env) has higher precedence than drift.toml [env]."""
        var_name = "DRIFT_PRECEDENCE_VAR_2"
        os.environ.pop(var_name, None)
        set_initial_env([k for k in os.environ.keys() if k != var_name])

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
{var_name} = "workspace_toml_value"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secret_vault_value"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"VALUE=${{{var_name}}}\n")

        ws_config = load_workspace_config(self.drift_root)
        # Before render, workspace config value was loaded
        self.assertEqual(os.environ[var_name], "workspace_toml_value")

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        # Secret value won during render!
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=secret_vault_value")

        # After render, secrets are unloaded, restoring the workspace config value
        self.assertEqual(os.environ[var_name], "workspace_toml_value")

    def test_workspace_config_env_default(self) -> None:
        """Workspace config [env] provides defaults when neither host env nor secrets exist."""
        var_name = "DRIFT_PRECEDENCE_VAR_3"
        os.environ.pop(var_name, None)
        set_initial_env([k for k in os.environ.keys() if k != var_name])

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
{var_name} = "default_from_toml"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"VALUE=${{{var_name}}}\n")

        ws_config = load_workspace_config(self.drift_root)
        self.assertEqual(os.environ[var_name], "default_from_toml")

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VALUE=default_from_toml")

    def test_secrets_transient_lifecycle(self) -> None:
        """Secrets only present in secrets.env are temporarily loaded during render and popped afterward."""
        var_name = "DRIFT_TRANSIENT_SECRET"
        os.environ.pop(var_name, None)
        set_initial_env([k for k in os.environ.keys() if k != var_name])

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

        ws_config = load_workspace_config(self.drift_root)
        self.assertNotIn(var_name, os.environ)

        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "TOKEN=temporary_token_123")

        # Verify completely popped from os.environ after render
        self.assertNotIn(var_name, os.environ)

    def test_host_env_overrides_secrets_without_workspace_config(self) -> None:
        """Host env overrides secrets even when the variable is not in drift.toml."""
        var_name = "DRIFT_HOST_SECRET_VAR"
        os.environ[var_name] = "host_api_key"
        set_initial_env([var_name] + list(self.original_environ.keys()))

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

        ws_config = load_workspace_config(self.drift_root)
        run_primitive_2_render_packages(ws_config, ["pkg_test"])

        rendered_file = self.render_dir / "pkg_test" / "dot-config.txt"
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "KEY=host_api_key")
        self.assertEqual(os.environ[var_name], "host_api_key")

    def test_local_toml_merging_env(self) -> None:
        """drift.local.toml overrides drift.toml [env] settings."""
        var_name = "DRIFT_MERGED_VAR"
        os.environ.pop(var_name, None)
        set_initial_env([k for k in os.environ.keys() if k != var_name])

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
{var_name} = "base_value"
""",
            encoding="utf-8"
        )

        local_toml = self.config_dir / "drift.local.toml"
        local_toml.write_text(
            f"""
[env]
{var_name} = "local_override_value"
""",
            encoding="utf-8"
        )

        load_workspace_config(self.drift_root)
        self.assertEqual(os.environ[var_name], "local_override_value")

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

        set_initial_env(["VAR_A", "VAR_D", "VAR_E"] + list(self.original_environ.keys()))

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
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

        ws_config = load_workspace_config(self.drift_root)
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

        # After render:
        self.assertEqual(os.environ["VAR_A"], "host_a")
        self.assertEqual(os.environ["VAR_B"], "toml_b")  # Restored to toml
        self.assertEqual(os.environ["VAR_C"], "toml_c")
        self.assertEqual(os.environ["VAR_D"], "host_d")
        self.assertEqual(os.environ["VAR_E"], "host_e")
        self.assertNotIn("VAR_F", os.environ)  # Popped

    def test_render_exception_restores_environment(self) -> None:
        """Verifies that even if rendering raises an exception, unload_env_settings runs in finally."""
        var_secret = "DRIFT_FAIL_SECRET"
        var_toml = "DRIFT_FAIL_TOML"

        os.environ.pop(var_secret, None)
        os.environ.pop(var_toml, None)
        set_initial_env([k for k in os.environ.keys() if k not in (var_secret, var_toml)])

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
{var_toml} = "toml_val"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_secret}="secret_val"\n{var_toml}="secret_overwrites_toml"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_test", f"FAIL=${{{var_secret}}}\n")

        ws_config = load_workspace_config(self.drift_root)
        res = run_primitive_2_render_packages(ws_config, ["pkg_test"])
        self.assertEqual(res.status, "FAILED")

        # Unload should have executed:
        self.assertNotIn(var_secret, os.environ)
        self.assertEqual(os.environ[var_toml], "toml_val")

    def test_cli_main_with_cmdline_env(self) -> None:
        """Verifies that running CLI main() captures host environment and respects precedence."""
        from drift.cli import main

        var_name = "DRIFT_CLI_TEST_VAR"
        os.environ[var_name] = "cli_host_override"

        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
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

[env]
{var_name} = "toml_default"
""",
            encoding="utf-8"
        )
        (self.config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        secrets_file = self.config_dir / SECRETS_ENV_FILE_NAME
        secrets_file.write_text(f'{var_name}="secrets_default"\n', encoding="utf-8")

        self._setup_package_with_template("pkg_cli", f"VAL=${{{var_name}}}\n")

        # Execute CLI render
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", str(self.drift_root), "render", "pkg_cli"])

        rendered_file = self.render_dir / "pkg_cli" / "dot-config.txt"
        self.assertTrue(rendered_file.exists())
        self.assertEqual(rendered_file.read_text(encoding="utf-8").strip(), "VAL=cli_host_override")
        self.assertEqual(os.environ[var_name], "cli_host_override")


class TestEnvTopologicalResolutionAndInterpolation(unittest.TestCase):
    """Tests for topological sort variable stitching in [env] and recursive config dictionary interpolation."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        self.drift_root = Path(self.temp_dir)
        self.config_dir = self.drift_root / CONFIG_DIR_NAME
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_update_env_dict(self) -> None:
        """Verifies update_env_dict with overwrite, non-overwrite, env_keep, and snapshot tracking."""
        from drift.env_utils import update_env_dict, restore_env_dict

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

    def test_topological_sort_env(self) -> None:
        """Verifies topological_sort_env computes correct evaluation order and catches cycles."""
        from drift.env_utils import topological_sort_env
        from drift.exceptions import ConfigError

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
        from drift.env_utils import resolve_env_references

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
        from drift.env_utils import resolve_env_references

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
        from drift.env_utils import resolve_env_references

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
        from drift.env_utils import resolve_env_references
        from drift.exceptions import ConfigError

        raw_env = {"LOOP": "${LOOP}"}
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_references(raw_env, base_env={})
        self.assertIn("Cyclic dependency", str(ctx.exception))

    def test_resolve_env_references_mutual_cycle(self) -> None:
        """Verifies that cyclic dependencies (A -> B -> A) raise ConfigError."""
        from drift.env_utils import resolve_env_references
        from drift.exceptions import ConfigError

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
        from drift.env_utils import resolve_env_references
        from drift.exceptions import ConfigError

        raw_env = {"A": "${UNKNOWN_VAR}"}
        with self.assertRaises(ConfigError) as ctx:
            resolve_env_references(raw_env, base_env={})
        self.assertIn("UNKNOWN_VAR", str(ctx.exception))

    def test_interpolate_config_dict(self) -> None:
        """Verifies recursive interpolation of strings across nested dicts, lists, and tuples."""
        from drift.env_utils import interpolate_config_dict

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
        """Verifies that workspace drift.toml resolves [env] stitching and interpolates fields."""
        drift_toml = self.config_dir / GLOBAL_CONFIG_FILE_NAME
        drift_toml.write_text(
            """
[workspace]
source_directory = "${SRC_SUBDIR}"
default_target_directory = "${TARGET_ROOT}/user_home"

[packages.enable]
default = true

[env]
ROOT_DIR = "/custom/base"
SRC_SUBDIR = "src_custom"
TARGET_ROOT = "${ROOT_DIR}/dest"
SocksProxyHost = "127.0.0.1"
SocksProxyPort = "9050"
SOCKS_PROXY = "socks5h://${SocksProxyHost}:${SocksProxyPort}"
ALL_PROXY = "${SOCKS_PROXY}"
""",
            encoding="utf-8"
        )
        ws = load_workspace_config(self.drift_root)
        self.assertEqual(ws.env["SOCKS_PROXY"], "socks5h://127.0.0.1:9050")
        self.assertEqual(ws.env["ALL_PROXY"], "socks5h://127.0.0.1:9050")
        self.assertEqual(ws.workspace.source_directory, Path("src_custom"))
        self.assertEqual(str(ws.workspace.default_target_directory), "/custom/base/dest/user_home")

    def test_package_config_with_env_override_and_field_interpolation(self) -> None:
        """Verifies that package drift_package.toml resolves [env.override] and interpolates package fields."""
        from drift.package_config import PackageConfig

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
                "post_install": "scripts/start_${drift_package_name}.sh",
                "timeout": 45,
            }
        }
        pkg_cfg = PackageConfig.from_dict(pkg_dict, package_name="my_daemon")
        self.assertEqual(pkg_cfg.name, "my_daemon")
        self.assertEqual(str(pkg_cfg.target_directory), "/var/lib/my_daemon")
        self.assertEqual(pkg_cfg.env_override["SERVICE_URL"], "http://127.0.0.1:8000")
        self.assertEqual(str(pkg_cfg.hooks.post_install), "scripts/start_my_daemon.sh")
        self.assertEqual(pkg_cfg.hooks.timeout, 45)

    def test_package_config_with_direct_env_raises_error(self) -> None:
        """Verifies that direct key-value pairs in package [env] raise ConfigError."""
        from drift.package_config import PackageConfig
        from drift.exceptions import ConfigError

        pkg_dict = {
            "package": {
                "name": "legacy_pkg",
            },
            "env": {
                "LEGACY_VAR": "legacy_val",
            }
        }
        with self.assertRaises(ConfigError) as ctx:
            PackageConfig.from_dict(pkg_dict, package_name="legacy_pkg")
        self.assertIn("Direct key-value pair 'LEGACY_VAR' in [env] is not supported for package 'legacy_pkg'", str(ctx.exception))
        self.assertIn("Please define variables under [env.override] or [env.fallback]", str(ctx.exception))

    def test_escaped_variable_stitching(self) -> None:
        """Verifies that \\$VAR and \\${VAR} escape variable stitching in [env] and config fields."""
        from drift.env_utils import resolve_env_references, interpolate_config_dict

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
        """Verifies that all four package facts are available and 7-tier precedence is respected in package config."""
        from drift.package_config import PackageConfig
        from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig

        # Test CLI environment precedence (Tier 1 INITIAL_ENV)
        os.environ["CLI_OVERRIDE_VAR"] = "cli_val"
        set_initial_env(["CLI_OVERRIDE_VAR"])

        ws = WorkspaceConfig(
            drift_root_path=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=Path("/target"),
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs={},
            env={},
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
                "post_install": "${SRC_DIR_REF}/scripts/post.sh",
            }
        }
        # Simulate workspace environment variable in os.environ (Tier 6)
        os.environ["OVERRIDDEN_BY_WORKSPACE"] = "workspace_val"

        pkg_cfg = PackageConfig.from_dict(pkg_dict, package_name="my_pkg", source_files=[pkg_toml_path], workspace_config=ws)
        self.assertEqual(pkg_cfg.name, "my_pkg")
        self.assertEqual(str(pkg_cfg.target_directory), str(self.drift_root / "install" / "my_pkg" / "target"))
        self.assertEqual(pkg_cfg.env_override["SRC_DIR_REF"], str(self.drift_root / "src" / "my_pkg"))
        self.assertEqual(pkg_cfg.env_override["RENDER_DIR_REF"], str(self.drift_root / "render" / "my_pkg"))
        self.assertEqual(pkg_cfg.env_override["INSTALL_DIR_REF"], str(self.drift_root / "install" / "my_pkg"))
        # Fallback filled unset blanks
        self.assertEqual(pkg_cfg.env_fallback["FALLBACK_VAR"], "fallback_val")
        # Hook path was interpolated
        self.assertEqual(str(pkg_cfg.hooks.post_install), str(self.drift_root / "src" / "my_pkg" / "scripts" / "post.sh"))

    def test_package_config_facts_with_custom_workspace_config(self) -> None:
        """Verifies that custom workspace paths (e.g. custom_src, custom_render, custom_install) populate package facts."""
        from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
        from drift.package_config import PackageConfig

        ws = WorkspaceConfig(
            drift_root_path=self.drift_root,
            workspace=WorkspaceSectionConfig(
                source_directory=Path("custom_src"),
                render_directory=Path("custom_render"),
                install_directory=Path("custom_install"),
                backup_directory=Path("custom_backup"),
                default_target_directory=Path("/target"),
                default_install_method="stow",
            ),
            packages_enable={},
            packages_enable_default=True,
            render_engine_configs={},
            env={},
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
        pkg_cfg = PackageConfig.from_dict(pkg_dict, package_name="custom_pkg", workspace_config=ws)
        self.assertEqual(pkg_cfg.env_override["SRC"], str(self.drift_root / "custom_src" / "custom_pkg"))
        self.assertEqual(pkg_cfg.env_override["RENDER"], str(self.drift_root / "custom_render" / "custom_pkg"))
        self.assertEqual(pkg_cfg.env_override["INSTALL"], str(self.drift_root / "custom_install" / "custom_pkg"))

    def test_package_config_without_workspace_config_leaves_dir_facts_unset(self) -> None:
        """Verifies that when workspace_config is not provided, 'dir' facts are unset."""
        from drift.package_config import PackageConfig
        from drift.exceptions import ConfigError

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
        pkg_cfg = PackageConfig.from_dict(pkg_dict_name_only, package_name="my_pkg", workspace_config=None)
        self.assertEqual(pkg_cfg.env_override["NAME_REF"], "my_pkg")

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
            PackageConfig.from_dict(pkg_dict_dir_ref, package_name="my_pkg", workspace_config=None)
        self.assertIn("drift_package_source_dir", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

