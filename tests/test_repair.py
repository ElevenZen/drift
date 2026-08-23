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
    GLOBAL_CONFIG_FILE_NAME,
    GLOBAL_CONFIG_LOCAL_FILE_NAME,
    PACKAGE_CONFIG_FILE_NAME,
    SECRETS_ENV_FILE_NAME,
    get_default_drift_local_toml_content,
    set_test_mode,
)
from drift.check_repo import (
    ComponentStatus,
    CheckResult,
    WorkspaceHealthReport,
    check_root_git_repo,
    check_workspace_config,
    check_state_registry,
    check_render_repo,
    check_install_repo,
    check_root_gitignore,
    check_install_stow_ignore,
    check_core_dirs,
    check_engine_inputs,
    check_existing_workspace_status,
)
from drift.workspace_init import (
    init_drift_workspace,
)
from drift.workspace_repair import (
    repair_drift_workspace,
)
from drift.cli import main


class TestCheckRepoModular(unittest.TestCase):
    """Tests for granular 3-value component checks in check_repo.py."""

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

        res = check_render_repo(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)
        self.assertTrue(report.is_broken())

    def test_missing_install_git_repo_is_broken(self) -> None:
        """If install/ exists but is missing its .git repository, status is BROKEN."""
        init_drift_workspace(self.drift_root)
        shutil.rmtree(self.drift_root / "install" / ".git")

        res = check_install_repo(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_missing_state_toml_is_not_found_for_check_and_broken_overall(self) -> None:
        """If install/state.toml is deleted after init, status is BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "install" / "state.toml").unlink()

        res = check_state_registry(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.NOT_FOUND)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_corrupt_drift_toml_is_broken(self) -> None:
        """Invalid syntax in drift.toml must report BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift.toml").write_text("invalid_toml = [ {", encoding="utf-8")

        res = check_workspace_config(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_missing_gitignore_rules_is_broken(self) -> None:
        """If .gitignore is missing mandatory ignore lines, it reports BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / ".gitignore").write_text("# empty\n", encoding="utf-8")

        res = check_root_gitignore(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

        report = check_existing_workspace_status(self.drift_root)
        self.assertEqual(report.overall_status, ComponentStatus.BROKEN)

    def test_file_blocking_directory_is_broken(self) -> None:
        """If render or install exists as a regular file, it reports BROKEN."""
        (self.drift_root / "render").write_text("i am a file", encoding="utf-8")

        res = check_render_repo(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.BROKEN)

    def test_missing_engine_inputs_is_broken(self) -> None:
        """If drift.toml declares an engine input file that is missing on disk, reports BROKEN."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "envsubst.bash").unlink()

        res = check_engine_inputs(self.drift_root)
        self.assertEqual(res.status, ComponentStatus.NOT_FOUND)

        from drift.workspace_config import load_workspace_config
        workspace_config = load_workspace_config(self.drift_root)
        report = check_existing_workspace_status(self.drift_root, workspace_config=workspace_config)
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

    def test_repair_recovers_missing_drift_local_toml(self) -> None:
        """Repair creates missing config/drift.local.toml template."""
        init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift.local.toml").unlink()

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("drift.local.toml" in a for a in actions))
        self.assertTrue((self.drift_root / "config" / "drift.local.toml").is_file())

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
        """If user has custom engine input in drift.toml that is missing, repair warns user."""
        init_drift_workspace(self.drift_root)
        # Append a custom engine to drift.toml
        config_path = self.drift_root / "config" / "drift.toml"
        content = config_path.read_text(encoding="utf-8")
        content += "\n[render.custom]\ninput_file = \"custom_input.txt\"\nsuffix = \"custom\"\nrender_command = \"cat {input} {src} > {dest}\"\n"
        config_path.write_text(content, encoding="utf-8")

        actions = repair_drift_workspace(self.drift_root)
        self.assertTrue(any("custom_input.txt" in a for a in actions))


if __name__ == "__main__":
    unittest.main()
