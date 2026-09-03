import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from drift.constants import PACKAGE_CONFIG_FILE_NAME, DRIFT_IGNORE_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.package_config import PackageConfig, PackageHooks
from drift.state_registry import (
        load_state_registry,
        save_state_registry,
        StateRegistry,
        PackageState
)
from drift.install_repo import (
        resolve_system_target,
        run_primitive_5_install_deployment,
        get_stow_version,
        is_stow_version_sufficient,
        find_internal_symlink_conflicts,
        resolve_single_internal_symlink_conflict,
        handle_internal_symlink_conflicts,
        deploy_single_stow_file,
)
from drift.file_utils import (
        ensure_dir_exists_with_sudo,
        ensure_directory_writable,
)


class TestInstallRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name).resolve()
        self.drift_root = temp_root / "drift_workspace"
        self.system_target_dir = temp_root / "system_home"

        # Create workspace structures
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        self.workspace_config = WorkspaceConfig(
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            packages_enable={
                "pkg_stow": True,
                "pkg_copy": True,
            },
            packages_enable_default=False,
            drift_root_path=self.drift_root,
            default_target_directory=self.system_target_dir
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_state_registry_load_and_save(self) -> None:
        """Verifies StateRegistry manages state.toml transitions correctly."""
        state_file = self.install_dir / "state.toml"
        
        # Test loading missing registry
        registry = load_state_registry(state_file)
        self.assertEqual(registry.packages, {})
        self.assertFalse(registry.has_deploying_package())

        # Test setting and saving states
        registry.set_package_state("nvim", "deploying")
        registry.set_package_state("tmux", "installed")
        self.assertTrue(registry.has_deploying_package())
        self.assertEqual(registry.get_package_state("nvim"), "deploying")
        self.assertEqual(registry.get_package_state("tmux"), "installed")

        save_state_registry(state_file, registry)
        self.assertTrue(os.path.isfile(state_file))

        # Test loading from file
        loaded = load_state_registry(state_file)
        self.assertEqual(loaded.get_package_state("nvim"), "deploying")
        self.assertEqual(loaded.get_package_state("tmux"), "installed")
        self.assertTrue(loaded.has_deploying_package())

        # Test removing
        loaded.remove_package("nvim")
        self.assertFalse(loaded.has_deploying_package())
        self.assertIsNone(loaded.get_package_state("nvim"))

    def test_package_state_dataclass(self) -> None:
        """Verifies the PackageState dataclass attributes and defaults."""
        p_state = PackageState(state="installed", last_deployed="2026-08-17", install_method="copy", deployed_files=[Path("file1"), Path("file2")])
        self.assertEqual(p_state.state, "installed")
        self.assertEqual(p_state.last_deployed, "2026-08-17")
        self.assertEqual(p_state.install_method, "copy")
        self.assertEqual(p_state.deployed_files, [Path("file1"), Path("file2")])

        # Test defaults
        default_state = PackageState(state="deploying")
        self.assertEqual(default_state.state, "deploying")
        self.assertIsNone(default_state.last_deployed)
        self.assertIsNone(default_state.install_method)
        self.assertEqual(default_state.deployed_files, [])

    def test_resolve_system_target(self) -> None:
        """Verifies resolves system targets correctly, translating dot- prefixes to dot."""
        # Simple file resolution
        resolved = resolve_system_target(Path("dot-bashrc"), self.system_target_dir)
        self.assertEqual(resolved, self.system_target_dir / ".bashrc")

        # Nested folder and file resolution
        resolved = resolve_system_target(Path("dot-config/nvim/dot-init.lua"), self.system_target_dir)
        self.assertEqual(resolved, self.system_target_dir / ".config" / "nvim" / ".init.lua")

        # Non-prefixed parts remain untouched
        resolved = resolve_system_target(Path("regular_dir/regular_file.txt"), self.system_target_dir)
        self.assertEqual(resolved, self.system_target_dir / "regular_dir" / "regular_file.txt")

    def test_install_stow_incremental_deployment(self) -> None:
        """Verifies stow incremental file-by-file manual symlinking deployment."""
        pkg = "pkg_stow"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "dot-bashrc"), "w", encoding="utf-8") as f:
            f.write("content of bashrc")

        # Run deployment
        from drift.stage_repo import PackageStageChanges
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=[Path("dot-bashrc")])])

        # Verify symlink is created
        target_file = os.path.join(self.system_target_dir, ".bashrc")
        self.assertTrue(os.path.islink(target_file))
        link_target = os.readlink(target_file)
        abs_link_target = os.path.abspath(os.path.join(os.path.dirname(target_file), link_target))
        self.assertEqual(abs_link_target, os.path.join(pkg_install_dir, "dot-bashrc"))

        # Verify state.toml transition
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        self.assertEqual(registry.get_package_state(pkg), "installed")

    def test_install_stow_collision_guard(self) -> None:
        """Verifies Stow Collision Guard backs up pre-existing physical files at target."""
        pkg = "pkg_stow"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        with open(os.path.join(pkg_install_dir, "dot-bashrc"), "w", encoding="utf-8") as f:
            f.write("new content")

        # Create physical non-symlink file at system target (simulating pre-existing collision file)
        target_file = os.path.join(self.system_target_dir, ".bashrc")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("pre-existing user content")

        # Run deployment
        from drift.stage_repo import PackageStageChanges
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=[Path("dot-bashrc")])])

        # Collision file should be backed up under backup/pkg_stow/overwritten/dot-bashrc
        backup_file = os.path.join(self.backup_dir, pkg, "overwritten", "dot-bashrc")
        self.assertTrue(os.path.isfile(backup_file))
        with open(backup_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "pre-existing user content")

        # System target is now successfully symlinked
        self.assertTrue(os.path.islink(target_file))
        link_target = os.readlink(target_file)
        abs_link_target = os.path.abspath(os.path.join(os.path.dirname(target_file), link_target))
        self.assertEqual(abs_link_target, os.path.join(pkg_install_dir, "dot-bashrc"))

    def test_install_copy_deployment_and_lifecycle_hooks(self) -> None:
        """Verifies copy deployment, lifecycle triggers, and copy collision guard."""
        pkg = "pkg_copy"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config with lifecycle hooks
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"

            [hooks]
            post_install = "on-install.sh"
            post_update = "on-update.sh"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello copy")

        # Write hooks in src/pkg_copy/
        with open(os.path.join(pkg_install_dir, "on-install.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook installed' > hook_ran.txt\n")
        with open(os.path.join(pkg_install_dir, "on-update.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook updated' > hook_ran.txt\n")

        # Simulation 1: First-Time Deploy (triggers collision guard and post_install)
        # Create a pre-existing target file
        target_file = os.path.join(self.system_target_dir, "test.txt")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("colliding user file")

        # Run first-time deployment
        from drift.stage_repo import PackageStageChanges
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=[Path("test.txt")])])

        # Target file is copied and pre-existing file backed up
        self.assertTrue(os.path.isfile(target_file))
        with open(target_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello copy")

        backup_file = os.path.join(self.backup_dir, pkg, "overwritten", "test.txt")
        self.assertTrue(os.path.isfile(backup_file))
        with open(backup_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "colliding user file")

        # post_install hook ran (check file created in target_dir)
        hook_marker = os.path.join(self.system_target_dir, "hook_ran.txt")
        self.assertTrue(os.path.isfile(hook_marker))
        with open(hook_marker, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hook installed")

        # Simulation 2: Update/Redeploy (bypasses collision guard, triggers post_update)
        # Modify content in install/
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("updated hello copy")

        # Modify test.txt slightly in system so it is different, ensuring it gets overwritten
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("drifted system file")

        # Clear hook marker first
        os.remove(hook_marker)

        # Run update deployment
        from drift.stage_repo import PackageStageChanges
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, modified_files=[Path("test.txt")])])

        # Target file should be directly overwritten
        with open(target_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "updated hello copy")

        # post_update hook ran
        self.assertTrue(os.path.isfile(hook_marker))
        with open(hook_marker, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hook updated")

    def test_install_copy_is_physical_not_link(self) -> None:
        """Verifies that 'copy' installation method results in real physical files, not symlinks."""
        pkg = "pkg_copy_physical"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # 2. Add a file in install/
        src_file = pkg_install_dir / "real_file.txt"
        src_file.write_text("actual content", encoding="utf-8")

        # 3. Run deployment
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # 4. Verify the target is a regular file, NOT a symlink
        target_file = self.system_target_dir / "real_file.txt"
        self.assertTrue(target_file.is_file())
        self.assertFalse(target_file.is_symlink(), f"Target file {target_file} should be a physical copy, not a symlink.")
        self.assertEqual(target_file.read_text(encoding="utf-8"), "actual content")

    def test_lifecycle_hook_failure_and_timeout(self) -> None:
        """Verifies trigger_package_lifecycle_hook handles failures and timeouts with detailed logging and RuntimeError."""
        from unittest.mock import patch
        import subprocess
        from drift.lifecycle_hooks import trigger_package_lifecycle_hook
        
        pkg = "pkg_copy"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        config = PackageConfig(
            name=pkg,
            target_directory=Path(self.system_target_dir),
            hooks=PackageHooks(post_install="on-install.sh")
        )
        
        # Write dummy hook script so hook_path.exists() is True
        hook_path = os.path.join(pkg_install_dir, "on-install.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("# dummy")

        hook_base_dir = Path(pkg_install_dir)
        cwd = Path(self.system_target_dir)

        # 1. Test CalledProcessError
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=5,
                cmd=["dummy.sh"],
                output="Some normal output",
                stderr="Some severe error output"
            )
            with self.assertRaises(RuntimeError) as ctx:
                trigger_package_lifecycle_hook(
                    pkg=pkg,
                    hook_name="post_install",
                    metadata=config,
                    hook_base_dir=hook_base_dir,
                    cwd=cwd
                )
            self.assertIn("failed with exit code 5", str(ctx.exception))
            self.assertIn("Some severe error output", str(ctx.exception))

        # 2. Test TimeoutExpired
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["dummy.sh"],
                timeout=120,
                output="Standard timeout output",
                stderr="Standard timeout stderr"
            )
            with self.assertRaises(RuntimeError) as ctx:
                trigger_package_lifecycle_hook(
                    pkg=pkg,
                    hook_name="post_install",
                    metadata=config,
                    hook_base_dir=hook_base_dir,
                    cwd=cwd
                )
            self.assertIn("timed out after 120 seconds", str(ctx.exception))
            self.assertIn("Standard timeout stderr", str(ctx.exception))

        # 3. Test missing hook script raises FileNotFoundError
        config_missing = PackageConfig(
            name=pkg,
            target_directory=Path(self.system_target_dir),
            hooks=PackageHooks(post_install="non_existent_script.sh")
        )
        with self.assertRaises(FileNotFoundError) as ctx:
            trigger_package_lifecycle_hook(
                pkg=pkg,
                hook_name="post_install",
                metadata=config_missing,
                hook_base_dir=hook_base_dir,
                cwd=cwd
            )
        self.assertIn("not found", str(ctx.exception))

    def test_lifecycle_hooks_sudo_privileges(self) -> None:
        """Verifies that only pre/post_install and pre/post_update hooks run with sudo when sudo=True."""
        from unittest.mock import patch
        from drift.lifecycle_hooks import trigger_package_lifecycle_hook

        pkg = "pkg_hooks_sudo"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        all_hooks = PackageHooks(
            probe="hook.sh",
            pre_source="hook.sh",
            pre_install="hook.sh",
            post_install="hook.sh",
            pre_update="hook.sh",
            post_update="hook.sh",
            pre_uninstall="hook.sh",
            post_uninstall="hook.sh",
            post_render="hook.sh",
            health="hook.sh"
        )
        config_sudo = PackageConfig(
            name=pkg,
            target_directory=Path(self.system_target_dir),
            sudo=True,
            hooks=all_hooks
        )

        hook_path = os.path.join(pkg_install_dir, "hook.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("# dummy")
        os.chmod(hook_path, 0o755)

        hook_base_dir = Path(pkg_install_dir)
        cwd = Path(self.system_target_dir)

        from drift.constants import LIFECYCLE_HOOK_NAMES

        # All lifecycle hooks always execute in user space without sudo (preserving all injected envs)
        for hook_name in LIFECYCLE_HOOK_NAMES:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                trigger_package_lifecycle_hook(
                    pkg=pkg,
                    hook_name=hook_name,
                    metadata=config_sudo,
                    hook_base_dir=hook_base_dir,
                    cwd=cwd
                )
                called_cmd = mock_run.call_args[0][0]
                self.assertNotEqual(called_cmd[0], "sudo", f"Hook '{hook_name}' should NOT run with sudo even when sudo=True")
                self.assertEqual(called_cmd[0], str(hook_path))

    def test_lifecycle_hook_non_executable_runs_via_interpreter_fallback(self) -> None:
        """Verifies that execute_hook_script falls back to interpreter without mutating disk permissions."""
        from unittest.mock import patch
        from drift.lifecycle_hooks import execute_hook_script

        pkg = "pkg_hook_perm"
        pkg_install_dir = Path(self.install_dir) / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        hook_script = pkg_install_dir / "hook.sh"
        hook_script.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
        hook_script.chmod(0o644)

        config = PackageConfig(name=pkg)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            execute_hook_script(
                hook_path=hook_script,
                pkg=pkg,
                hook_name="pre_install",
                metadata=config,
                cwd=Path(self.system_target_dir)
            )
            called_cmd = mock_run.call_args[0][0]
            # On POSIX, non-executable .sh runs via /bin/bash fallback
            if sys.platform != "win32":
                self.assertEqual(called_cmd[0], "/bin/bash")
                self.assertEqual(called_cmd[1], str(hook_script))
                # Disk mode remains unchanged (no runtime mutation)
                self.assertFalse(bool(hook_script.stat().st_mode & 0o111))

    def test_lifecycle_hooks_receive_package_envs(self) -> None:
        """Verifies that lifecycle hooks receive drift_package_name, drift_package_target_dir, and drift_package_install_method in env."""
        from drift.install_repo import deploy_package_impl
        from drift.state_registry import StateRegistry

        pkg = "pkg_env_hooks"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Do NOT specify target_directory in package config, so it falls back to workspace_config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"

            [hooks]
            post_install = "hook.sh"
            """)

        hook_path = os.path.join(pkg_install_dir, "hook.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n")
        os.chmod(hook_path, 0o755)

        state_file = self.drift_root / "state.toml"
        registry = load_state_registry(state_file)

        captured_envs = {}
        def mock_run_cmd(cmd, **kwargs):
            captured_envs["drift_package_name"] = os.environ.get("drift_package_name")
            captured_envs["drift_package_target_dir"] = os.environ.get("drift_package_target_dir")
            captured_envs["drift_package_install_method"] = os.environ.get("drift_package_install_method")
            from unittest.mock import MagicMock
            res = MagicMock()
            res.returncode = 0
            return res

        from unittest.mock import patch
        with patch("subprocess.run", side_effect=mock_run_cmd):
            deploy_package_impl(
                workspace_config=self.workspace_config,
                pkg=pkg,
                state_registry=registry,
                state_file=state_file,
                resolve_symlinks=False,
                force=True
            )

        # Verifies workspace_config default target directory was properly passed and not clobbered
        self.assertEqual(captured_envs.get("drift_package_name"), pkg)
        self.assertEqual(captured_envs.get("drift_package_target_dir"), str(Path(self.system_target_dir).expanduser()))
        self.assertEqual(captured_envs.get("drift_package_install_method"), "copy")

        # Confirm envs were unloaded cleanly
        self.assertNotIn("drift_package_name", os.environ)
        self.assertNotIn("drift_package_target_dir", os.environ)
        self.assertNotIn("drift_package_install_method", os.environ)

    def test_install_copy_respects_ignore(self) -> None:
        """Verifies that 'copy' installation method respects .drift_ignore patterns."""
        pkg = "pkg_copy_ignore"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Add files: one to keep, one to ignore
        with open(os.path.join(pkg_install_dir, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("should be copied")
        with open(os.path.join(pkg_install_dir, "ignore_me.txt"), "w", encoding="utf-8") as f:
            f.write("should be ignored")
        
        # Add .drift_ignore
        with open(os.path.join(pkg_install_dir, DRIFT_IGNORE_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("ignore_me.txt\n")

        # Run full deployment (no package_changes passed)
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # Verify results
        kept_file = os.path.join(self.system_target_dir, "keep.txt")
        ignored_file = os.path.join(self.system_target_dir, "ignore_me.txt")
        config_file = os.path.join(self.system_target_dir, PACKAGE_CONFIG_FILE_NAME)
        ignore_file_on_target = os.path.join(self.system_target_dir, DRIFT_IGNORE_FILE_NAME)

        self.assertTrue(os.path.isfile(kept_file), "keep.txt should be copied")
        self.assertFalse(os.path.exists(ignored_file), "ignore_me.txt should be ignored")
        self.assertFalse(os.path.exists(config_file), "drift_package.toml should not be copied")
        self.assertFalse(os.path.exists(ignore_file_on_target), ".drift_ignore should not be copied")

    def test_symlinked_parent_safety_abort(self) -> None:
        """Verifies that a symlinked parent directory outside the package's target_dir raises a RuntimeError to prevent deleting/recreating unrelated system folders."""
        pkg = "pkg_stow"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Setup target directory for the package inside system_target_dir
        pkg_target_dir = os.path.join(self.system_target_dir, "pkg_safety_target")

        # Write package config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{pkg_target_dir}"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "dot-bashrc"), "w", encoding="utf-8") as f:
            f.write("some file content")

        # Let's make the parent directory of pkg_target_dir, which is self.system_target_dir, a symlink pointing into drift_root!
        # First remove existing directory to make it a symlink
        os.rmdir(self.system_target_dir)
        
        fake_drift_dest = os.path.join(self.drift_root, "fake_drift_dest")
        os.makedirs(fake_drift_dest, exist_ok=True)
        os.symlink(fake_drift_dest, self.system_target_dir)

        # Now, attempting to deploy should raise a RuntimeError containing "Safety Abort"
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_5_install_deployment(
                self.workspace_config,
                [pkg],
                package_changes=None
            )
        
        self.assertIn("Safety Abort", str(ctx.exception))
        self.assertIn("lies outside", str(ctx.exception))

    def test_symlinked_parent_rebuilt_inside_target_dir(self) -> None:
        """Verifies that a parent symlink situated INSIDE the package's target_dir is successfully backed up, deleted, and rebuilt as a physical folder."""
        pkg = "pkg_stow"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file under subfolder nested_app in install
        nested_src_dir = os.path.join(pkg_install_dir, "nested_app")
        os.makedirs(nested_src_dir, exist_ok=True)
        with open(os.path.join(nested_src_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("config content")

        # Make the parent "nested_app" inside system_target_dir a symlink pointing to drift_root (simulating folding/stow conflict inside target)
        nested_target_symlink = os.path.join(self.system_target_dir, "nested_app")
        fake_drift_dest = os.path.join(self.drift_root, "fake_drift_dest")
        os.makedirs(fake_drift_dest, exist_ok=True)
        os.symlink(fake_drift_dest, nested_target_symlink)

        # Deploy
        from drift.stage_repo import PackageStageChanges
        run_primitive_5_install_deployment(
            self.workspace_config,
            [pkg],
            package_changes=[PackageStageChanges(package_name=pkg, added_files=[Path("nested_app/config.json")])]
        )

        # 1. Parent symlink should be removed and rebuilt as a physical directory
        self.assertTrue(os.path.isdir(nested_target_symlink))
        self.assertFalse(os.path.islink(nested_target_symlink))

        # 2. Backup path structure should preserve the nested relative path (overwritten/nested_app)
        backup_parent = os.path.join(self.backup_dir, pkg, "overwritten", "nested_app")
        self.assertTrue(os.path.exists(backup_parent))

        # 3. File nested_app/config.json should be successfully deployed as a symlink
        deployed_file = os.path.join(nested_target_symlink, "config.json")
        self.assertTrue(os.path.islink(deployed_file))
        self.assertEqual(
            os.path.abspath(os.path.join(os.path.dirname(deployed_file), os.readlink(deployed_file))),
            os.path.abspath(os.path.join(nested_src_dir, "config.json"))
        )

    @patch("drift.install_repo.ensure_dir_exists_with_sudo")
    @patch("subprocess.run")
    def test_run_stow_deployment(self, mock_run, mock_ensure_dir) -> None:
        """Verifies that run_stow_deployment builds the correct stow command, ensures target exists, and runs it."""
        from drift.install_repo import run_stow_deployment
        
        # 1. Test standard stow command without sudo
        run_stow_deployment(
            install_base=Path("/install"),
            target_dir=Path("/target"),
            pkg="pkg_a",
            sudo=False,
            stow_sufficient=True
        )
        
        mock_ensure_dir.assert_called_once_with(Path("/target"), False)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(
            args[0],
            ["stow", "--no-folding", "--dotfiles", "-d", "/install", "-t", "/target", "pkg_a"]
        )
        self.assertEqual(kwargs.get("cwd"), "/install")
        
        # 2. Test stow command with sudo
        mock_run.reset_mock()
        mock_ensure_dir.reset_mock()
        run_stow_deployment(
            install_base=Path("/install"),
            target_dir=Path("/target"),
            pkg="pkg_a",
            sudo=True,
            stow_sufficient=True
        )
        mock_ensure_dir.assert_called_once_with(Path("/target"), True)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(
            args[0],
            ["sudo", "stow", "--no-folding", "--dotfiles", "-d", "/install", "-t", "/target", "pkg_a"]
        )
        
        # 3. Test stow command when version is insufficient
        with self.assertRaises(RuntimeError) as ctx:
            run_stow_deployment(
                install_base=Path("/install"),
                target_dir=Path("/target"),
                pkg="pkg_a",
                sudo=False,
                stow_sufficient=False
            )
        self.assertIn("insufficient", str(ctx.exception))

    def test_collision_guard_ignored_file_deletion(self) -> None:
        """Verifies that if a staged file matches .drift_ignore,
        the collision guard will ignore its corresponding file the host system."""
        # 1. Create a package in install/ State Database
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file under install/pkg_stow, e.g., ignored_file.txt
        with open(os.path.join(pkg_install_dir, "ignored_file.txt"), "w", encoding="utf-8") as f:
            f.write("should be ignored")

        # Write .drift_ignore to install/pkg_stow telling it to ignore ignored_file.txt
        with open(os.path.join(pkg_install_dir, DRIFT_IGNORE_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("ignored_file.txt\n")

        # Create that file at system target (simulating it was previously deployed or exists there)
        system_file = self.system_target_dir / "ignored_file.txt"
        with open(system_file, "w", encoding="utf-8") as f:
            f.write("pre-existing on target")

        # Create stow-local-ignore inside install/pkg_stow (simulating staging done)
        stow_ignore_path = pkg_install_dir / ".stow-local-ignore"
        with open(stow_ignore_path, "w", encoding="utf-8") as f:
            f.write("^/ignored_file.txt\n")

        # Execute deployment
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # 1. The ignored_file.txt should be ignored.
        self.assertTrue(system_file.exists())

        # 2. It should stay untouched.
        with open(system_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "pre-existing on target")

    def test_standalone_apply_prunes_orphaned_files(self) -> None:
        """Verifies that standalone deploy_package (without package_changes) reconciles the state database,

        and prunes any orphaned files that are no longer present in the install/ package folder.
        """
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup two physical files under install/pkg_stow
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        with open(os.path.join(pkg_install_dir, "file1.txt"), "w", encoding="utf-8") as f:
            f.write("first file")
        with open(os.path.join(pkg_install_dir, "file2.txt"), "w", encoding="utf-8") as f:
            f.write("second file")

        # First deployment (registers both file1.txt and file2.txt in state.toml)
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # Verify both files are deployed
        system_file1 = self.system_target_dir / "file1.txt"
        system_file2 = self.system_target_dir / "file2.txt"
        self.assertTrue(system_file1.exists() or system_file1.is_symlink())
        self.assertTrue(system_file2.exists() or system_file2.is_symlink())

        # Verify state.toml has registered them in deployed_files
        from drift.state_registry import load_state_registry
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        self.assertEqual(sorted(registry.get_package_deployed_files(pkg)), [Path("file1.txt"), Path("file2.txt")])

        # 2. Simulate manual deletion of file2.txt from install/pkg_stow
        os.remove(os.path.join(pkg_install_dir, "file2.txt"))

        # Re-run standalone deployment (without package_changes)
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # 3. Assert file2.txt is pruned from system target
        self.assertFalse(system_file2.exists())
        # file1.txt should still exist
        self.assertTrue(system_file1.exists() or system_file1.is_symlink())

        # Assert file2.txt was backed up under deleted_files
        backup_pruned = self.backup_dir / pkg / "deleted_files" / "file2.txt"
        self.assertTrue(backup_pruned.exists())
        with open(backup_pruned, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "second file")

        # Assert state.toml was updated and only contains file1.txt
        registry2 = load_state_registry(state_file)
        self.assertEqual(registry2.get_package_deployed_files(pkg), [Path("file1.txt")])

    def test_standalone_apply_prunes_stale_stow_links(self) -> None:
        """Verifies that standalone deploy_package with install_method="stow" unlinks/deletes stale stow links,

        but does NOT attempt to create a backup of a broken Stow link (which has no target data).
        """
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup config with stow method
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        with open(os.path.join(pkg_install_dir, "file1.txt"), "w", encoding="utf-8") as f:
            f.write("first stow file")
        with open(os.path.join(pkg_install_dir, "file2.txt"), "w", encoding="utf-8") as f:
            f.write("second stow file")

        # Deploy first time using stow
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # Verify links are deployed
        system_file1 = self.system_target_dir / "file1.txt"
        system_file2 = self.system_target_dir / "file2.txt"
        self.assertTrue(os.path.islink(system_file1))
        self.assertTrue(os.path.islink(system_file2))

        # 2. Simulate manual deletion of file2.txt from install/pkg_stow (which breaks its symlink)
        os.remove(os.path.join(pkg_install_dir, "file2.txt"))

        # Re-run standalone deployment
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # 3. Assert the stale symlink was successfully pruned/deleted from the host target
        self.assertFalse(os.path.exists(system_file2))
        self.assertFalse(os.path.islink(system_file2))

        # Assert no backup was created (since the broken Stow link contains no real file target content)
        backup_pruned = self.backup_dir / pkg / "deleted_files" / "file2.txt"
        self.assertFalse(backup_pruned.exists())

    def test_collision_guard_handles_internal_and_external_symlinks(self) -> None:
        """Verifies collision guard behavior with internal (dangling/valid) and external symlinks.

        - Symlink pointing into drift_root: deleted without backup.
        - Symlink pointing outside drift_root (resolvable): treated as collision, target contents backed up, and replaced.
        - Symlink pointing outside drift_root (broken): treated as collision, symlink itself backed up, and replaced.
        """
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup config with stow method
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)

        # We have three files to deploy: internal_link.txt, external_link.txt, and external_broken.txt
        with open(os.path.join(pkg_install_dir, "internal_link.txt"), "w", encoding="utf-8") as f:
            f.write("internal target content")
        with open(os.path.join(pkg_install_dir, "external_link.txt"), "w", encoding="utf-8") as f:
            f.write("external target content")
        with open(os.path.join(pkg_install_dir, "external_broken.txt"), "w", encoding="utf-8") as f:
            f.write("external broken content")

        # Now, before deployment, let's pre-create symlinks at the system target paths:
        system_internal = self.system_target_dir / "internal_link.txt"
        system_external = self.system_target_dir / "external_link.txt"
        system_external_broken = self.system_target_dir / "external_broken.txt"

        # A. Internal symlink (pointing inside drift_root, e.g. a dangling pointer into install_dir or render_dir)
        # Note: can point to a non-existent path inside drift_root (dangling)
        dangling_drift_target = self.install_dir / "some_deleted_package" / "file.txt"
        os.symlink(dangling_drift_target, system_internal)

        # B. External symlink (pointing outside drift_root, e.g. pointing to a random external config file)
        external_temp = tempfile.TemporaryDirectory()
        self.addCleanup(external_temp.cleanup)
        external_file = Path(external_temp.name) / "external_source.txt"
        with open(external_file, "w", encoding="utf-8") as f:
            f.write("external config source")
        os.symlink(external_file, system_external)

        # C. External broken symlink (pointing outside drift_root, but target does not exist)
        nonexistent_external_file = Path("/tmp/nonexistent_external_target_file_12345.txt")
        os.symlink(nonexistent_external_file, system_external_broken)

        # Execute deployment
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # 2. Assertions:
        # - The internal symlink was deleted without backup:
        self.assertFalse((self.backup_dir / pkg / "overwritten" / "internal_link.txt").exists())
        # But it should be replaced by the newly stowed symlink pointing to pkg_install_dir:
        self.assertTrue(system_internal.is_symlink())
        
        # - The external symlink was backed up by its content because the link can be resolved:
        backup_external = self.backup_dir / pkg / "overwritten" / "external_link.txt"
        self.assertTrue(backup_external.exists())
        with open(backup_external, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "external config source")
        # And it should be replaced by the newly stowed symlink pointing to pkg_install_dir:
        self.assertTrue(system_external.is_symlink())

        # - The external broken symlink was backed up as a symlink itself because it cannot be resolved:
        backup_external_broken = self.backup_dir / pkg / "overwritten" / "external_broken.txt"
        self.assertTrue(backup_external_broken.is_symlink())
        self.assertEqual(backup_external_broken.readlink(), nonexistent_external_file)
        # And it should be replaced by the newly stowed symlink pointing to pkg_install_dir:
        self.assertTrue(system_external_broken.is_symlink())

    def test_install_target_cannot_be_inside_drift_root(self) -> None:
        """Verifies that the installation deployment raises ValueError if the target directory is inside or equal to drift_root."""
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Setup config with target_directory equal to drift_root
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.drift_root}"
            """)

        # Execute deployment and assert ValueError
        with self.assertRaises(ValueError) as ctx:
            run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx.exception))

        # 2. Setup config with target_directory INSIDE drift_root (e.g. self.drift_root / "polluted_dir")
        polluted_dir = self.drift_root / "polluted_dir"
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{polluted_dir}"
            """)

        # Execute deployment and assert ValueError
        with self.assertRaises(ValueError) as ctx:
            run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx.exception))

    def test_run_primitive_6_commit_install_repo(self) -> None:
        """Verifies staging and committing changes within the install state repository (Primitive 6)."""
        from drift.install_repo import run_primitive_6_commit_install_repo
        import subprocess

        # 1. Initialize Git repository inside the install directory
        subprocess.run(["git", "init"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), capture_output=True, check=True)

        # 2. Write a file inside the install directory under a package folder
        pkg_dir = self.install_dir / "pkg_a"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        test_file_path = pkg_dir / "file.txt"
        test_file_path.write_text("Hello install", encoding="utf-8")

        # Create another package folder with an uncommitted change
        pkg_b_dir = self.install_dir / "pkg_b"
        pkg_b_dir.mkdir(parents=True, exist_ok=True)
        pkg_b_file = pkg_b_dir / "file.txt"
        pkg_b_file.write_text("Hello pkg_b", encoding="utf-8")

        # 3. Commit only pkg_a specifically
        scoped_msg = "Commit pkg_a in install"
        run_primitive_6_commit_install_repo(self.workspace_config, scoped_msg, ["pkg_a"])

        # 4. Verify only pkg_a was committed, and pkg_b remains untracked
        status_res = subprocess.run(
            ["git", "-C", str(self.install_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        status_output = status_res.stdout.strip()
        self.assertNotIn("pkg_a/", status_output)
        self.assertTrue("?? pkg_b/" in status_output or "?? pkg_b/file.txt" in status_output)

        # Verify the commit message of the scoped commit
        log_res = subprocess.run(
            ["git", "-C", str(self.install_dir), "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(log_res.stdout.strip(), scoped_msg)

        # 5. Commit remaining changes (pkg_b) unscoped
        unscoped_msg = "Commit remaining install changes"
        run_primitive_6_commit_install_repo(self.workspace_config, unscoped_msg)

        # Verify repo is clean now
        status_clean = subprocess.run(
            ["git", "-C", str(self.install_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True
        )
        self.assertEqual(status_clean.stdout.strip(), "")

        # 6. Call again on a clean repo (should return gracefully without error)
        run_primitive_6_commit_install_repo(self.workspace_config, "No-op commit")


    def test_deploy_failure_leaves_state_as_deploying(self) -> None:
        """Verifies that if deployment fails midway, the package state remains 'deploying' in state.toml."""
        pkg = "pkg_fail"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config with a post_install hook that will fail
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"

            [hooks]
            post_install = "fail.sh"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello fail")
            
        # Write failing hook script
        hook_path = os.path.join(pkg_install_dir, "fail.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook_path, 0o755)

        # Attempt to deploy - should fail due to hook
        with self.assertRaises(RuntimeError):
            run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # Check state.toml
        state_file = os.path.join(self.install_dir, "state.toml")
        from drift.state_registry import load_state_registry
        registry = load_state_registry(Path(state_file))
        self.assertEqual(registry.get_package_state(pkg), "deploying")
        self.assertTrue(registry.has_deploying_package())

    def test_deploy_aborts_if_already_deploying(self) -> None:
        """Verifies that deployment aborts if a package is already in 'deploying' state."""
        pkg = "pkg_deploying"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Pre-set state to 'deploying'
        state_file = os.path.join(self.install_dir, "state.toml")
        from drift.state_registry import load_state_registry, save_state_registry
        registry = load_state_registry(Path(state_file))
        registry.set_package_state(pkg, "deploying")
        save_state_registry(Path(state_file), registry)

        # Attempt to deploy - should abort with Safety Abort
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_5_install_deployment(self.workspace_config, [pkg])
        
        self.assertIn("Safety Abort", str(ctx.exception))
        self.assertIn("currently in 'deploying' state", str(ctx.exception))

        # Attempt with force=True - should proceed (and succeed here)
        run_primitive_5_install_deployment(self.workspace_config, [pkg], force=True)
        
        # Verify success after force
        registry = load_state_registry(Path(state_file))
        self.assertEqual(registry.get_package_state(pkg), "installed")

    def test_install_package_name_starts_with_dot_dash(self) -> None:
        """Verifies that deploying/installing a package whose name starts with 'dot-' works exactly as expected and preserves files on the target."""
        pkg_name = "dot-my_pkg"
        pkg_install_dir = self.install_dir / pkg_name
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Enable in workspace
        self.workspace_config.packages_enable[pkg_name] = True

        # Write config and file
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_name}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        with open(pkg_install_dir / "static.txt", "w", encoding="utf-8") as f:
            f.write("deployed static content")

        # Run deployment
        run_primitive_5_install_deployment(self.workspace_config, [pkg_name])

        # Verify output target file exists under system target dir (name preserved, files copied)
        target_file = self.system_target_dir / "static.txt"
        self.assertTrue(target_file.is_file())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "deployed static content")

        # Verify state in state.toml is registered under 'dot-my_pkg'
        state_file = self.install_dir / "state.toml"
        from drift.state_registry import load_state_registry
        registry = load_state_registry(state_file)
        self.assertEqual(registry.get_package_state(pkg_name), "installed")

    def test_skipped_package_not_set_to_deploying_state(self) -> None:
        """Verifies that skipped packages (enable_install=False or missing dir) are not set to 'deploying' in state.toml."""
        from drift.install_repo import deploy_package_impl
        from drift.state_registry import load_state_registry

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)

        # 1. Test package with enable_install = false
        pkg_disabled = "pkg_disabled"
        pkg_dir = self.install_dir / pkg_disabled
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_disabled}"
        install_method = "copy"
        enable_install = false
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res_disabled = deploy_package_impl(
            workspace_config=self.workspace_config,
            pkg=pkg_disabled,
            state_registry=registry,
            state_file=state_file,
            resolve_symlinks=True,
            force=False
        )
        self.assertEqual(res_disabled.status, "SKIPPED")
        # Check that state.toml did not transition this package into 'deploying'
        reloaded = load_state_registry(state_file)
        self.assertNotEqual(reloaded.get_package_state(pkg_disabled), "deploying")

        # 2. Test package with missing install directory (corrupted stage)
        pkg_missing = "pkg_missing_dir"
        # Setup drift_package.toml in source only so load_config_for_install doesn't find it in install/
        # Or place drift_package.toml in a file instead of directory
        # If install/pkg_missing_dir doesn't exist, load_config_for_install raises error before deploy_package_impl
        # If install/pkg_missing_dir has a config file but is not a dir for files:
        pkg_missing_dir = self.install_dir / pkg_missing
        pkg_missing_dir.mkdir(parents=True, exist_ok=True)
        (pkg_missing_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_missing}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        # Remove directory right after loading config or test missing install_pkg_dir
        shutil.rmtree(pkg_missing_dir)

        # Mock config loading to return metadata for missing dir
        from drift.package_config import PackageConfig
        metadata = PackageConfig(name=pkg_missing, install_method="copy", target_directory=self.system_target_dir)
        with patch("drift.install_repo.load_config_for_install", return_value=metadata):
            res_missing = deploy_package_impl(
                workspace_config=self.workspace_config,
                pkg=pkg_missing,
                state_registry=registry,
                state_file=state_file,
                resolve_symlinks=True,
                force=False
            )
            self.assertEqual(res_missing.status, "SKIPPED")
            reloaded2 = load_state_registry(state_file)
            self.assertNotEqual(reloaded2.get_package_state(pkg_missing), "deploying")

    def test_deploy_executes_hooks_ignored_in_drift_ignore(self) -> None:
        """Verifies that hook scripts listed in .drift_ignore are staged to install/, executed, and not deployed to host."""
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.render_package import render_package

        pkg = "pkg_ignored_hook"
        pkg_src = self.source_dir / pkg
        pkg_src.mkdir(parents=True, exist_ok=True)

        marker_file = self.system_target_dir / "hook_ran_marker.txt"

        # 1. Config with lifecycle hook under [hooks]
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        pre_install = "pre_hook.sh"
        """, encoding="utf-8")

        # 2. .drift_ignore ignoring the hook script
        (pkg_src / DRIFT_IGNORE_FILE_NAME).write_text("pre_hook.sh\n", encoding="utf-8")

        # 3. Hook script and valid config file
        (pkg_src / "pre_hook.sh").write_text(f"#!/bin/sh\necho 'hook executed successfully' > '{marker_file}'\n", encoding="utf-8")
        (pkg_src / "app_setting.conf").write_text("setting = 42\n", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = True

        # Render -> Stage -> Install
        render_package(self.workspace_config, pkg_src)
        run_primitive_4_stage_render_to_install(self.workspace_config, pkg)
        run_primitive_5_install_deployment(self.workspace_config, [pkg])

        # Assert:
        # A. Hook executed and wrote marker file
        self.assertTrue(marker_file.is_file())
        self.assertEqual(marker_file.read_text(encoding="utf-8").strip(), "hook executed successfully")

        # B. app_setting.conf is deployed to system target
        self.assertTrue((self.system_target_dir / "app_setting.conf").is_file())

        # C. pre_hook.sh is NOT deployed to system target
        self.assertFalse((self.system_target_dir / "pre_hook.sh").exists())
        self.assertFalse((self.system_target_dir / DRIFT_IGNORE_FILE_NAME).exists())
        self.assertFalse((self.system_target_dir / PACKAGE_CONFIG_FILE_NAME).exists())

    def test_collision_guard_ignored_file_matching_drift_root_symlink_not_collided(self) -> None:
        """Verifies collision guard behavior:
        1. Valid stow link pointing to this package's file is NOT removed.
        2. Ignored file on system is untouched.
        3. Rogue internal symlink pointing to another drift file is backed up and replaced.
        """
        pkg = "pkg_symlink_guard"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Package config
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        # 2. .drift_ignore and files
        (pkg_install_dir / DRIFT_IGNORE_FILE_NAME).write_text("ignored_hook.sh\n", encoding="utf-8")
        (pkg_install_dir / ".stow-local-ignore").write_text("ignored_hook.sh\n.drift_ignore\ndrift_package.toml\n", encoding="utf-8")
        (pkg_install_dir / "ignored_hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (pkg_install_dir / "valid_file.txt").write_text("valid content", encoding="utf-8")
        (pkg_install_dir / "rogue_link.txt").write_text("rogue target content", encoding="utf-8")

        drift_internal_target = self.drift_root / "config" / "drift.toml"
        drift_internal_target.parent.mkdir(parents=True, exist_ok=True)
        drift_internal_target.write_text("[workspace]\n", encoding="utf-8")

        # 3. Setup system target directory:
        # A. valid_file.txt already points to pkg_install_dir / valid_file.txt (valid stow link)
        from drift.file_utils import get_relative_path
        system_valid = self.system_target_dir / "valid_file.txt"
        system_valid.symlink_to(get_relative_path(self.system_target_dir, pkg_install_dir / "valid_file.txt"))

        # B. ignored_hook.sh on system is an obsolete file
        system_ignored = self.system_target_dir / "ignored_hook.sh"
        system_ignored.symlink_to(drift_internal_target)

        # C. rogue_link.txt points to wrong internal drift file
        system_rogue = self.system_target_dir / "rogue_link.txt"
        system_rogue.symlink_to(drift_internal_target)

        # 4. Execute install deployment
        res = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 5. Assertions:
        # A. Valid stow link is preserved and not backed up as overwritten
        self.assertTrue(system_valid.is_symlink())
        self.assertEqual(system_valid.resolve(), (pkg_install_dir / "valid_file.txt").resolve())
        self.assertFalse((self.backup_dir / pkg / "overwritten" / "valid_file.txt").exists())

        # B. Ignored file on system is ignored and stay the same.
        self.assertTrue(system_ignored.exists())
        with open(system_ignored, "r", encoding="utf-8") as f:
            system_ignored_content = f.read()
            # The system ignored_hook.sh file points to the drift_internal_target, which contains "[workspace]\n"
            self.assertEqual(system_ignored_content, "[workspace]\n")

        # C. Rogue link was backed up and replaced with the correct stow link
        self.assertTrue(system_rogue.is_symlink())
        self.assertEqual(system_rogue.resolve(), (pkg_install_dir / "rogue_link.txt").resolve())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "rogue_link.txt").exists())

    def test_stow_link_pointing_to_different_file_in_same_pkg_updated_full_deploy(self) -> None:
        """Verifies that under full deployment, if a host symlink points to a different file in the same package's install dir,
        it is recognized as valid for this package and updated to the desired target file upon deployment.
        """
        pkg = "pkg_switch_target_full"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "file_a.txt").write_text("content A", encoding="utf-8")
        (pkg_install_dir / "file_b.txt").write_text("content B", encoding="utf-8")

        # Setup: file_a.txt on system currently points to file_b.txt in the same package install dir
        system_file_a = self.system_target_dir / "file_a.txt"
        system_file_a.symlink_to(pkg_install_dir / "file_b.txt")

        # Execute full deployment
        res = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # file_a.txt now points to file_a.txt in pkg_install_dir
        self.assertTrue(system_file_a.is_symlink())
        self.assertEqual(system_file_a.resolve(), (pkg_install_dir / "file_a.txt").resolve())

        # file_b.txt is also properly deployed
        system_file_b = self.system_target_dir / "file_b.txt"
        self.assertTrue(system_file_b.is_symlink())
        self.assertEqual(system_file_b.resolve(), (pkg_install_dir / "file_b.txt").resolve())

    def test_stow_link_pointing_to_different_file_in_same_pkg_updated_partial_deploy(self) -> None:
        """Verifies that under partial/incremental deployment (via PackageStageChanges),
        a host symlink pointing to a different file in the same package is safely updated to the desired target file.
        """
        pkg = "pkg_switch_target_partial"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "file_a.txt").write_text("content A", encoding="utf-8")
        (pkg_install_dir / "file_b.txt").write_text("content B", encoding="utf-8")

        # Setup: file_a.txt on system currently points to file_b.txt in the same package install dir
        system_file_a = self.system_target_dir / "file_a.txt"
        system_file_a.symlink_to(pkg_install_dir / "file_b.txt")

        # Execute partial deployment modifying file_a.txt
        from drift.stage_repo import PackageStageChanges
        changes = [PackageStageChanges(package_name=pkg, modified_files=[Path("file_a.txt")])]
        res = run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=changes)
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # file_a.txt now points to file_a.txt in pkg_install_dir
        self.assertTrue(system_file_a.is_symlink())
        self.assertEqual(system_file_a.resolve(), (pkg_install_dir / "file_a.txt").resolve())

    def test_internal_symlink_directory_children_processed_paths(self) -> None:
        """Verifies that when an internal symlink points to drift_root for a directory,
        its children in install_pkg_dir are added to processed_paths, avoiding double collision handling.
        """
        pkg = "pkg_dir_symlink_guard"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        sub_dir = pkg_install_dir / "my_dir"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "file1.txt").write_text("file 1 content", encoding="utf-8")
        nested_dir = sub_dir / "nested"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "file2.txt").write_text("file 2 content", encoding="utf-8")

        # Setup: system_target_dir / my_dir is a symlink pointing to drift_root/config
        drift_internal_dir = self.drift_root / "config"
        drift_internal_dir.mkdir(parents=True, exist_ok=True)
        system_dir = self.system_target_dir / "my_dir"
        system_dir.symlink_to(drift_internal_dir)

        res = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # 1. system_dir is now a physical directory (not a symlink)
        self.assertTrue(system_dir.is_dir())
        self.assertFalse(system_dir.is_symlink())
        # 2. Children deployed properly
        self.assertTrue((system_dir / "file1.txt").is_file())
        self.assertEqual((system_dir / "file1.txt").read_text(encoding="utf-8"), "file 1 content")
        self.assertTrue((system_dir / "nested" / "file2.txt").is_file())
        self.assertEqual((system_dir / "nested" / "file2.txt").read_text(encoding="utf-8"), "file 2 content")
        # 3. Collision backup was recorded for the symlink
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "my_dir").exists())

    def test_copy_mode_update_target_symlink_backed_up_and_replaced(self) -> None:
        """Verifies that in copy mode, even during an update (not first time),
        if the target on host is a symlink, it is detected as a collision, backed up,
        and replaced with a physical regular file instead of writing through the link.
        """
        pkg = "pkg_copy_symlink_update"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "app.conf").write_text("setting=new\n", encoding="utf-8")

        # Register package as already "installed" in state.toml (so is_first_time is False)
        state_file = self.install_dir / "state.toml"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        save_state_registry(
            state_file,
            StateRegistry(packages={
                pkg: PackageState(
                    state="installed",
                    install_method="copy",
                    last_deployed="2026-08-20T00:00:00Z",
                    deployed_files=[Path("app.conf")]
                )
            })
        )

        # Host system has app.conf as a symlink pointing to an external file
        external_file = self.drift_root.parent / "external_target.conf"
        external_file.write_text("setting=external_original\n", encoding="utf-8")

        system_file = self.system_target_dir / "app.conf"
        system_file.symlink_to(external_file)

        # Run deployment update
        res = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # 1. system_file is now a regular physical file (not a symlink)
        self.assertTrue(system_file.is_file())
        self.assertFalse(system_file.is_symlink())
        self.assertEqual(system_file.read_text(encoding="utf-8"), "setting=new\n")
        # 2. External file was NOT touched/overwritten
        self.assertEqual(external_file.read_text(encoding="utf-8"), "setting=external_original\n")
        # 3. Collision backup was saved
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "app.conf").exists())

    def test_switch_method_from_stow_to_copy_backs_up_and_replaces_symlinks(self) -> None:
        """Verifies that when a package deployed with 'stow' switches to 'copy',
        the collision handler backs up the previous symlinks/files into overwritten/
        and replaces them with physical copies.
        """
        pkg = "pkg_stow_to_copy"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Initial deployment with 'stow'
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "config.json").write_text('{"version": 1}', encoding="utf-8")
        sub_dir = pkg_install_dir / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "tool.sh").write_text("#!/bin/bash\necho hi", encoding="utf-8")

        res1 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res1.status, "SUCCESS")

        host_config = self.system_target_dir / "config.json"
        host_tool = self.system_target_dir / "sub" / "tool.sh"
        self.assertTrue(host_config.is_symlink())
        self.assertTrue(host_tool.is_symlink())

        # 2. Switch install_method to 'copy'
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res2 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res2.status, "SUCCESS")

        # Assert:
        # - Files on host are now regular physical files
        self.assertTrue(host_config.is_file())
        self.assertFalse(host_config.is_symlink())
        self.assertEqual(host_config.read_text(encoding="utf-8"), '{"version": 1}')

        self.assertTrue(host_tool.is_file())
        self.assertFalse(host_tool.is_symlink())
        self.assertEqual(host_tool.read_text(encoding="utf-8"), "#!/bin/bash\necho hi")

        # - Previous deployed files are backed up to overwritten/
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "config.json").exists())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "sub" / "tool.sh").exists())

    def test_switch_method_from_copy_to_stow_backs_up_and_replaces_physical_files(self) -> None:
        """Verifies that when a package deployed with 'copy' switches to 'stow',
        the collision handler backs up the previous physical files into overwritten/
        and replaces them with stow symlinks.
        """
        pkg = "pkg_copy_to_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # 1. Initial deployment with 'copy'
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "settings.ini").write_text("key=value1\n", encoding="utf-8")
        nested_dir = pkg_install_dir / "nested"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "data.txt").write_text("data payload 1\n", encoding="utf-8")

        res1 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res1.status, "SUCCESS")

        host_settings = self.system_target_dir / "settings.ini"
        host_data = self.system_target_dir / "nested" / "data.txt"
        self.assertTrue(host_settings.is_file())
        self.assertFalse(host_settings.is_symlink())
        self.assertTrue(host_data.is_file())
        self.assertFalse(host_data.is_symlink())

        # 2. Switch install_method to 'stow'
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "stow"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res2 = run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertEqual(res2.status, "SUCCESS")

        # Assert:
        # - Files on host are now symlinks pointing to pkg_install_dir
        self.assertTrue(host_settings.is_symlink())
        self.assertEqual(host_settings.resolve(), (pkg_install_dir / "settings.ini").resolve())

        self.assertTrue(host_data.is_symlink())
        self.assertEqual(host_data.resolve(), (pkg_install_dir / "nested" / "data.txt").resolve())

        # - Previous physical files are backed up to overwritten/ with original content
        backup_settings = self.backup_dir / pkg / "overwritten" / "settings.ini"
        backup_data = self.backup_dir / pkg / "overwritten" / "nested" / "data.txt"
        self.assertTrue(backup_settings.exists())
        self.assertEqual(backup_settings.read_text(encoding="utf-8"), "key=value1\n")
        self.assertTrue(backup_data.exists())
        self.assertEqual(backup_data.read_text(encoding="utf-8"), "data payload 1\n")

    def test_install_fails_if_hook_file_missing_in_install(self) -> None:
        """Verifies that installation raises FileNotFoundError if a configured hook file is missing in install/."""
        pkg = "pkg_hook_missing"
        self.workspace_config.packages_enable[pkg] = True

        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        pre_install = "scripts/missing.sh"
        """, encoding="utf-8")
        (pkg_install_dir / "app.conf").write_text("hello", encoding="utf-8")

        with self.assertRaises(FileNotFoundError) as cm:
            run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertIn("missing.sh", str(cm.exception))

    def test_install_fails_if_hook_file_is_directory_in_install(self) -> None:
        """Verifies that installation raises ValueError if a configured hook file is a directory in install/."""
        pkg = "pkg_hook_is_dir"
        self.workspace_config.packages_enable[pkg] = True

        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "scripts").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "scripts" / "hook_dir").mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        post_install = "scripts/hook_dir"
        """, encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            run_primitive_5_install_deployment(self.workspace_config, [pkg])
        self.assertIn("not a regular file", str(cm.exception))

    def test_find_internal_symlink_conflicts_direct(self) -> None:
        """Directly verifies find_internal_symlink_conflicts helper function."""
        from drift.ignore import DriftIgnore
        pkg = "pkg_find_conflicts"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / "nested").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "nested" / "app.conf").write_text("hello", encoding="utf-8")
        (pkg_install_dir / "root.conf").write_text("root", encoding="utf-8")
        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """)

        # Set up conflicts on system target:
        # 1. 'nested' is a symlink pointing into drift_root
        fake_internal_dest = self.drift_root / "fake_internal"
        fake_internal_dest.mkdir(parents=True, exist_ok=True)
        (self.system_target_dir / "nested").symlink_to(fake_internal_dest)

        # 2. 'root.conf' is a symlink pointing outside drift_root (e.g. /tmp) -> ignored
        outside_target = Path(tempfile.gettempdir()) / "outside_drift.txt"
        outside_target.write_text("outside", encoding="utf-8")
        (self.system_target_dir / "root.conf").symlink_to(outside_target)

        conflicts = find_internal_symlink_conflicts(
            workspace_config=self.workspace_config,
            install_pkg_dir=pkg_install_dir,
            ignore_handler=DriftIgnore(),
            target_dir=self.system_target_dir
        )

        # Should only find 'nested' as an internal symlink conflict
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][0], Path("nested"))
        self.assertEqual(conflicts[0][1], self.system_target_dir / "nested")

    def test_resolve_single_internal_symlink_conflict_direct(self) -> None:
        """Directly verifies resolve_single_internal_symlink_conflict helper function."""
        from drift.ignore import DriftIgnore
        pkg = "pkg_resolve_conflict"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / "sub_dir").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "sub_dir" / "file.txt").write_text("file content", encoding="utf-8")

        config = PackageConfig(name=pkg, install_method="copy", target_directory=str(self.system_target_dir))

        fake_drift_dest = self.drift_root / "fake_drift_dest"
        fake_drift_dest.mkdir(parents=True, exist_ok=True)

        system_target = self.system_target_dir / "sub_dir"
        system_target.symlink_to(fake_drift_dest)

        processed_paths: set = set()

        resolve_single_internal_symlink_conflict(
            workspace_config=self.workspace_config,
            pkg=pkg,
            install_pkg_dir=pkg_install_dir,
            metadata=config,
            ignore_handler=DriftIgnore(),
            repo_rel=Path("sub_dir"),
            system_target=system_target,
            resolve_symlinks=True,
            processed_paths=processed_paths
        )

        # 1. system_target is now a physical directory
        self.assertTrue(system_target.is_dir())
        self.assertFalse(system_target.is_symlink())

        # 2. Backup path structure was created
        backup_path = self.backup_dir / pkg / "overwritten" / "sub_dir"
        self.assertTrue(backup_path.exists())

        # 3. processed_paths contains sub_dir and its child
        self.assertIn(Path("sub_dir"), processed_paths)
        self.assertIn(Path("sub_dir/file.txt"), processed_paths)

    def test_resolve_single_internal_symlink_conflict_valid_stow_skipped(self) -> None:
        """Verifies that a valid Stow relative symlink pointing to the current package is skipped."""
        from drift.ignore import DriftIgnore
        pkg = "pkg_stow_valid"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "valid_file.txt").write_text("valid content", encoding="utf-8")

        config = PackageConfig(name=pkg, install_method="stow", target_directory=str(self.system_target_dir))

        # Create valid relative symlink pointing into install_pkg_dir
        system_target = self.system_target_dir / "valid_file.txt"
        rel_to_install = os.path.relpath(pkg_install_dir / "valid_file.txt", self.system_target_dir)
        os.symlink(rel_to_install, system_target)

        processed_paths: set = set()

        resolve_single_internal_symlink_conflict(
            workspace_config=self.workspace_config,
            pkg=pkg,
            install_pkg_dir=pkg_install_dir,
            metadata=config,
            ignore_handler=DriftIgnore(),
            repo_rel=Path("valid_file.txt"),
            system_target=system_target,
            resolve_symlinks=True,
            processed_paths=processed_paths
        )

        # System target remains untouched as a symlink and not marked in processed_paths
        self.assertTrue(system_target.is_symlink())
        self.assertEqual(len(processed_paths), 0)

    @patch("drift.install_repo.create_symlink_manually_with_sudo")
    def test_deploy_single_stow_file_skips_when_already_pointing_to_source(self, mock_create_symlink) -> None:
        """Verifies deploy_single_stow_file skips recreating symlink if target already points to source."""
        pkg = "pkg_stow_skip"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        src_file = pkg_install_dir / "app.conf"
        src_file.write_text("config data", encoding="utf-8")

        system_target = self.system_target_dir / "app.conf"
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        # 1. Target does not exist -> creates symlink
        deploy_single_stow_file(
            rel_file=Path("app.conf"),
            install_pkg_dir=pkg_install_dir,
            target_dir=self.system_target_dir,
            sudo=False
        )
        self.assertEqual(mock_create_symlink.call_count, 1)

        # Create the actual relative symlink on filesystem
        rel_target = os.path.relpath(src_file, self.system_target_dir)
        os.symlink(rel_target, system_target)
        mock_create_symlink.reset_mock()

        # 2. Target already exists and points to src_file -> should skip recreation
        deploy_single_stow_file(
            rel_file=Path("app.conf"),
            install_pkg_dir=pkg_install_dir,
            target_dir=self.system_target_dir,
            sudo=False
        )
        mock_create_symlink.assert_not_called()

        # 3. Target points to an invalid/different location -> should call create_symlink
        system_target.unlink()
        other_file = Path(tempfile.gettempdir()) / "other.conf"
        other_file.write_text("other", encoding="utf-8")
        os.symlink(other_file, system_target)

        deploy_single_stow_file(
            rel_file=Path("app.conf"),
            install_pkg_dir=pkg_install_dir,
            target_dir=self.system_target_dir,
            sudo=False
        )
        self.assertEqual(mock_create_symlink.call_count, 1)


class TestStowVersionDetection(unittest.TestCase):
    """Tests for GNU Stow version retrieval and version checking logic."""

    @patch("drift.install_repo.run_command")
    def test_get_stow_version_string_stdout(self, mock_run_command) -> None:
        mock_res = subprocess.CompletedProcess(args=["stow", "--version"], returncode=0, stdout="stow (GNU Stow) version 2.4.1\n")
        mock_run_command.return_value = mock_res
        version = get_stow_version()
        self.assertEqual(version, "2.4.1")

    @patch("drift.install_repo.run_command")
    def test_get_stow_version_bytes_stdout(self, mock_run_command) -> None:
        mock_res = subprocess.CompletedProcess(args=["stow", "--version"], returncode=0, stdout=b"stow (GNU Stow) version 2.3.1\n")
        mock_run_command.return_value = mock_res
        version = get_stow_version()
        self.assertEqual(version, "2.3.1")

    @patch("drift.install_repo.run_command")
    def test_get_stow_version_command_fails(self, mock_run_command) -> None:
        mock_run_command.side_effect = FileNotFoundError("No such file or directory: 'stow'")
        version = get_stow_version()
        self.assertIsNone(version)

    def test_is_stow_version_sufficient(self) -> None:
        self.assertTrue(is_stow_version_sufficient("2.4.1"))
        self.assertTrue(is_stow_version_sufficient("2.4.2"))
        self.assertTrue(is_stow_version_sufficient("2.5.0"))
        self.assertTrue(is_stow_version_sufficient("3.0.0"))
        self.assertFalse(is_stow_version_sufficient("2.4.0"))
        self.assertFalse(is_stow_version_sufficient("2.3.1"))
        self.assertFalse(is_stow_version_sufficient("1.9.0"))
        self.assertFalse(is_stow_version_sufficient("invalid"))


if __name__ == "__main__":
    unittest.main()
