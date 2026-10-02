"""Unit tests for Git utility functions."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.core.constants import set_test_mode
from drift.utils.git_utils import (
    git_init_repo,
    configure_repo_git_user,
    check_repo_can_commit,
    assert_repo_can_commit,
    check_repo_git_user_synced,
    commit_staged_repo_changes,
)
from drift.utils.process_utils import run_command


class TestGitUtilsUserConfig(unittest.TestCase):
    """Tests for configure_repo_git_user, check_repo_can_commit, and assert_repo_can_commit."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name).resolve() / "test_repo"
        git_init_repo(self.repo_dir, "test_repo")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_configure_repo_git_user_sets_both(self) -> None:
        """configure_repo_git_user writes user.name and user.email to local git config."""
        configure_repo_git_user(self.repo_dir, user_name="Test Bot", user_email="bot@example.com")

        res_name = run_command(
            ["git", "-C", str(self.repo_dir), "config", "--local", "user.name"],
            text=True,
        )
        self.assertEqual(res_name.stdout.strip(), "Test Bot")

        res_email = run_command(
            ["git", "-C", str(self.repo_dir), "config", "--local", "user.email"],
            text=True,
        )
        self.assertEqual(res_email.stdout.strip(), "bot@example.com")

    def test_configure_repo_git_user_name_only(self) -> None:
        """configure_repo_git_user configures only user.name when email is None."""
        configure_repo_git_user(self.repo_dir, user_name="Name Only")

        res_name = run_command(
            ["git", "-C", str(self.repo_dir), "config", "--local", "user.name"],
            text=True,
        )
        self.assertEqual(res_name.stdout.strip(), "Name Only")

        res_email = run_command(
            ["git", "-C", str(self.repo_dir), "config", "--local", "user.email"],
            text=True,
            check=False,
            suppress_output=True,
        )
        self.assertNotEqual(res_email.returncode, 0)

    def test_configure_repo_git_user_noop_when_both_none(self) -> None:
        """configure_repo_git_user is a silent no-op when both are None."""
        configure_repo_git_user(self.repo_dir, user_name=None, user_email=None)

        res_name = run_command(
            ["git", "-C", str(self.repo_dir), "config", "--local", "user.name"],
            text=True,
            check=False,
            suppress_output=True,
        )
        self.assertNotEqual(res_name.returncode, 0)

    def test_check_repo_can_commit_returns_none_when_configured(self) -> None:
        """check_repo_can_commit returns None when identity is configured."""
        configure_repo_git_user(self.repo_dir, user_name="Valid User", user_email="valid@example.com")
        self.assertIsNone(check_repo_can_commit(self.repo_dir))
        # assert_repo_can_commit should not raise
        assert_repo_can_commit(self.repo_dir)

    def test_check_repo_can_commit_detects_missing_name(self) -> None:
        """check_repo_can_commit detects missing user.name when unconfigured and no env."""
        configure_repo_git_user(self.repo_dir, user_email="valid@example.com")

        env_clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        with patch.dict(os.environ, env_clean, clear=True):
            # Also mock global git config to return empty
            with patch("drift.utils.git_utils.run_command") as mock_cmd:
                mock_cmd.return_value.returncode = 1
                mock_cmd.return_value.stdout = ""

                err = check_repo_can_commit(self.repo_dir)
                self.assertIsNotNone(err)
                assert err is not None
                self.assertIn("user.name", err)
                self.assertIn("git_user_name", err)

                with self.assertRaises(RuntimeError) as ctx:
                    assert_repo_can_commit(self.repo_dir)
                self.assertIn("user.name", str(ctx.exception))

    def test_check_repo_can_commit_detects_missing_email(self) -> None:
        """check_repo_can_commit detects missing user.email when unconfigured and no env."""
        env_clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        with patch.dict(os.environ, env_clean, clear=True):
            def fake_run(cmd, **kwargs):
                from subprocess import CompletedProcess
                if "user.name" in cmd:
                    return CompletedProcess(cmd, 0, stdout="Valid User\n")
                if "user.email" in cmd:
                    return CompletedProcess(cmd, 1, stdout="")
                return CompletedProcess(cmd, 0, stdout="")

            with patch("drift.utils.git_utils.run_command", side_effect=fake_run):
                err = check_repo_can_commit(self.repo_dir)
                self.assertIsNotNone(err)
                assert err is not None
                self.assertIn("user.email", err)
                self.assertIn("git_user_email", err)

                with self.assertRaises(RuntimeError) as ctx:
                    assert_repo_can_commit(self.repo_dir)
                self.assertIn("user.email", str(ctx.exception))

    def test_check_repo_can_commit_nonexistent_directory(self) -> None:
        """check_repo_can_commit returns error string for nonexistent directory."""
        nonexistent = self.repo_dir / "does_not_exist"
        err = check_repo_can_commit(nonexistent)
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("does not exist", err)

        with self.assertRaises(RuntimeError):
            assert_repo_can_commit(nonexistent)

    def test_check_repo_git_user_synced_matches(self) -> None:
        """check_repo_git_user_synced returns None when local config matches expected."""
        configure_repo_git_user(self.repo_dir, user_name="Expected Name", user_email="expected@example.com")
        self.assertIsNone(check_repo_git_user_synced(self.repo_dir, expected_name="Expected Name", expected_email="expected@example.com"))

    def test_check_repo_git_user_synced_mismatch_name(self) -> None:
        """check_repo_git_user_synced returns error message when name differs."""
        configure_repo_git_user(self.repo_dir, user_name="Old Name", user_email="same@example.com")
        err = check_repo_git_user_synced(self.repo_dir, expected_name="New Name", expected_email="same@example.com")
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("user.name", err)

    def test_check_repo_git_user_synced_mismatch_email(self) -> None:
        """check_repo_git_user_synced returns error message when email differs."""
        configure_repo_git_user(self.repo_dir, user_name="Same Name", user_email="old@example.com")
        err = check_repo_git_user_synced(self.repo_dir, expected_name="Same Name", expected_email="new@example.com")
        self.assertIsNotNone(err)
        assert err is not None
        self.assertIn("user.email", err)

    def test_check_repo_git_user_synced_none_expected(self) -> None:
        """check_repo_git_user_synced returns None when expected values are None."""
        self.assertIsNone(check_repo_git_user_synced(self.repo_dir, expected_name=None, expected_email=None))


class TestCommitStagedRepoChanges(unittest.TestCase):
    """Tests for commit_staged_repo_changes."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name).resolve() / "test_repo"
        git_init_repo(self.repo_dir, "test_repo")
        configure_repo_git_user(self.repo_dir, user_name="Test User", user_email="test@example.com")
        # Initial commit
        init_file = self.repo_dir / "init.txt"
        init_file.write_text("initial", encoding="utf-8")
        run_command(["git", "-C", str(self.repo_dir), "add", "init.txt"])
        run_command(["git", "-C", str(self.repo_dir), "commit", "-m", "initial commit"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_commit_staged_repo_changes_no_staged_changes(self) -> None:
        """Returns False when index is clean (nothing staged)."""
        # Unstaged change exists
        dirty_file = self.repo_dir / "init.txt"
        dirty_file.write_text("dirty unstaged", encoding="utf-8")

        head_before = run_command(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"], text=True).stdout.strip()
        committed = commit_staged_repo_changes(self.repo_dir, "Should not commit", "test repo")
        self.assertFalse(committed)

        head_after = run_command(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"], text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

    def test_commit_staged_repo_changes_with_staged_changes(self) -> None:
        """Commits only staged changes and returns True."""
        # Create staged file
        staged_file = self.repo_dir / "file_staged.txt"
        staged_file.write_text("staged content", encoding="utf-8")
        run_command(["git", "-C", str(self.repo_dir), "add", "file_staged.txt"])

        # Create unstaged file
        unstaged_file = self.repo_dir / "file_unstaged.txt"
        unstaged_file.write_text("unstaged content", encoding="utf-8")

        head_before = run_command(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"], text=True).stdout.strip()
        committed = commit_staged_repo_changes(self.repo_dir, "Commit staged only", "test repo")
        self.assertTrue(committed)

        head_after = run_command(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"], text=True).stdout.strip()
        self.assertNotEqual(head_before, head_after)

        # Staged file is committed, unstaged file remains untracked
        res = run_command(["git", "-C", str(self.repo_dir), "status", "--porcelain"], text=True)
        self.assertIn("?? file_unstaged.txt", res.stdout)
        self.assertNotIn("file_staged.txt", res.stdout)

    def test_commit_staged_repo_changes_nonexistent_directory(self) -> None:
        """Raises FileNotFoundError when repo_path does not exist."""
        nonexistent = self.repo_dir / "nonexistent"
        with self.assertRaises(FileNotFoundError):
            commit_staged_repo_changes(nonexistent, "Commit msg", "nonexistent repo")


if __name__ == "__main__":
    unittest.main()
