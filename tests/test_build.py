import os
import sys
import tempfile
import unittest
import subprocess
import zipapp
from pathlib import Path


class TestBuildArtifacts(unittest.TestCase):
    """Verifies that Drift can be packaged into standalone distribution artifacts and executed cleanly."""

    def test_zipapp_packaging_and_execution(self) -> None:
        """Verifies that zipapp bundles drift correctly and executes CLI commands end-to-end."""
        repo_root = Path(__file__).resolve().parent.parent
        src_dir = repo_root / "src"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            zipapp_path = temp_path / "drift_test_app"

            # 1. Create zipapp
            zipapp.create_archive(
                source=src_dir,
                target=zipapp_path,
                interpreter="/usr/bin/env python3",
                main="drift.cli:main"
            )
            self.assertTrue(zipapp_path.exists())
            zipapp_path.chmod(0o755)

            # 2. Test execution of --help via zipapp without PYTHONPATH
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env["GIT_AUTHOR_NAME"] = "Drift Test"
            env["GIT_AUTHOR_EMAIL"] = "test@drift.local"
            env["GIT_COMMITTER_NAME"] = "Drift Test"
            env["GIT_COMMITTER_EMAIL"] = "test@drift.local"

            res_help = subprocess.run(
                [sys.executable, str(zipapp_path), "--help"],
                capture_output=True,
                text=True,
                env=env
            )
            self.assertEqual(res_help.returncode, 0)
            self.assertIn("drift: Decoupled Two-Stage Git-Backed Dotfiles Manager", res_help.stdout)

            # 3. Test help documentation loading from inside zipapp
            res_doc = subprocess.run(
                [sys.executable, str(zipapp_path), "help", "drift.toml"],
                capture_output=True,
                text=True,
                env=env
            )
            self.assertEqual(res_doc.returncode, 0)
            self.assertIn("drift.toml Complete Global Configuration Reference", res_doc.stdout)

            # 4. Test end-to-end workspace initialization, package creation, and deployment
            workspace_dir = temp_path / "workspace"
            target_dir = temp_path / "target"
            workspace_dir.mkdir()
            target_dir.mkdir()

            res_init = subprocess.run(
                [sys.executable, str(zipapp_path), "init"],
                cwd=str(workspace_dir),
                capture_output=True,
                text=True,
                env=env
            )
            self.assertEqual(res_init.returncode, 0)
            self.assertTrue((workspace_dir / "config" / "drift.toml").exists())

            res_new = subprocess.run(
                [sys.executable, str(zipapp_path), "new", "test_pkg", "--target", str(target_dir)],
                cwd=str(workspace_dir),
                capture_output=True,
                text=True,
                env=env
            )
            self.assertEqual(res_new.returncode, 0)
            self.assertTrue((workspace_dir / "src" / "test_pkg" / "drift_package.toml").exists())

            # Add configuration file and deploy
            (workspace_dir / "src" / "test_pkg" / "app.conf").write_text("setting = 42\n", encoding="utf-8")

            res_deploy = subprocess.run(
                [sys.executable, str(zipapp_path), "deploy", "test_pkg"],
                cwd=str(workspace_dir),
                capture_output=True,
                text=True,
                env=env
            )
            self.assertEqual(res_deploy.returncode, 0)
            self.assertTrue((target_dir / "app.conf").exists())
            self.assertEqual((target_dir / "app.conf").read_text(encoding="utf-8"), "setting = 42\n")


if __name__ == "__main__":
    unittest.main()
