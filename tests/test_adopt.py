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

        from unittest.mock import patch
        from io import StringIO
        self.stdout_patcher = patch("sys.stdout", StringIO())
        self.stdout_patcher.start()

    def tearDown(self) -> None:
        self.stdout_patcher.stop()
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src script"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init script"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Drift permissions in install/
        install_script.chmod(0o755)

        # Run adopt
        run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=False)

        # Source script should now have executable bit (0o755)
        self.assertTrue(bool(src_script.stat().st_mode & 0o111))

        # Check install/ git status - nothing should be staged
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init src app.conf"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init app.conf"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Record HEAD commit sha before adopt
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Modify install_file so that patch cannot apply cleanly to src_file template
        install_file.write_text("CONFLICTING_MODIFICATION=VALUE\nEXTRA_LINE=999\n", encoding="utf-8")

        # Run adopt
        run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=False)

        # HEAD commit in install/ repo MUST NOT have changed (install was not committed)
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # In install/ repo, the modified file must remain as an UNSTAGED drift (' M')
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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
        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init multi-pkg src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init multi-pkg install"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Drift in install/
        (install_clean / "config.json").write_text('{"version": 2}', encoding="utf-8")
        (install_conflict / "server.conf").write_text("CONFLICTING_KEY=VALUE\nPORT=9999\n", encoding="utf-8")

        # Run adopt across both packages
        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg_clean, pkg_conflict], interactive=False)

        # Only pkg_clean should be resolved
        self.assertEqual(resolved, [pkg_clean])

        # src/ for pkg_clean should be adopted
        self.assertEqual((src_clean / "config.json").read_text(encoding="utf-8"), '{"version": 2}')
        # src/ for pkg_conflict should be untouched
        self.assertIn("HOST=$MY_HOST", (src_conflict / "server.conf.envst").read_text(encoding="utf-8"))

        # In install/ repo:
        # pkg_clean should be committed (not in status)
        # pkg_conflict/server.conf should be UNSTAGED (' M')
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertNotIn(pkg_clean, res.stdout)
        self.assertIn(f" M {pkg_conflict}/server.conf", res.stdout)
        self.assertNotIn(f"M  {pkg_conflict}/server.conf", res.stdout)

    def test_adopt_partial_conflict_in_single_package_unstages_and_skips_commit(self) -> None:
        """Verifies that if one file in a package has a conflict, the entire package is un-staged and not committed."""
        pkg = "pkg_partial"
        src_pkg = self.src_dir / pkg
        src_pkg.mkdir(parents=True, exist_ok=True)
        install_pkg = self.install_dir / pkg
        install_pkg.mkdir(parents=True, exist_ok=True)

        (src_pkg / "static_file.txt").write_text("static v1", encoding="utf-8")
        (install_pkg / "static_file.txt").write_text("static v1", encoding="utf-8")

        (src_pkg / "template_file.conf.envst").write_text("KEY=$VAL\nLINE=A\n", encoding="utf-8")
        (install_pkg / "template_file.conf").write_text("KEY=RENDERED\nLINE=A\n", encoding="utf-8")

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init partial src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init partial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Drift in install/: static_file is clean, template_file has conflict
        (install_pkg / "static_file.txt").write_text("static v2", encoding="utf-8")
        (install_pkg / "template_file.conf").write_text("CONFLICT=TRUE\nLINE=Z\n", encoding="utf-8")

        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=False)

        # Package was not fully resolved
        self.assertEqual(resolved, [])

        # HEAD commit in install/ did not change
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # Conflict file is UNSTAGED
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertIn(f" M {pkg}/template_file.conf", res.stdout)
        self.assertNotIn(f"M  {pkg}/template_file.conf", res.stdout)

    def test_adopt_permission_drift_on_templated_file_applies_cleanly(self) -> None:
        """Verifies that a permission-only drift on a templated file adopts cleanly and commits."""
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init template perm src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init template perm install"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Drift mode to 0o755 in install/
        install_script.chmod(0o755)

        resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=False)

        # Package must be resolved cleanly
        self.assertEqual(resolved, [pkg])

        # Source template file must now have executable bit set
        self.assertTrue(bool(src_template.stat().st_mode & 0o111))

        # install/ repo should be committed and clean
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init notes src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init notes install"], cwd=str(self.install_dir), check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Drift in install/
        (install_pkg / "notes.txt").write_text("notes v2", encoding="utf-8")

        # Mock input to choose [3] Skip file
        with patch("builtins.input", return_value="3"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=True)

        self.assertEqual(resolved, [])

        # HEAD commit in install/ did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File remains as UNSTAGED drift in install/
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init add skip src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init add skip install"], cwd=str(self.install_dir), check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Add new untracked file in install/
        (install_pkg / "wild_addition.txt").write_text("wild content", encoding="utf-8")

        # Interactive choose [4] Skip file
        with patch("builtins.input", return_value="4"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=True)

        self.assertEqual(resolved, [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is untracked '??', NOT staged 'A '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init del skip src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init del skip install"], cwd=str(self.install_dir), check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Delete file in install/
        (install_pkg / "delete_me.txt").unlink()

        # Interactive choose [3] Skip file
        with patch("builtins.input", return_value="3"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=True)

        self.assertEqual(resolved, [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is unstaged deletion ' D', NOT staged 'D '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
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

        subprocess.run(["git", "add", "."], cwd=str(self.workspace_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conflict interactive src"], cwd=str(self.workspace_path), check=True, capture_output=True)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init conflict interactive install"], cwd=str(self.install_dir), check=True, capture_output=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()

        # Conflicting modification in install/
        (install_pkg / "service.conf").write_text("COMPLETELY_DIFFERENT=999\nOTHER=ZZZ\n", encoding="utf-8")

        # Interactive choose [5] Skip file
        with patch("builtins.input", return_value="5"):
            resolved = run_primitive_adopt_drifts(self.workspace_config, [pkg], interactive=True)

        self.assertEqual(resolved, [])

        # install/ repo HEAD did not advance
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.install_dir), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head_before, head_after)

        # File is unstaged modification ' M', NOT staged 'M '
        res = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertIn(f" M {pkg}/service.conf", res.stdout)
        self.assertNotIn(f"M  {pkg}/service.conf", res.stdout)


if __name__ == "__main__":
    unittest.main()
