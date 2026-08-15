import os
import sys
import tempfile
import unittest
from io import StringIO

from drift.cli import main


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = self.temp_dir.name

        # Create config and env file
        self.config_dir = os.path.join(self.drift_root, "config")
        os.makedirs(self.config_dir, exist_ok=True)
        with open(os.path.join(self.config_dir, "drift.toml"), "w", encoding="utf-8") as f:
            f.write("""
            [workspace]
            source_directory = "src"
            render_directory = "render"

            [packages]
            pkg_a = true
            pkg_b = false
            """)

        # Create packages
        self.src_dir = os.path.join(self.drift_root, "src")
        os.makedirs(self.src_dir, exist_ok=True)

        for pkg in ("pkg_a", "pkg_b"):
            pkg_path = os.path.join(self.src_dir, pkg)
            os.makedirs(pkg_path, exist_ok=True)
            with open(os.path.join(pkg_path, "package.toml"), "w", encoding="utf-8") as f:
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

        self.assertIn("✨ Successfully rendered all enabled packages!", stdout.getvalue())

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

        self.assertIn("✨ Successfully rendered package 'pkg_b'!", stdout.getvalue())

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
        self.assertIn("Package directory does not exist", stderr.getvalue())

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

        self.assertIn("✨ Successfully rendered package 'pkg_a'!", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
