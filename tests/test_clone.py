"""Unit and integration tests for 'drift clone' command and workspace bootstrapping engine."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.workspace_clone import (
    extract_repo_name_from_url,
    is_drift_repository,
    run_primitive_clone,
)
from drift.workspace_init import init_drift_workspace
from drift.result_models import CloneResult
from drift.cli.argparse_backend import run_argparse_cli


class TestWorkspaceClone(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()

        self.remote_base = self.base_path / "remotes"
        self.remote_base.mkdir(parents=True, exist_ok=True)

        self.dest_base = self.base_path / "dest"
        self.dest_base.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_git_repo(self, repo_path: Path) -> Path:
        """Helper to create and initialize a git repository with an initial commit."""
        repo_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Drift Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "drift@example.com"], check=True, capture_output=True)
        return repo_path

    def test_extract_repo_name_from_url(self):
        """Verifies name extraction across various Git URL formats."""
        self.assertEqual(extract_repo_name_from_url("https://github.com/user/dotfiles.git"), "dotfiles")
        self.assertEqual(extract_repo_name_from_url("https://github.com/user/my-dotfiles"), "my-dotfiles")
        self.assertEqual(extract_repo_name_from_url("git@github.com:user/configs.git"), "configs")
        self.assertEqual(extract_repo_name_from_url("git@server.internal:team/custom_repo"), "custom_repo")
        self.assertEqual(extract_repo_name_from_url("/var/repos/dotfiles.git/"), "dotfiles")
        self.assertEqual(extract_repo_name_from_url("../local/my-repo/"), "my-repo")
        self.assertEqual(extract_repo_name_from_url(""), "dotfiles")

    def test_clone_drift_workspace_and_auto_repair(self):
        """Verifies cloning a valid Drift workspace automatically triggers repair and database healing."""
        # 1. Prepare remote Drift workspace
        remote_repo = self._create_git_repo(self.remote_base / "drift_dotfiles")
        init_drift_workspace(remote_repo, no_git_root=True)

        # Create a sample package
        pkg_src = remote_repo / "src" / "nvim"
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / "drift_package.toml").write_text("[package]\nname = 'nvim'\n", encoding="utf-8")
        (pkg_src / "init.lua").write_text("-- nvim config\n", encoding="utf-8")

        # Commit everything to remote repo (render/ and install/ are gitignored)
        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote_repo), "commit", "-m", "Initial drift workspace"], check=True, capture_output=True)

        # 2. Clone to destination
        dest_path = self.dest_base / "my_cloned_drift"
        res = run_primitive_clone(
            git_url=str(remote_repo),
            target_dir=dest_path
        )

        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_drift_workspace)
        self.assertEqual(res.target_directory, str(dest_path))
        self.assertIsNone(res.converted_legacy_package)

        # Verify repaired components
        self.assertTrue((dest_path / "render" / ".git").exists())
        self.assertTrue((dest_path / "install" / ".git").exists())
        self.assertTrue((dest_path / "install" / "state.toml").exists())
        self.assertTrue((dest_path / "install" / ".stow-local-ignore").exists())
        self.assertTrue((dest_path / "config" / "drift.local.toml").exists())
        self.assertTrue((dest_path / "config" / "secrets.env").exists())
        self.assertTrue((dest_path / ".gitignore").exists())

        # Verify next steps contain instructions
        self.assertGreater(len(res.recommended_next_steps), 0)
        self.assertIn("drift.local.toml", " ".join(res.recommended_next_steps))
        self.assertIn("secrets.env", " ".join(res.recommended_next_steps))
        self.assertEqual(res.recommended_next_command, f"cd {dest_path.name} && drift deploy")

        # Verify formatted output
        text = res.format_text()
        self.assertIn("Detected Drift workspace", text)
        self.assertIn("Workspace successfully cloned and prepared!", text)

    def test_clone_legacy_plain_dotfiles_and_auto_conversion(self):
        """Verifies cloning a legacy plain dotfiles repo converts it into a valid Drift package."""
        # 1. Prepare remote plain dotfiles repo
        remote_repo = self._create_git_repo(self.remote_base / "legacy_dotfiles")
        (remote_repo / ".bashrc").write_text("export FOO=bar\n", encoding="utf-8")
        (remote_repo / ".tmux.conf").write_text("set -g mouse on\n", encoding="utf-8")
        (remote_repo / "README.md").write_text("# My Legacy Dotfiles\n", encoding="utf-8")
        (remote_repo / "setup.sh").write_text("#!/bin/sh\necho setup\n", encoding="utf-8")
        config_nvim = remote_repo / ".config" / "nvim"
        config_nvim.mkdir(parents=True, exist_ok=True)
        (config_nvim / "init.lua").write_text("vim.opt.number = true\n", encoding="utf-8")

        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote_repo), "commit", "-m", "Legacy dotfiles"], check=True, capture_output=True)

        # 2. Clone to destination
        dest_path = self.dest_base / "legacy_dotfiles"
        res = run_primitive_clone(
            git_url=str(remote_repo),
            target_dir=dest_path
        )

        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse(res.is_drift_workspace)
        self.assertEqual(res.converted_legacy_package, "legacy_dotfiles")

        # Verify Drift structure
        self.assertTrue((dest_path / "config" / "drift.toml").exists())
        self.assertTrue((dest_path / "render" / ".git").exists())
        self.assertTrue((dest_path / "install" / ".git").exists())

        # Verify migrated package
        pkg_dir = dest_path / "src" / "legacy_dotfiles"
        self.assertTrue(pkg_dir.exists())
        self.assertTrue((pkg_dir / ".bashrc").exists())
        self.assertTrue((pkg_dir / ".tmux.conf").exists())
        self.assertTrue((pkg_dir / ".config" / "nvim" / "init.lua").exists())
        self.assertTrue((pkg_dir / "drift_package.toml").exists())
        self.assertTrue((pkg_dir / ".drift_ignore").exists())

        # Verify package is enabled in config/drift.toml
        config_content = (dest_path / "config" / "drift.toml").read_text(encoding="utf-8")
        self.assertIn("legacy_dotfiles = true", config_content)

        # Verify formatted output
        text = res.format_text()
        self.assertIn("Detected plain dotfiles repository", text)
        self.assertIn("Converted repository into a Drift workspace!", text)

    def test_clone_to_existing_non_empty_dir_fails(self):
        """Verifies cloning fails cleanly if target directory exists and is non-empty."""
        remote_repo = self._create_git_repo(self.remote_base / "sample_repo")
        (remote_repo / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote_repo), "commit", "-m", "init"], check=True, capture_output=True)

        dest_path = self.dest_base / "occupied_dir"
        dest_path.mkdir(parents=True, exist_ok=True)
        (dest_path / "existing_file.txt").write_text("blocking\n", encoding="utf-8")

        res = run_primitive_clone(
            git_url=str(remote_repo),
            target_dir=dest_path
        )
        self.assertEqual(res.status, "FAILED")
        self.assertIn("already exists and is not empty", res.error_message)

    def test_clone_with_no_repair(self):
        """Verifies cloning with no_repair=True skips workspace repair."""
        remote_repo = self._create_git_repo(self.remote_base / "drift_no_repair")
        init_drift_workspace(remote_repo, no_git_root=True)
        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote_repo), "commit", "-m", "init"], check=True, capture_output=True)

        dest_path = self.dest_base / "cloned_no_repair"
        res = run_primitive_clone(
            git_url=str(remote_repo),
            target_dir=dest_path,
            no_repair=True
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.is_drift_workspace)
        self.assertEqual(len(res.repaired_actions), 0)
        # render/.git and install/.git should not exist when repair is skipped
        self.assertFalse((dest_path / "render" / ".git").exists())
        self.assertFalse((dest_path / "install" / ".git").exists())

    def test_cli_clone_json_output(self):
        """Verifies CLI execution with 'drift clone --json'."""
        remote_repo = self._create_git_repo(self.remote_base / "cli_repo")
        init_drift_workspace(remote_repo, no_git_root=True)
        subprocess.run(["git", "-C", str(remote_repo), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote_repo), "commit", "-m", "init"], check=True, capture_output=True)

        dest_path = self.dest_base / "cli_dest"

        with patch("sys.stdout.write") as mock_stdout:
            run_argparse_cli(["clone", str(remote_repo), str(dest_path), "--json"])
            output = "".join(call.args[0] for call in mock_stdout.call_args_list)
            data = json.loads(output)
            self.assertEqual(data["command"], "clone")
            self.assertEqual(data["status"], "SUCCESS")
            self.assertTrue(data["is_drift_workspace"])
            self.assertEqual(data["target_directory"], str(dest_path))


if __name__ == "__main__":
    unittest.main()
