import os
import sys
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from drift.git_utils import (
    is_git_tracked,
    is_bare_repository,
    is_detached_head,
    is_merge_or_rebase_in_progress,
    git_init_repo,
    append_to_gitignore,
)
from drift.workspace_init import (
    init_drift_workspace,
)
from drift.cli import main
from drift.cli.argparse_backend import run_argparse_cli


class TestInitWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_in_empty_non_git_directory(self) -> None:
        """Verifies that init works in an empty directory that is not tracked by Git.

        It should initialize a main Git repo, then set up the workspace.
        """
        init_drift_workspace(self.drift_root)

        # Check main git repo was initialized
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, ".git")))

        # Check `.gitignore` was created and contains folders to ignore
        gitignore_path = os.path.join(self.drift_root, ".gitignore")
        self.assertTrue(os.path.isfile(gitignore_path))
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("render/", content)
        self.assertIn("install/", content)

        # Check render and install sub-repos exist with .git
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, "render", ".git")))
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, "install", ".git")))

        # Check `.stow-local-ignore` inside install/
        stow_ignore = os.path.join(self.drift_root, "install", ".stow-local-ignore")
        self.assertTrue(os.path.isfile(stow_ignore))
        with open(stow_ignore, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "state.toml")

        # Check config/drift.toml template was created
        config_file = os.path.join(self.drift_root, "config", "drift.toml")
        self.assertTrue(os.path.isfile(config_file))
        with open(config_file, "r", encoding="utf-8") as f:
            drift_toml = f.read()
        self.assertIn("[workspace]", drift_toml)
        self.assertIn("source_directory = \"src\"", drift_toml)

        # Check config/drift.local.toml template was created
        local_config_file = os.path.join(self.drift_root, "config", "drift.local.toml")
        self.assertTrue(os.path.isfile(local_config_file))

        # Check envsubst.bash, mustache.envst.json, and jinja2.mustache.json were created
        envsubst_bash = os.path.join(self.drift_root, "config", "envsubst.bash")
        self.assertTrue(os.path.isfile(envsubst_bash))
        mustache_json = os.path.join(self.drift_root, "config", "mustache.envst.json")
        self.assertTrue(os.path.isfile(mustache_json))
        jinja2_json = os.path.join(self.drift_root, "config", "jinja2.mustache.json")
        self.assertTrue(os.path.isfile(jinja2_json))

        # Check install/state.toml was created
        state_file = os.path.join(self.drift_root, "install", "state.toml")
        self.assertTrue(os.path.isfile(state_file))
        with open(state_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "[packages]")

        # Check src directory was created
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, "src")))

    def test_init_in_non_empty_non_git_directory_raises_error(self) -> None:
        """Verifies that init in a non-empty, non-git directory raises an error."""
        # Create a dummy file to make directory non-empty
        with open(os.path.join(self.drift_root, "dummy.txt"), "w") as f:
            f.write("dummy")

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertIn("Directory is not empty and not tracked by git", str(cm.exception))

    def test_init_in_existing_git_repository(self) -> None:
        """Verifies that init in an existing Git repo sets up workspace correctly at repo root."""
        # Create a subfolder and initialize git at self.drift_root
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        # Create a sub-directory and try initializing inside it
        sub_dir = self.drift_root / "some" / "subdir"
        os.makedirs(sub_dir, exist_ok=True)

        init_drift_workspace(sub_dir)

        # It should resolve self.drift_root as the workspace top-level root
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, ".git")))
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, "render", ".git")))
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, "install", ".git")))
        self.assertTrue(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_init_twice_raises_error(self) -> None:
        """Verifies that running init twice raises an error."""
        init_drift_workspace(self.drift_root)

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertIn("already initialized", str(cm.exception))

    def test_init_twice_with_force_succeeds(self) -> None:
        """Verifies that running init twice with force=True succeeds and overwrites config files."""
        init_drift_workspace(self.drift_root)

        # Modify drift.toml to see if it gets overwritten
        config_file = os.path.join(self.drift_root, "config", "drift.toml")
        with open(config_file, "w") as f:
            f.write("corrupted_or_modified_toml_content")

        # Running again with force=True should not raise an error
        init_drift_workspace(self.drift_root, force=True)

        # Check that drift.toml was overwritten back to default content
        with open(config_file, "r") as f:
            content = f.read()
        self.assertIn("[workspace]", content)

    def test_init_corrupt_config_raises_custom_error(self) -> None:
        """Verifies that an existing corrupt/invalid drift.toml raises a helpful validation error."""
        init_drift_workspace(self.drift_root)

        config_file = os.path.join(self.drift_root, "config", "drift.toml")
        with open(config_file, "w") as f:
            f.write("this is invalid toml = [ { ")

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertTrue(
            "broken components" in str(cm.exception) or "corrupt configuration" in str(cm.exception)
        )
        self.assertIn("drift repair", str(cm.exception))
        self.assertIn("--force", str(cm.exception))

    def test_init_non_empty_non_git_with_force_succeeds(self) -> None:
        """Verifies that init with force works on a non-empty, non-git directory."""
        with open(os.path.join(self.drift_root, "somefile.txt"), "w") as f:
            f.write("hello")

        init_drift_workspace(self.drift_root, force=True)
        self.assertTrue(os.path.isdir(os.path.join(self.drift_root, ".git")))
        self.assertTrue(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_init_bare_repository_raises_error(self) -> None:
        """Verifies that initializing inside a bare git repository raises an error."""
        subprocess.run(["git", "init", "--bare"], cwd=self.drift_root, check=True, capture_output=True)

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertIn("Bare Git repositories are not supported", str(cm.exception))

    def test_init_detached_head_raises_error(self) -> None:
        """Verifies that initializing inside a repository with detached HEAD raises an error."""
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)
        # We need an initial commit to detached checkout
        # Set config locally first
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.drift_root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.drift_root, check=True)
        # Create commit
        with open(os.path.join(self.drift_root, "file.txt"), "w") as f:
            f.write("content")
        subprocess.run(["git", "add", "file.txt"], cwd=self.drift_root, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.drift_root, check=True)
        # Detach HEAD
        subprocess.run(["git", "checkout", "HEAD~0"], cwd=self.drift_root, check=True, capture_output=True)

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertIn("Git repository is in a detached HEAD state", str(cm.exception))

        # But with force, it should skip health checks and succeed
        init_drift_workspace(self.drift_root, force=True)
        self.assertTrue(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_init_merge_in_progress_raises_error(self) -> None:
        """Verifies that initializing inside a repository with a merge/rebase in progress raises an error."""
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)
        # Manually create MERGE_HEAD under .git to simulate a merge in progress
        git_dir_res = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.drift_root, capture_output=True, text=True, check=True)
        git_dir = os.path.join(self.drift_root, git_dir_res.stdout.strip())
        merge_head_file = os.path.join(git_dir, "MERGE_HEAD")
        with open(merge_head_file, "w") as f:
            f.write("dummy_commit_hash")

        self.assertTrue(is_merge_or_rebase_in_progress(self.drift_root))

        with self.assertRaises(RuntimeError) as cm:
            init_drift_workspace(self.drift_root)
        self.assertIn("middle of a merge or rebase operation", str(cm.exception))

    def test_init_non_writable_raises_error(self) -> None:
        """Verifies that a non-writable path raises a PermissionError."""
        with patch("os.access", return_value=False):
            with self.assertRaises(PermissionError):
                init_drift_workspace(self.drift_root)

    def test_cli_init_typer(self) -> None:
        """Verifies that typer_backend CLI successfully initializes the workspace."""
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", str(self.drift_root), "init"])
        finally:
            sys.stdout = original_stdout

        self.assertIn("Initialized drift workspace!", stdout.getvalue())
        self.assertIn("Created render/ sandbox Git database.", stdout.getvalue())
        self.assertIn("Created install/ local state Git database.", stdout.getvalue())
        self.assertIn("Generated drift.toml template.", stdout.getvalue())
        self.assertIn("Generated config/envsubst.bash, config/mustache.envst.json, and config/jinja2.mustache.json.", stdout.getvalue())

        # Check drift.toml exists
        self.assertTrue(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_cli_init_argparse(self) -> None:
        """Verifies that argparse_backend CLI successfully initializes the workspace."""
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            run_argparse_cli(["-C", str(self.drift_root), "init"])
        finally:
            sys.stdout = original_stdout

        self.assertIn("Initialized drift workspace!", stdout.getvalue())
        self.assertIn("Created render/ sandbox Git database.", stdout.getvalue())
        self.assertIn("Created install/ local state Git database.", stdout.getvalue())
        self.assertIn("Generated drift.toml template.", stdout.getvalue())
        self.assertIn("Generated config/envsubst.bash, config/mustache.envst.json, and config/jinja2.mustache.json.", stdout.getvalue())

        # Check drift.toml exists
        self.assertTrue(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_cli_init_typer_with_force(self) -> None:
        """Verifies that typer_backend CLI successfully initializes with --force."""
        main(["-C", str(self.drift_root), "init"])
        
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", str(self.drift_root), "init", "--force"])
        finally:
            sys.stdout = original_stdout

        self.assertIn("Initialized drift workspace!", stdout.getvalue())

    def test_init_with_no_git_root_literal(self) -> None:
        """Verifies that --no-git-root stops drift from climbing to the Git repository root."""
        # Initialize Git at self.drift_root
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        # Create a sub-directory
        sub_dir = self.drift_root / "nested_dir"
        os.makedirs(sub_dir, exist_ok=True)

        # Initialize with no_git_root=True
        init_drift_workspace(sub_dir, no_git_root=True)

        # Workspace should be initialized inside nested_dir literally, not self.drift_root
        self.assertTrue(os.path.isdir(os.path.join(sub_dir, "render", ".git")))
        self.assertTrue(os.path.isdir(os.path.join(sub_dir, "install", ".git")))
        self.assertTrue(os.path.isfile(os.path.join(sub_dir, "config", "drift.toml")))
        # The parent self.drift_root should NOT have these folders/files created
        self.assertFalse(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_cli_init_typer_with_no_git_root(self) -> None:
        """Verifies that Typer CLI respects --no-git-root."""
        # Initialize Git at self.drift_root
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        # Create a sub-directory
        sub_dir = os.path.join(self.drift_root, "nested_typer_dir")
        os.makedirs(sub_dir, exist_ok=True)

        # Run with --no-git-root
        main(["-C", sub_dir, "--no-git-root", "init"])

        # Workspace should be inside nested_typer_dir
        self.assertTrue(os.path.isfile(os.path.join(sub_dir, "config", "drift.toml")))
        self.assertFalse(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))

    def test_cli_init_argparse_with_no_git_root(self) -> None:
        """Verifies that Argparse CLI respects --no-git-root."""
        # Initialize Git at self.drift_root
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        # Create a sub-directory
        sub_dir = os.path.join(self.drift_root, "nested_argparse_dir")
        os.makedirs(sub_dir, exist_ok=True)

        # Run with --no-git-root
        run_argparse_cli(["-C", sub_dir, "--no-git-root", "init"])

        # Workspace should be inside nested_argparse_dir
        self.assertTrue(os.path.isfile(os.path.join(sub_dir, "config", "drift.toml")))
        self.assertFalse(os.path.isfile(os.path.join(self.drift_root, "config", "drift.toml")))


if __name__ == "__main__":
    unittest.main()
