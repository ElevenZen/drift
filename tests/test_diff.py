import unittest
import os
import io
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch
from drift.workspace_config import WorkspaceConfig
from drift.workspace_diff import run_primitive_diff

# Disable interactive pagers during tests to prevent blocking and pop-up windows.
os.environ["PAGER"] = "cat"
os.environ["GIT_PAGER"] = "cat"

class TestDiff(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"
        
        for d in [self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.system_target_dir,
            packages_enable={"pkg_a": True}
        )
        
        # Initialize Git in render and install
        for d in [self.render_dir, self.install_dir]:
            subprocess.run(["git", "init"], cwd=str(d), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(d), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(d), capture_output=True, check=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_diff_template(self):
        """Verifies Diff A (Template Evolution)."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("initial content")
        
        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        
        # 1. Render and Commit
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        
        # 2. Modify Template
        (pkg_src_dir / "file.txt").write_text("modified content")
        
        from drift.result_models import DiffType
        # 3. Run Diff A
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.TEMPLATE)
            self.assertIn("modified content", stdout.getvalue())

    def test_diff_system(self):
        """Verifies Diff B (System Drift)."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")
        
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType
        
        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")
        
        # 2. Drift System
        (self.system_target_dir / "file.txt").write_text("drifted content")
        
        # 3. Run Diff B
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.SYSTEM)
            self.assertIn("drifted content", stdout.getvalue())

    def test_diff_pending(self):
        """Verifies Diff Δ (Pending Delta)."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")
        
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType
        
        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")
        
        # 2. Modify Template (Ready to be staged/deployed)
        (pkg_src_dir / "file.txt").write_text("new version content")
        
        # 3. Run Diff Δ
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING)
            self.assertIn("new version content", stdout.getvalue())

    def test_diff_enum_types(self):
        """Verifies run_primitive_diff accepts DiffType enum members."""
        from drift.result_models import DiffType
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING)
            run_primitive_diff(self.workspace_config, diff_type=DiffType.TEMPLATE)
            run_primitive_diff(self.workspace_config, diff_type=DiffType.SYSTEM)

    def test_invalid_diff_type_casting(self):
        """Verifies invalid diff_type strings fail with ValueError during cast."""
        from drift.cli.actions import execute_diff
        with self.assertRaises(ValueError):
            execute_diff(self.drift_root, diff_type="invalid_type")

if __name__ == "__main__":
    unittest.main()
