"""Tests for workspace health checks, 3-value logic, and drift repair."""

import os
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT,
    set_test_mode,
)
from drift.workspace_check import (
    ComponentStatus,
    CheckResult,
    WorkspaceHealthReport,
    probe_existing_workspace_structure,
    check_workspace_config,
    check_state_registry,
    check_render_repo,
    check_install_repo,
    check_root_gitignore,
    check_render_gitignore,
    check_install_gitignore,
    check_install_stow_ignore,
    check_core_dirs,
    check_engine_inputs,
    check_existing_workspace_status,
)
from drift.workspace_init import (
    init_drift_workspace,
)
from drift.exceptions import ConfigError
from drift.workspace_repair import (
    repair_drift_workspace,
    repair_workspace_config,
)
from drift.workspace_config import (
    WorkspaceConfig,
    load_workspace_config,
)
from drift.cli import main


class TestCheckRepoModular(unittest.TestCase):
    """Tests for granular 3-value component checks in workspace_check.py."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fresh_directory_status_is_not_found(self) -> None:
        """A completely fresh directory must return NOT_FOUND across all checks and overall."""
        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.NOT_FOUND)
        self.assertTrue(report.is_fresh())
        self.assertFalse(report.is_healthy())
        self.assertFalse(report.is_broken())
        self.assertFalse(bool(report))

    def test_fresh_repo_with_only_root_git_is_not_found(self) -> None:
        """A fresh directory with only a root .git repo must still return NOT_FOUND."""
        from drift.git_utils import git_init_repo
        git_init_repo(self.drift_root, "main")
        self.assertFalse(probe_existing_workspace_structure(self.drift_root))

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.NOT_FOUND)
        self.assertTrue(report.is_fresh())

    def test_missing_workspace_config_with_existing_artifacts_is_broken(self) -> None:
        """If workspace artifacts exist but drift_workspace.toml is missing, status is BROKEN."""
        (self.drift_root / "src").mkdir(parents=True)
        self.assertTrue(probe_existing_workspace_structure(self.drift_root))

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)
        self.assertTrue(report.is_broken())
        self.assertEqual(len(report.checks), 1)
        self.assertEqual(report.checks[0].name, "Workspace Configuration")
        self.assertEqual(report.checks[0].status, ComponentStatus.NOT_FOUND)

    def test_fully_initialized_workspace_is_good(self) -> None:
        """A properly initialized workspace must report GOOD across all checks and overall."""
        init_drift_workspace(self.drift_root)
        report = check_existing_workspace_status(self.drift_root)

        self.assertEqual(report.overall_status, ComponentStatus.GOOD)
        self.assertTrue(report.is_healthy())
        self.assertFalse(report.is_fresh())
        self.assertFalse(report.is_broken())
        self.assertTrue(bool(report))

        for check in report.checks:
            self.assertEqual(check.status, ComponentStatus.GOOD, f"Check '{check.name}' was not GOOD: {check.details}")

    def test_missing_render_git_repo_is_broken(self) -> None:
        """If render/ exists but is missing its .git repository, status is BROKEN."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render" / ".git")
        ws_config = load_workspace_config(self.drift_root)

        res = check_render_repo(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)
        self.assertTrue(report.is_broken())

    def test_missing_install_git_repo_is_broken(self) -> None:
        """If install/ exists but is missing its .git repository, status is BROKEN."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "install" / ".git")
        ws_config = load_workspace_config(self.drift_root)

        res = check_install_repo(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_missing_state_toml_is_not_found_for_check_and_broken_overall(self) -> None:
        """If install/state.toml is deleted after init, status is BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "install" / "state.toml").unlink()
        ws_config = load_workspace_config(self.drift_root)

        res = check_state_registry(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.NOT_FOUND)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_corrupt_drift_workspace_toml_is_broken(self) -> None:
        """Invalid syntax in drift_workspace.toml must report BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift_workspace.toml").write_text("invalid_toml = [ {", encoding="utf-8")

        res = check_workspace_config(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)
        self.assertTrue(report.is_broken())
        self.assertEqual(len(report.checks), 1)
        self.assertEqual(report.checks[0].name, "Workspace Configuration")

    def test_missing_gitignore_rules_is_broken(self) -> None:
        """If .gitignore is missing mandatory ignore lines, it reports BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / ".gitignore").write_text("# empty\n", encoding="utf-8")
        ws_config = load_workspace_config(self.drift_root)

        res = check_root_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_file_blocking_directory_is_broken(self) -> None:
        """If render or install exists as a regular file, it reports BROKEN."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render")
        (self.drift_root / "render").write_text("i am a file", encoding="utf-8")
        ws_config = load_workspace_config(self.drift_root)

        res = check_render_repo(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

    def test_missing_engine_inputs_is_broken(self) -> None:
        """If drift_workspace.toml declares an engine input file that is missing on disk, reports BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "envsubst.bash").unlink()
        ws_config = load_workspace_config(self.drift_root)

        res = check_engine_inputs(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_all_engine_inputs_missing_is_not_found(self) -> None:
        """If all declared engine input files are missing on disk, reports NOT_FOUND for that check."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "envsubst.bash").unlink()
        (self.drift_root / "config" / "mustache.envst.json").unlink()
        (self.drift_root / "config" / "jinja2.mustache.json").unlink()
        ws_config = load_workspace_config(self.drift_root)

        res = check_engine_inputs(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.NOT_FOUND)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)


class TestWorkspaceRepair(unittest.TestCase):
    """Tests for repair_drift_workspace and drift repair CLI."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repair_on_healthy_workspace_is_noop(self) -> None:
        """Running repair on a fully healthy workspace performs no actions."""
        init_drift_workspace(self.drift_root)
        actions = repair_drift_workspace(self.drift_root)
        self.assertEqual(len(actions), 0)

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_dry_run_does_not_modify_disk(self) -> None:
        """dry_run=True returns planned actions without applying changes."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render" / ".git")

        actions = repair_drift_workspace(self.drift_root, dry_run=True)
        self.assertTrue(any("render" in a for a in actions))

        # Still broken after dry run
        self.assertFalse((self.drift_root / "render" / ".git").exists())

    def test_repair_recovers_missing_render_and_install_git_repos(self) -> None:
        """Repair reinitializes .git in render and install subdirectories."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render" / ".git")
        shutil.rmtree(self.drift_root / "install" / ".git")

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("render" in a for a in actions))
        self.assertTrue(any("install" in a for a in actions))

        self.assertTrue((self.drift_root / "render" / ".git").is_dir())
        self.assertTrue((self.drift_root / "install" / ".git").is_dir())

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_recovers_missing_state_toml_and_stow_ignore(self) -> None:
        """Repair restores install/state.toml and install/.stow-local-ignore."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "install" / "state.toml").unlink()
        (self.drift_root / "install" / ".stow-local-ignore").unlink()

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("state.toml" in a for a in actions))
        self.assertTrue(any(".stow-local-ignore" in a for a in actions))

        self.assertTrue((self.drift_root / "install" / "state.toml").is_file())
        self.assertTrue((self.drift_root / "install" / ".stow-local-ignore").is_file())

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_recovers_missing_gitignore_rules(self) -> None:
        """Repair appends missing isolation rules to .gitignore."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / ".gitignore").write_text("# only comments\n", encoding="utf-8")

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any(".gitignore" in a for a in actions))

        content = (self.drift_root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("render/", content)
        self.assertIn("install/", content)
        self.assertIn("*.local.toml", content)
        self.assertIn("config/secrets.env", content)
        self.assertIn("__pycache__/", content)
        self.assertIn(".venv/", content)

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_updates_partial_root_gitignore_with_python_rules(self) -> None:
        """Repair updates an existing .gitignore that has base isolation but lacks Python rules."""
        init_drift_workspace(self.drift_root)
        # Write only the original 4 rules without Python rules
        (self.drift_root / ".gitignore").write_text(
            "render/\ninstall/\n*.local.toml\nconfig/secrets.env\n",
            encoding="utf-8"
        )
        ws_config = load_workspace_config(self.drift_root)
        res = check_root_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any(".gitignore" in a for a in actions))

        content = (self.drift_root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", content)
        self.assertIn("*.py[cod]", content)
        self.assertIn(".venv/", content)

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_recovers_missing_internal_gitignores(self) -> None:
        """Repair restores missing render/.gitignore and install/.gitignore."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "render" / ".gitignore").unlink()
        (self.drift_root / "install" / ".gitignore").unlink()
        ws_config = load_workspace_config(self.drift_root)

        res_render = check_render_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res_render.status, ComponentStatus.NOT_FOUND)
        res_install = check_install_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res_install.status, ComponentStatus.NOT_FOUND)

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("render/.gitignore" in a for a in actions))
        self.assertTrue(any("install/.gitignore" in a for a in actions))

        self.assertTrue((self.drift_root / "render" / ".gitignore").is_file())
        self.assertTrue((self.drift_root / "install" / ".gitignore").is_file())

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_updates_partial_internal_gitignores(self) -> None:
        """Repair updates existing internal gitignores missing Python rules."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "render" / ".gitignore").write_text("*.swp\n.DS_Store\n", encoding="utf-8")
        (self.drift_root / "install" / ".gitignore").write_text("*.swp\n.DS_Store\n", encoding="utf-8")
        ws_config = load_workspace_config(self.drift_root)

        res_render = check_render_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res_render.status, ComponentStatus.BROKEN)
        res_install = check_install_gitignore(self.drift_root, workspace_config=ws_config)
        self.assertEqual(res_install.status, ComponentStatus.BROKEN)

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("render/.gitignore" in a for a in actions))
        self.assertTrue(any("install/.gitignore" in a for a in actions))

        render_gi = (self.drift_root / "render" / ".gitignore").read_text(encoding="utf-8")
        install_gi = (self.drift_root / "install" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", render_gi)
        self.assertIn("*.py[cod]", render_gi)
        self.assertIn("__pycache__/", install_gi)
        self.assertIn("*.py[cod]", install_gi)

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_recovers_missing_engine_input_templates(self) -> None:
        """Repair creates missing engine input templates."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "envsubst.bash").unlink()
        (self.drift_root / "config" / "mustache.envst.json").unlink()

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("envsubst.bash" in a for a in actions))
        self.assertTrue(any("mustache.envst.json" in a for a in actions))

        self.assertTrue((self.drift_root / "config" / "envsubst.bash").is_file())
        self.assertTrue((self.drift_root / "config" / "mustache.envst.json").is_file())

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_recovers_missing_drift_workspace_local_toml(self) -> None:
        """Repair creates missing config/drift_workspace.local.toml template."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift_workspace.local.toml").unlink()

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("drift_workspace.local.toml" in a for a in actions))
        self.assertTrue((self.drift_root / "config" / "drift_workspace.local.toml").is_file())

    def test_repair_recovers_missing_secrets_env(self) -> None:
        """Repair creates missing config/secrets.env template and ensures it is gitignored."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "secrets.env").unlink()

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("secrets.env" in a for a in actions))
        self.assertTrue((self.drift_root / "config" / "secrets.env").is_file())

        res = subprocess.run(["git", "check-ignore", "config/secrets.env"], cwd=str(self.drift_root), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("config/secrets.env", res.stdout.strip())

    def test_cli_repair_command_executes_cleanly(self) -> None:
        """Verifies that 'drift repair' CLI command runs and heals damaged workspaces."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render" / ".git")

        # Run CLI repair
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", str(self.drift_root), "repair"])

        self.assertTrue((self.drift_root / "render" / ".git").is_dir())
        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_init_refuses_partial_workspace_and_hints_repair(self) -> None:
        """drift init on a damaged workspace refuses to run and recommends 'drift repair'."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "render" / ".git")

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)

        err_msg = str(cm.exception)
        self.assertIn("drift repair", err_msg)
        self.assertIn("--force", err_msg)


    def test_repair_render_and_install_existing_broken_git_prints_error(self) -> None:
        """If render/.git exists but is a bare repo, repair does not re-init and warns the user."""
        init_drift_workspace(self.drift_root)
        # Set core.bare = true in render/.git/config
        subprocess.run(["git", "-C", str(self.drift_root / "render"), "config", "core.bare", "true"], check=True)

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("bare Git repository" in a or "Manual resolution required" in a for a in actions))

    def test_repair_custom_engine_input_warns_user(self) -> None:
        """If user has custom engine input in drift_workspace.toml that is missing, repair warns user."""
        init_drift_workspace(self.drift_root)
        # Append a custom engine to drift_workspace.toml
        config_path = self.drift_root / "config" / "drift_workspace.toml"
        content = config_path.read_text(encoding="utf-8")
        content += "\n[render.custom]\ninput_file = \"custom_input.txt\"\nsuffix = \"custom\"\nrender_command = \"cat {input} {src} > {dest}\"\n"
        config_path.write_text(content, encoding="utf-8")

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("custom_input.txt" in a for a in actions))

    def test_legacy_drift_toml_detection_raises_error(self) -> None:
        """Verifies that load_workspace_config raises ConfigError and check_workspace_config returns BROKEN when legacy file exists."""
        init_drift_workspace(self.drift_root)
        # Create legacy drift.toml in config/
        legacy_file = self.drift_root / "config" / "drift.toml"
        legacy_file.write_text("[workspace]\n", encoding="utf-8")

        from drift.exceptions import ConfigError
        from drift.workspace_config import load_workspace_config

        # 1. load_workspace_config must fail fast on legacy file
        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            with self.assertRaises(ConfigError) as ctx:
                load_workspace_config(self.drift_root)
        self.assertIn("Legacy workspace configuration file [drift.toml] is no longer supported", str(ctx.exception))
        self.assertIn("drift repair", str(ctx.exception))

        # 2. check_workspace_config reports BROKEN because legacy drift.toml is present
        res = check_workspace_config(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)
        self.assertIn("Legacy configuration file 'config/drift.toml' detected", res.details)

        # 3. If drift_workspace.toml is missing, check_workspace_config still reports BROKEN
        (self.drift_root / "config" / "drift_workspace.toml").unlink()
        res_missing = check_workspace_config(self.drift_root)
        self.assertEqual(res_missing.status, ComponentStatus.BROKEN)

    def test_repair_renames_legacy_drift_toml_and_local_toml(self) -> None:
        """Verifies that repair automatically renames legacy config files to drift_workspace.* when target does not exist."""
        init_drift_workspace(self.drift_root)
        ws_file = self.drift_root / "config" / "drift_workspace.toml"
        local_file = self.drift_root / "config" / "drift_workspace.local.toml"

        legacy_main = self.drift_root / "config" / "drift.toml"
        legacy_local = self.drift_root / "config" / "drift.local.toml"

        ws_file.rename(legacy_main)
        local_file.rename(legacy_local)

        self.assertTrue(legacy_main.is_file())
        self.assertTrue(legacy_local.is_file())
        self.assertFalse(ws_file.exists())
        self.assertFalse(local_file.exists())

        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("Renamed legacy workspace configuration file 'config/drift.toml' to 'config/drift_workspace.toml'" in a for a in actions))
        self.assertTrue(any("Renamed legacy workspace configuration file 'config/drift.local.toml' to 'config/drift_workspace.local.toml'" in a for a in actions))

        self.assertFalse(legacy_main.exists())
        self.assertFalse(legacy_local.exists())
        self.assertTrue(ws_file.is_file())
        self.assertTrue(local_file.is_file())

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_raises_config_error_when_target_already_exists(self) -> None:
        """Verifies that repair refuses to overwrite existing config files and raises ConfigError for user inspection."""
        init_drift_workspace(self.drift_root)
        local_file = self.drift_root / "config" / "drift_workspace.local.toml"
        self.assertTrue(local_file.is_file())

        legacy_local = self.drift_root / "config" / "drift.local.toml"
        legacy_local.write_text('[env]\nLEGACY_KEY = "legacy_value"\n', encoding="utf-8")

        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            with self.assertRaises(ConfigError) as ctx:
                repair_drift_workspace(self.drift_root)

        self.assertIn("Cannot migrate legacy file 'config/drift.local.toml'", str(ctx.exception))
        self.assertIn("Target configuration file 'config/drift_workspace.local.toml' already exists", str(ctx.exception))
        self.assertIn("Please manually inspect, merge, and remove 'config/drift.local.toml'", str(ctx.exception))
        self.assertTrue(legacy_local.is_file())
        self.assertTrue(local_file.is_file())

    def test_repair_when_drift_workspace_toml_and_drift_local_toml_both_exist_without_workspace_local(self) -> None:
        """Verifies that when drift_workspace.toml and drift.local.toml both exist (no drift_workspace.local.toml), repair renames drift.local.toml."""
        init_drift_workspace(self.drift_root)
        # Delete the default drift_workspace.local.toml
        (self.drift_root / "config" / "drift_workspace.local.toml").unlink()

        # Create legacy drift.local.toml
        legacy_local = self.drift_root / "config" / "drift.local.toml"
        legacy_local.write_text('[env]\nCUSTOM_OVERRIDE = "active"\n', encoding="utf-8")

        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            actions = repair_drift_workspace(self.drift_root)

        self.assertTrue(any("Renamed legacy workspace configuration file 'config/drift.local.toml' to 'config/drift_workspace.local.toml'" in a for a in actions))
        self.assertFalse(legacy_local.exists())
        new_local = self.drift_root / "config" / "drift_workspace.local.toml"
        self.assertTrue(new_local.is_file())
        self.assertIn("CUSTOM_OVERRIDE", new_local.read_text(encoding="utf-8"))

        # Verify load_workspace_config works seamlessly without deprecation error
        ws_config = load_workspace_config(self.drift_root)
        self.assertEqual(ws_config.env.get("CUSTOM_OVERRIDE"), "active")

        report = check_existing_workspace_status(self.drift_root)
        self.assertTrue(report.is_healthy())

    def test_repair_dry_run_plans_legacy_rename_without_modifying(self) -> None:
        """Verifies that dry-run repair lists legacy file renames without modifying disk."""
        init_drift_workspace(self.drift_root)
        ws_file = self.drift_root / "config" / "drift_workspace.toml"
        (self.drift_root / "config" / "drift_workspace.local.toml").unlink()
        legacy_main = self.drift_root / "config" / "drift.toml"
        ws_file.rename(legacy_main)

        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            actions = repair_drift_workspace(self.drift_root, dry_run=True)
        self.assertTrue(any("Renamed legacy workspace configuration file 'config/drift.toml' to 'config/drift_workspace.toml'" in a for a in actions))
        self.assertTrue(legacy_main.is_file())
        self.assertFalse(ws_file.exists())

    def test_repair_dry_run_on_uninitialized_workspace(self) -> None:
        """Verifies that dry-run repair on a directory with no config files plans actions without errors."""
        with patch("sys.stderr", StringIO()), patch("sys.stdout", StringIO()):
            actions = repair_drift_workspace(self.drift_root, dry_run=True)
        self.assertTrue(any("Generated default 'config/drift_workspace.toml'" in a for a in actions))
        self.assertFalse((self.drift_root / "config" / "drift_workspace.toml").exists())

    def test_repair_workspace_config_broken_fails_fast(self) -> None:
        """When workspace config has invalid syntax, repair_drift_workspace raises ConfigError immediately."""
        init_drift_workspace(self.drift_root)
        config_path = self.drift_root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME
        config_path.write_text("invalid_syntax = [ {", encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            repair_drift_workspace(self.drift_root)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_repair_workspace_config_helper_returns_tuple(self) -> None:
        """repair_workspace_config returns (actions, WorkspaceConfig) tuple."""
        init_drift_workspace(self.drift_root)
        actions, ws_config = repair_workspace_config(self.drift_root)
        self.assertEqual(len(actions), 0)
        self.assertEqual(ws_config.drift_root, self.drift_root)

    def test_repair_with_custom_workspace_paths(self) -> None:
        """repair_drift_workspace consumes custom paths from workspace_config correctly."""
        (self.drift_root / CONFIG_DIR_NAME).mkdir(parents=True)
        config_path = self.drift_root / CONFIG_DIR_NAME / WORKSPACE_CONFIG_FILE_NAME
        config_path.write_text("""
[workspace]
render_directory = "custom_render"
install_directory = "custom_install"
source_directory = "custom_src"

[packages.enable]
DEFAULT = true
""", encoding="utf-8")

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("custom_render" in a for a in actions))
        self.assertTrue(any("custom_install" in a for a in actions))
        self.assertTrue(any("custom_src" in a for a in actions))

        self.assertTrue((self.drift_root / "custom_render" / ".git").is_dir())
        self.assertTrue((self.drift_root / "custom_install" / ".git").is_dir())
        self.assertTrue((self.drift_root / "custom_src").is_dir())


if __name__ == "__main__":
    unittest.main()

