"""Unit tests for Primitive 7 & Stage 1: drift adopt."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from drift.workspace_config import WorkspaceConfig
from drift.adopt_repo import (
    get_drifted_packages,
    check_source_cleanliness,
    get_package_drifts,
    generate_unified_patch,
    check_patch_conflicts,
    apply_source_patch,
    test_file_conflict,
    resolve_source_file_path,
    adopt_addition,
    ignore_addition,
    adopt_deletion,
    adopt_modification,
    fallback_over_render,
    adopt_single_package,
    run_primitive_adopt_drifts,
)


class TestAdopt(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.workspace_path = Path(self.temp_dir.name) / "workspace"
        self.workspace_path.mkdir()

        self.src_dir = self.workspace_path / "src"
        self.src_dir.mkdir()
        
        self.install_dir = self.workspace_path / "install"
        self.install_dir.mkdir()

        self.backup_dir = self.workspace_path / "backup"
        self.backup_dir.mkdir()

        # Initialize install repo as a git repository
        subprocess.run(["git", "init"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), check=True, capture_output=True)
        # Commit a dummy state.toml to establish HEAD
        with open(self.install_dir / "state.toml", "w", encoding="utf-8") as f:
            f.write("# dummy")
        subprocess.run(["git", "add", "state.toml"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Initialize main workspace repo as a git repository (for cleanliness check)
        subprocess.run(["git", "init"], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.workspace_path), check=True, capture_output=True)
        # Commit a dummy file
        with open(self.workspace_path / ".gitignore", "w", encoding="utf-8") as f:
            f.write("install/\nbackup/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial workspace commit"], cwd=str(self.workspace_path), check=True, capture_output=True)

        config_dir = self.workspace_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "envsubst.bash").write_text("#!/bin/bash\n", encoding="utf-8")

        from drift.workspace_config import RenderEngineConfig
        env_engine = RenderEngineConfig(
            name="envsubst",
            input_file=Path("envsubst.bash"),
            suffix="envst",
            render_command="bash -c 'source %i && envsubst < %s'"
        )
        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.workspace_path,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.workspace_path / "system_home",
            packages_enable={},
            render_engine_config = {"envsubst": env_engine}
        )
        

    def tearDown(self) -> None:
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

    def test_check_source_cleanliness(self) -> None:
        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)

        # Clean source directory should pass
        check_source_cleanliness(self.workspace_config, pkg, force=False)

        # Create uncommitted dirty file in source
        dirty_file = src_pkg_dir / "dirty.txt"
        dirty_file.write_text("unstaged change", encoding="utf-8")

        # Clean check should raise RuntimeError
        with self.assertRaises(RuntimeError):
            check_source_cleanliness(self.workspace_config, pkg, force=False)

        # Passing force=True should bypass check without error
        check_source_cleanliness(self.workspace_config, pkg, force=True)

    def test_get_package_drifts(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Setup a tracked file in Git HEAD
        tracked_file = pkg_install_dir / "tracked.txt"
        tracked_file.write_text("original content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Tracked files"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Setup a Deletion
        deleted_file = pkg_install_dir / "deleted.txt"
        deleted_file.write_text("will be deleted", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "To delete"], cwd=str(self.install_dir), check=True, capture_output=True)
        deleted_file.unlink()

        # Setup an Addition (Untracked) at the very end
        (pkg_install_dir / "added.txt").write_text("new content", encoding="utf-8")

        # Modify tracked file
        tracked_file.write_text("modified content", encoding="utf-8")

        # Run drift extraction
        additions, deletions, modifications, renames = get_package_drifts(self.install_dir, pkg)
        self.assertEqual(additions, [Path("added.txt")])
        self.assertEqual(deletions, [Path("deleted.txt")])
        self.assertEqual(modifications, [Path("tracked.txt")])
        self.assertEqual(renames, [])

    def test_get_package_drifts_rename(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup tracked file
        orig_file = pkg_install_dir / "old_name.txt"
        orig_file.write_text("hello", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 2. Perform git rename
        subprocess.run(["git", "mv", "pkg_a/old_name.txt", "pkg_a/new_name.txt"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Get package drifts
        additions, deletions, modifications, renames = get_package_drifts(self.install_dir, pkg)

        # Rename should be detected as a distinct rename branch
        self.assertEqual(deletions, [])
        self.assertEqual(additions, [])
        self.assertEqual(modifications, [])
        self.assertEqual(renames, [(Path("old_name.txt"), Path("new_name.txt"))])

    def test_apply_source_patch_clean(self) -> None:
        pkg = "pkg_a"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Create original file and commit
        tracked_file = pkg_install_dir / "file.txt"
        tracked_file.write_text("line 1\nline 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "original file"], cwd=str(self.install_dir), check=True, capture_output=True)

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

        # Test for a file that does not exist yet
        rel_path = Path("dot-bashrc")
        resolved = resolve_source_file_path(self.workspace_config, pkg, rel_path)
        self.assertIsNone(resolved) # Does not exist yet

        # Create static file dot-bashrc
        static_file = src_pkg_dir / "dot-bashrc"
        static_file.write_text("static", encoding="utf-8")
        resolved = resolve_source_file_path(self.workspace_config, pkg, rel_path)
        self.assertEqual(resolved, static_file)

        # Clean static, test template resolution (dot-bashrc -> dot-bashrc.envst)
        static_file.unlink()
        template_file = src_pkg_dir / "dot-bashrc.envst"
        template_file.write_text("templated", encoding="utf-8")
        resolved = resolve_source_file_path(self.workspace_config, pkg, rel_path)
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
        adopt_deletion(self.workspace_config, pkg, rel_path)

        # File should be removed from src/
        self.assertFalse(src_file.exists())

    def test_adopt_rename(self) -> None:
        """Verifies that adopting a rename correctly renames the template file inside src/ and applies any content diff."""
        from drift.adopt_repo import adopt_rename

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
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 3. Rename and modify file inside install/
        subprocess.run(["git", "mv", "pkg_a/dot-old_name.txt", "pkg_a/dot-new_name.txt"], cwd=str(self.install_dir), check=True, capture_output=True)
        (pkg_install_dir / "dot-new_name.txt").write_text("template content\ntemplate content\ntemplate content modified\n", encoding="utf-8")

        # 4. Adopt the rename
        from drift.adopt_repo import generate_adjusted_patch
        patch_content = generate_adjusted_patch(
            self.install_dir,
            pkg,
            Path("dot-new_name.txt"),
            old_rel_path=Path("dot-old_name.txt"),
            target_src_filename="dot-new_name.envst.txt"
        )
        adopt_rename(self.workspace_config, pkg, pkg_install_dir,
                     Path("dot-old_name.txt"), Path("dot-new_name.txt"), patch_content)

        # Verify old template is deleted, new template is created with dot- prefix and correct template suffixes, and modifications are applied!
        self.assertFalse(src_old_file.exists())
        
        src_new_file = src_pkg_dir / "dot-new_name.envst.txt"
        self.assertTrue(src_new_file.exists())
        self.assertEqual(src_new_file.read_text(encoding="utf-8"), "template content\ntemplate content\ntemplate content modified\n")

    def test_adopt_rename_missing_old_source_file(self) -> None:
        """Verifies that adopting a rename when the old source file does not exist correctly creates a new file and applies the full content."""
        from drift.adopt_repo import adopt_rename, generate_adjusted_patch

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup old file in install/ and commit, but NO template in src/
        install_old_file = pkg_install_dir / "dot-old_name.txt"
        install_old_file.write_text("template content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add old_name"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 2. Rename and modify file inside install/
        subprocess.run(["git", "mv", "pkg_a/dot-old_name.txt", "pkg_a/dot-new_name.txt"], cwd=str(self.install_dir), check=True, capture_output=True)
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
        adopt_rename(self.workspace_config, pkg, pkg_install_dir, Path("dot-old_name.txt"), Path("dot-new_name.txt"), patch_content)

        # Verify a new file is created with new name in src/ and contains full content
        src_new_file = src_pkg_dir / "dot-new_name.txt"
        self.assertTrue(src_new_file.exists())
        self.assertEqual(src_new_file.read_text(encoding="utf-8"), "template content\ntemplate content modified\n")

    def test_adopt_addition_conflict_target_exists(self) -> None:
        """Verifies that non-interactive adopt skips addition if the target already exists in source."""
        from drift.adopt_repo import handle_single_addition

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
            self.workspace_config,
            pkg,
            pkg_install_dir,
            rel_path,
            interactive=False
        )
        self.assertFalse(resolved)

    def test_adopt_deletion_target_missing(self) -> None:
        """Verifies that adopting deletion when target does not exist in source skips gracefully and returns True."""
        from drift.adopt_repo import handle_single_deletion

        pkg = "pkg_a"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # File does NOT exist in source, but is deleted in install pkg
        rel_path = Path("dot-missing.txt")

        resolved = handle_single_deletion(
            self.workspace_config,
            pkg,
            pkg_install_dir,
            rel_path,
            interactive=False
        )
        self.assertTrue(resolved)

    def test_adopt_rename_unstage_on_skip(self) -> None:
        """Verifies that if a rename is skipped or failed, both old and new paths get unstaged at the end."""
        from drift.adopt_repo import adopt_single_package

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
        
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init old"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 2. Rename on disk in install/ (but target_existing_src will trigger a conflict in non-interactive mode!)
        install_new_file = pkg_install_dir / "dot-new.txt"
        install_old_file.rename(install_new_file)
        
        # Create dot-new.txt in src/ to cause a target collision conflict, forcing the rename to skip/fail
        src_new_file = src_pkg_dir / "dot-new.txt"
        src_new_file.write_text("collision", encoding="utf-8")

        # 3. Run adopt_single_package non-interactively
        adopt_single_package(self.workspace_config, pkg, interactive=False)

        # 4. Check git status. Both pkg_a/dot-old.txt and pkg_a/dot-new.txt should be unstaged (not in staged index)
        # because the rename conflicted and was skipped, triggering selective git restore --staged.
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        scripts_dir = src_pkg_dir / "scripts"
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
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"scripts/prepare_src.sh\"\n",
            encoding="utf-8"
        )

        # Create a drift in install/
        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        adopt_single_package(self.workspace_config, pkg, interactive=False)

        # Hook must have run and generated hook_executed.txt in src_pkg_dir
        hook_out = src_pkg_dir / "hook_executed.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), "STATIC_HOOK_RAN")

        # Copied static hook must exist in render/
        rendered_hook = self.workspace_path / "render" / pkg / "scripts" / "prepare_src.sh"
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

        scripts_dir = src_pkg_dir / "scripts"
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
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"scripts/prepare_src.envst.sh\"\n",
            encoding="utf-8"
        )

        # Create a drift in install/
        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        adopt_single_package(self.workspace_config, pkg, interactive=False)

        # Hook must have run and generated hook_executed.txt in src_pkg_dir
        hook_out = src_pkg_dir / "hook_executed.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), f"HOOK_RAN_{pkg}")

        # Rendered hook must exist in render/
        rendered_hook = self.workspace_path / "render" / pkg / "scripts" / "prepare_src.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn(f"HOOK_RAN_{pkg}", rendered_hook.read_text(encoding="utf-8"))

    def test_adopt_pre_source_hook_failure_aborts_adopt(self) -> None:
        """Verifies that an error in pre_source hook is not suppressed and aborts adopt."""
        pkg = "pkg_adopt_failing_hook"
        src_pkg_dir = self.src_dir / pkg
        src_pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = src_pkg_dir / "scripts"
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
            f"[package]\nname = \"{pkg}\"\n\n[hooks]\npre_source = \"scripts/failing.sh\"\n",
            encoding="utf-8"
        )

        (pkg_install_dir / "new_file.txt").write_text("drift content", encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            adopt_single_package(self.workspace_config, pkg, interactive=False)
        self.assertIn("failed with exit code 1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
