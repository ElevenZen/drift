import os
import sys
import tempfile
import unittest
import subprocess
from io import StringIO
from unittest.mock import patch

from drift.cli import main
from tests.test_utils import TestCaseUtilityMixin


class TestCLI(TestCaseUtilityMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = os.path.join(self.temp_dir.name, "drift_workspace")
        os.makedirs(self.drift_root, exist_ok=True)

        # Initialize Git in the temporary directory
        subprocess.run(["git", "init"], cwd=self.drift_root, check=True, capture_output=True)

        # Create config and env file
        self.config_dir = os.path.join(self.drift_root, "config")
        os.makedirs(self.config_dir, exist_ok=True)
        with open(os.path.join(self.config_dir, "drift.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            source_directory = "src"
            render_directory = "render"

            [packages.enable]
            pkg_a = true
            pkg_b = false
            """)

        # Create packages
        self.src_dir = os.path.join(self.drift_root, "src")
        os.makedirs(self.src_dir, exist_ok=True)

        for pkg in ("pkg_a", "pkg_b"):
            pkg_path = os.path.join(self.src_dir, pkg)
            os.makedirs(pkg_path, exist_ok=True)
            with open(os.path.join(pkg_path, "drift_package.toml"), "w", encoding="utf-8") as f:
                f.write(f"""
                [package]
                name = "{pkg}"
                enable_render = true
                """)
            with open(os.path.join(pkg_path, "file.txt"), "w", encoding="utf-8") as f:
                f.write(f"Hello from {pkg}")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_help(self) -> None:
        """Verifies that the CLI help option displays correctly."""
        stdout = StringIO()
        stderr = StringIO()

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = stdout
        sys.stderr = stderr

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

        self.assertIn("drift: Decoupled Two-Stage Git-Backed Dotfiles Manager", stdout.getvalue())

    def test_cli_render_all_packages(self) -> None:
        """Verifies that running 'render' without a package argument renders all enabled packages."""
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "render"])
        finally:
            sys.stdout = original_stdout

        self.assertIn_stripped("✨ Successfully rendered all enabled packages!", stdout.getvalue())

        # pkg_a is enabled -> should be rendered
        self.assertTrue(os.path.exists(os.path.join(self.drift_root, "render", "pkg_a", "file.txt")))
        # pkg_b is disabled -> should NOT be rendered
        self.assertFalse(os.path.exists(os.path.join(self.drift_root, "render", "pkg_b", "file.txt")))

    def test_cli_render_single_package(self) -> None:
        """Verifies that running 'render <package>' renders only that specific package."""
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "render", "pkg_b"])
        finally:
            sys.stdout = original_stdout

        self.assertIn_stripped("✨ Successfully rendered package", stdout.getvalue())

        # pkg_b was explicitly requested -> should be rendered even if disabled in bulk
        self.assertTrue(os.path.exists(os.path.join(self.drift_root, "render", "pkg_b", "file.txt")))
        # pkg_a was not requested -> should NOT be rendered
        self.assertFalse(os.path.exists(os.path.join(self.drift_root, "render", "pkg_a", "file.txt")))

    def test_cli_render_nonexistent_package_raises_error(self) -> None:
        """Verifies that rendering a non-existent package exits with an error."""
        stderr = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["-C", self.drift_root, "render", "nonexistent_pkg"])
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stderr = original_stderr

        self.assertIn("❌ [ERROR]", stderr.getvalue())
        self.assertIn("packages not found", stderr.getvalue())
        self.assertIn("nonexistent_pkg", stderr.getvalue())

    def test_cli_render_failure_does_not_print_success_message(self) -> None:
        """Verifies that when rendering fails, CLI exits with error code and does not print success."""
        from pathlib import Path
        # Enable var render engine in drift.local.toml
        (Path(self.drift_root) / "config" / "drift.local.toml").write_text("""
        [render.var]
        suffix = "var"
        render_command = "internal"
        """)

        # Create a package with broken template (var engine with undefined variable)
        broken_pkg = Path(self.drift_root) / "src" / "broken_pkg"
        broken_pkg.mkdir(parents=True, exist_ok=True)
        (broken_pkg / "drift_package.toml").write_text("[package]\ninstall_method='stow'\n")
        (broken_pkg / "bad.var").write_text("Hello $UNDEFINED_TEST_VARIABLE_XYZ_999\n")

        stdout = StringIO()
        stderr = StringIO()
        original_stdout, original_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["-C", self.drift_root, "render", "broken_pkg"])
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr

        self.assertNotIn("Successfully rendered", stdout.getvalue())
        self.assertIn("❌ [ERROR]", stderr.getvalue())
        self.assertIn("broken_pkg", stderr.getvalue())

    def test_argparse_backend_explicitly(self) -> None:
        """Explicitly tests the argparse CLI backend fallback."""
        from drift.cli import run_argparse_cli

        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            run_argparse_cli(["-C", self.drift_root, "render", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        self.assertIn_stripped("✨ Successfully rendered package", stdout.getvalue())

    def test_render_outside_git_repository_raises_friendly_error(self) -> None:
        """Verifies that running render outside a Git repository prints our friendly error message."""
        with tempfile.TemporaryDirectory() as non_git_dir_path:
            stderr = StringIO()
            original_stderr = sys.stderr
            sys.stderr = stderr

            try:
                with self.assertRaises(SystemExit) as cm:
                    main(["-C", non_git_dir_path, "render"])
                self.assertEqual(cm.exception.code, 1)
            finally:
                sys.stderr = original_stderr

            self.assertIn_stripped("is not inside a Git repository", stderr.getvalue())
            self.assertIn_stripped("drift requires a Git-backed workspace", stderr.getvalue())
            self.assertIn_stripped("Run 'drift init' to initialize a new workspace", stderr.getvalue())

    def test_cli_stage(self) -> None:
        """Verifies that running 'stage' stages the package into install directory."""
        # 1. Initialize the workspace properly to setup directories and git repos
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", self.drift_root, "init", "--force"])
            main(["-C", self.drift_root, "render", "pkg_a"])

        # 3. Stage package a
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "stage", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        # Verify that pkg_a files are copied to install/
        self.assertTrue(os.path.exists(os.path.join(self.drift_root, "install", "pkg_a", "file.txt")))

    def test_cli_render_commit(self) -> None:
        """Verifies that running 'render-commit' commits sandbox changes."""
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", self.drift_root, "init", "--force"])
            main(["-C", self.drift_root, "render", "pkg_a"])

        render_dir = os.path.join(self.drift_root, "render")
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=render_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=render_dir, check=True, capture_output=True)

        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "render-commit", "-m", "Manual templates commit", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        # Verify commit worked in render repo
        res = subprocess.run(["git", "log", "-n", "1", "--oneline"], cwd=render_dir, capture_output=True, text=True, check=True)
        self.assertIn("Manual templates commit", res.stdout)

    def test_cli_apply(self) -> None:
        """Verifies that running 'apply' deploys files to target directories."""
        # Write a drift_package.toml with target_directory outside drift_root
        pkg_path = os.path.join(self.src_dir, "pkg_a")
        target_dir = os.path.join(self.temp_dir.name, "system_home")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(pkg_path, "drift_package.toml"), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            enable_render = true
            target_directory = "{target_dir}"
            """)

        # 1. Initialize, render, stage, then apply
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", self.drift_root, "init", "--force"])
            main(["-C", self.drift_root, "render", "pkg_a"])
            main(["-C", self.drift_root, "stage", "pkg_a"])

        # 2. Run apply CLI
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "apply", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        # Verify that pkg_a files are applied/deployed to system target (simulating system_home)
        self.assertTrue(os.path.islink(os.path.join(target_dir, "file.txt")))

    def test_cli_install_commit(self) -> None:
        """Verifies that running 'install-commit' commits install state changes."""
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", self.drift_root, "init", "--force"])
            main(["-C", self.drift_root, "render", "pkg_a"])
            main(["-C", self.drift_root, "stage", "pkg_a"])

        install_dir = os.path.join(self.drift_root, "install")
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=install_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=install_dir, check=True, capture_output=True)

        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "install-commit", "-m", "Manual state commit", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        # Verify commit worked in install repo
        res = subprocess.run(["git", "log", "-n", "1", "--oneline"], cwd=install_dir, capture_output=True, text=True, check=True)
        self.assertIn("Manual state commit", res.stdout)

    def test_cli_reverse_sync(self) -> None:
        """Verifies that running 'reverse-sync' synchronizes changes from host back to install/."""
        # Setup: Render and Stage pkg_a
        pkg_path = os.path.join(self.src_dir, "pkg_a")
        target_dir = os.path.join(self.temp_dir.name, "system_home_reverse")
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(pkg_path, "drift_package.toml"), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            enable_render = true
            install_method = "copy"
            target_directory = "{target_dir}"
            """)

        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            main(["-C", self.drift_root, "init", "--force"])
            main(["-C", self.drift_root, "render", "pkg_a"])
            main(["-C", self.drift_root, "stage", "pkg_a"])
            main(["-C", self.drift_root, "apply", "pkg_a"])

        # Simulate host change
        target_file = os.path.join(target_dir, "file.txt")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("modified on host")

        # Run reverse-sync CLI
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            main(["-C", self.drift_root, "reverse-sync", "pkg_a"])
        finally:
            sys.stdout = original_stdout

        # Verify that pkg_a files in install/ are updated
        install_file = os.path.join(self.drift_root, "install", "pkg_a", "file.txt")
        with open(install_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "modified on host")
        self.assertIn_stripped("Successfully reverse-synced package", stdout.getvalue())

    def test_cli_new_with_target_and_method(self) -> None:
        """Verifies that 'new' command with target and method options works in both Typer and Argparse backends."""
        from drift.cli import run_argparse_cli

        # 1. Test Typer Backend (main)
        with patch("sys.stdout", StringIO()):
            main(["-C", self.drift_root, "new", "typer_pkg", "--target", "/tmp/typer_target", "--method", "copy"])
        typer_config_file = os.path.join(self.src_dir, "typer_pkg", "drift_package.toml")
        self.assertTrue(os.path.isfile(typer_config_file))
        with open(typer_config_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn('target_directory = "/tmp/typer_target"', content)
            self.assertIn('install_method = "copy"', content)

        # 2. Test Argparse Backend fallback
        with patch("sys.stdout", StringIO()):
            run_argparse_cli(["-C", self.drift_root, "new", "argparse_pkg", "--target", "/tmp/argparse_target", "--method", "copy"])
        argparse_config_file = os.path.join(self.src_dir, "argparse_pkg", "drift_package.toml")
        self.assertTrue(os.path.isfile(argparse_config_file))
        with open(argparse_config_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn('target_directory = "/tmp/argparse_target"', content)
            self.assertIn('install_method = "copy"', content)

    @patch("os.environ", {"SUDO_USER": "testuser"})
    def test_cli_sudo_user_prohibited(self) -> None:
        """Verifies that running under sudo is prohibited and exits with 1."""
        stderr = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stderr = original_stderr

        self.assertIn("Running under 'sudo' is strictly prohibited", stderr.getvalue())

    @patch("os.getuid", return_value=0, create=True)
    @patch("getpass.getuser", return_value="testuser")
    @patch("os.environ", {})
    def test_cli_root_privilege_prohibited(self, mock_getuser, mock_getuid) -> None:
        """Verifies that running with root privilege for a non-root user is prohibited."""
        stderr = StringIO()
        original_stderr = sys.stderr
        sys.stderr = stderr

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
            self.assertEqual(cm.exception.code, 1)
        finally:
            sys.stderr = original_stderr

        self.assertIn("Running with root privilege is prohibited unless you are the actual 'root' user.", stderr.getvalue())

    @patch("os.getuid", return_value=0, create=True)
    @patch("getpass.getuser", return_value="root")
    @patch("os.environ", {})
    def test_cli_real_root_allowed(self, mock_getuser, mock_getuid) -> None:
        """Verifies that the actual 'root' user (without sudo) is allowed to run the program."""
        stdout = StringIO()
        original_stdout = sys.stdout
        sys.stdout = stdout

        try:
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
            self.assertEqual(cm.exception.code, 0)
        finally:
            sys.stdout = original_stdout

        self.assertIn("drift: Decoupled Two-Stage Git-Backed Dotfiles Manager", stdout.getvalue())

    def test_cli_no_hooks_flags_across_commands(self) -> None:
        """Verifies that both --no-hooks and --no-hook flags pass no_hooks=True to action handlers."""
        from drift.cli import run_argparse_cli

        commands_to_test = [
            ("render", "drift.cli.cli_handlers.execute_render", "drift.cli.cli_handlers.execute_render", ["render"]),
            ("apply", "drift.cli.cli_handlers.execute_apply", "drift.cli.cli_handlers.execute_apply", ["apply"]),
            ("deploy", "drift.cli.cli_handlers.execute_deploy", "drift.cli.cli_handlers.execute_deploy", ["deploy"]),
            ("adopt", "drift.cli.cli_handlers.execute_adopt", "drift.cli.cli_handlers.execute_adopt", ["adopt", "pkg_a"]),
            ("add", "drift.cli.cli_handlers.execute_add", "drift.cli.cli_handlers.execute_add", ["add", "pkg_a", "/dev/null"]),
            ("uninstall", "drift.cli.cli_handlers.execute_uninstall", "drift.cli.cli_handlers.execute_uninstall", ["uninstall", "pkg_a"]),
            ("rollback", "drift.cli.cli_handlers.execute_rollback", "drift.cli.cli_handlers.execute_rollback", ["rollback"]),
            ("gc", "drift.cli.cli_handlers.execute_gc", "drift.cli.cli_handlers.execute_gc", ["gc"]),
        ]

        for flag in ["--no-hooks", "--no-hook"]:
            for cmd_name, typer_target, argparse_target, cmd_args in commands_to_test:
                with patch(typer_target) as mock_typer_action:
                    with patch("sys.stdout", StringIO()):
                        main(["-C", self.drift_root] + cmd_args + [flag])
                    self.assertTrue(mock_typer_action.called, f"Typer {cmd_name} with {flag} was not called")
                    _, kwargs = mock_typer_action.call_args
                    self.assertTrue(kwargs.get("no_hooks"), f"Typer {cmd_name} with {flag} did not pass no_hooks=True")

                with patch(argparse_target) as mock_argparse_action:
                    with patch("sys.stdout", StringIO()):
                        run_argparse_cli(["-C", self.drift_root] + cmd_args + [flag])
                    self.assertTrue(mock_argparse_action.called, f"Argparse {cmd_name} with {flag} was not called")
                    _, kwargs = mock_argparse_action.call_args
                    self.assertTrue(kwargs.get("no_hooks"), f"Argparse {cmd_name} with {flag} did not pass no_hooks=True")


if __name__ == "__main__":
    unittest.main()
