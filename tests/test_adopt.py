"""Unit tests for Primitive 7 & Stage 1: drift adopt."""

import os
import sys
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from io import StringIO
from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DRIFT_KEEP_FILE_NAME,
    set_test_mode,
)
from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.config.render_engine_config import RenderEngineRegistry
from drift.hooks.lifecycle_hooks import HookExecFlags
from drift.primitives.adopt_repo import (
    AdoptOptions,
    AdoptPlan,
    AdoptStatus,
    PackageAdoptPlan,
    get_drifted_packages,
    assert_source_file_clean,
    generate_unified_patch,
    check_patch_conflicts,
    apply_source_patch,
    test_file_conflict,
    resolve_source_file_path,
    adopt_addition,
    ignore_addition,
    adopt_deletion,
    patch_and_edit,
    adopt_rename,
    fallback_over_render,
    fallback_side_by_side,
    adopt_one_package_drifts,
    run_primitive_adopt_drifts,
    prepare_adopt,
    execute_adopt,
    plan_package_adopt,
    execute_package_adopt,
    handle_single_addition,
    handle_single_deletion,
)

set_test_mode(True)


class TestAdopt(unittest.TestCase):

    def setUp(self) -> None:
        set_test_mode(True)
        self.stdout_patcher = patch("sys.stdout", StringIO())
        self.stderr_patcher = patch("sys.stderr", StringIO())
        self.stdout_patcher.start()
        self.stderr_patcher.start()

        self.temp_dir = TemporaryDirectory()
        self.workspace_path = (Path(self.temp_dir.name) / "workspace").resolve()
        self.workspace_path.mkdir()

        self.src_dir = self.workspace_path / "src"
        self.src_dir.mkdir()
        
        self.install_dir = self.workspace_path / "install"
        self.install_dir.mkdir()

        self.backup_dir = self.workspace_path / "backup"
        self.backup_dir.mkdir()

        # Initialize install repo as a git repository
        subprocess.run(["git", "init"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.install_dir, check=True, capture_output=True)
        # Commit a dummy state.toml to establish HEAD
        with open(self.install_dir / "state.toml", "w", encoding="utf-8") as f:
            f.write("# dummy")
        subprocess.run(["git", "add", "state.toml"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=self.install_dir, check=True, capture_output=True)

        # Initialize main workspace repo as a git repository (for cleanliness check)
        subprocess.run(["git", "init"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.workspace_path, check=True, capture_output=True)
        # Commit a dummy file
        with open(self.workspace_path / ".gitignore", "w", encoding="utf-8") as f:
            f.write("install/\nbackup/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial workspace commit"], cwd=self.workspace_path, check=True, capture_output=True)

        config_dir = self.workspace_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        from drift.config.render_engine_config import RenderEngineConfig
        from drift.config.workspace_config import WorkspaceSectionConfig
        env_engine = RenderEngineConfig(
            name="envsubst",
            input_file=config_dir / "envsubst.bash",
            suffix="envst",
            render_command=["bash", "-c", "source %i && envsubst < %s"]
        )
        self.workspace_config = WorkspaceConfig(
            drift_root=self.workspace_path,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.workspace_path / "system_home",
            ),
            packages_enable={},
            render_engine_configs=RenderEngineRegistry({"envsubst": env_engine})
        )

    def tearDown(self) -> None:
        self.stdout_patcher.stop()
        self.stderr_patcher.stop()
        set_test_mode(True)
        self.temp_dir.cleanup()

    def test_get_drifted_packages(self) -> None:
        # Create a package source folder
        pkg = "pkg_a"
        (self.src_dir / pkg).mkdir(parents=True, exist_ok=True)

        # Before any uncommitted files in install/, drifted list should be empty
        self.assertEqual(get_drifted_packages(self.workspace_config), [])

        # Create an uncommitted file inside install/pkg_a
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "file.txt").write_text("drift", encoding="utf-8")

        # Now get_drifted_packages should detect pkg_a
        self.assertEqual(get_drifted_packages(self.workspace_config), [pkg])

        # Create modifications to root metadata files in install/ (state.toml, .gitignore)
        (self.install_dir / ".gitignore").write_text("*.swp\n", encoding="utf-8")
        (self.install_dir / "state.toml").write_text("# modified\n", encoding="utf-8")

        # Root metadata files must not appear in drifted packages
        self.assertEqual(get_drifted_packages(self.workspace_config), [pkg])

    def test_assert_source_file_clean(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        # Non-existent file should pass
        non_existent = src_pkg_dir / "non_existent.txt"
        assert_source_file_clean(non_existent, force=False, pkg=pkg)

        # Clean tracked file should pass
        clean_file = src_pkg_dir / "clean.txt"
        clean_file.write_text("tracked content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add clean.txt"], cwd=self.workspace_path, check=True, capture_output=True)
        assert_source_file_clean(clean_file, force=False, pkg=pkg)

        # Modify file to make it dirty
        clean_file.write_text("dirty uncommitted change", encoding="utf-8")

        # Clean check should raise DirtySourceFileError (SafeguardError & RuntimeError) with guidance message
        from drift.core.exceptions import DirtySourceFileError, SafeguardError

        with self.assertRaises(DirtySourceFileError) as ctx:
            assert_source_file_clean(clean_file, force=False, pkg=pkg)
        self.assertIsInstance(ctx.exception, SafeguardError)
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertEqual(ctx.exception.packages, [pkg])
        self.assertEqual(ctx.exception.source_file, clean_file)
        self.assertIn("has uncommitted local modifications", str(ctx.exception))
        self.assertIn(f"drift adopt {pkg} --force", str(ctx.exception))

        # Passing force=True should bypass check without error
        assert_source_file_clean(clean_file, force=True, pkg=pkg)

    def test_plan_package_adopt(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Setup a tracked file in Git HEAD
        tracked_file = pkg_install_dir / "tracked.txt"
        tracked_file.write_text("original content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Tracked files"], cwd=self.install_dir, check=True, capture_output=True)

        # Setup a Deletion
        deleted_file = pkg_install_dir / "deleted.txt"
        deleted_file.write_text("will be deleted", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "To delete"], cwd=self.install_dir, check=True, capture_output=True)
        deleted_file.unlink()

        # Setup an Addition (Untracked) at the very end
        (pkg_install_dir / "added.txt").write_text("new content", encoding="utf-8")

        # Modify tracked file
        tracked_file.write_text("modified content", encoding="utf-8")

        # Run drift extraction
        plan = plan_package_adopt(self.workspace_config, pkg)
        self.assertEqual(plan.additions, [Path("added.txt")])
        self.assertEqual(plan.deletions, [Path("deleted.txt")])
        self.assertEqual(plan.modifications, [Path("tracked.txt")])
        self.assertEqual(plan.renames, [])

    def test_plan_package_adopt_rename(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup tracked file
        orig_file = pkg_install_dir / "old_name.txt"
        orig_file.write_text("hello", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Perform git rename
        subprocess.run(["git", "mv", "pkg_a/old_name.txt", "pkg_a/new_name.txt"], cwd=self.install_dir, check=True, capture_output=True)

        # Get package drifts
        plan = plan_package_adopt(self.workspace_config, pkg)

        # Rename should be detected as a distinct rename branch
        self.assertEqual(plan.deletions, [])
        self.assertEqual(plan.additions, [])
        self.assertEqual(plan.modifications, [])
        self.assertEqual(plan.renames, [(Path("old_name.txt"), Path("new_name.txt"))])

    def test_apply_source_patch_clean(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Create original file and commit
        tracked_file = pkg_install_dir / "file.txt"
        tracked_file.write_text("line 1\nline 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "original file"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Modify file in install base
        tracked_file.write_text("line 1 modified\nline 2\n", encoding="utf-8")

        # 3. Create original file in src/
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        src_file = src_pkg_dir / "file.txt"
        src_file.write_text("line 1\nline 2\n", encoding="utf-8")

        # 4. Generate diff & apply patch
        patch_content = generate_unified_patch(self.install_dir, Path(pkg) / "file.txt")
        self.assertTrue(bool(patch_content.strip()))

        success = apply_source_patch(src_file, patch_content, accept_conflicts=False)
        self.assertTrue(success)
        self.assertEqual(src_file.read_text(encoding="utf-8"), "line 1 modified\nline 2\n")

    def test_resolve_source_file_path(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        render_engines = self.workspace_config.render_engine_configs

        # Test for a file that does not exist yet
        rel_path = Path("dot-bashrc")
        resolved = resolve_source_file_path(render_engines, src_pkg_dir, rel_path)
        self.assertIsNone(resolved) # Does not exist yet

        # Create static file dot-bashrc
        static_file = src_pkg_dir / "dot-bashrc"
        static_file.write_text("static", encoding="utf-8")
        resolved = resolve_source_file_path(render_engines, src_pkg_dir, rel_path)
        self.assertEqual(resolved, static_file)

        # Clean static, test template resolution (dot-bashrc -> dot-bashrc.envst)
        static_file.unlink()
        template_file = src_pkg_dir / "dot-bashrc.envst"
        template_file.write_text("templated", encoding="utf-8")
        resolved = resolve_source_file_path(render_engines, src_pkg_dir, rel_path)
        self.assertEqual(resolved, template_file)

    def test_adopt_addition(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Create wild addition in FCD (dot-config/qBittorrent/config.ini)
        rel_path = Path("dot-config/qBittorrent/config.ini")
        install_file = pkg_install_dir / rel_path
        install_file.parent.mkdir(parents=True, exist_ok=True)
        install_file.write_text("some configuration", encoding="utf-8")

        # Run adopt_addition
        adopt_addition(src_pkg_dir, pkg_install_dir, rel_path)

        # Symmetrically converted to dot-config/... in src/ pkg dir
        expected_src = src_pkg_dir / "dot-config/qBittorrent/config.ini"
        self.assertTrue(expected_src.exists())
        self.assertEqual(expected_src.read_text(encoding="utf-8"), "some configuration")

    def test_ignore_addition(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Create wild addition
        rel_path = Path("untracked.txt")
        install_file = pkg_install_dir / rel_path
        install_file.write_text("some file", encoding="utf-8")

        # Create initial empty .drift_ignore
        (src_pkg_dir / ".drift_ignore").write_text("# ignore config", encoding="utf-8")

        ignore_addition(src_pkg_dir, pkg_install_dir, rel_path)

        # File is unlinked from install/
        self.assertFalse(install_file.exists())

        # Pattern added to .drift_ignore
        ignore_content = (src_pkg_dir / ".drift_ignore").read_text(encoding="utf-8")
        self.assertIn("untracked.txt", ignore_content)

    def test_adopt_deletion(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        # Create file in source folder
        rel_path = Path("dot-bashrc")
        src_file = src_pkg_dir / "dot-bashrc"
        src_file.write_text("bash", encoding="utf-8")

        # Run adopt deletion
        adopt_deletion(self.workspace_config.render_engine_configs, src_pkg_dir, rel_path, pkg=pkg)

        # File should be removed from src/
        self.assertFalse(src_file.exists())

    def test_adopt_rename(self) -> None:
        """Verifies that adopting a rename correctly renames the template file inside src/ and applies any content diff."""
        from drift.primitives.adopt_repo import adopt_rename

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup old template in src/ (matching dot- prefix)
        src_old_file = src_pkg_dir / "dot-old_name.envst.txt"
        src_old_file.write_text("template content\ntemplate content\ntemplate content\n", encoding="utf-8")

        # 2. Setup old file in install/ (matching repo format) and commit
        install_old_file = pkg_install_dir / "dot-old_name.txt"
        install_old_file.write_text("template content\ntemplate content\ntemplate content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=self.install_dir, check=True, capture_output=True)

        # 3. Rename and modify file inside install/
        subprocess.run(["git", "mv", "pkg_a/dot-old_name.txt", "pkg_a/dot-new_name.txt"], cwd=self.install_dir, check=True, capture_output=True)
        (pkg_install_dir / "dot-new_name.txt").write_text("template content\ntemplate content\ntemplate content modified\n", encoding="utf-8")

        # 4. Adopt the rename
        from drift.primitives.adopt_repo import generate_adjusted_patch
        patch_content = generate_adjusted_patch(
            self.install_dir,
            pkg,
            Path("dot-new_name.txt"),
            old_rel_path=Path("dot-old_name.txt"),
            target_src_filename="dot-new_name.envst.txt"
        )
        adopt_rename(self.workspace_config.render_engine_configs, src_pkg_dir,
                     Path("dot-old_name.txt"), Path("dot-new_name.txt"), patch_content)

        # Verify old template is deleted, new template is created with dot- prefix and correct template suffixes, and modifications are applied!
        self.assertFalse(src_old_file.exists())
        
        src_new_file = src_pkg_dir / "dot-new_name.envst.txt"
        self.assertTrue(src_new_file.exists())
        self.assertEqual(src_new_file.read_text(encoding="utf-8"), "template content\ntemplate content\ntemplate content modified\n")

    def test_adopt_rename_missing_old_source_file(self) -> None:
        """Verifies that adopting a rename when the old source file does not exist correctly creates a new file and applies the full content."""
        from drift.primitives.adopt_repo import adopt_rename, generate_adjusted_patch

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup old file in install/ and commit, but NO template in src/
        install_old_file = pkg_install_dir / "dot-old_name.txt"
        install_old_file.write_text("template content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Rename and modify file inside install/
        subprocess.run(["git", "mv", "pkg_a/dot-old_name.txt", "pkg_a/dot-new_name.txt"], cwd=self.install_dir, check=True, capture_output=True)
        (pkg_install_dir / "dot-new_name.txt").write_text("template content\ntemplate content modified\n", encoding="utf-8")

        # 3. Generate the patch (with old_rel_path=None to represent addition)
        patch_content = generate_adjusted_patch(
            self.install_dir,
            pkg,
            Path("dot-new_name.txt"),
            old_rel_path=None,
            target_src_filename="dot-new_name.txt"
        )

        # 4. Adopt the rename
        adopt_rename(self.workspace_config.render_engine_configs, src_pkg_dir, Path("dot-old_name.txt"), Path("dot-new_name.txt"), patch_content)

        # Verify a new file is created with new name in src/ and contains full content
        src_new_file = src_pkg_dir / "dot-new_name.txt"
        self.assertTrue(src_new_file.exists())
        self.assertEqual(src_new_file.read_text(encoding="utf-8"), "template content\ntemplate content modified\n")

    def test_adopt_rename_rollback_on_patch_failure(self) -> None:
        """Verifies that when a patch fails during adopt_rename, new_src_file is unlinked and old_src_file remains untouched."""
        from drift.primitives.adopt_repo import adopt_rename

        pkg = "pkg_rollback"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        old_file = src_pkg_dir / "dot-old.txt"
        old_file.write_text("original content", encoding="utf-8")

        # An unmatchable/corrupt patch that will fail to apply
        invalid_patch = (
            "--- a/dot-old.txt\n"
            "+++ b/dot-new.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-nonexistent line that fails\n"
            "+replacement\n"
        )

        res = adopt_rename(
            self.workspace_config.render_engine_configs,
            src_pkg_dir,
            Path("dot-old.txt"),
            Path("dot-new.txt"),
            invalid_patch,
            has_patch_conflict=False,
            accept_conflicts=False,
        )

        self.assertIsNone(res)
        # Old file must remain intact
        self.assertTrue(old_file.exists())
        self.assertEqual(old_file.read_text(encoding="utf-8"), "original content")
        # New file must not exist
        self.assertFalse((src_pkg_dir / "dot-new.txt").exists())

    def test_adopt_addition_conflict_target_exists(self) -> None:
        """Verifies that non-interactive adopt skips addition if the target already exists in source."""
        from drift.primitives.adopt_repo import handle_single_addition

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # File exists in source package
        (src_pkg_dir / "dot-existing.txt").write_text("source content", encoding="utf-8")
        
        # File is also added in install pkg
        rel_path = Path("dot-existing.txt")
        (pkg_install_dir / "dot-existing.txt").write_text("install content", encoding="utf-8")

        # non-interactive handle_single_addition should return False due to collision conflict
        resolved = handle_single_addition(
            self.workspace_config.render_engine_configs,
            src_pkg_dir,
            src_pkg_dir,
            pkg_install_dir,
            rel_path,
            interactive=False
        )
        self.assertFalse(resolved)

    def test_adopt_deletion_target_missing(self) -> None:
        """Verifies that adopting deletion when target does not exist in source skips gracefully and returns True."""
        from drift.primitives.adopt_repo import handle_single_deletion

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # File does NOT exist in source, but is deleted in install pkg
        rel_path = Path("dot-missing.txt")

        resolved = handle_single_deletion(
            self.workspace_config.render_engine_configs,
            pkg,
            src_pkg_dir,
            rel_path,
            interactive=False
        )
        self.assertTrue(resolved)

    def test_adopt_rename_unstage_on_skip(self) -> None:
        """Verifies that if a rename is skipped or failed, both old and new paths get unstaged at the end."""
        from drift.primitives.adopt_repo import adopt_one_package_drifts

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup old template in src/ and committed file in install/
        src_old_file = src_pkg_dir / "dot-old.txt"
        src_old_file.write_text("old content", encoding="utf-8")
        install_old_file = pkg_install_dir / "dot-old.txt"
        install_old_file.write_text("old content", encoding="utf-8")
        
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init old"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Rename on disk in install/ (but target_existing_src will trigger a conflict in non-interactive mode!)
        install_new_file = pkg_install_dir / "dot-new.txt"
        install_old_file.rename(install_new_file)
        
        # Create dot-new.txt in src/ to cause a target collision conflict, forcing the rename to skip/fail
        src_new_file = src_pkg_dir / "dot-new.txt"
        src_new_file.write_text("collision", encoding="utf-8")

        # 3. Run adopt_one_package_drifts non-interactively
        adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))

        # 4. Check git status. Both pkg_a/dot-old.txt and pkg_a/dot-new.txt should be unstaged (not in staged index)
        # because the rename conflicted and was skipped, triggering selective git restore --staged.
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        # They should appear as unstaged (M, D, ?, etc.) but NOT staged (staged would be in the first column)
        # We can check that there are no staged changes (staged changes start with 'M ', 'D ', 'R ', 'A ', etc.)
        for line in res.stdout.splitlines():
            if len(line) >= 2:
                # The first character is for staged index changes. It should be empty/space or untracked '??'
                self.assertIn(line[0], [" ", "?"])

    def test_adopt_triggers_pre_source_hook_static(self) -> None:
        """Verifies that a static pre_source hook is triggered in src/pkg before adopt processing."""
        pkg = "pkg_adopt_static_hook"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = src_pkg_dir / DRIFT_HOOKS_DIR_NAME
        scripts_dir.mkdir()
        hook_script = scripts_dir / "prepare_src.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'STATIC_HOOK_RAN' > hook_executed.txt\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        pkg_toml = src_pkg_dir / "drift_package.toml"
        pkg_toml.write_text(
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"drift_hooks/prepare_src.sh\"\n",
            encoding="utf-8"
        )

        # Create a drift in install/
        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))

        # Hook must have run and generated hook_executed.txt in render pkg .drift/hooks dir
        hook_out = self.workspace_path / "render" / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "hook_executed.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), "STATIC_HOOK_RAN")

        # Copied static hook must exist in render/
        rendered_hook = self.workspace_path / "render" / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "prepare_src.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn("STATIC_HOOK_RAN", rendered_hook.read_text(encoding="utf-8"))

    def test_adopt_triggers_pre_source_hook_templated(self) -> None:
        """Verifies that a templated pre_source hook is rendered and triggered in src/pkg before adopt processing."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        pkg = "pkg_adopt_template_hook"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = src_pkg_dir / DRIFT_HOOKS_DIR_NAME
        scripts_dir.mkdir()
        hook_script = scripts_dir / "prepare_src.envst.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo \"HOOK_RAN_${drift_package_name}\" > hook_executed.txt\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        pkg_toml = src_pkg_dir / "drift_package.toml"
        pkg_toml.write_text(
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"drift_hooks/prepare_src.sh\"\n",
            encoding="utf-8"
        )

        # Create a drift in install/
        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))

        # Hook must have run and generated hook_executed.txt in render pkg .drift/hooks dir
        hook_out = self.workspace_path / "render" / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "hook_executed.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), f"HOOK_RAN_{pkg}")

        # Rendered hook must exist in render/
        rendered_hook = self.workspace_path / "render" / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "prepare_src.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn(f"HOOK_RAN_{pkg}", rendered_hook.read_text(encoding="utf-8"))

    def test_adopt_pre_source_hook_failure_aborts_adopt(self) -> None:
        """Verifies that an error in pre_source hook is not suppressed and aborts adopt."""
        pkg = "pkg_adopt_failing_hook"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = src_pkg_dir / DRIFT_HOOKS_DIR_NAME
        scripts_dir.mkdir()
        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'Fatal error' >&2\n"
            "exit 1\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        pkg_toml = src_pkg_dir / "drift_package.toml"
        pkg_toml.write_text(
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"drift_hooks/failing.sh\"\n",
            encoding="utf-8"
        )

        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            adopt_one_package_drifts(
                self.workspace_config,
                pkg,
                options=AdoptOptions(interactive=False, flags=HookExecFlags(streaming=False)),
            )
        self.assertIn("failed with exit code 1", str(ctx.exception))

    def test_adopt_with_package_source_directory_subfolder(self) -> None:
        """Verifies drift adopt properly routes additions and modifications into subfolder source_directory."""
        pkg = "pkg_sub_adopt"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        dotfiles_dir = src_pkg_dir / "dotfiles"
        (dotfiles_dir / "dot-config").mkdir(parents=True, exist_ok=True)
        (dotfiles_dir / "dot-config" / "app.conf").write_text("setting=original\n", encoding="utf-8")

        (src_pkg_dir / "drift_package.toml").write_text(
            f"[package]\nname = \"{pkg}\"\nsource_directory = \"dotfiles\"\n",
            encoding="utf-8"
        )

        # Commit initial files in workspace source repo
        subprocess.run(["git", "-C", self.workspace_path.as_posix(), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.workspace_path.as_posix(), "commit", "-m", "add pkg_sub_adopt source"], check=True, capture_output=True)

        # Stage / install structure
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config" / "app.conf").write_text("setting=original\n", encoding="utf-8")

        # Initial commit in install repo
        subprocess.run(["git", "-C", self.install_dir.as_posix(), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", self.install_dir.as_posix(), "commit", "-m", "init"], capture_output=True)

        # Create drifts in install/
        # 1. Modify app.conf
        (pkg_install_dir / "dot-config" / "app.conf").write_text("setting=modified_in_install\n", encoding="utf-8")
        # 2. Add new file
        (pkg_install_dir / "new_tool.sh").write_text("#!/bin/sh\necho new_tool\n", encoding="utf-8")

        adopt_res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))
        self.assertEqual(adopt_res.status, "SUCCESS")
        self.assertIn("dot-config/app.conf", adopt_res.adopted_modifications)
        self.assertIn("new_tool.sh", adopt_res.adopted_additions)

        # Verify adopted into dotfiles/ subfolder
        self.assertEqual(
            (dotfiles_dir / "dot-config" / "app.conf").read_text(encoding="utf-8"),
            "setting=modified_in_install\n"
        )
        self.assertTrue((dotfiles_dir / "new_tool.sh").is_file())
        self.assertEqual(
            (dotfiles_dir / "new_tool.sh").read_text(encoding="utf-8"),
            "#!/bin/sh\necho new_tool\n"
        )

        # Verify root of package does not contain new_tool.sh
        self.assertFalse((src_pkg_dir / "new_tool.sh").exists())

    def test_adopt_permission_only_drift_applies_cleanly(self) -> None:
        """Verifies that adopting a file mode/permission drift updates source file permissions cleanly."""
        pkg = "pkg_perm"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        src_script = src_pkg_dir / "script.sh"
        src_script.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        src_script.chmod(0o644)

        install_script = pkg_install_dir / "script.sh"
        install_script.write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        install_script.chmod(0o644)

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src script"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init script"], cwd=self.install_dir, check=True, capture_output=True)

        # Drift permissions in install/
        install_script.chmod(0o755)

        # Run adopt
        run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=False))

        # Source script should now have executable bit (0o755) on POSIX systems
        if sys.platform != "win32":
            self.assertTrue(bool(src_script.stat().st_mode & 0o111))

        # Check install/ git status - nothing should be staged
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if len(line) >= 2:
                self.assertIn(line[0], [" ", "?"])

    def test_adopt_conflict_leaves_file_unstaged_and_unresolved(self) -> None:
        """Verifies that a patch conflict during adopt leaves the file as unresolved unstaged drift."""
        pkg = "pkg_conflict"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Create a template in src/ and compiled version in install/
        src_file = src_pkg_dir / "app.conf.envst"
        src_file.write_text("TEMPLATE_VAR=$MY_VAR\nEXTRA_LINE=1\n", encoding="utf-8")

        install_file = pkg_install_dir / "app.conf"
        install_file.write_text("TEMPLATE_VAR=RENDERED_VALUE\nEXTRA_LINE=1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src app.conf"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init app.conf"], cwd=self.install_dir, check=True, capture_output=True)

        # Record HEAD commit sha before adopt
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Modify install_file so that patch cannot apply cleanly to src_file template
        install_file.write_text("CONFLICTING_MODIFICATION=VALUE\nEXTRA_LINE=999\n", encoding="utf-8")

        # Run adopt
        run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=False))

        # HEAD commit in install/ repo MUST NOT have changed (install was not committed)
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # In install/ repo, the modified file must remain as an UNSTAGED drift (' M')
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(" M pkg_conflict/app.conf", res.stdout)
        self.assertNotIn("M  pkg_conflict/app.conf", res.stdout)

    def test_adopt_multi_package_commits_only_resolved_packages(self) -> None:
        """Verifies that across multiple packages, only fully resolved packages are committed in install/ repo."""
        pkg_clean = "pkg_clean"
        pkg_conflict = "pkg_conflict_multi"

        # Setup pkg_clean
        src_clean = self.src_dir / pkg_clean
        src_clean.mkdir(parents=True, exist_ok=True)
        install_clean = self.install_dir / pkg_clean
        install_clean.mkdir(parents=True, exist_ok=True)

        (src_clean / "config.json").write_text('{"version": 1}', encoding="utf-8")
        (install_clean / "config.json").write_text('{"version": 1}', encoding="utf-8")

        # Setup pkg_conflict
        src_conflict = self.src_dir / pkg_conflict
        src_conflict.mkdir(parents=True, exist_ok=True)
        install_conflict = self.install_dir / pkg_conflict
        install_conflict.mkdir(parents=True, exist_ok=True)

        (src_conflict / "server.conf.envst").write_text("HOST=$MY_HOST\nPORT=8080\n", encoding="utf-8")
        (install_conflict / "server.conf").write_text("HOST=localhost\nPORT=8080\n", encoding="utf-8")

        # Commit clean initial state in workspace and install
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init multi-pkg src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init multi-pkg install"], cwd=self.install_dir, check=True, capture_output=True)

        # Drift in install/
        (install_clean / "config.json").write_text('{"version": 2}', encoding="utf-8")
        (install_conflict / "server.conf").write_text("CONFLICTING_KEY=VALUE\nPORT=9999\n", encoding="utf-8")

        # Run adopt across both packages
        resolved = run_primitive_adopt_drifts(
            self.workspace_config,
            [pkg_clean, pkg_conflict],
            options=AdoptOptions(interactive=False),
        )

        # Only pkg_clean should be fully resolved
        self.assertEqual(list(resolved), [pkg_clean])
        self.assertEqual(resolved.status, "PARTIAL")
        pkg_res_map = {p.package: p for p in resolved.packages}
        self.assertEqual(pkg_res_map[pkg_clean].status, "SUCCESS")
        self.assertEqual(pkg_res_map[pkg_conflict].status, "SKIPPED")
        self.assertEqual(pkg_res_map[pkg_conflict].conflicted_files, ["server.conf"])
        self.assertEqual(pkg_res_map[pkg_conflict].skipped_files, [])

        # src/ for pkg_clean should be adopted
        self.assertEqual((src_clean / "config.json").read_text(encoding="utf-8"), '{"version": 2}')
        # src/ for pkg_conflict should be untouched
        self.assertIn("HOST=$MY_HOST", (src_conflict / "server.conf.envst").read_text(encoding="utf-8"))

        # In install/ repo:
        # pkg_clean should be committed (not in status)
        # pkg_conflict/server.conf should be UNSTAGED (' M')
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertNotIn(pkg_clean, res.stdout)
        self.assertIn(f" M {pkg_conflict}/server.conf", res.stdout)
        self.assertNotIn(f"M  {pkg_conflict}/server.conf", res.stdout)

    def test_adopt_partial_conflict_in_single_package_unstages_and_skips_commit(self) -> None:
        """Verifies that if one file in a package has a conflict, adopted files are committed while conflicting files remain unstaged."""
        pkg = "pkg_partial"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "static_file.txt").write_text("static v1", encoding="utf-8")
        (install_pkg / "static_file.txt").write_text("static v1", encoding="utf-8")

        (src_pkg / "template_file.conf.envst").write_text("KEY=$VAL\nLINE=A\n", encoding="utf-8")
        (install_pkg / "template_file.conf").write_text("KEY=RENDERED\nLINE=A\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init partial src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init partial install"], cwd=self.install_dir, check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Drift in install/: static_file is clean, template_file has conflict
        (install_pkg / "static_file.txt").write_text("static v2", encoding="utf-8")
        (install_pkg / "template_file.conf").write_text("CONFLICT=TRUE\nLINE=Z\n", encoding="utf-8")

        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=False))

        # Package was partially resolved (static_file adopted, template_file conflicted)
        self.assertEqual(list(resolved), [])
        self.assertEqual(resolved.status, "PARTIAL")
        self.assertEqual(resolved.packages[0].status, "PARTIAL")
        self.assertEqual(resolved.packages[0].adopted_modifications, ["static_file.txt"])
        self.assertEqual(resolved.packages[0].conflicted_files, ["template_file.conf"])
        self.assertEqual(resolved.packages[0].skipped_files, [])

        # HEAD commit in install/ DID change because static_file was adopted and committed
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(head_before, head_after)

        # Clean file was adopted into source and committed in install/
        self.assertEqual((src_pkg / "static_file.txt").read_text(encoding="utf-8"), "static v2")

        # Conflict file is UNSTAGED
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(f" M {pkg}/template_file.conf", res.stdout)
        self.assertNotIn(f"M  {pkg}/template_file.conf", res.stdout)
        self.assertNotIn("static_file.txt", res.stdout)

    def test_adopt_permission_drift_on_templated_file_applies_cleanly(self) -> None:
        """Verifies that a permission-only drift on a templated file adopts cleanly and commits."""
        if sys.platform == "win32":
            self.skipTest("POSIX file permissions not applicable on Windows")

        pkg = "pkg_template_perm"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        src_template = src_pkg / "template_script.sh.envst"
        src_template.write_text("#!/bin/bash\nexport THEME=$THEME_NAME\n", encoding="utf-8")
        src_template.chmod(0o644)

        install_script = install_pkg / "template_script.sh"
        install_script.write_text("#!/bin/bash\nexport THEME=Dark\n", encoding="utf-8")
        install_script.chmod(0o644)

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init template perm src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init template perm install"], cwd=self.install_dir, check=True, capture_output=True)

        # Drift mode to 0o755 in install/
        install_script.chmod(0o755)

        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=False))

        # Package must be resolved cleanly
        self.assertEqual(list(resolved), [pkg])
        self.assertEqual(resolved.status, "SUCCESS")

        # Source template file must now have executable bit set on POSIX systems
        self.assertTrue(bool(src_template.stat().st_mode & 0o111))

        # install/ repo should be committed and clean
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "")

    def test_adopt_interactive_skip_unstages_file_and_skips_commit(self) -> None:
        """Verifies that choosing Skip in interactive mode unstages the file and skips commit in install/."""
        from unittest.mock import patch

        pkg = "pkg_interactive_skip"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "notes.txt").write_text("notes v1", encoding="utf-8")
        (install_pkg / "notes.txt").write_text("notes v1", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init notes src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init notes install"], cwd=self.install_dir, check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Drift in install/
        (install_pkg / "notes.txt").write_text("notes v2", encoding="utf-8")

        # Mock input to choose [5] Skip file
        with patch("builtins.input", return_value="5"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=True))

        self.assertEqual(list(resolved), [])

        # HEAD commit in install/ did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File remains as UNSTAGED drift in install/
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(f" M {pkg}/notes.txt", res.stdout)
        self.assertNotIn(f"M  {pkg}/notes.txt", res.stdout)

    def test_adopt_skipped_addition_stays_unstaged_in_install_repo(self) -> None:
        """Verifies that an untracked addition that is skipped remains as untracked ('??') and unstaged."""
        from unittest.mock import patch

        pkg = "pkg_add_skip"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "base.txt").write_text("base", encoding="utf-8")
        (install_pkg / "base.txt").write_text("base", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init add skip src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init add skip install"], cwd=self.install_dir, check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Add new untracked file in install/
        (install_pkg / "wild_addition.txt").write_text("wild content", encoding="utf-8")

        # Interactive choose [4] Skip file
        with patch("builtins.input", return_value="4"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=True))

        self.assertEqual(list(resolved), [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is untracked '??', NOT staged 'A '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(f"?? {pkg}/wild_addition.txt", res.stdout)
        self.assertNotIn(f"A  {pkg}/wild_addition.txt", res.stdout)

    def test_adopt_skipped_deletion_stays_unstaged_in_install_repo(self) -> None:
        """Verifies that a deletion that is skipped remains as unstaged deletion (' D') and not staged ('D ')."""
        from unittest.mock import patch

        pkg = "pkg_del_skip"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "delete_me.txt").write_text("del content", encoding="utf-8")
        (install_pkg / "delete_me.txt").write_text("del content", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init del skip src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init del skip install"], cwd=self.install_dir, check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Delete file in install/
        (install_pkg / "delete_me.txt").unlink()

        # Interactive choose [3] Skip file
        with patch("builtins.input", return_value="3"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=True))

        self.assertEqual(list(resolved), [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is unstaged deletion ' D', NOT staged 'D '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(f" D {pkg}/delete_me.txt", res.stdout)
        self.assertNotIn(f"D  {pkg}/delete_me.txt", res.stdout)

    def test_adopt_interactive_conflict_skip_stays_unstaged(self) -> None:
        """Verifies that choosing Skip on a conflicted template in interactive mode leaves the file unstaged (' M')."""
        from unittest.mock import patch

        pkg = "pkg_conflict_interactive"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "service.conf.envst").write_text("PORT=$MY_PORT\nKEY=ABC\n", encoding="utf-8")
        (install_pkg / "service.conf").write_text("PORT=8000\nKEY=ABC\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conflict interactive src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conflict interactive install"], cwd=self.install_dir, check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()

        # Conflicting modification in install/
        (install_pkg / "service.conf").write_text("COMPLETELY_DIFFERENT=999\nOTHER=ZZZ\n", encoding="utf-8")

        # Interactive choose [6] Skip file on conflict menu
        with patch("builtins.input", return_value="6"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=True))

        self.assertEqual(list(resolved), [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.install_dir, capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is unstaged modification ' M', NOT staged 'M '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        self.assertIn(f" M {pkg}/service.conf", res.stdout)
        self.assertNotIn(f"M  {pkg}/service.conf", res.stdout)

    def test_adopt_with_subfolder_source_directory(self) -> None:
        """Verifies that adopting additions, modifications, and deletions operates inside source_directory subfolder."""
        pkg = "pkg_adopt_subfolder"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        subfolder_dir = src_pkg / "dotfiles"
        subfolder_dir.mkdir(parents=True, exist_ok=True)

        pkg_toml = src_pkg / "drift_package.toml"
        pkg_toml.write_text(f'[package]\nname = "{pkg}"\nsource_directory = "dotfiles"\n', encoding="utf-8")

        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        # Existing file in source subfolder and install/
        (subfolder_dir / "app.conf").write_text("setting=original\n", encoding="utf-8")
        (install_pkg / "app.conf").write_text("setting=original\n", encoding="utf-8")

        # Initial commit in src and install
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src subfolder"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install subfolder"], cwd=self.install_dir, check=True, capture_output=True)

        # 1. Modify existing file on host (in install/)
        (install_pkg / "app.conf").write_text("setting=modified_on_host\n", encoding="utf-8")

        # 2. Add new file on host (in install/)
        (install_pkg / "new_tool.conf").write_text("tool=active\n", encoding="utf-8")

        # Run adopt
        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], options=AdoptOptions(interactive=False))
        self.assertEqual(list(resolved), [pkg])
        self.assertEqual(resolved.status, "SUCCESS")

        # Verify modified file updated in source subfolder
        self.assertEqual((subfolder_dir / "app.conf").read_text(encoding="utf-8"), "setting=modified_on_host\n")

        # Verify added file created inside source subfolder
        self.assertTrue((subfolder_dir / "new_tool.conf").is_file())
        self.assertEqual((subfolder_dir / "new_tool.conf").read_text(encoding="utf-8"), "tool=active\n")

        # Verify not created at package root
        self.assertFalse((src_pkg / "new_tool.conf").exists())

    def test_adopt_dry_run_detects_renames_and_leaves_index_clean(self) -> None:
        """Verifies that dry-run mode accurately detects file renames and leaves install git index unstaged."""
        pkg = "pkg_dry_run_rename"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "source_name.txt").write_text("content to rename", encoding="utf-8")
        (install_pkg / "source_name.txt").write_text("content to rename", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init dry run rename src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init dry run rename install"], cwd=self.install_dir, check=True, capture_output=True)

        # Rename file on host (in install/)
        (install_pkg / "source_name.txt").unlink()
        (install_pkg / "renamed_dest.txt").write_text("content to rename", encoding="utf-8")

        # Run adopt in dry_run mode
        res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(dry_run=True))
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.adopted_renames, ["source_name.txt -> renamed_dest.txt"])
        self.assertEqual(res.adopted_additions, [])
        self.assertEqual(res.adopted_deletions, [])

        # Source folder was NOT changed in dry-run
        self.assertTrue((src_pkg / "source_name.txt").exists())
        self.assertFalse((src_pkg / "renamed_dest.txt").exists())

        # Git index in install/ must remain unstaged (no staged changes)
        status_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.install_dir, capture_output=True, text=True)
        for line in status_res.stdout.splitlines():
            if len(line) >= 2:
                self.assertIn(line[0], [" ", "?"])

    @patch("drift.primitives.adopt_repo.launch_single_file_editor")
    @patch("drift.primitives.adopt_repo.apply_source_patch")
    def test_patch_and_edit_success_and_failure(self, mock_apply: MagicMock, mock_launch: MagicMock) -> None:
        """Verifies patch_and_edit applies patch, syncs permissions, opens editor, and handles failures."""
        mock_apply.return_value = True
        src_file = self.src_dir / "pkg_a" / "test.txt"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("orig", encoding="utf-8")
        src_file.chmod(0o644)

        install_file = self.install_dir / "pkg_a" / "test.txt"
        install_file.parent.mkdir(parents=True, exist_ok=True)
        install_file.write_text("mod", encoding="utf-8")
        install_file.chmod(0o755)

        # 1. Clean patch without editor, with file mode sync
        self.assertTrue(patch_and_edit(src_file, "patch content", install_file=install_file, accept_conflicts=False, open_editor=False))
        mock_apply.assert_called_once_with(src_file, "patch content", accept_conflicts=False)
        mock_launch.assert_not_called()
        if sys.platform != "win32":
            self.assertTrue(bool(src_file.stat().st_mode & 0o111))

        # 2. Conflicted patch with editor
        mock_apply.reset_mock()
        mock_launch.reset_mock()
        self.assertTrue(patch_and_edit(src_file, "conflict patch", install_file=install_file, accept_conflicts=True, open_editor=True))
        mock_apply.assert_called_once_with(src_file, "conflict patch", accept_conflicts=True)
        mock_launch.assert_called_once_with(src_file)

        # 3. Patch application failure returns False
        mock_apply.reset_mock()
        mock_launch.reset_mock()
        mock_apply.return_value = False
        self.assertFalse(patch_and_edit(src_file, "failing patch"))
        mock_launch.assert_not_called()

        # 4. Failure when EDITOR is unset or invalid
        mock_apply.reset_mock()
        mock_launch.reset_mock()
        mock_apply.return_value = True
        mock_launch.side_effect = RuntimeError("Environment variable $EDITOR is not set")
        self.assertFalse(patch_and_edit(src_file, "patch content", open_editor=True))

    @patch("drift.primitives.adopt_repo.launch_side_by_side_editor")
    def test_fallback_side_by_side_success_and_failure(self, mock_launch: MagicMock) -> None:
        """Verifies fallback_side_by_side returns True on success and False on RuntimeError."""
        src_file = self.src_dir / "pkg_a" / "test.txt"
        install_file = self.install_dir / "pkg_a" / "test.txt"

        # Success
        self.assertTrue(fallback_side_by_side(src_file, install_file))
        mock_launch.assert_called_once_with([(src_file, install_file)])

        # Failure when EDITOR is unset or invalid
        mock_launch.side_effect = RuntimeError("Editor 'nano' is not supported")
        self.assertFalse(fallback_side_by_side(src_file, install_file))

    def test_adopt_modifications_deletions_additions_with_package_level_render_engine(self) -> None:
        """Verifies adopt handles modifications, deletions, and additions when package defines its own render engine."""
        pkg = "pkg_adopt_engine"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        data_file = src_pkg / "custom.env"
        data_file.write_text("ENV_KEY=val\n", encoding="utf-8")

        # Package defines [render.custom] which is NOT in workspace_config
        (src_pkg / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
[package]
name = "{pkg}"
enable_render = true

[render.custom]
input_file = "custom.env"
suffix = "custom"
render_command = "bash -c 'cat %i %s'"
""")

        # 1. Setup existing templated files in src/ and rendered files in install/
        src_config = src_pkg / "config.custom.ini"
        src_config.write_text("[section]\nkey=original_val\n", encoding="utf-8")

        src_app = src_pkg / "app.custom.txt"
        src_app.write_text("hello original app\n", encoding="utf-8")

        install_config = install_pkg / "config.ini"
        install_config.write_text("[section]\nkey=original_val\n", encoding="utf-8")

        install_app = install_pkg / "app.txt"
        install_app.write_text("hello original app\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_adopt_engine"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_adopt_engine"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Host modifications:
        # - modify config.ini
        install_config.write_text("[section]\nkey=modified_val\n", encoding="utf-8")
        # - delete app.txt
        install_app.unlink()
        # - add new_file.txt
        (install_pkg / "new_file.txt").write_text("new content added\n", encoding="utf-8")

        # 3. Run adopt
        res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.adopted_modifications, ["config.ini"])
        self.assertEqual(res.adopted_deletions, ["app.txt"])
        self.assertEqual(res.adopted_additions, ["new_file.txt"])

        # 4. Verify source transformations:
        # Templated file config.custom.ini adopted modifications
        self.assertEqual(src_config.read_text(encoding="utf-8"), "[section]\nkey=modified_val\n")
        # Templated file app.custom.txt was deleted
        self.assertFalse(src_app.exists())
        # Added file new_file.txt was created in source
        self.assertTrue((src_pkg / "new_file.txt").is_file())
        self.assertEqual((src_pkg / "new_file.txt").read_text(encoding="utf-8"), "new content added\n")

    def test_adopt_renames_with_package_level_render_engine(self) -> None:
        """Verifies adopt renames templated source files according to package-level render engine suffixes."""
        pkg = "pkg_adopt_rename_engine"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        data_file = src_pkg / "custom.env"
        data_file.write_text("ENV_KEY=val\n", encoding="utf-8")

        (src_pkg / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
[package]
name = "{pkg}"
enable_render = true

[render.custom]
input_file = "custom.env"
suffix = "custom"
render_command = "bash -c 'cat %i %s'"
""")

        src_doc = src_pkg / "document.custom.md"
        src_doc.write_text("# Document Title\n", encoding="utf-8")

        install_doc = install_pkg / "document.md"
        install_doc.write_text("# Document Title\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_adopt_rename_engine"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_adopt_rename_engine"], cwd=self.install_dir, check=True, capture_output=True)

        # Host renames document.md -> manual.md
        subprocess.run(["git", "mv", f"{pkg}/document.md", f"{pkg}/manual.md"], cwd=self.install_dir, check=True, capture_output=True)

        # Run adopt
        res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.adopted_renames, ["document.md -> manual.md"])

        # Verify old templated source is removed and new templated source exists with .custom.md suffix
        self.assertFalse(src_doc.exists())
        new_src_doc = src_pkg / "manual.custom.md"
        self.assertTrue(new_src_doc.is_file())
        self.assertEqual(new_src_doc.read_text(encoding="utf-8"), "# Document Title\n")

    def test_adopt_interactive_clean_modification_adopt_and_edit(self) -> None:
        """Verifies choosing option [2] on clean modification applies patch and launches editor."""
        from io import StringIO
        pkg = "pkg_edit_opt"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "config.txt").write_text("line1\nline2\n", encoding="utf-8")
        (install_pkg / "config.txt").write_text("line1\nline2\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_edit_opt"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_edit_opt"], cwd=self.install_dir, check=True, capture_output=True)

        # Modify install
        (install_pkg / "config.txt").write_text("line1\nline2_modified\n", encoding="utf-8")

        with patch("builtins.input", return_value="2"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor, \
             patch("sys.stdout", StringIO()) as mock_stdout:
            res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=True))
            self.assertEqual(res.status, "SUCCESS")
            mock_editor.assert_called_once_with(src_pkg / "config.txt")
            self.assertEqual((src_pkg / "config.txt").read_text(encoding="utf-8"), "line1\nline2_modified\n")
            # Verify diff printed to stdout
            self.assertIn("-line2", mock_stdout.getvalue())
            self.assertIn("+line2_modified", mock_stdout.getvalue())

    def test_adopt_interactive_clean_modification_side_by_side(self) -> None:
        """Verifies choosing option [3] on clean modification launches side-by-side editor."""
        pkg = "pkg_sbs_opt"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "config.txt").write_text("orig\n", encoding="utf-8")
        (install_pkg / "config.txt").write_text("orig\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_sbs_opt"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_sbs_opt"], cwd=self.install_dir, check=True, capture_output=True)

        (install_pkg / "config.txt").write_text("modified\n", encoding="utf-8")

        with patch("builtins.input", return_value="3"), \
             patch("drift.primitives.adopt_repo.launch_side_by_side_editor") as mock_sbs:
            res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=True))
            self.assertEqual(res.status, "SUCCESS")
            mock_sbs.assert_called_once_with([(src_pkg / "config.txt", install_pkg / "config.txt")])

    def test_adopt_interactive_clean_rename_adopt_and_edit(self) -> None:
        """Verifies choosing option [2] on clean rename applies rename/patch and opens editor."""
        pkg = "pkg_rename_edit"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        content_orig = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        content_mod = "line 1\nline 2\nline 3\nline 4\nline 5 updated\n"
        (src_pkg / "old_name.txt").write_text(content_orig, encoding="utf-8")
        (install_pkg / "old_name.txt").write_text(content_orig, encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_rename_edit"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_rename_edit"], cwd=self.install_dir, check=True, capture_output=True)

        subprocess.run(["git", "mv", f"{pkg}/old_name.txt", f"{pkg}/new_name.txt"], cwd=self.install_dir, check=True, capture_output=True)
        (install_pkg / "new_name.txt").write_text(content_mod, encoding="utf-8")

        with patch("builtins.input", return_value="2"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor:
            res = adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=True))
            self.assertEqual(res.status, "SUCCESS")
            mock_editor.assert_called_once_with(src_pkg / "new_name.txt")
            self.assertTrue((src_pkg / "new_name.txt").is_file())
            self.assertEqual((src_pkg / "new_name.txt").read_text(encoding="utf-8"), content_mod)

    def test_adopt_non_interactive_logs_diff_debug(self) -> None:
        """Verifies non-interactive adoption logs diffs at DEBUG level."""
        set_test_mode(True, enable_logging=True)
        try:
            pkg = "pkg_debug_log"
            src_pkg = self.src_dir / pkg
            src_pkg.mkdir(parents=True, exist_ok=True)
            install_pkg = self.install_dir / pkg
            install_pkg.mkdir(parents=True, exist_ok=True)

            (src_pkg / "file.txt").write_text("initial\n", encoding="utf-8")
            (install_pkg / "file.txt").write_text("initial\n", encoding="utf-8")

            subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init src pkg_debug_log"], cwd=self.workspace_path, check=True, capture_output=True)

            subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init install pkg_debug_log"], cwd=self.install_dir, check=True, capture_output=True)

            (install_pkg / "file.txt").write_text("changed_text\n", encoding="utf-8")

            with self.assertLogs("drift.primitives.adopt_repo", level="DEBUG") as cm:
                adopt_one_package_drifts(self.workspace_config, pkg, options=AdoptOptions(interactive=False))
                self.assertTrue(any("Diff for 'file.txt'" in log for log in cm.output))
                self.assertTrue(any("+changed_text" in log for log in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_adopt_file_cleanliness_allows_clean_files_with_dirty_neighbor(self) -> None:
        """Verifies that adopting a clean file succeeds even if a sibling file in the same package is dirty."""
        pkg = "pkg_sibling"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "file_a.txt").write_text("orig_a\n", encoding="utf-8")
        (src_pkg / "file_b.txt").write_text("orig_b\n", encoding="utf-8")
        (install_pkg / "file_a.txt").write_text("orig_a\n", encoding="utf-8")
        (install_pkg / "file_b.txt").write_text("orig_b\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_sibling"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_sibling"], cwd=self.install_dir, check=True, capture_output=True)

        # 1. Dirty file_a in src/
        (src_pkg / "file_a.txt").write_text("dirty_uncommitted_a\n", encoding="utf-8")

        # 2. Modify only file_b in install/ (drift)
        (install_pkg / "file_b.txt").write_text("drifted_b\n", encoding="utf-8")

        # 3. Adopting without force should SUCCEED because file_b is clean in src/
        res = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], options=AdoptOptions(interactive=False, force=False)
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual((src_pkg / "file_b.txt").read_text(encoding="utf-8"), "drifted_b\n")
        # file_a should remain untouched and dirty
        self.assertEqual((src_pkg / "file_a.txt").read_text(encoding="utf-8"), "dirty_uncommitted_a\n")

        # 4. Now modify file_a in install/ too (drift on dirty file)
        (install_pkg / "file_a.txt").write_text("drifted_a\n", encoding="utf-8")

        # 5. Adopting without force should FAIL with RuntimeError pointing to file_a.txt
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_adopt_drifts(
                self.workspace_config, [pkg], options=AdoptOptions(interactive=False, force=False)
            )
        self.assertIn("file_a.txt", str(ctx.exception))
        self.assertIn(f"drift adopt {pkg} --force", str(ctx.exception))

        # 6. Adopting with force=True should SUCCEED and overwrite file_a.txt
        res_force = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], options=AdoptOptions(interactive=False, force=True)
        )
        self.assertEqual(res_force.status, "SUCCESS")
        self.assertEqual((src_pkg / "file_a.txt").read_text(encoding="utf-8"), "drifted_a\n")

    def test_adopt_deletion_dirty_file_safeguard(self) -> None:
        """Verifies that adopting a file deletion respects the file-level cleanliness safeguard."""
        pkg = "pkg_del_dirty"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "del_me.txt").write_text("orig_del\n", encoding="utf-8")
        (install_pkg / "del_me.txt").write_text("orig_del\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src pkg_del_dirty"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install pkg_del_dirty"], cwd=self.install_dir, check=True, capture_output=True)

        # Dirty del_me.txt in src/
        (src_pkg / "del_me.txt").write_text("dirty_uncommitted_del\n", encoding="utf-8")

        # Delete del_me.txt in install/
        (install_pkg / "del_me.txt").unlink()

        # Adopting deletion without force should fail
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_adopt_drifts(
                self.workspace_config, [pkg], options=AdoptOptions(interactive=False, force=False)
            )
        self.assertIn("del_me.txt", str(ctx.exception))
        self.assertIn(f"drift adopt {pkg} --force", str(ctx.exception))
        self.assertTrue((src_pkg / "del_me.txt").exists())

        # Adopting deletion with force=True should succeed and delete source file
        res = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], options=AdoptOptions(interactive=False, force=True)
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse((src_pkg / "del_me.txt").exists())

    def test_prepare_adopt_and_execute_adopt(self) -> None:
        """Verifies decoupled prepare_adopt and execute_adopt workflow."""
        pkg1 = "pkg_plan_1"
        pkg2 = "pkg_plan_2"

        src_pkg1 = self.src_dir / pkg1
        src_pkg1.mkdir(parents=True, exist_ok=True)
        install_pkg1 = self.install_dir / pkg1
        install_pkg1.mkdir(parents=True, exist_ok=True)

        src_pkg2 = self.src_dir / pkg2
        src_pkg2.mkdir(parents=True, exist_ok=True)
        install_pkg2 = self.install_dir / pkg2
        install_pkg2.mkdir(parents=True, exist_ok=True)

        # Base files
        (src_pkg1 / "config.txt").write_text("v1\n", encoding="utf-8")
        (install_pkg1 / "config.txt").write_text("v1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init plan test src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init plan test install"], cwd=self.install_dir, check=True, capture_output=True)

        # Introduce drifts: modification in pkg1, addition in pkg2
        (install_pkg1 / "config.txt").write_text("v2_modified\n", encoding="utf-8")
        (install_pkg2 / "new_tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

        # 1. Plan Phase
        plan = prepare_adopt(
            self.workspace_config,
            [pkg1, pkg2],
            options=AdoptOptions(interactive=False, no_render=True),
        )
        self.assertIsInstance(plan, AdoptPlan)
        self.assertTrue(plan.has_drifts)
        self.assertEqual(sorted(plan.drifted_packages), sorted([pkg1, pkg2]))

        plan1 = plan.get_package_plan(pkg1)
        self.assertIsNotNone(plan1)
        assert plan1 is not None
        self.assertEqual(plan1.modifications, [Path("config.txt")])
        self.assertIn("config.txt", plan1.mod_patches)
        self.assertIn("v2_modified", plan1.mod_patches["config.txt"])

        plan2 = plan.get_package_plan(pkg2)
        self.assertIsNotNone(plan2)
        assert plan2 is not None
        self.assertEqual(plan2.additions, [Path("new_tool.sh")])

        # Verify staging index in install/ was completely restored during planning
        res_cached = subprocess.run(
            ["git", "-C", self.install_dir.as_posix(), "diff", "--cached", "--quiet"],
            capture_output=True
        )
        self.assertEqual(res_cached.returncode, 0, "Index should be clean after planning")

        # 2. Execution Phase
        exec_res = execute_adopt(self.workspace_config, plan)
        self.assertEqual(exec_res.status, "SUCCESS")
        self.assertEqual(len(exec_res.packages), 2)

        # Both packages should be successfully adopted in src/
        self.assertEqual((src_pkg1 / "config.txt").read_text(encoding="utf-8"), "v2_modified\n")
        self.assertEqual((src_pkg2 / "new_tool.sh").read_text(encoding="utf-8"), "#!/bin/sh\necho hi\n")

        # Working tree in install/ should now be clean and committed
        status_res = subprocess.run(["git", "-C", self.install_dir.as_posix(), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status_res.stdout.strip(), "")

    def test_plan_package_adopt_rename_with_precomputed_patch(self) -> None:
        """Verifies plan_package_adopt correctly precomputes rename patches and execute_package_adopt applies them."""
        from drift.primitives.adopt_repo import get_rename_patch_key

        pkg = "pkg_rename_plan"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        initial_content = "line1=apple\nline2=banana\nline3=cherry\nline4=date\nline5=elderberry\n"
        (src_pkg / "old_name.conf").write_text(initial_content, encoding="utf-8")
        (install_pkg / "old_name.conf").write_text(initial_content, encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init rename plan src"], cwd=self.workspace_path, check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init rename plan install"], cwd=self.install_dir, check=True, capture_output=True)

        # Perform git rename and modification in install/
        subprocess.run(["git", "-C", self.install_dir.as_posix(), "mv", f"{pkg}/old_name.conf", f"{pkg}/new_name.conf"], check=True)
        modified_content = "line1=apple\nline2=banana\nline3=cherry_modified\nline4=date\nline5=elderberry\n"
        (install_pkg / "new_name.conf").write_text(modified_content, encoding="utf-8")

        # Plan single package
        pkg_plan = plan_package_adopt(self.workspace_config, pkg)
        self.assertIsInstance(pkg_plan, PackageAdoptPlan)
        self.assertEqual(pkg_plan.renames, [(Path("old_name.conf"), Path("new_name.conf"))])

        # Patch must be precomputed and stored under get_rename_patch_key("old_name.conf", "new_name.conf")
        patch_key = get_rename_patch_key(Path("old_name.conf"), Path("new_name.conf"))
        self.assertEqual(patch_key, "old_name.conf//->//new_name.conf")
        self.assertIn(patch_key, pkg_plan.rename_patches)
        self.assertIn("cherry_modified", pkg_plan.rename_patches[patch_key])

        # Index in install/ must be clean (un-staged)
        res_cached = subprocess.run(
            ["git", "-C", self.install_dir.as_posix(), "diff", "--cached", "--quiet"],
            capture_output=True
        )
        self.assertEqual(res_cached.returncode, 0)

        # Execute
        result = execute_package_adopt(self.workspace_config, pkg_plan, options=AdoptOptions(interactive=False))
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.adopted_renames, ["old_name.conf -> new_name.conf"])

        # Source file should be renamed and updated
        self.assertFalse((src_pkg / "old_name.conf").exists())
        self.assertTrue((src_pkg / "new_name.conf").exists())
        self.assertEqual((src_pkg / "new_name.conf").read_text(encoding="utf-8"), modified_content)

    def test_plan_package_adopt_keep_file_addition_translation(self) -> None:
        """Verifies that .drift_keep addition in install/ is translated to parent directory addition in adopt plan."""
        pkg = "pkg_keep_add"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        # Create an empty directory in install/ with .drift_keep
        new_dir = install_pkg / "my_empty_folder"
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / DRIFT_KEEP_FILE_NAME).write_bytes(b"")

        plan = plan_package_adopt(self.workspace_config, pkg)
        self.assertIn(Path("my_empty_folder"), plan.additions)
        self.assertNotIn(Path("my_empty_folder") / DRIFT_KEEP_FILE_NAME, plan.additions)

    def test_plan_package_adopt_keep_file_deletion_translation(self) -> None:
        """Verifies that .drift_keep deletion in install/ is translated to parent directory deletion in adopt plan."""
        pkg = "pkg_keep_del"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        # Pre-commit an empty directory with .drift_keep in install/
        old_dir = install_pkg / "old_empty_folder"
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / DRIFT_KEEP_FILE_NAME).write_bytes(b"")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src keep del"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install keep del"], cwd=self.install_dir, check=True, capture_output=True)

        # Delete the empty folder in install/
        shutil.rmtree(old_dir)

        plan = plan_package_adopt(self.workspace_config, pkg)
        self.assertIn(Path("old_empty_folder"), plan.deletions)
        self.assertNotIn(Path("old_empty_folder") / DRIFT_KEEP_FILE_NAME, plan.deletions)

    def test_plan_package_adopt_keep_file_rename_split(self) -> None:
        """Verifies that renaming a .drift_keep file is split into deletion and addition."""
        pkg = "pkg_keep_rename"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        dir_a = install_pkg / "dir_a"
        dir_a.mkdir(parents=True, exist_ok=True)
        (dir_a / DRIFT_KEEP_FILE_NAME).write_bytes(b"")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src keep rename"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install keep rename"], cwd=self.install_dir, check=True, capture_output=True)

        # Rename dir_a to dir_b using git mv
        (install_pkg / "dir_b").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", self.install_dir.as_posix(), "mv", f"{pkg}/dir_a/{DRIFT_KEEP_FILE_NAME}", f"{pkg}/dir_b/{DRIFT_KEEP_FILE_NAME}"], check=True)
        # Unstage so it's a working tree change
        subprocess.run(["git", "-C", self.install_dir.as_posix(), "restore", "--staged", "--", pkg], check=True)

        plan = plan_package_adopt(self.workspace_config, pkg)
        # Renames should NOT contain keep files
        self.assertEqual(len(plan.renames), 0)
        # Deletions should have dir_a, additions should have dir_b
        self.assertIn(Path("dir_a"), plan.deletions)
        self.assertIn(Path("dir_b"), plan.additions)

    def test_plan_package_adopt_corrupt_keep_file_warning(self) -> None:
        """Verifies that modified keep files log a warning and are stripped from modifications."""
        pkg = "pkg_corrupt_keep"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        keep_file = install_pkg / "my_dir" / DRIFT_KEEP_FILE_NAME
        keep_file.parent.mkdir(parents=True, exist_ok=True)
        keep_file.write_bytes(b"")

        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init install corrupt keep"], cwd=self.install_dir, check=True, capture_output=True)

        # Modify keep file to have non-empty content
        keep_file.write_text("corrupted content", encoding="utf-8")

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.primitives.adopt_repo", level="WARNING") as log_cm:
                plan = plan_package_adopt(self.workspace_config, pkg)
                self.assertTrue(any("CORRUPT" in msg for msg in log_cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

        # Must not be listed in modifications
        self.assertNotIn(Path("my_dir") / DRIFT_KEEP_FILE_NAME, plan.modifications)

    def test_handle_single_addition_empty_dir_non_interactive(self) -> None:
        """Verifies non-interactive directory addition creates empty directory in src/ without .drift_keep."""
        pkg = "pkg_add_dir"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("new_empty_folder")
        install_folder = pkg_install_dir / rel_path
        install_folder.mkdir(parents=True, exist_ok=True)
        (install_folder / DRIFT_KEEP_FILE_NAME).write_bytes(b"")

        resolved = handle_single_addition(
            self.workspace_config.render_engine_configs,
            src_pkg_dir,
            src_pkg_dir,
            pkg_install_dir,
            rel_path,
            interactive=False,
        )
        self.assertTrue(resolved)

        # In src/, new_empty_folder must exist as directory, but NOT contain .drift_keep
        src_folder = src_pkg_dir / rel_path
        self.assertTrue(src_folder.is_dir())
        self.assertFalse((src_folder / DRIFT_KEEP_FILE_NAME).exists())
        self.assertEqual(list(src_folder.iterdir()), [])

    def test_handle_single_addition_empty_dir_preexisting_skips(self) -> None:
        """Verifies that if target directory already exists in source, directory addition skips cleanly and returns True."""
        pkg = "pkg_preexist_dir"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("existing_folder")
        (src_pkg_dir / rel_path).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / rel_path).mkdir(parents=True, exist_ok=True)

        resolved = handle_single_addition(
            self.workspace_config.render_engine_configs,
            src_pkg_dir,
            src_pkg_dir,
            pkg_install_dir,
            rel_path,
            interactive=False,
        )
        self.assertTrue(resolved)

    def test_handle_single_addition_empty_dir_file_collision(self) -> None:
        """Verifies that if a regular file with the same name exists in source, directory addition conflicts and returns False."""
        pkg = "pkg_dir_file_collision"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("collision_name")
        (src_pkg_dir / rel_path).write_text("file in source", encoding="utf-8")
        (pkg_install_dir / rel_path).mkdir(parents=True, exist_ok=True)

        resolved = handle_single_addition(
            self.workspace_config.render_engine_configs,
            src_pkg_dir,
            src_pkg_dir,
            pkg_install_dir,
            rel_path,
            interactive=False,
        )
        self.assertFalse(resolved)

    def test_handle_single_deletion_empty_dir_non_interactive(self) -> None:
        """Verifies that deleting an empty directory in source succeeds in non-interactive mode."""
        pkg = "pkg_del_empty_dir"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("empty_to_delete")
        src_dir = src_pkg_dir / rel_path
        src_dir.mkdir(parents=True, exist_ok=True)

        resolved = handle_single_deletion(
            self.workspace_config.render_engine_configs,
            pkg,
            src_pkg_dir,
            rel_path,
            interactive=False,
        )
        self.assertTrue(resolved)
        self.assertFalse(src_dir.exists())

    def test_handle_single_deletion_non_empty_dir_non_interactive_discards(self) -> None:
        """Verifies that deleting a non-empty directory in source discards deletion with warning and preserves files."""
        pkg = "pkg_del_non_empty_dir"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("non_empty_to_preserve")
        src_dir = src_pkg_dir / rel_path
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "user_file.txt").write_text("user content", encoding="utf-8")

        # Commit source file so it passes cleanliness guard
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src non empty"], cwd=self.workspace_path, check=True, capture_output=True)

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.primitives.adopt_repo", level="WARNING") as log_cm:
                resolved = handle_single_deletion(
                    self.workspace_config.render_engine_configs,
                    pkg,
                    src_pkg_dir,
                    rel_path,
                    interactive=False,
                )
                self.assertTrue(any("DISCARD" in msg for msg in log_cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_handle_single_deletion_non_empty_dir_interactive_options(self) -> None:
        """Verifies interactive choices for non-empty directory deletion: discard, confirmed delete, and aborted delete."""
        pkg = "pkg_del_non_empty_interactive"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        rel_path = Path("interactive_non_empty")
        src_dir = src_pkg_dir / rel_path
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "file1.txt").write_text("content 1", encoding="utf-8")
        (src_dir / "file2.txt").write_text("content 2", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init interactive non empty"], cwd=self.workspace_path, check=True, capture_output=True)

        # 1. Choice [1]: Discard deletion -> preserves files, returns True
        with patch("builtins.input", return_value="1"):
            resolved = handle_single_deletion(
                self.workspace_config.render_engine_configs,
                pkg,
                src_pkg_dir,
                rel_path,
                interactive=True,
            )
            self.assertTrue(resolved)
            self.assertTrue(src_dir.is_dir())
            self.assertTrue((src_dir / "file1.txt").exists())

        # 2. Choice [2] then 'n': Abort deletion -> preserves files, returns False
        with patch("builtins.input", side_effect=["2", "n"]):
            resolved = handle_single_deletion(
                self.workspace_config.render_engine_configs,
                pkg,
                src_pkg_dir,
                rel_path,
                interactive=True,
            )
            self.assertFalse(resolved)
            self.assertTrue(src_dir.is_dir())
            self.assertTrue((src_dir / "file1.txt").exists())

        # 3. Choice [2] then 'y': Confirm deletion -> deletes directory and all files, returns True
        with patch("builtins.input", side_effect=["2", "y"]):
            resolved = handle_single_deletion(
                self.workspace_config.render_engine_configs,
                pkg,
                src_pkg_dir,
                rel_path,
                interactive=True,
            )
            self.assertTrue(resolved)
            self.assertFalse(src_dir.exists())

    def test_get_drifted_packages_with_spaces_and_renames(self) -> None:
        """Verifies get_drifted_packages handles Git-quoted paths containing spaces and staged renames."""
        pkg_space = "pkg_space"
        pkg_rename = "pkg_rename"
        for p in (pkg_space, pkg_rename):
            (self.src_dir / p).mkdir(parents=True, exist_ok=True)
            (self.install_dir / p).mkdir(parents=True, exist_ok=True)

        # 1. File with spaces in filename (causes Git porcelain to wrap path in double quotes)
        (self.install_dir / pkg_space / "my file with spaces.txt").write_text("spaced", encoding="utf-8")

        # 2. Staged rename in pkg_rename
        orig = self.install_dir / pkg_rename / "old.txt"
        orig.write_text("rename me", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit orig"], cwd=self.install_dir, check=True, capture_output=True)
        # Re-create spaced file after commit so it's untracked, and git mv orig
        (self.install_dir / pkg_space / "my file with spaces.txt").write_text("spaced v2", encoding="utf-8")
        subprocess.run(["git", "mv", f"{pkg_rename}/old.txt", f"{pkg_rename}/new.txt"], cwd=self.install_dir, check=True, capture_output=True)

        self.assertEqual(get_drifted_packages(self.workspace_config), [pkg_rename, pkg_space])

    def test_generate_adjusted_patch_preserves_dash_and_plus_body_lines(self) -> None:
        """Verifies generate_adjusted_patch only rewrites headers before '@@' and preserves '--- '/'+++ ' lines in hunks."""
        from drift.primitives.adopt_repo import generate_adjusted_patch

        pkg = "pkg_patch_body"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Original file has a line starting with `-- ` (which becomes `--- ` when deleted in unified diff)
        # plus enough unchanged lines (>= 50% similarity) for git diff to emit a single rename diff.
        old_file = pkg_install_dir / "old_script.lua"
        old_file.write_text(
            "local x = 1\n"
            "local a = 1\n"
            "local b = 2\n"
            "local c = 3\n"
            "-- old lua comment\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_script.lua"], cwd=self.install_dir, check=True, capture_output=True)

        # Rename and replace `-- old lua comment` with `++ counter comment` (becomes `+++ counter comment` in diff)
        subprocess.run(["git", "mv", f"{pkg}/old_script.lua", f"{pkg}/new_script.lua"], cwd=self.install_dir, check=True, capture_output=True)
        (pkg_install_dir / "new_script.lua").write_text(
            "local x = 1\n"
            "local a = 1\n"
            "local b = 2\n"
            "local c = 3\n"
            "++ counter comment\n",
            encoding="utf-8",
        )

        adj_patch = generate_adjusted_patch(
            install_base=self.install_dir,
            pkg=pkg,
            new_rel_path=Path("new_script.lua"),
            old_rel_path=Path("old_script.lua"),
            target_src_filename="new_script.envst.lua",
        )

        lines = adj_patch.splitlines()
        self.assertEqual(lines[0], "--- a/new_script.envst.lua")
        self.assertEqual(lines[1], "+++ b/new_script.envst.lua")
        # Body lines inside the hunk must NOT be rewritten into `--- a/...` or `+++ b/...`
        self.assertIn("--- old lua comment", lines)
        self.assertIn("+++ counter comment", lines)
        self.assertEqual(lines.count("--- a/new_script.envst.lua"), 1)
        self.assertEqual(lines.count("+++ b/new_script.envst.lua"), 1)

        # Verify patch applies cleanly to a source template
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        target_src = src_pkg_dir / "new_script.envst.lua"
        target_src.write_text(
            "local x = $X_VAL\n"
            "local a = 1\n"
            "local b = 2\n"
            "local c = 3\n"
            "-- old lua comment\n",
            encoding="utf-8",
        )
        self.assertTrue(apply_source_patch(target_src, adj_patch))
        self.assertEqual(
            target_src.read_text(encoding="utf-8"),
            "local x = $X_VAL\n"
            "local a = 1\n"
            "local b = 2\n"
            "local c = 3\n"
            "++ counter comment\n",
        )

    def test_adopt_custom_engine_template_patches_instead_of_overwriting(self) -> None:
        """Verifies custom render engine templates (e.g. .j2) are patched rather than statically overwritten,
        and PackageAdoptPlan.format_text classifies static .toml/.sh files as static overwrites rather than templates.
        """
        from drift.config.render_engine_config import RenderEngineConfig

        pkg = "pkg_j2_adopt"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        # Register a j2 engine in workspace_config
        j2_engine = RenderEngineConfig(
            name="jinja2",
            input_file=None,
            suffix="j2",
            render_command=["cat", "%s"],
        )
        self.workspace_config.render_engine_configs["jinja2"] = j2_engine

        # Template in src/ has a Jinja2 expression on line 1 and static context on lines 2-5
        src_j2 = src_pkg / "config.toml.j2"
        src_j2.write_text(
            "host = '{{ hostname }}'\n"
            "line2 = 'a'\n"
            "line3 = 'b'\n"
            "line4 = 'c'\n"
            "port = 8080\n",
            encoding="utf-8",
        )
        # Static .toml file in src/ (NOT a template)
        src_static = src_pkg / "static.toml"
        src_static.write_text("mode = 'v1'\n", encoding="utf-8")

        # Rendered files in install/
        install_j2 = install_pkg / "config.toml"
        install_j2.write_text(
            "host = 'prod-box'\n"
            "line2 = 'a'\n"
            "line3 = 'b'\n"
            "line4 = 'c'\n"
            "port = 8080\n",
            encoding="utf-8",
        )
        install_static = install_pkg / "static.toml"
        install_static.write_text("mode = 'v1'\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init j2 and static src"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init j2 and static install"], cwd=self.install_dir, check=True, capture_output=True)

        # Drift port on line 5 of install/config.toml and mode in install/static.toml
        install_j2.write_text(
            "host = 'prod-box'\n"
            "line2 = 'a'\n"
            "line3 = 'b'\n"
            "line4 = 'c'\n"
            "port = 9090\n",
            encoding="utf-8",
        )
        install_static.write_text("mode = 'v2'\n", encoding="utf-8")

        plan = plan_package_adopt(self.workspace_config, pkg)

        # Verify plan.format_text() formats `config.toml.j2` as a template patch and `static.toml` as a Static Overwrite
        preview_text = plan.format_text()
        self.assertIn("config.toml [Patch applies cleanly to: config.toml.j2]", preview_text)
        self.assertIn("static.toml [Static Overwrite of: static.toml]", preview_text)

        # Execute adopt and verify `{{ hostname }}` in config.toml.j2 was preserved (patched, not overwritten!)
        res = execute_package_adopt(self.workspace_config, plan, AdoptOptions(interactive=False))
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("{{ hostname }}", src_j2.read_text(encoding="utf-8"))
        self.assertIn("port = 9090", src_j2.read_text(encoding="utf-8"))
        self.assertEqual(src_static.read_text(encoding="utf-8"), "mode = 'v2'\n")

    def test_check_is_drifted_path_converged_matrix(self) -> None:
        """Unit test probing check_is_drifted_path_converged across identical, content-divergent, permission-divergent, and deleted paths."""
        from drift.primitives.adopt_repo import check_is_drifted_path_converged

        pkg = "pkg_conv_matrix"
        install_pkg = self.install_dir / pkg
        render_pkg = self.workspace_config.render_path / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)
        render_pkg.mkdir(parents=True, exist_ok=True)

        # Case 1: Both files absent (converged deletion)
        self.assertTrue(check_is_drifted_path_converged(install_pkg, render_pkg, Path("deleted.txt")))

        # Case 2: Present in install/, absent in render/ (not converged)
        (install_pkg / "only_install.txt").write_text("hello", encoding="utf-8")
        self.assertFalse(check_is_drifted_path_converged(install_pkg, render_pkg, Path("only_install.txt")))

        # Case 3: Identical content and permissions (converged)
        (install_pkg / "same.txt").write_text("identical", encoding="utf-8")
        (render_pkg / "same.txt").write_text("identical", encoding="utf-8")
        self.assertTrue(check_is_drifted_path_converged(install_pkg, render_pkg, Path("same.txt")))

        # Case 4: Different content (not converged)
        (install_pkg / "diff.txt").write_text("v1", encoding="utf-8")
        (render_pkg / "diff.txt").write_text("v2", encoding="utf-8")
        self.assertFalse(check_is_drifted_path_converged(install_pkg, render_pkg, Path("diff.txt")))

        # Case 5: Identical content, different executable permission bit on POSIX (not converged)
        if sys.platform != "win32":
            (install_pkg / "script.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            (render_pkg / "script.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            (install_pkg / "script.sh").chmod(0o755)
            (render_pkg / "script.sh").chmod(0o644)
            self.assertFalse(check_is_drifted_path_converged(install_pkg, render_pkg, Path("script.sh")))

    def test_resolve_and_commit_converged_drifts_full_and_partial(self) -> None:
        """Two-Pass Idempotency Test: Verifies resolve_and_commit_converged_drifts commits only converged files and preserves divergent files."""
        from drift.primitives.adopt_repo import resolve_and_commit_converged_drifts
        from drift.utils.git_utils import get_git_status_porcelain

        pkg = "pkg_conv_commit"
        src_pkg = self.src_dir / pkg
        install_pkg = self.install_dir / pkg
        render_pkg = self.workspace_config.render_path / pkg
        for d in (src_pkg, install_pkg, render_pkg):
            d.mkdir(parents=True, exist_ok=True)
            (d / "conv.txt").write_text("v1", encoding="utf-8")
            (d / "div.txt").write_text("v1", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conv src"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conv install"], cwd=self.install_dir, check=True, capture_output=True)

        # Modify conv.txt identically in src/, render/, and install/; modify div.txt only in install/
        (src_pkg / "conv.txt").write_text("v2-converged", encoding="utf-8")
        (render_pkg / "conv.txt").write_text("v2-converged", encoding="utf-8")
        (install_pkg / "conv.txt").write_text("v2-converged", encoding="utf-8")
        (install_pkg / "div.txt").write_text("v2-diverged-host", encoding="utf-8")

        # Pass 1 (Cold / Partial Convergence Commit):
        converged_1 = resolve_and_commit_converged_drifts(self.workspace_config, [pkg], dry_run=False)
        self.assertEqual(converged_1, {pkg: [Path("conv.txt")]})
        status_lines = get_git_status_porcelain(self.install_dir, f"{pkg}/")
        self.assertEqual(len(status_lines), 1)
        self.assertIn(f"{pkg}/div.txt", status_lines[0])

        # Now converge div.txt in render/ as well
        (src_pkg / "div.txt").write_text("v2-diverged-host", encoding="utf-8")
        (render_pkg / "div.txt").write_text("v2-diverged-host", encoding="utf-8")
        converged_2 = resolve_and_commit_converged_drifts(self.workspace_config, [pkg], dry_run=False)
        self.assertEqual(converged_2, {pkg: [Path("div.txt")]})
        self.assertEqual(get_git_status_porcelain(self.install_dir, f"{pkg}/"), [])

        # Pass 2 (Warm / Idempotent Pass):
        converged_warm = resolve_and_commit_converged_drifts(self.workspace_config, [pkg], dry_run=False)
        self.assertEqual(converged_warm, {})
        self.assertEqual(get_git_status_porcelain(self.install_dir, f"{pkg}/"), [])

    def test_adopt_repo_converged_files_partition_and_direct_staging(self) -> None:
        """Verifies plan_package_adopt partitions converged files into plan.converged_files, supports skip_patches=True, and execute_package_adopt stages them directly without touching src/."""
        from drift.utils.git_utils import get_git_status_porcelain

        pkg = "pkg_adopt_conv_plan"
        src_pkg = self.src_dir / pkg
        install_pkg = self.install_dir / pkg
        render_pkg = self.workspace_config.render_path / pkg
        for d in (src_pkg, install_pkg, render_pkg):
            d.mkdir(parents=True, exist_ok=True)
            (d / "conv_mod.txt").write_text("v1", encoding="utf-8")
            (d / "div_mod.txt").write_text("v1", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init adopt conv src"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init adopt conv install"], cwd=self.install_dir, check=True, capture_output=True)

        # Modify conv_mod.txt in render/ and install/ (e.g. template rendered in render/, reverse-synced in install/),
        # while leaving conv_mod.txt in src/ with an uncommitted local edit!
        # Because conv_mod.txt is converged (install/ == render/), normal adopt with no_render=True should stage it directly
        # without touching src/conv_mod.txt or triggering a dirty-source error!
        (src_pkg / "conv_mod.txt").write_text("uncommitted_template_source", encoding="utf-8")
        (render_pkg / "conv_mod.txt").write_text("v2-rendered-and-synced", encoding="utf-8")
        (install_pkg / "conv_mod.txt").write_text("v2-rendered-and-synced", encoding="utf-8")
        (install_pkg / "div_mod.txt").write_text("v2-diverged-host", encoding="utf-8")

        # 1. Verify plan_package_adopt partitions converged vs divergent
        pkg_plan = plan_package_adopt(self.workspace_config, pkg)
        self.assertEqual(pkg_plan.converged_files, [Path("conv_mod.txt")])
        self.assertEqual(pkg_plan.modifications, [Path("div_mod.txt")])
        self.assertNotIn("conv_mod.txt", pkg_plan.mod_patches)
        self.assertIn("div_mod.txt", pkg_plan.mod_patches)
        self.assertTrue(pkg_plan.has_drifts)
        self.assertTrue(pkg_plan.has_divergent_drifts)
        self.assertEqual(pkg_plan.total_drifts, 2)

        # 2. Verify prepare_adopt(..., AdoptOptions(skip_patches=True, no_render=True)) skips unified diff generation
        no_patch_plan = prepare_adopt(self.workspace_config, [pkg], AdoptOptions(skip_patches=True, no_render=True))
        no_patch_pkg_plan = no_patch_plan.get_package_plan(pkg)
        assert no_patch_pkg_plan is not None
        self.assertEqual(no_patch_pkg_plan.converged_files, [Path("conv_mod.txt")])
        self.assertEqual(no_patch_pkg_plan.modifications, [Path("div_mod.txt")])
        self.assertEqual(no_patch_pkg_plan.mod_patches, {})
        self.assertEqual(no_patch_pkg_plan.rename_patches, {})

        # 3. Verify pkg_plan.format_text() formats converged files
        preview_text = pkg_plan.format_text()
        self.assertIn("Converged (already identical to render/, will be staged directly):", preview_text)
        self.assertIn("conv_mod.txt", preview_text)

        # 4. Execute normal adopt (with no_render=True to preserve synthetic render/ state) and verify both converged and divergent files are resolved and committed
        res = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(interactive=False, force=False, no_render=True)
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.packages[0].adopted_converged, ["conv_mod.txt"])
        self.assertEqual(res.packages[0].adopted_modifications, ["div_mod.txt"])
        # src/conv_mod.txt was NOT overwritten by adopt (still holds uncommitted_template_source)
        self.assertEqual((src_pkg / "conv_mod.txt").read_text(encoding="utf-8"), "uncommitted_template_source")
        # src/div_mod.txt WAS updated by adopt
        self.assertEqual((src_pkg / "div_mod.txt").read_text(encoding="utf-8"), "v2-diverged-host")
        # install/ is completely clean
        self.assertEqual(get_git_status_porcelain(self.install_dir, f"{pkg}/"), [])

    def test_adopt_end_to_end_live_host_drifts_copy_and_symlink_two_pass_idempotency(self) -> None:
        """Two-Pass Idempotency Test: Verifies run_primitive_adopt_drifts automatically runs reverse-sync and render before adopting live host edits across copy and symlink packages."""
        from drift.primitives.deploy_repo import run_primitive_deploy_pipeline
        from drift.primitives.workspace_repair import repair_drift_workspace
        from drift.utils.git_utils import get_git_status_porcelain

        repair_drift_workspace(self.workspace_path)

        pkg_copy = "pkg_live_copy"
        pkg_sym = "pkg_live_sym"
        target_copy = Path(self.temp_dir.name) / "target_live_copy"
        target_sym = Path(self.temp_dir.name) / "target_live_sym"

        src_copy = self.src_dir / pkg_copy
        src_sym = self.src_dir / pkg_sym
        src_copy.mkdir(parents=True, exist_ok=True)
        src_sym.mkdir(parents=True, exist_ok=True)

        # Configure pkg_copy with install_method = "copy" and an env var
        (src_copy / "drift_package.toml").write_text(
            '[package]\n'
            f'target_directory = "{target_copy.as_posix()}"\n'
            'install_method = "copy"\n'
            '[env.default]\n'
            'APP_HOST = "localhost"\n',
            encoding="utf-8",
        )
        # Template file in pkg_copy with enough context lines for clean git apply
        (src_copy / "app.envst.conf").write_text(
            "host = $APP_HOST\n"
            "line_a = 1\n"
            "line_b = 2\n"
            "line_c = 3\n"
            "port = 8000\n",
            encoding="utf-8",
        )
        # Converged template in pkg_copy: user updates both src/ template and live target
        (src_copy / "conv.envst.conf").write_text(
            "host = $APP_HOST\n"
            "mode = v1\n",
            encoding="utf-8",
        )

        # Configure pkg_sym with install_method = "symlink"
        (src_sym / "drift_package.toml").write_text(
            '[package]\n'
            f'target_directory = "{target_sym.as_posix()}"\n'
            'install_method = "symlink"\n',
            encoding="utf-8",
        )
        (src_sym / "sym.txt").write_text("sym_v1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init live packages"], cwd=self.workspace_path, check=True, capture_output=True)

        # Enable and deploy both packages (render -> stage -> apply)
        self.workspace_config.packages_enable[pkg_copy] = True
        self.workspace_config.packages_enable[pkg_sym] = True
        deploy_res = run_primitive_deploy_pipeline(self.workspace_config, [pkg_copy, pkg_sym])
        self.assertEqual(deploy_res.status, "SUCCESS")

        # Now simulate live user workflow WITHOUT running reverse-sync or render manually:
        # 1. User edits live copy target `target_copy/app.conf` (port 8000 -> 9000)
        (target_copy / "app.conf").write_text(
            "host = localhost\n"
            "line_a = 1\n"
            "line_b = 2\n"
            "line_c = 3\n"
            "port = 9000\n",
            encoding="utf-8",
        )
        # 2. User updates `src_copy/conv.envst.conf` AND `target_copy/conv.conf` to match ("mode = v2")
        (src_copy / "conv.envst.conf").write_text(
            "host = $APP_HOST\n"
            "mode = v2\n",
            encoding="utf-8",
        )
        (target_copy / "conv.conf").write_text(
            "host = localhost\n"
            "mode = v2\n",
            encoding="utf-8",
        )
        # 3. User edits live symlink target `target_sym/sym.txt`
        (target_sym / "sym.txt").write_text("sym_v2\n", encoding="utf-8")

        # Pass 1 (Cold / Mutation Pass): Run `run_primitive_adopt_drifts` directly!
        res1 = run_primitive_adopt_drifts(self.workspace_config, [pkg_copy, pkg_sym])
        self.assertEqual(res1.status, "SUCCESS")
        pkg_results = {r.package: r for r in res1.packages}
        self.assertEqual(pkg_results[pkg_copy].status, "SUCCESS")
        self.assertEqual(pkg_results[pkg_copy].adopted_converged, ["conv.conf"])
        self.assertEqual(pkg_results[pkg_copy].adopted_modifications, ["app.conf"])
        self.assertEqual(pkg_results[pkg_sym].status, "SUCCESS")
        self.assertEqual(pkg_results[pkg_sym].adopted_modifications, ["sym.txt"])

        # Verify template variable `$APP_HOST` was preserved in `src_copy/app.envst.conf` and port was patched to 9000
        self.assertEqual(
            (src_copy / "app.envst.conf").read_text(encoding="utf-8"),
            "host = $APP_HOST\n"
            "line_a = 1\n"
            "line_b = 2\n"
            "line_c = 3\n"
            "port = 9000\n",
        )
        # Verify converged template `src_copy/conv.envst.conf` kept `$APP_HOST`
        self.assertEqual(
            (src_copy / "conv.envst.conf").read_text(encoding="utf-8"),
            "host = $APP_HOST\n"
            "mode = v2\n",
        )
        # Verify static file in `src_sym/sym.txt` was updated
        self.assertEqual((src_sym / "sym.txt").read_text(encoding="utf-8"), "sym_v2\n")
        # Verify install/ git repository is completely clean
        self.assertEqual(get_git_status_porcelain(self.install_dir), [])

        # Pass 2 (Warm / Idempotent Pass): Re-running immediately produces UP_TO_DATE with zero adopted changes or mutations
        res2 = run_primitive_adopt_drifts(self.workspace_config, [pkg_copy, pkg_sym])
        self.assertEqual(res2.status, "UP_TO_DATE")
        self.assertTrue(all(p.status == "UP_TO_DATE" and not p.has_adopted_changes for p in res2.packages))
        self.assertEqual(get_git_status_porcelain(self.install_dir), [])

    def test_adopt_options_no_reverse_sync_and_no_render_flags(self) -> None:
        """Verifies AdoptOptions(no_reverse_sync=True) skips pulling live copy changes and AdoptOptions(no_render=True) skips rendering src/ before planning."""
        from drift.primitives.deploy_repo import run_primitive_deploy_pipeline
        from drift.primitives.workspace_repair import repair_drift_workspace

        repair_drift_workspace(self.workspace_path)

        pkg = "pkg_opt_flags"
        target_dir = Path(self.temp_dir.name) / "target_opt_flags"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "drift_package.toml").write_text(
            '[package]\n'
            f'target_directory = "{target_dir.as_posix()}"\n'
            'install_method = "copy"\n'
            '[env.default]\n'
            'GREETING = "hello"\n',
            encoding="utf-8",
        )
        (src_pkg / "msg.envst.txt").write_text("$GREETING v1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init opt flags pkg"], cwd=self.workspace_path, check=True, capture_output=True)

        self.workspace_config.packages_enable[pkg] = True
        run_primitive_deploy_pipeline(self.workspace_config, [pkg])

        # Edit live target file in target_dir AND update template in src_pkg to match ("$GREETING v2\n")
        (target_dir / "msg.txt").write_text("hello v2\n", encoding="utf-8")
        (src_pkg / "msg.envst.txt").write_text("$GREETING v2\n", encoding="utf-8")

        # 1. With no_reverse_sync=True, live target edit in target_dir is NOT pulled into install/, so install/ has no drifts!
        plan_no_sync = prepare_adopt(
            self.workspace_config, [pkg], AdoptOptions(no_reverse_sync=True, no_render=True)
        )
        self.assertFalse(plan_no_sync.has_drifts)

        # 2. With no_reverse_sync=False and no_render=True, reverse-sync pulls "hello v2\n" into install/,
        #    but render/ still has "hello v1\n", so msg.txt is classified as a divergent modification (NOT converged)!
        plan_no_render = prepare_adopt(
            self.workspace_config, [pkg], AdoptOptions(no_reverse_sync=False, no_render=True)
        )
        pkg_plan_no_render = plan_no_render.get_package_plan(pkg)
        assert pkg_plan_no_render is not None
        self.assertEqual(pkg_plan_no_render.converged_files, [])
        self.assertEqual(pkg_plan_no_render.modifications, [Path("msg.txt")])

        # 3. With default options (no_reverse_sync=False, no_render=False), render updates render/ to "hello v2\n",
        #    so msg.txt is recognized as converged!
        plan_default = prepare_adopt(
            self.workspace_config, [pkg], AdoptOptions(no_reverse_sync=False, no_render=False)
        )
        pkg_plan_default = plan_default.get_package_plan(pkg)
        assert pkg_plan_default is not None
        self.assertEqual(pkg_plan_default.converged_files, [Path("msg.txt")])
        self.assertEqual(pkg_plan_default.modifications, [])

    def test_adopt_pre_source_hook_deduplication_with_and_without_no_render(self) -> None:
        """Verifies pre_source hook executes exactly once per package whether pre-adoption rendering is enabled or disabled via no_render."""
        from drift.hooks.lifecycle_hooks import HookExecFlags
        from drift.primitives.deploy_repo import run_primitive_deploy_pipeline
        from drift.primitives.workspace_repair import repair_drift_workspace

        if sys.platform == "win32":
            return

        repair_drift_workspace(self.workspace_path)

        pkg = "pkg_hook_dedup"
        target_dir = Path(self.temp_dir.name) / "target_hook_dedup"
        marker_file = Path(self.temp_dir.name) / "pre_source_count.log"
        src_pkg = self.src_dir / pkg
        hooks_dir = src_pkg / "drift_hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_script = hooks_dir / "pre_source.sh"
        hook_script.write_text(
            f"#!/bin/sh\necho 'fired' >> \"{marker_file.as_posix()}\"\n",
            encoding="utf-8",
        )
        hook_script.chmod(0o755)

        (src_pkg / "drift_package.toml").write_text(
            '[package]\n'
            f'target_directory = "{target_dir.as_posix()}"\n'
            'install_method = "copy"\n'
            '[hooks]\n'
            'pre_source = "drift_hooks/pre_source.sh"\n',
            encoding="utf-8",
        )
        (src_pkg / "data.txt").write_text("v1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init hook dedup pkg"], cwd=self.workspace_path, check=True, capture_output=True)

        # Deploy initial state (this fires pre_source once during deploy's render step; clear marker afterwards)
        self.workspace_config.packages_enable[pkg] = True
        run_primitive_deploy_pipeline(self.workspace_config, [pkg])
        if marker_file.exists():
            marker_file.unlink()

        # Case 1: Default adopt (no_render=False) -> pre_source runs during pre-adoption render and is NOT duplicated during execute_package_adopt
        (target_dir / "data.txt").write_text("v2\n", encoding="utf-8")
        res1 = run_primitive_adopt_drifts(self.workspace_config, [pkg], AdoptOptions(no_render=False))
        self.assertEqual(res1.status, "SUCCESS")
        self.assertTrue(marker_file.exists())
        self.assertEqual(len(marker_file.read_text(encoding="utf-8").splitlines()), 1)

        # Commit updated src/data.txt so source cleanliness guard passes for Case 2
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit v2 src"], cwd=self.workspace_path, check=True, capture_output=True)
        marker_file.unlink()

        # Case 2: Adopt with no_render=True -> pre_source was not triggered in prepare_adopt, so execute_package_adopt triggers it once
        (target_dir / "data.txt").write_text("v3\n", encoding="utf-8")
        res2 = run_primitive_adopt_drifts(self.workspace_config, [pkg], AdoptOptions(no_render=True))
        self.assertEqual(res2.status, "SUCCESS")
        self.assertTrue(marker_file.exists())
        self.assertEqual(len(marker_file.read_text(encoding="utf-8").splitlines()), 1)

        # Commit updated src/data.txt so source cleanliness guard passes for Case 3
        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit v3 src"], cwd=self.workspace_path, check=True, capture_output=True)
        marker_file.unlink()

        # Case 3: Adopt with no_hooks=True -> pre_source is suppressed in both prepare_adopt and execute_package_adopt
        (target_dir / "data.txt").write_text("v4\n", encoding="utf-8")
        res3 = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(flags=HookExecFlags(no_hooks=True))
        )
        self.assertEqual(res3.status, "SUCCESS")
        self.assertFalse(marker_file.exists())

    def test_adopt_status_5_state_matrix_and_format_text(self) -> None:
        """Verifies the 5-state AdoptStatus model (UP_TO_DATE, SUCCESS, PARTIAL, SKIPPED, FAILED),
        separation of conflicted_files vs skipped_files, and pure format_text() methods across plans and results.
        """
        from drift.models import AdoptResult, AdoptStatus, PackageAdoptResult
        from drift.primitives.adopt_repo import ItemAdoptOutcome

        # 0. Verify ItemAdoptOutcome truthiness
        self.assertTrue(bool(ItemAdoptOutcome.ADOPTED))
        self.assertFalse(bool(ItemAdoptOutcome.SKIPPED))
        self.assertFalse(bool(ItemAdoptOutcome.CONFLICT))

        pkg = "pkg_status_matrix"
        src_pkg = self.src_dir / pkg
        install_pkg = self.install_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "clean.txt").write_text("v1\n", encoding="utf-8")
        (install_pkg / "clean.txt").write_text("v1\n", encoding="utf-8")
        (src_pkg / "tmpl.conf.envst").write_text("KEY=$VAL\nLINE=1\n", encoding="utf-8")
        (install_pkg / "tmpl.conf").write_text("KEY=RENDERED\nLINE=1\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init status matrix src"], cwd=self.workspace_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init status matrix install"], cwd=self.install_dir, check=True, capture_output=True)

        # 1. UP_TO_DATE: No drifts in package or workspace
        res_up_to_date = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(no_render=True, no_reverse_sync=True)
        )
        self.assertEqual(res_up_to_date.status, AdoptStatus.UP_TO_DATE)
        self.assertEqual(res_up_to_date.packages[0].status, AdoptStatus.UP_TO_DATE)
        self.assertEqual(res_up_to_date.format_text(verbose=False), "✨ No drifted packages found in local state database.")
        self.assertIn(f"📦 Package '{pkg}' [✨ UP_TO_DATE]:", res_up_to_date.format_text(verbose=True))
        self.assertIn("No drifts detected.", res_up_to_date.format_text(verbose=True))

        # 2. Dry-run preview formatting (conflict vs clean modification)
        (install_pkg / "clean.txt").write_text("v2\n", encoding="utf-8")
        (install_pkg / "tmpl.conf").write_text("CONFLICT_LINE=X\nLINE=99\n", encoding="utf-8")
        res_dry = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(dry_run=True, no_render=True, no_reverse_sync=True)
        )
        self.assertTrue(res_dry.dry_run)
        dry_text = res_dry.format_text()
        self.assertIn("🔍 [DRY-RUN] Planned Drift Adoption Operations (install/ -> src/):", dry_text)
        self.assertIn(f"📦 Package '{pkg}':", dry_text)
        self.assertIn("~ clean.txt [Static Overwrite of: clean.txt]", dry_text)
        self.assertIn("~ tmpl.conf [CONFLICTS with template: tmpl.conf.envst]", dry_text)
        # Ensure `plan` is excluded from JSON serialization
        dry_dict = res_dry.to_dict()
        self.assertNotIn("plan", dry_dict["packages"][0])

        # 3. Interactive SKIPPED vs non-interactive CONFLICT -> PARTIAL
        # In interactive mode, user skips `clean.txt` (option 5) and `tmpl.conf` (option 6 on conflict menu)
        with patch("builtins.input", side_effect=["5", "6"]), patch("sys.stdout", StringIO()):
            res_skip_all = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_skip_all.status, AdoptStatus.SKIPPED)
        self.assertEqual(res_skip_all.packages[0].status, AdoptStatus.SKIPPED)
        self.assertEqual(sorted(res_skip_all.packages[0].skipped_files), ["clean.txt", "tmpl.conf"])
        self.assertEqual(res_skip_all.packages[0].conflicted_files, [])
        self.assertIn("[⚠️ SKIPPED]", res_skip_all.format_text())
        self.assertIn("[S] Skipped:", res_skip_all.format_text())

        # 4. Non-interactive execution: `clean.txt` is adopted, `tmpl.conf` has a template conflict -> PARTIAL
        res_partial = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(interactive=False, no_render=True, no_reverse_sync=True)
        )
        self.assertEqual(res_partial.status, AdoptStatus.PARTIAL)
        self.assertEqual(res_partial.packages[0].status, AdoptStatus.PARTIAL)
        self.assertEqual(res_partial.packages[0].adopted_modifications, ["clean.txt"])
        self.assertEqual(res_partial.packages[0].conflicted_files, ["tmpl.conf"])
        self.assertEqual(res_partial.packages[0].skipped_files, [])
        self.assertEqual(res_partial.packages[0].unresolved_files, ["tmpl.conf"])
        self.assertTrue(res_partial.packages[0].has_unresolved_files)
        partial_text = res_partial.format_text()
        self.assertIn("[⚠️ PARTIAL]", partial_text)
        self.assertIn("[!] Conflicted (requires manual resolution or --accept-conflicts):", partial_text)
        self.assertIn("! tmpl.conf", partial_text)

        # 5. Now only `tmpl.conf` remains drifted in install/; non-interactive adopt on it alone -> SKIPPED (with conflicted_files)
        res_conflict_only = run_primitive_adopt_drifts(
            self.workspace_config, [pkg], AdoptOptions(interactive=False, no_render=True, no_reverse_sync=True)
        )
        self.assertEqual(res_conflict_only.status, AdoptStatus.SKIPPED)
        self.assertEqual(res_conflict_only.packages[0].status, AdoptStatus.SKIPPED)
        self.assertEqual(res_conflict_only.packages[0].conflicted_files, ["tmpl.conf"])
        self.assertEqual(res_conflict_only.packages[0].skipped_files, [])

        # 6. Resolve remaining conflicted file via interactive Over-render & Freeze (option "4") -> SUCCESS
        with patch("builtins.input", return_value="4"), patch("sys.stdout", StringIO()):
            res_success = run_primitive_adopt_drifts(
                self.workspace_config,
                [pkg],
                AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True),
            )
        self.assertEqual(res_success.status, AdoptStatus.SUCCESS)
        self.assertEqual(res_success.packages[0].status, AdoptStatus.SUCCESS)
        self.assertEqual(list(res_success), [pkg])
        self.assertIn("[✨ SUCCESS]", res_success.format_text())

        # 7. Simulated FAILED result formatting
        res_failed = AdoptResult(
            command="adopt",
            status=AdoptStatus.FAILED,
            packages=[PackageAdoptResult(package=pkg, status=AdoptStatus.FAILED, error="Simulated error")],
            error_message="Simulated error",
        )
        self.assertIn("[❌ FAILED]", res_failed.format_text())
        self.assertIn("Simulated error", res_failed.format_text())

    def test_adopt_models_reexports_and_non_optional_plan(self) -> None:
        """Verifies all domain models reside in drift.models.*, are re-exported in drift.models,
        primitives, render, and drift, have a non-Optional PackageAdoptResult.plan,
        and cause zero circular imports across import orders.
        """
        import drift
        import drift.models as models_pkg
        import drift.models.adopt_models as adopt_models
        import drift.models.deploy_models as deploy_models
        import drift.models.install_models as install_models
        import drift.models.render_models as render_models
        import drift.models.reverse_sync_models as reverse_sync_models
        import drift.models.stage_models as stage_models
        import drift.models.uninstall_models as uninstall_models
        import drift.models.workspace_models as workspace_models
        import drift.primitives as primitives_pkg
        import drift.primitives.adopt_repo as adopt_repo
        import drift.render as render_pkg

        adopt_model_names = (
            "AdoptStatus",
            "ItemAdoptOutcome",
            "get_rename_patch_key",
            "AdoptOptions",
            "PackageDriftFiles",
            "PackageAdoptPlan",
            "AdoptPlan",
            "PackageAdoptResult",
            "AdoptResult",
        )
        for name in adopt_model_names:
            canonical = getattr(adopt_models, name)
            self.assertIs(getattr(models_pkg, name), canonical)
            self.assertIs(getattr(adopt_repo, name), canonical)
            self.assertIs(getattr(drift, name), canonical)
        for name in ("ItemAdoptOutcome", "AdoptOptions", "PackageDriftFiles", "PackageAdoptPlan", "AdoptPlan"):
            self.assertIs(getattr(primitives_pkg, name), getattr(adopt_models, name))

        # Verify all other domain model modules are re-exported identically across facades
        submodule_checks = (
            (reverse_sync_models, ("PackageReverseSyncPlan", "ReverseSyncPlan", "PackageReverseSyncResult", "ReverseSyncResult")),
            (render_models, ("RenderOptions", "PackageRenderResult", "RenderResult")),
            (stage_models, ("StageOptions", "PackageStagePlan", "StagePlan", "StageResult")),
            (install_models, ("InstallOptions", "PackageInstallContext", "PackageInstallPlan", "InstallPlan", "PackageInstallResult", "InstallResult")),
            (uninstall_models, ("UninstallOptions", "PackageUninstallContext", "PackageUninstallPlan", "UninstallPlan", "RestoredBackup", "PackageUninstallResult", "UninstallResult")),
            (deploy_models, ("NextActionType", "DeployOptions", "CompletedStep", "DeployFailure", "DeployResult", "RollbackResult", "PackageDeployPreview", "WorkspaceDeployPreview", "DeployPreview")),
            (workspace_models, ("WorkspaceDiffType", "GcResult", "NewPackageResult", "AddResourcePlan", "AddResourceResult", "PackageStatus", "StatusResult", "FileDiffDetail", "PackageDiffDetail", "DiffResult", "RepairCheckDetail", "RepairResult", "ComponentStatus", "CheckResult", "WorkspaceHealthReport", "PackageHealthStatus", "PackageHealthResult", "HealthResult", "CloneResult", "HookResult")),
        )
        for mod, names in submodule_checks:
            for name in names:
                canonical = getattr(mod, name)
                self.assertIs(getattr(models_pkg, name), canonical)
                self.assertIs(getattr(drift, name), canonical)
        self.assertIs(getattr(render_pkg, "RenderOptions"), render_models.RenderOptions)
        import drift.primitives.render_repo as render_repo
        self.assertIs(getattr(primitives_pkg, "run_primitive_2_render_packages"), render_repo.run_primitive_2_render_packages)
        self.assertIs(getattr(primitives_pkg, "run_primitive_3_commit_render_repo"), render_repo.run_primitive_3_commit_render_repo)
        self.assertIs(getattr(drift, "run_primitive_2_render_packages"), render_repo.run_primitive_2_render_packages)
        self.assertIs(getattr(drift, "run_primitive_3_commit_render_repo"), render_repo.run_primitive_3_commit_render_repo)

        # Verify PackageAdoptResult.plan is non-Optional and syncs package name bidirectionally
        res_from_pkg = adopt_models.PackageAdoptResult(package="pkg_alpha")
        self.assertIsInstance(res_from_pkg.plan, adopt_models.PackageAdoptPlan)
        self.assertEqual(res_from_pkg.plan.package, "pkg_alpha")
        self.assertIn("📦 Package 'pkg_alpha':", res_from_pkg.format_text(dry_run=True))

        res_from_plan = adopt_models.PackageAdoptResult(plan=adopt_models.PackageAdoptPlan(package="pkg_beta"))
        self.assertEqual(res_from_plan.package, "pkg_beta")
        self.assertEqual(res_from_plan.plan.package, "pkg_beta")

        # Verify zero circular imports when importing modules in various orders in fresh Python processes
        for import_stmt in (
            "import drift.models; import drift.config; import drift.primitives.adopt_repo",
            "import drift.config; import drift.models; import drift.primitives.adopt_repo",
            "import drift.models.adopt_models; import drift.models.install_models; import drift.models.render_models; import drift.config",
            "import drift.primitives.adopt_repo; import drift.models; import drift.config",
        ):
            proc = subprocess.run(
                [sys.executable, "-c", f"import sys; sys.path.insert(0, 'src'); {import_stmt}"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=f"Import failed for '{import_stmt}': {proc.stderr}")

    def test_apply_source_patch_accept_conflicts_exit_code_1_and_failures(self) -> None:
        """Verifies apply_source_patch and patch_and_edit handle real 'patch --merge' exit code 1
        by writing conflict markers and returning True, while rejecting fatal/reject failures.
        """
        pkg = "pkg_merge_conflict_patch"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        # 1. Rendered baseline in install/ HEAD
        install_file = pkg_install_dir / "auth.json"
        install_file.write_text(
            '{\n  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  }\n}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline auth.json"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Host drift in install/ working tree
        install_file.write_text(
            '{\n  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  },\n'
            '  "google": {\n    "type": "api_key",\n    "key": "real_key_2"\n  }\n}\n',
            encoding="utf-8",
        )
        patch_content = generate_unified_patch(self.install_dir, Path(pkg) / "auth.json")
        self.assertTrue(bool(patch_content.strip()))

        # 3. Source template with ${NEW_API_KEY}
        template_text = '{\n  "newapi": {\n    "type": "api_key",\n    "key": "${NEW_API_KEY}"\n  }\n}\n'
        src_file = src_pkg_dir / "auth.envst.json"
        src_file.write_text(template_text, encoding="utf-8")

        # 4. Without accept_conflicts: returns False and does not write conflict markers or leave .rej
        self.assertFalse(apply_source_patch(src_file, patch_content, accept_conflicts=False))
        self.assertEqual(src_file.read_text(encoding="utf-8"), template_text)
        self.assertFalse(src_file.with_suffix(".json.rej").exists())

        # 5. With accept_conflicts=True ('patch --merge' exits with code 1): returns True and writes markers
        self.assertTrue(apply_source_patch(src_file, patch_content, accept_conflicts=True))
        merged_text = src_file.read_text(encoding="utf-8")
        self.assertIn("<<<<<<<", merged_text)
        self.assertIn('"key": "${NEW_API_KEY}"', merged_text)
        self.assertIn("=======", merged_text)
        self.assertIn('"google": {', merged_text)
        self.assertIn(">>>>>>>", merged_text)
        self.assertFalse(src_file.with_suffix(".json.rej").exists())

        # 6. Verify patch_and_edit with accept_conflicts=True & open_editor=True launches editor and returns True
        src_file.write_text(template_text, encoding="utf-8")
        with patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor:
            self.assertTrue(
                patch_and_edit(
                    src_file,
                    patch_content,
                    install_file=install_file,
                    accept_conflicts=True,
                    open_editor=True,
                )
            )
            mock_editor.assert_called_once_with(src_file)
        self.assertIn("<<<<<<<", src_file.read_text(encoding="utf-8"))

        # 7. Fatal patch error (exit code >= 2) returns False even when accept_conflicts=True
        with patch("drift.primitives.adopt_repo.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=["patch"], returncode=2, stdout="", stderr="fatal")
            self.assertFalse(apply_source_patch(src_file, patch_content, accept_conflicts=True))

    def test_adopt_interactive_conflict_menu_6_options_modification_and_rename(self) -> None:
        """Verifies the 6-option conflict menu order for both modifications and renames:
        [1] Write Merge Conflicts
        [2] Open Merge Conflict Editor
        [3] Open Side-by-Side Reference
        [4] Over-render & Freeze
        [5] Discard / Restore
        [6] Skip file
        plus non-interactive AdoptOptions(accept_conflicts=True).
        """
        from io import StringIO

        pkg = "pkg_conflict_menu_6"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        tmpl_init = '{\n  "newapi": {\n    "type": "api_key",\n    "key": "${NEW_API_KEY}"\n  }\n}\n'
        inst_base = '{\n  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  }\n}\n'
        inst_drift = (
            '{\n  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  },\n'
            '  "google": {\n    "type": "api_key",\n    "key": "real_key_2"\n  }\n}\n'
        )

        def reset_modification_fixture() -> tuple[Path, Path]:
            list(map(Path.unlink, filter(Path.is_file, src_pkg.glob("*"))))
            list(map(Path.unlink, filter(Path.is_file, install_pkg.glob("*"))))
            s_file = src_pkg / "auth.envst.json"
            i_file = install_pkg / "auth.json"
            s_file.write_text(tmpl_init, encoding="utf-8")
            i_file.write_text(inst_base, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=self.workspace_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "reset mod src"], cwd=self.workspace_path, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=self.install_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "reset mod install"], cwd=self.install_dir, check=True, capture_output=True)
            i_file.write_text(inst_drift, encoding="utf-8")
            return s_file, i_file

        # --- Modification Option [1]: Write Merge Conflicts (no editor) ---
        s_file, _ = reset_modification_fixture()
        with patch("builtins.input", return_value="1"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor, \
             patch("sys.stdout", StringIO()) as mock_out:
            res_opt1 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_opt1.status, AdoptStatus.SUCCESS)
        mock_editor.assert_not_called()
        self.assertIn("<<<<<<<", s_file.read_text(encoding="utf-8"))
        menu_out = mock_out.getvalue()
        self.assertIn("[1] Write Merge Conflicts", menu_out)
        self.assertIn("[2] Open Merge Conflict Editor", menu_out)
        self.assertIn("[3] Open Side-by-Side Reference", menu_out)
        self.assertIn("[4] Over-render & Freeze", menu_out)
        self.assertIn("[5] Discard modifications / Restore", menu_out)
        self.assertIn("[6] Skip file", menu_out)

        # --- Modification Option [2]: Open Merge Conflict Editor (launches editor) ---
        s_file, _ = reset_modification_fixture()
        with patch("builtins.input", return_value="2"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor, \
             patch("sys.stdout", StringIO()):
            res_opt2 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_opt2.status, AdoptStatus.SUCCESS)
        mock_editor.assert_called_once_with(s_file)
        self.assertIn("<<<<<<<", s_file.read_text(encoding="utf-8"))

        # --- Modification Option [3]: Open Side-by-Side Reference ---
        s_file, i_file = reset_modification_fixture()
        with patch("builtins.input", return_value="3"), \
             patch("drift.primitives.adopt_repo.launch_side_by_side_editor") as mock_sbs, \
             patch("sys.stdout", StringIO()):
            res_opt3 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_opt3.status, AdoptStatus.SUCCESS)
        mock_sbs.assert_called_once_with([(s_file, i_file)])

        # --- Modification Option [4]: Over-render & Freeze ---
        s_file, _ = reset_modification_fixture()
        with patch("builtins.input", return_value="4"), patch("sys.stdout", StringIO()):
            res_opt4 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_opt4.status, AdoptStatus.SUCCESS)
        self.assertEqual(s_file.read_text(encoding="utf-8"), inst_drift)
        self.assertEqual((src_pkg / "auth.envst.json.bak").read_text(encoding="utf-8"), tmpl_init)

        # --- Modification Option [5]: Discard / Restore ---
        s_file, _ = reset_modification_fixture()
        with patch("builtins.input", return_value="5"), patch("sys.stdout", StringIO()):
            res_opt5 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_opt5.status, AdoptStatus.SUCCESS)
        self.assertEqual(s_file.read_text(encoding="utf-8"), tmpl_init)

        # --- Non-interactive accept_conflicts=True ---
        s_file, _ = reset_modification_fixture()
        res_non_inter = run_primitive_adopt_drifts(
            self.workspace_config,
            [pkg],
            AdoptOptions(interactive=False, accept_conflicts=True, no_render=True, no_reverse_sync=True),
        )
        self.assertEqual(res_non_inter.status, AdoptStatus.SUCCESS)
        self.assertIn("<<<<<<<", s_file.read_text(encoding="utf-8"))

        # --- Conflicted Rename Options [1]-[6] ---
        ren_prefix = "".join(f'  "provider_{i}": {{"type": "static", "enabled": true}},\n' for i in range(8))
        ren_tmpl_init = "{\n" + ren_prefix + '  "newapi": {\n    "type": "api_key",\n    "key": "${NEW_API_KEY}"\n  }\n}\n'
        ren_inst_base = "{\n" + ren_prefix + '  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  }\n}\n'
        ren_inst_drift = (
            "{\n" + ren_prefix + '  "newapi": {\n    "type": "api_key",\n    "key": "real_key_1"\n  },\n'
            '  "google": {\n    "type": "api_key",\n    "key": "real_key_2"\n  }\n}\n'
        )

        def reset_rename_fixture() -> tuple[Path, Path]:
            list(map(Path.unlink, filter(Path.is_file, src_pkg.glob("*"))))
            list(map(Path.unlink, filter(Path.is_file, install_pkg.glob("*"))))
            old_s = src_pkg / "old_auth.envst.json"
            old_i = install_pkg / "old_auth.json"
            old_s.write_text(ren_tmpl_init, encoding="utf-8")
            old_i.write_text(ren_inst_base, encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=self.workspace_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "reset rename src"], cwd=self.workspace_path, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=self.install_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "reset rename install"], cwd=self.install_dir, check=True, capture_output=True)
            subprocess.run(["git", "mv", f"{pkg}/old_auth.json", f"{pkg}/new_auth.json"], cwd=self.install_dir, check=True, capture_output=True)
            new_i = install_pkg / "new_auth.json"
            new_i.write_text(ren_inst_drift, encoding="utf-8")
            return old_s, new_i

        # Rename Option [1]: Write Merge Conflicts (no editor)
        reset_rename_fixture()
        with patch("builtins.input", return_value="1"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor, \
             patch("sys.stdout", StringIO()) as mock_ren_out:
            res_ren1 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren1.status, AdoptStatus.SUCCESS)
        mock_editor.assert_not_called()
        new_src = src_pkg / "new_auth.envst.json"
        self.assertTrue(new_src.is_file())
        self.assertFalse((src_pkg / "old_auth.envst.json").exists())
        self.assertIn("<<<<<<<", new_src.read_text(encoding="utf-8"))
        ren_menu_out = mock_ren_out.getvalue()
        self.assertIn("[1] Write Merge Conflicts", ren_menu_out)
        self.assertIn("[2] Open Merge Conflict Editor", ren_menu_out)
        self.assertIn("[3] Open Side-by-Side Reference", ren_menu_out)
        self.assertIn("[4] Over-render & Freeze", ren_menu_out)
        self.assertIn("[5] Discard rename / Restore", ren_menu_out)
        self.assertIn("[6] Skip file", ren_menu_out)

        # Rename Option [2]: Open Merge Conflict Editor (with editor)
        reset_rename_fixture()
        with patch("builtins.input", return_value="2"), \
             patch("drift.primitives.adopt_repo.launch_single_file_editor") as mock_editor, \
             patch("sys.stdout", StringIO()):
            res_ren2 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren2.status, AdoptStatus.SUCCESS)
        new_src = src_pkg / "new_auth.envst.json"
        mock_editor.assert_called_once_with(new_src)
        self.assertIn("<<<<<<<", new_src.read_text(encoding="utf-8"))

        # Rename Option [3]: Open Side-by-Side Reference
        reset_rename_fixture()
        with patch("builtins.input", return_value="3"), \
             patch("drift.primitives.adopt_repo.launch_side_by_side_editor") as mock_sbs, \
             patch("sys.stdout", StringIO()):
            res_ren3 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren3.status, AdoptStatus.SUCCESS)
        mock_sbs.assert_called_once_with([(src_pkg / "new_auth.envst.json", install_pkg / "new_auth.json")])

        # Rename Option [4]: Over-render & Freeze
        reset_rename_fixture()
        with patch("builtins.input", return_value="4"), patch("sys.stdout", StringIO()):
            res_ren4 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren4.status, AdoptStatus.SUCCESS)
        self.assertEqual((src_pkg / "new_auth.envst.json").read_text(encoding="utf-8"), ren_inst_drift)
        self.assertEqual((src_pkg / "new_auth.envst.json.bak").read_text(encoding="utf-8"), ren_tmpl_init)

        # Rename Option [5]: Discard rename / Restore
        reset_rename_fixture()
        with patch("builtins.input", return_value="5"), patch("sys.stdout", StringIO()):
            res_ren5 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren5.status, AdoptStatus.SUCCESS)
        self.assertTrue((src_pkg / "old_auth.envst.json").is_file())
        self.assertFalse((src_pkg / "new_auth.envst.json").exists())

        # Rename Option [6]: Skip file
        reset_rename_fixture()
        with patch("builtins.input", return_value="6"), patch("sys.stdout", StringIO()):
            res_ren6 = run_primitive_adopt_drifts(
                self.workspace_config, [pkg], AdoptOptions(interactive=True, no_render=True, no_reverse_sync=True)
            )
        self.assertEqual(res_ren6.status, AdoptStatus.SKIPPED)
        self.assertTrue((src_pkg / "old_auth.envst.json").is_file())
        self.assertFalse((src_pkg / "new_auth.envst.json").exists())


if __name__ == "__main__":
    unittest.main()
