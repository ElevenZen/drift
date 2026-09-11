import unittest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.workspace_status import run_primitive_status

class TestStatus(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"
        
        for d in [self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir,
            ),
            packages_enable={"pkg_a": True}
        )
        
        # Initialize Git in render and install
        for d in [self.render_dir, self.install_dir]:
            subprocess.run(["git", "init"], cwd=str(d), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(d), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(d), capture_output=True, check=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_status_clean(self):
        """Verifies status is CLEAN when everything is in sync."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")
        
        # 1. Initial render, stage, apply
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.render_package import run_primitive_3_commit_render_repo
        
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")
        
        # 2. Run status
        results = run_primitive_status(self.workspace_config)
        
        self.assertEqual(len(results), 1)
        s = results[0]
        self.assertEqual(s.name, pkg)
        self.assertEqual(s.pending_status, "CLEAN")

    def test_status_modified_drifted_staged(self):
        """Verifies status correctly detects Template Modified, System Drifted, and Pending Staged."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("original content")
        
        # Initial state setup
        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")
        
        # 1. Modify src (Template Status A should become MODIFIED after status runs render)
        (pkg_src_dir / "file.txt").write_text("src modified content")
        
        # 2. Modify system (System Status B should become DRIFTED after status runs reverse-sync)
        (self.system_target_dir / "file.txt").write_text("system drifted content")
        
        # 3. Modify render (manually to simulate a pending stage change that was already rendered but not staged)
        # Actually, status runs render, so src modification will already update render.
        # So Pending Delta will see render (src content) vs install (original content).
        
        # 4. Run status
        results = run_primitive_status(self.workspace_config)
        
        self.assertEqual(len(results), 1)
        s = results[0]
        self.assertEqual(s.name, pkg)
        self.assertEqual(s.template_status, "MODIFIED")
        self.assertEqual(s.system_status, "DRIFTED")
        self.assertEqual(s.pending_status, "STAGED")
        
        # 5. Verify formatting methods
        text = results.format_text()
        self.assertIn("Package: pkg_a", text)
        self.assertIn("[A] Template: MODIFIED", text)
        self.assertIn("[B] System:   DRIFTED", text)
        self.assertIn("[Δ] Pending:  STAGED", text)

        status_model = results.to_status_result()
        self.assertEqual(status_model.overall_status, "DRIFTED")
        self.assertEqual(len(status_model.packages), 1)

        diff_model = results.to_diff_result()
        self.assertEqual(diff_model.command, "diff")
        self.assertEqual(len(diff_model.packages), 1)
        self.assertTrue(diff_model.packages[0].has_changes)

    def test_status_managed_config_files(self):
        """Verifies changing drift_package.toml marks template MODIFIED and pending STAGED."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("content\n")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo

        # Initial full deployment and clean state
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # Clean check
        clean_res = run_primitive_status(self.workspace_config)
        self.assertEqual(clean_res[0].template_status, "CLEAN")
        self.assertEqual(clean_res[0].pending_status, "CLEAN")

        # Modify drift_package.toml
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="stow"\n')

        # Status check
        modified_res = run_primitive_status(self.workspace_config)
        self.assertEqual(modified_res[0].template_status, "MODIFIED")
        self.assertEqual(modified_res[0].pending_status, "STAGED")
        self.assertTrue(any("drift_package.toml" in str(p) for p in modified_res[0].pending_changes.modified))

    def test_status_empty(self):
        """Verifies empty workspace status format."""
        from drift.workspace_status import WorkspaceStatusResult
        empty_res = WorkspaceStatusResult(packages=[])
        self.assertEqual(len(empty_res), 0)
        self.assertEqual(empty_res.format_text(), "")
        self.assertEqual(empty_res.overall_status, "CLEAN")

if __name__ == "__main__":
    unittest.main()
