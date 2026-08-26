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
from drift.workspace_config import (
    WorkspaceConfig,
    load_workspace_config,
    load_env_settings,
    unload_env_settings,
    parse_secrets_env,
)
from drift.render_package import (
    render_package,
    run_primitive_2_render_packages,
)


class TestLoadEnvSettingsUnit(unittest.TestCase):
    """Unit tests for load_env_settings and unload_env_settings."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.original_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_load_env_settings_empty(self) -> None:
        """Verifies that loading empty envs returns None and modifies nothing."""
        result = load_env_settings([])
        self.assertIsNone(result)

    def test_load_env_settings_overwrite_true(self) -> None:
        """Verifies that overwrite=True (default) updates existing variables and tracks original values."""
        os.environ["TEST_EXISTING"] = "old_value"
        os.environ.pop("TEST_NEW", None)

        envs = [("TEST_EXISTING", "new_value"), ("TEST_NEW", "created_value")]
        saved_envs = load_env_settings(envs, overwrite=True)

        self.assertIsNotNone(saved_envs)
        self.assertEqual(os.environ["TEST_EXISTING"], "new_value")
        self.assertEqual(os.environ["TEST_NEW"], "created_value")

        # Check saved values
        saved_dict = dict(saved_envs)
        self.assertEqual(saved_dict["TEST_EXISTING"], "old_value")
        self.assertIsNone(saved_dict["TEST_NEW"])

    def test_load_env_settings_overwrite_false(self) -> None:
        """Verifies that overwrite=False skips existing environment variables."""
        os.environ["TEST_EXISTING"] = "original_value"
        os.environ.pop("TEST_NEW", None)

        envs = [("TEST_EXISTING", "attempted_overwrite"), ("TEST_NEW", "new_val")]
        saved_envs = load_env_settings(envs, overwrite=False)

        self.assertIsNotNone(saved_envs)
        # Existing should NOT be modified
        self.assertEqual(os.environ["TEST_EXISTING"], "original_value")
        # New should be added
        self.assertEqual(os.environ["TEST_NEW"], "new_val")

        # saved_envs should only contain TEST_NEW
        saved_dict = dict(saved_envs)
        self.assertNotIn("TEST_EXISTING", saved_dict)
        self.assertIn("TEST_NEW", saved_dict)

    def test_load_env_settings_with_env_keep(self) -> None:
        """Verifies that variables in env_keep are protected from being overwritten."""
        os.environ["TEST_KEPT"] = "keep_me"
        os.environ["TEST_OVERWRITABLE"] = "old_val"

        envs = [("TEST_KEPT", "new_val_1"), ("TEST_OVERWRITABLE", "new_val_2")]
        saved_envs = load_env_settings(envs, overwrite=True, env_keep=["TEST_KEPT"])

        self.assertIsNotNone(saved_envs)
        self.assertEqual(os.environ["TEST_KEPT"], "keep_me")
        self.assertEqual(os.environ["TEST_OVERWRITABLE"], "new_val_2")

        saved_dict = dict(saved_envs)
        self.assertNotIn("TEST_KEPT", saved_dict)
        self.assertEqual(saved_dict["TEST_OVERWRITABLE"], "old_val")

    def test_load_env_settings_duplicate_keys_in_input(self) -> None:
        """Verifies that duplicate keys in env list preserve the true original value."""
        os.environ["TEST_DUP"] = "initial"
        envs = [("TEST_DUP", "first_change"), ("TEST_DUP", "second_change")]
        saved_envs = load_env_settings(envs, overwrite=True)

        self.assertIsNotNone(saved_envs)
        self.assertEqual(os.environ["TEST_DUP"], "second_change")
        self.assertEqual(len(saved_envs), 1)
        self.assertEqual(saved_envs[0], ("TEST_DUP", "initial"))

        unload_env_settings(saved_envs)
        self.assertEqual(os.environ["TEST_DUP"], "initial")

    def test_unload_env_settings(self) -> None:
        """Verifies that unload_env_settings cleanly restores pre-existing values and pops new ones."""
        os.environ["TEST_RESTORE"] = "before_load"
        os.environ.pop("TEST_POP", None)

        saved = load_env_settings([("TEST_RESTORE", "during_load"), ("TEST_POP", "during_load")])
        self.assertEqual(os.environ["TEST_RESTORE"], "during_load")
        self.assertEqual(os.environ["TEST_POP"], "during_load")

        unload_env_settings(saved)
        self.assertEqual(os.environ["TEST_RESTORE"], "before_load")
        self.assertNotIn("TEST_POP", os.environ)

        # Unloading None is a no-op
        unload_env_settings(None)


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


if __name__ == "__main__":
    unittest.main()

