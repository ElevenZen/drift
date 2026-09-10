import unittest
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from drift.editor_utils import (
    get_configured_editor,
    launch_single_file_editor,
    launch_vim_diff,
    launch_vscode_diff,
    launch_emacs_diff,
    launch_side_by_side_editor,
    SUPPORTED_EDITORS,
)


class TestEditorUtils(unittest.TestCase):

    def test_get_configured_editor_success(self) -> None:
        """Verifies get_configured_editor returns the EDITOR env var when set."""
        with patch.dict(os.environ, {"EDITOR": "nvim -u NONE"}):
            self.assertEqual(get_configured_editor(), "nvim -u NONE")

    def test_get_configured_editor_unset_raises_error(self) -> None:
        """Verifies get_configured_editor raises RuntimeError if EDITOR is unset or empty."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_configured_editor()
            self.assertIn("Environment variable $EDITOR is not set", str(ctx.exception))

        with patch.dict(os.environ, {"EDITOR": "   "}):
            with self.assertRaises(RuntimeError) as ctx:
                get_configured_editor()
            self.assertIn("Environment variable $EDITOR is not set", str(ctx.exception))

    def test_parse_editor_command_with_quotes_and_flags(self) -> None:
        """Verifies parse_editor_command uses shlex to parse quoted arguments and flags."""
        from drift.editor_utils import parse_editor_command

        tokens, name = parse_editor_command('code --reuse-window --wait')
        self.assertEqual(tokens, ["code", "--reuse-window", "--wait"])
        self.assertEqual(name, "code")

        tokens, name = parse_editor_command('"/usr/local/bin/my nvim" -u "init config.lua"')
        self.assertEqual(tokens, ["/usr/local/bin/my nvim", "-u", "init config.lua"])
        self.assertEqual(name, "my nvim")

    @patch("subprocess.run")
    def test_launch_single_file_editor_terminal(self, mock_run: MagicMock) -> None:
        """Verifies launch_single_file_editor launches terminal editors directly."""
        mock_run.return_value = MagicMock(returncode=0)
        file_path = Path("/tmp/test.txt")

        with patch.dict(os.environ, {"EDITOR": "nvim"}):
            launch_single_file_editor(file_path)
            mock_run.assert_called_once_with(["nvim", str(file_path)], check=True)

    @patch("subprocess.run")
    def test_launch_single_file_editor_vscode(self, mock_run: MagicMock) -> None:
        """Verifies launch_single_file_editor passes --wait for VS Code variants."""
        mock_run.return_value = MagicMock(returncode=0)
        file_path = Path("/tmp/test.txt")

        with patch.dict(os.environ, {"EDITOR": "code"}):
            launch_single_file_editor(file_path)
            mock_run.assert_called_once_with(["code", "--wait", str(file_path)], check=True)

        mock_run.reset_mock()
        with patch.dict(os.environ, {"EDITOR": "/usr/bin/codium"}):
            launch_single_file_editor(file_path)
            mock_run.assert_called_once_with(["/usr/bin/codium", "--wait", str(file_path)], check=True)

    @patch("subprocess.run")
    def test_launch_single_file_editor_missing_binary(self, mock_run: MagicMock) -> None:
        """Verifies launch_single_file_editor handles missing binary gracefully."""
        mock_run.side_effect = FileNotFoundError("Executable not found")
        with patch.dict(os.environ, {"EDITOR": "non_existent_editor"}):
            with self.assertRaises(RuntimeError) as ctx:
                launch_single_file_editor(Path("/tmp/test.txt"))
            self.assertIn("not found", str(ctx.exception))

    @patch("subprocess.run")
    def test_launch_vim_diff_single_pair(self, mock_run: MagicMock) -> None:
        """Verifies launch_vim_diff with a single file pair."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))
        launch_vim_diff("nvim", [p1])
        mock_run.assert_called_once_with(
            ["nvim", str(p1[0]), "-c", f"vert diffsplit {p1[1]}"],
            check=False
        )

    @patch("subprocess.run")
    def test_launch_vim_diff_multiple_pairs(self, mock_run: MagicMock) -> None:
        """Verifies launch_vim_diff with multiple file pairs creates tabpages."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))
        p2 = (Path("/tmp/f2_a.txt"), Path("/tmp/f2_b.txt"))
        launch_vim_diff("vim", [p1, p2])
        expected_cmd = [
            "vim",
            str(p1[0]),
            "-c", f"vert diffsplit {p1[1]}",
            "-c", f"tabe {p2[0]}",
            "-c", f"vert diffsplit {p2[1]}",
            "-c", "tabfirst",
        ]
        mock_run.assert_called_once_with(expected_cmd, check=False)

    @patch("subprocess.run")
    def test_launch_vscode_diff_single_and_multiple_pairs(self, mock_run: MagicMock) -> None:
        """Verifies launch_vscode_diff uses --reuse-window and --wait on the final pair."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))
        p2 = (Path("/tmp/f2_a.txt"), Path("/tmp/f2_b.txt"))

        # Single pair
        launch_vscode_diff("code", [p1])
        mock_run.assert_called_once_with(
            ["code", "--diff", str(p1[0]), str(p1[1]), "--reuse-window", "--wait"],
            check=False
        )

        # Multiple pairs
        mock_run.reset_mock()
        launch_vscode_diff("code-oss", [p1, p2])
        self.assertEqual(mock_run.call_count, 2)
        mock_run.assert_has_calls([
            call(["code-oss", "--diff", str(p1[0]), str(p1[1]), "--reuse-window"], check=False),
            call(["code-oss", "--diff", str(p2[0]), str(p2[1]), "--reuse-window", "--wait"], check=False),
        ])

    @patch("subprocess.run")
    def test_launch_emacs_diff_single_and_multiple_pairs(self, mock_run: MagicMock) -> None:
        """Verifies launch_emacs_diff evaluates ediff and tab-bar-mode expressions."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))
        p2 = (Path("/tmp/f2_a.txt"), Path("/tmp/f2_b.txt"))

        # Single pair
        launch_emacs_diff("emacs", [p1])
        expected_single_elisp = (
            f'(progn (setq ediff-split-window-function \'split-window-horizontally) '
            f'(setq ediff-window-setup-function \'ediff-setup-windows-plain) '
            f'(setq auto-save-default nil) '
            f'(setq make-backup-files nil) '
            f'(setq create-lockfiles nil) '
            f'(ediff-files "{p1[0]}" "{p1[1]}"))'
        )
        mock_run.assert_called_once_with(
            ["emacs", "--eval", expected_single_elisp],
            check=False
        )

        # Multiple pairs
        mock_run.reset_mock()
        launch_emacs_diff("emacs", [p1, p2])
        expected_elisp = (
            f'(progn (setq ediff-split-window-function \'split-window-horizontally) '
            f'(setq ediff-window-setup-function \'ediff-setup-windows-plain) '
            f'(setq auto-save-default nil) '
            f'(setq make-backup-files nil) '
            f'(setq create-lockfiles nil) '
            f'(tab-bar-mode 1) '
            f'(ediff-files "{p1[0]}" "{p1[1]}") '
            f'(tab-new) '
            f'(ediff-files "{p2[0]}" "{p2[1]}") '
            f'(tab-bar-select-tab 1))'
        )
        mock_run.assert_called_once_with(
            ["emacs", "--eval", expected_elisp],
            check=False
        )

    @patch("subprocess.run")
    def test_launch_side_by_side_editor_dispatcher(self, mock_run: MagicMock) -> None:
        """Verifies launch_side_by_side_editor dispatches to appropriate editor adapters."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))

        with patch.dict(os.environ, {"EDITOR": "nvim"}):
            launch_side_by_side_editor([p1])
            mock_run.assert_called_once()

        mock_run.reset_mock()
        with patch.dict(os.environ, {"EDITOR": "codium"}):
            launch_side_by_side_editor([p1])
            mock_run.assert_called_once()

        mock_run.reset_mock()
        with patch.dict(os.environ, {"EDITOR": "emacs"}):
            launch_side_by_side_editor([p1])
            mock_run.assert_called_once()

    def test_launch_side_by_side_editor_unsupported_raises_error(self) -> None:
        """Verifies launch_side_by_side_editor raises RuntimeError for unsupported editors."""
        p1 = (Path("/tmp/f1_a.txt"), Path("/tmp/f1_b.txt"))
        with patch.dict(os.environ, {"EDITOR": "nano"}):
            with self.assertRaises(RuntimeError) as ctx:
                launch_side_by_side_editor([p1])
            self.assertIn("Editor 'nano' is not supported for side-by-side diffing (-y)", str(ctx.exception))
            for ed in ["nvim", "vim", "code", "emacs"]:
                self.assertIn(ed, str(ctx.exception))

    @patch("subprocess.run")
    def test_launch_side_by_side_editor_empty_pairs_noop(self, mock_run: MagicMock) -> None:
        """Verifies launch_side_by_side_editor does nothing if file_pairs is empty."""
        launch_side_by_side_editor([])
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
