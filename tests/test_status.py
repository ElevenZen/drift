import unittest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.primitives.workspace_status import run_primitive_status

class TestStatus(unittest.TestCase):
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
        from drift.render.render_package import run_primitive_2_render_packages
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.install_repo import run_primitive_5_install, run_primitive_6_commit_install_repo
        from drift.render.render_package import run_primitive_3_commit_render_repo
        
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install(self.workspace_config)
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
        from drift.render.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.install_repo import run_primitive_5_install, run_primitive_6_commit_install_repo
        
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install(self.workspace_config)
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
        self.assertIn("Enabled Packages: pkg_a", text)
        self.assertIn("Package: pkg_a", text)
        self.assertIn("State:    installed", text)
        self.assertIn("Target:   ", text)
        self.assertIn("Method:   copy", text)
        self.assertIn("[A] Template: MODIFIED", text)
        self.assertIn("[B] System:   DRIFTED", text)
        self.assertIn("[Δ] Pending:  STAGED", text)

        self.assertEqual(results.overall_status, "DRIFTED")
        self.assertEqual(len(results.packages), 1)

        from drift.primitives.workspace_diff import run_primitive_15_workspace_diff
        from drift.core.result_models import DiffType
        diff_res = run_primitive_15_workspace_diff(self.workspace_config, diff_type=DiffType.PENDING, quiet=True)
        self.assertEqual(diff_res.command, "diff")
        self.assertEqual(len(diff_res.packages), 1)
        self.assertTrue(diff_res.packages[0].has_changes)

    def test_status_managed_config_files(self):
        """Verifies changing drift_package.toml marks template MODIFIED and pending STAGED."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("content\n")

        from drift.render.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.install_repo import run_primitive_5_install, run_primitive_6_commit_install_repo

        # Initial full deployment and clean state
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # Clean check
        clean_res = run_primitive_status(self.workspace_config)
        self.assertEqual(clean_res[0].template_status, "CLEAN")
        self.assertEqual(clean_res[0].pending_status, "CLEAN")

        # Modify drift_package.toml
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="symlink"\n')

        # Status check
        modified_res = run_primitive_status(self.workspace_config)
        self.assertEqual(modified_res[0].template_status, "MODIFIED")
        self.assertEqual(modified_res[0].pending_status, "STAGED")
        self.assertIsNotNone(modified_res[0].pending_changes)
        self.assertTrue(any("drift_package.toml" in str(p) for p in modified_res[0].pending_changes.modified))

    def test_status_list_only(self):
        """Verifies status list_only (-l, --list) returns fast metadata without render/compare."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")

        from drift.render.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.install_repo import run_primitive_5_install, run_primitive_6_commit_install_repo

        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # Run status with list_only=True
        res = run_primitive_status(self.workspace_config, list_only=True)
        self.assertEqual(res.overall_status, "UNKNOWN")
        self.assertTrue(res.list_only)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].state, "installed")
        self.assertEqual(res[0].install_method, "copy")
        self.assertEqual(res[0].template_status, "UNKNOWN")
        self.assertEqual(res[0].system_status, "UNKNOWN")
        self.assertEqual(res[0].pending_status, "UNKNOWN")
        self.assertIsNotNone(res[0].last_deployed)

        text = res.format_text()
        self.assertIn("Enabled Packages: pkg_a", text)
        self.assertIn("Package: pkg_a", text)
        self.assertIn("State:    installed", text)
        self.assertIn("Method:   copy", text)
        self.assertIn("Deployed:", text)
        self.assertNotIn("[A] Template:", text)

    def test_status_new_template(self):
        """Verifies status detects a brand new untracked package as Template: NEW and Pending: NEW."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")

        res = run_primitive_status(self.workspace_config)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].template_status, "NEW")
        self.assertEqual(res[0].pending_status, "NEW")
        self.assertIsNone(res[0].template_changes)

        text = res.format_text()
        self.assertIn("[A] Template: NEW", text)
        self.assertIn("[Δ] Pending:  NEW", text)

    def test_status_empty(self):
        """Verifies empty workspace status format."""
        from drift.core.result_models import StatusResult
        empty_res = StatusResult(packages=[])
        self.assertEqual(len(empty_res), 0)
        self.assertEqual(empty_res.format_text(), "")
        self.assertEqual(empty_res.overall_status, "CLEAN")

    def test_execute_status_fails_fast_when_workspace_structure_broken(self) -> None:
        """Verifies execute_status fails with ConfigError and hints 'drift repair' when workspace structure is broken."""
        from drift.cli.actions import execute_status
        from drift.core.constants import PACKAGE_CONFIG_FILE_NAME
        from drift.core.exceptions import ConfigError
        from drift.primitives.workspace_init import init_drift_workspace

        init_drift_workspace(self.drift_root, force=True)

        # Put a legacy root metadata file in render/pkg_a
        pkg_render = self.render_dir / "pkg_a"
        pkg_render.mkdir(parents=True, exist_ok=True)
        (pkg_render / PACKAGE_CONFIG_FILE_NAME).write_text("[package]\n", encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            execute_status(self.drift_root)
        self.assertIn("drift repair", str(ctx.exception))
        self.assertIn("Package Metadata Structure", str(ctx.exception))

    def test_pending_delta_status_filters_out_concrete_directories(self) -> None:
        """Verifies pending delta status counts only file changes and ignores intermediate directories."""
        from drift.primitives.workspace_status import calculate_pending_delta
        from drift.core.result_models import PackageStatus

        pkg = "pkg_a"
        render_pkg = self.render_dir / pkg
        install_pkg = self.install_dir / pkg

        # Setup install_pkg with an initial file
        install_pkg.mkdir(parents=True, exist_ok=True)
        (install_pkg / "base.txt").write_text("base\n")

        # Setup render_pkg with the base file plus a nested directory containing one file
        render_pkg.mkdir(parents=True, exist_ok=True)
        (render_pkg / "base.txt").write_text("base\n")
        nested_file = render_pkg / "nested" / "sub" / "file.txt"
        nested_file.parent.mkdir(parents=True, exist_ok=True)
        nested_file.write_text("nested content\n")

        status, diff = calculate_pending_delta(render_pkg, install_pkg)
        self.assertEqual(status, "STAGED")
        self.assertIsNotNone(diff)
        assert diff is not None
        # Exactly 1 added file, directory entries 'nested' and 'nested/sub' are excluded
        self.assertEqual(diff.added, [Path("nested/sub/file.txt")])
        self.assertEqual(diff.modified, [])
        self.assertEqual(diff.deleted, [])

        pkg_status = PackageStatus(name=pkg, pending_status=status, pending_changes=diff)
        formatted = pkg_status.format_text()
        self.assertIn("(+1, ~0, -0 files)", formatted)

    def test_pending_delta_status_filters_out_temporary_files(self) -> None:
        """Verifies pending delta status ignores editor temporary/swap/backup and OS metadata files."""
        from drift.primitives.workspace_status import calculate_pending_delta

        pkg = "pkg_a"
        render_pkg = self.render_dir / pkg
        install_pkg = self.install_dir / pkg

        install_pkg.mkdir(parents=True, exist_ok=True)
        (install_pkg / "base.txt").write_text("base\n")
        (install_pkg / ".DS_Store").write_text("os metadata\n")

        render_pkg.mkdir(parents=True, exist_ok=True)
        (render_pkg / "base.txt").write_text("base\n")
        (render_pkg / "#file.txt#").write_text("emacs auto-save\n")
        (render_pkg / "notes.txt~").write_text("backup file\n")

        # Only temp files differ between render and install, so pending status should be CLEAN
        status, diff = calculate_pending_delta(render_pkg, install_pkg)
        self.assertEqual(status, "CLEAN")
        self.assertIsNone(diff)

    def test_status_ignores_render_lockfile(self) -> None:
        """Verifies drift status ignores render_lock.json in render/ and install/ repositories."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content")

        from drift.render.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.primitives.install_repo import run_primitive_5_install, run_primitive_6_commit_install_repo
        from drift.core.constants import RENDER_LOCK_FILE_NAME, DRIFT_INTERNAL_DIR_NAME

        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # Initial status should be clean
        res = run_primitive_status(self.workspace_config)
        self.assertEqual(res.overall_status, "CLEAN")
        self.assertEqual(res[0].template_status, "CLEAN")
        self.assertEqual(res[0].pending_status, "CLEAN")

        # Manually alter render_lock.json in render/ and install/
        render_lock = self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME
        render_lock.parent.mkdir(parents=True, exist_ok=True)
        render_lock.write_text('{"nodes": {"changed": 1}}\n')

        install_lock = self.install_dir / pkg / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME
        install_lock.parent.mkdir(parents=True, exist_ok=True)
        install_lock.write_text('{"nodes": {"changed": 2}}\n')

        # Status should remain CLEAN because lockfile is excluded from diff/status
        res2 = run_primitive_status(self.workspace_config)
        self.assertEqual(res2.overall_status, "CLEAN")
        self.assertEqual(res2[0].template_status, "CLEAN")
        self.assertEqual(res2[0].pending_status, "CLEAN")
        self.assertEqual(res2[0].system_status, "CLEAN")

        # If a package in git only contains render_lock.json, it should be audited as NEW
        from drift.primitives.workspace_status import audit_repo_package_status
        (self.render_dir / "pkg_only_lock" / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (self.render_dir / "pkg_only_lock" / DRIFT_INTERNAL_DIR_NAME / RENDER_LOCK_FILE_NAME).write_text("{}\n")
        subprocess.run(["git", "-C", str(self.render_dir), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.render_dir), "commit", "-m", "add lock only"], capture_output=True, check=True)
        status, diff = audit_repo_package_status(self.render_dir, "pkg_only_lock", "MODIFIED")
        self.assertEqual(status, "NEW")
        self.assertIsNone(diff)

        # If a package contains a user file named render_lock.json outside .drift/, it is NOT treated as NEW
        (self.render_dir / "pkg_user_lock" / "my_config").mkdir(parents=True, exist_ok=True)
        (self.render_dir / "pkg_user_lock" / "my_config" / RENDER_LOCK_FILE_NAME).write_text("{}\n")
        subprocess.run(["git", "-C", str(self.render_dir), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.render_dir), "commit", "-m", "add user lock"], capture_output=True, check=True)
        status, diff = audit_repo_package_status(self.render_dir, "pkg_user_lock", "MODIFIED")
        self.assertEqual(status, "CLEAN")
        self.assertIsNone(diff)


if __name__ == "__main__":
    unittest.main()
