import unittest
import os
import io
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
from drift.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
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

    def test_diff_managed_config_files(self):
        """Verifies changes to drift_package.toml appear in Template and Pending diffs."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("content\n")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy and commit
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # 2. Modify drift_package.toml in src/
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="stow"\n')

        # 3. Diff A (Template Evolution) should show change in drift_package.toml
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.TEMPLATE)
            out = stdout.getvalue()
            self.assertIn("drift_package.toml", out)
            self.assertIn("install_method", out)
            self.assertIn("stow", out)

        # 4. Diff Δ (Pending Delta) should show change in drift_package.toml
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING)
            out = stdout.getvalue()
            self.assertIn("drift_package.toml", out)
            self.assertIn("install_method", out)
            self.assertIn("stow", out)

    def test_diff_stow_local_ignore_excluded(self):
        """Verifies synthetic .stow-local-ignore in install/ is not reported as deleted in Pending diff."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("content\n")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy and commit (generates .stow-local-ignore in install/)
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # Verify .stow-local-ignore exists in install/ but not in render/
        self.assertTrue((self.install_dir / pkg / ".stow-local-ignore").exists())
        self.assertFalse((self.render_dir / pkg / ".stow-local-ignore").exists())

        # 2. Diff Δ should be completely empty (no false positive deletion of .stow-local-ignore)
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING)
            out = stdout.getvalue()
            self.assertNotIn(".stow-local-ignore", out)

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

    @patch("drift.workspace_diff.launch_side_by_side_editor")
    def test_diff_side_by_side_template_evolution(self, mock_launch: MagicMock) -> None:
        """Verifies run_primitive_diff with side_by_side=True extracts HEAD and calls launch_side_by_side_editor."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("initial content")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.result_models import DiffType

        # 1. Render and commit
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")

        # 2. Modify template
        (pkg_src_dir / "file.txt").write_text("modified content")

        captured_pairs = []

        def side_effect(pairs):
            for l, r in pairs:
                captured_pairs.append((l.read_text(), r))

        mock_launch.side_effect = side_effect

        # 3. Run Diff A with side_by_side=True
        run_primitive_diff(self.workspace_config, diff_type=DiffType.TEMPLATE, side_by_side=True)
        mock_launch.assert_called_once()
        self.assertEqual(len(captured_pairs), 1)
        left_content, right = captured_pairs[0]
        self.assertEqual(right, self.render_dir / pkg / "file.txt")
        self.assertEqual(left_content, "initial content")

    @patch("drift.workspace_diff.launch_side_by_side_editor")
    def test_diff_side_by_side_pending_delta(self, mock_launch: MagicMock) -> None:
        """Verifies run_primitive_diff with side_by_side=True pairs install and render files."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content v1")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # 2. Modify Template
        (pkg_src_dir / "file.txt").write_text("content v2")

        # 3. Run Diff Δ with side_by_side=True
        run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING, side_by_side=True)
        mock_launch.assert_called_once()
        pairs = mock_launch.call_args[0][0]
        self.assertEqual(len(pairs), 1)
        left, right = pairs[0]
        self.assertEqual(left, self.install_dir / pkg / "file.txt")
        self.assertEqual(right, self.render_dir / pkg / "file.txt")

    def test_diff_side_by_side_unset_editor_raises_error(self) -> None:
        """Verifies run_primitive_diff with side_by_side=True raises RuntimeError if EDITOR is unset."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"')
        (pkg_src_dir / "file.txt").write_text("content v1")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # 2. Modify Template so diff exists
        (pkg_src_dir / "file.txt").write_text("content v2")

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING, side_by_side=True)
            self.assertIn("Environment variable $VISUAL or $EDITOR is not set", str(ctx.exception))


    def test_diff_excludes_temporary_files_in_pending(self) -> None:
        """Verifies editor temporary and OS metadata files are excluded from Pending diff."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("v1\n")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # 2. Add editor temp files in install and render
        (self.install_dir / pkg / "#file.txt#").write_text("emacs auto save")
        (self.install_dir / pkg / ".#file.txt").write_text("emacs lock")
        (self.install_dir / pkg / "file.txt~").write_text("backup")
        (self.install_dir / pkg / ".file.txt.swp").write_text("vim swap")
        (self.install_dir / pkg / ".DS_Store").write_text("os metadata")

        # 3. Modify actual file
        (pkg_src_dir / "file.txt").write_text("v2\n")

        # 4. Run Diff Δ
        with io.StringIO() as stdout, patch("sys.stdout", stdout):
            run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING)
            out = stdout.getvalue()
            self.assertIn("v2", out)
            self.assertNotIn("#file.txt#", out)
            self.assertNotIn(".#file.txt", out)
            self.assertNotIn("file.txt~", out)
            self.assertNotIn(".file.txt.swp", out)
            self.assertNotIn(".DS_Store", out)

    @patch("drift.workspace_diff.launch_side_by_side_editor")
    def test_diff_side_by_side_excludes_temporary_files(self, mock_launch: MagicMock) -> None:
        """Verifies side-by-side diff pairs exclude editor temporary and OS metadata files."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / "drift_package.toml").write_text(f'[package]\nname="{pkg}"\ninstall_method="copy"\n')
        (pkg_src_dir / "file.txt").write_text("v1\n")

        from drift.render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
        from drift.result_models import DiffType

        # 1. Full Deploy
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_3_commit_render_repo(self.workspace_config, "initial render")
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        run_primitive_6_commit_install_repo(self.workspace_config, "initial install")

        # 2. Add editor temp files in install/
        (self.install_dir / pkg / "#file.txt#").write_text("emacs auto save")
        (self.install_dir / pkg / ".DS_Store").write_text("os metadata")

        # 3. Modify template
        (pkg_src_dir / "file.txt").write_text("v2\n")

        # 4. Run Diff Δ with side_by_side=True
        run_primitive_diff(self.workspace_config, diff_type=DiffType.PENDING, side_by_side=True)
        mock_launch.assert_called_once()
        pairs = mock_launch.call_args[0][0]
        self.assertEqual(len(pairs), 1)
        left, right = pairs[0]
        self.assertEqual(left, self.install_dir / pkg / "file.txt")
        self.assertEqual(right, self.render_dir / pkg / "file.txt")


if __name__ == "__main__":
    unittest.main()

