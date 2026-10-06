import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from typing import Optional, List, Dict, Set, Sequence, Any

from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    DRIFT_HOOKS_DIR_NAME,
    InstallMethod,
    BackupSubfolder,
    set_test_mode,
)
from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.config.package_config import PackageConfig, PackageSectionConfig
from drift.config.package_hooks import PackageHooks
from drift.hooks.lifecycle_hooks import HookExecFlags
from drift.core.folder_diff import FolderDiff
from drift.core.ignore import DriftIgnore
from drift.core.state_registry import (
        load_state_registry,
        save_state_registry,
        StateRegistry,
        PackageState
)
from drift.core.exceptions import (
    InstallCollisionError,
    CrossPackageCollisionError,
    HookMissingError,
    PackageInstallDirMissingError,
    ConfigError,
    MidwayTransactionError,
)
from drift.config.package_config import (
    PackageConfig,
    PackageDependencies,
    PackageDependency,
)
from drift.primitives.install_repo import (
        run_primitive_5_install,
        prepare_install,
        execute_install,
        InstallPlan,
        plan_package_install,
        execute_package_actions,
        execute_package_install,
        execute_package_install_impl,
        install_one_package,
        InstallOptions,
        PackageInstallContext,
        assert_packages_install_ready,
)
from drift.core.file_action import (
        FileActionExecutionContext,
        FileActionType,
        FileAction,
        DELETE_ACTION_TYPES,
        execute_single_action,
        execute_delivery_actions,
        format_action_line,
        format_action_summary,
)
from drift.core.folder_delivery import (
        DeliveryInspectionContext,
        plan_folder_delivery,
)
from drift.core.result_models import (
        PackageInstallPlan,
)
from drift.utils.path_utils import compute_relative_symlink_target
from drift.primitives.package_assertions import (
        assert_no_cross_package_conflicts,
        assert_no_cyclic_package_dependencies,
        resolve_package_install_order,
)
from drift.utils.file_ops import (
        ensure_dir,
        assert_writable,
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
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir
            ),
            packages_enable={
                "pkg_symlink": True,
                "pkg_copy": True,
            },
            packages_enable_default=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_state_registry_load_and_save(self) -> None:
        """Verifies StateRegistry manages state.toml transitions correctly."""
        state_file = self.install_dir / "state.toml"
        
        # Test loading missing registry
        registry = load_state_registry(state_file)
        self.assertEqual(registry.packages, {})
        self.assertEqual(registry.state_file, state_file)
        self.assertFalse(registry.has_installing_package())

        # Test setting and saving states using registry.save()
        registry.set_package_state("nvim", "installing")
        registry.set_package_state("tmux", "installed")
        self.assertTrue(registry.has_installing_package())
        self.assertEqual(registry.get_package_state("nvim"), "installing")
        self.assertEqual(registry.get_package_state("tmux"), "installed")

        registry.save()
        self.assertTrue(os.path.isfile(state_file))

        # Test loading from file
        loaded = load_state_registry(state_file)
        self.assertEqual(loaded.state_file, state_file)
        self.assertEqual(loaded.get_package_state("nvim"), "installing")
        self.assertEqual(loaded.get_package_state("tmux"), "installed")
        self.assertTrue(loaded.has_installing_package())

        # Test removing and saving again
        loaded.remove_package("nvim")
        self.assertFalse(loaded.has_installing_package())
        self.assertIsNone(loaded.get_package_state("nvim"))
        loaded.save()

    def test_state_registry_state_filtering(self) -> None:
        """Verifies filter_by_states, get_midway_packages, and is_package_in_midway_state."""
        registry = StateRegistry()
        registry.set_package_state("pkg1", "staging")
        registry.set_package_state("pkg2", "installing")
        registry.set_package_state("pkg3", "installed")
        registry.set_package_state("pkg4", "staged")

        # Test is_package_in_midway_state
        self.assertTrue(registry.is_package_in_midway_state("pkg1"))
        self.assertTrue(registry.is_package_in_midway_state("pkg2"))
        self.assertFalse(registry.is_package_in_midway_state("pkg3"))
        self.assertFalse(registry.is_package_in_midway_state("pkg4"))
        self.assertFalse(registry.is_package_in_midway_state("non_existent"))

        # Test filter_by_states with all packages
        installed = registry.filter_by_states(["installed"])
        self.assertEqual(installed, [("pkg3", "installed")])

        # Test filter_by_states with target_packages subset
        staging = registry.filter_by_states(["staging"], target_packages=["pkg1", "pkg3"])
        self.assertEqual(staging, [("pkg1", "staging")])

        # Test get_midway_packages with all packages
        midway_all = registry.get_midway_packages()
        self.assertEqual(midway_all, [("pkg1", "staging"), ("pkg2", "installing")])

        # Test get_rollback_eligible_packages with all packages
        rollback_all = registry.get_rollback_eligible_packages()
        self.assertEqual(rollback_all, [("pkg1", "staging"), ("pkg2", "installing"), ("pkg4", "staged")])

        # Test get_midway_packages with target_packages subset
        midway_subset = registry.get_midway_packages(["pkg2", "pkg3"])
        self.assertEqual(midway_subset, [("pkg2", "installing")])

        # Test filter_by_states and get_midway_packages with lazy generators (unmaterialized)
        states_gen = (s for s in ["staging", "installing"])
        pkgs_gen = (p for p in ["pkg1", "pkg3"])
        self.assertEqual(registry.filter_by_states(states_gen, target_packages=pkgs_gen), [("pkg1", "staging")])

        pkgs_midway_gen = (p for p in ["pkg2", "pkg3"])
        self.assertEqual(registry.get_midway_packages(pkgs_midway_gen), [("pkg2", "installing")])


    def test_package_state_dataclass(self) -> None:
        """Verifies the PackageState dataclass attributes and defaults."""
        p_state = PackageState(state="installed", last_deployed="2026-08-17", install_method=InstallMethod.COPY, deployed_files=[Path("file1"), Path("file2")])
        self.assertEqual(p_state.state, "installed")
        self.assertEqual(p_state.last_deployed, "2026-08-17")
        self.assertEqual(p_state.install_method, InstallMethod.COPY)
        self.assertEqual(p_state.deployed_files, [Path("file1"), Path("file2")])

        # Test defaults
        default_state = PackageState(state="installing")
        self.assertEqual(default_state.state, "installing")
        self.assertIsNone(default_state.last_deployed)
        self.assertIsNone(default_state.install_method)
        self.assertEqual(default_state.deployed_files, [])
        self.assertIsNone(default_state.last_deployed)
        self.assertIsNone(default_state.install_method)
        self.assertEqual(default_state.deployed_files, [])

    def test_install_symlink_incremental_deployment(self) -> None:
        """Verifies symlink incremental file-by-file manual symlinking deployment."""
        pkg = "pkg_symlink"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "dot-bashrc"), "w", encoding="utf-8") as f:
            f.write("content of bashrc")

        # Run deployment
        run_primitive_5_install(
            self.workspace_config,
            [pkg],
        )

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

    def test_install_symlink_collision_guard(self) -> None:
        """Verifies Symlink Collision Guard backs up pre-existing physical files at target."""
        pkg = "pkg_symlink"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.system_target_dir}"
            """)

        with open(os.path.join(pkg_install_dir, "dot-bashrc"), "w", encoding="utf-8") as f:
            f.write("new content")

        # Create physical non-symlink file at system target (simulating pre-existing collision file)
        target_file = os.path.join(self.system_target_dir, ".bashrc")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("pre-existing user content")

        # Run deployment
        run_primitive_5_install(
            self.workspace_config,
            [pkg],
        )

        # Collision file should be backed up under backup/pkg_symlink/overwritten/dot-bashrc
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
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config with lifecycle hooks
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"

            [hooks]
            post_install = "drift_hooks/on-install.sh"
            post_update = "drift_hooks/on-update.sh"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello copy")

        # Write hooks in install/pkg_copy/.drift/hooks/
        hooks_dir = os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, DRIFT_INTERNAL_HOOKS_DIR_NAME)
        os.makedirs(hooks_dir, exist_ok=True)
        with open(os.path.join(hooks_dir, "on-install.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook installed' > \"$drift_package_target_dir/hook_ran.txt\"\n")
        with open(os.path.join(hooks_dir, "on-update.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook updated' > \"$drift_package_target_dir/hook_ran.txt\"\n")

        # Simulation 1: First-Time Deploy (triggers collision guard and post_install)
        # Create a pre-existing target file
        target_file = os.path.join(self.system_target_dir, "test.txt")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("colliding user file")

        # Run first-time deployment
        run_primitive_5_install(
            self.workspace_config,
            [pkg],
        )

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

        # Simulation 2: Update/Reinstall (bypasses collision guard, triggers post_update)
        # Modify content in install/
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("updated hello copy")

        # Modify test.txt slightly in system so it is different, ensuring it gets overwritten
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("drifted system file")

        # Clear hook marker first
        os.remove(hook_marker)

        # Run update deployment
        run_primitive_5_install(
            self.workspace_config,
            [pkg],
        )

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
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # 1. Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
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
        run_primitive_5_install(self.workspace_config, [pkg])

        # 4. Verify the target is a regular file, NOT a symlink
        target_file = self.system_target_dir / "real_file.txt"
        self.assertTrue(target_file.is_file())
        self.assertFalse(target_file.is_symlink(), f"Target file {target_file} should be a physical copy, not a symlink.")
        self.assertEqual(target_file.read_text(encoding="utf-8"), "actual content")

    def test_lifecycle_hook_failure_and_timeout(self) -> None:
        """Verifies trigger_hook handles failures and timeouts with detailed logging and RuntimeError."""
        from unittest.mock import patch
        import subprocess
        from drift.hooks.lifecycle_hooks import trigger_hook
        
        pkg = "pkg_copy"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        config = PackageConfig(
            PackageSectionConfig(
                name=pkg,
                target_directory=Path(self.system_target_dir),
            ),
            hooks=PackageHooks(post_install=Path(pkg_install_dir) / "on-install.sh")
        )
        
        # Write dummy hook script so hook_path.exists() is True
        hook_path = os.path.join(pkg_install_dir, "on-install.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("# dummy")

        cwd = Path(self.system_target_dir)

        # 1. Test CalledProcessError
        with patch("drift.hooks.lifecycle_hooks.run_command") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=5,
                cmd=["dummy.sh"],
                output="Some normal output",
                stderr="Some severe error output"
            )
            with self.assertRaises(RuntimeError) as ctx:
                trigger_hook(
                    pkg=pkg,
                    hook_name="post_install",
                    metadata=config,
                    cwd=cwd
                )
            self.assertIn("failed with exit code 5", str(ctx.exception))
            self.assertIn("Some severe error output", str(ctx.exception))

        # 2. Test TimeoutExpired
        with patch("drift.hooks.lifecycle_hooks.run_command") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["dummy.sh"],
                timeout=120,
                output="Standard timeout output",
                stderr="Standard timeout stderr"
            )
            with self.assertRaises(RuntimeError) as ctx:
                trigger_hook(
                    pkg=pkg,
                    hook_name="post_install",
                    metadata=config,
                    cwd=cwd
                )
            self.assertIn("timed out after 120 seconds", str(ctx.exception))
            self.assertIn("Standard timeout stderr", str(ctx.exception))

        # 3. Test missing hook script raises FileNotFoundError
        config_missing = PackageConfig(
            PackageSectionConfig(
                name=pkg,
                target_directory=Path(self.system_target_dir),
            ),
            hooks=PackageHooks(post_install=Path(pkg_install_dir) / "non_existent_script.sh")
        )
        with self.assertRaises(FileNotFoundError) as ctx:
            trigger_hook(
                pkg=pkg,
                hook_name="post_install",
                metadata=config_missing,
                cwd=cwd
            )
        self.assertIn("not found", str(ctx.exception))

    def test_lifecycle_hooks_sudo_privileges(self) -> None:
        """Verifies that only pre/post_install and pre/post_update hooks run with sudo when sudo=True."""
        from unittest.mock import patch
        from drift.hooks.lifecycle_hooks import trigger_hook

        pkg = "pkg_hooks_sudo"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(pkg_install_dir, exist_ok=True)

        hook_abs = Path(pkg_install_dir) / "hook.sh"
        all_hooks = PackageHooks(
            probe=hook_abs,
            pre_source=hook_abs,
            pre_install=hook_abs,
            post_install=hook_abs,
            pre_update=hook_abs,
            post_update=hook_abs,
            pre_uninstall=hook_abs,
            post_uninstall=hook_abs,
            post_render=hook_abs,
            health=hook_abs
        )
        config_sudo = PackageConfig(
            PackageSectionConfig(
                name=pkg,
                target_directory=Path(self.system_target_dir),
                sudo=True,
            ),
            hooks=all_hooks
        )

        hook_path = os.path.join(pkg_install_dir, "hook.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("# dummy")
        os.chmod(hook_path, 0o755)

        cwd = Path(self.system_target_dir)

        from drift.core.constants import LIFECYCLE_HOOK_NAMES

        # All lifecycle hooks always execute in user space without sudo (preserving all injected envs)
        for hook_name in LIFECYCLE_HOOK_NAMES:
            with patch("drift.hooks.lifecycle_hooks.run_command") as mock_run:
                mock_run.return_value.returncode = 0
                trigger_hook(
                    pkg=pkg,
                    hook_name=hook_name,
                    metadata=config_sudo,
                    cwd=cwd
                )
                called_cmd = mock_run.call_args[0][0]
                self.assertNotEqual(called_cmd[0], "sudo", f"Hook '{hook_name}' should NOT run with sudo even when sudo=True")
                self.assertEqual(called_cmd[0], str(hook_path))

    def test_lifecycle_hook_non_executable_runs_via_interpreter_fallback(self) -> None:
        """Verifies that execute_hook_script falls back to interpreter without mutating disk permissions."""
        from unittest.mock import patch
        from drift.hooks.lifecycle_hooks import execute_hook_script

        pkg = "pkg_hook_perm"
        pkg_install_dir = Path(self.install_dir) / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        hook_script = pkg_install_dir / "hook.sh"
        hook_script.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
        hook_script.chmod(0o644)

        config = PackageConfig(PackageSectionConfig(name=pkg))

        with patch("drift.hooks.lifecycle_hooks.run_command") as mock_run:
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
        from drift.primitives.install_repo import install_one_package
        from drift.core.state_registry import StateRegistry

        pkg = "pkg_env_hooks"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Do NOT specify target_directory in package config, so it falls back to workspace_config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"

            [hooks]
            post_install = "drift_hooks/hook.sh"
            """)

        hooks_dir = os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, DRIFT_INTERNAL_HOOKS_DIR_NAME)
        os.makedirs(hooks_dir, exist_ok=True)
        hook_path = os.path.join(hooks_dir, "hook.sh")
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
            res.stdout = ""
            res.stderr = ""
            return res

        meta = PackageConfig.from_install_dir(self.install_dir / pkg, self.workspace_config)
        with patch("drift.hooks.lifecycle_hooks.run_command", side_effect=mock_run_cmd):
            install_one_package(
                workspace_config=self.workspace_config,
                state_registry=registry,
                metadata=meta,
                options=InstallOptions(resolve_symlinks=False, force=True)
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
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
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
        
        # Add .drift_ignore in .drift/
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, DRIFT_IGNORE_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("ignore_me.txt\n")

        # Run full deployment (no package_changes passed)
        run_primitive_5_install(self.workspace_config, [pkg])

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
        pkg = "pkg_symlink"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Setup target directory for the package inside system_target_dir
        pkg_target_dir = os.path.join(self.system_target_dir, "pkg_safety_target")

        # Write package config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
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
            run_primitive_5_install(
                self.workspace_config,
                [pkg],
            )
        
        self.assertIn("Safety Abort", str(ctx.exception))
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx.exception))

    def test_symlinked_parent_rebuilt_inside_target_dir(self) -> None:
        """Verifies that a parent symlink situated INSIDE the package's target_dir is successfully backed up, deleted, and rebuilt as a physical folder."""
        pkg = "pkg_symlink"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file under subfolder nested_app in install
        nested_src_dir = os.path.join(pkg_install_dir, "nested_app")
        os.makedirs(nested_src_dir, exist_ok=True)
        with open(os.path.join(nested_src_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write("config content")

        # Make the parent "nested_app" inside system_target_dir a symlink pointing to drift_root (simulating symlink conflict inside target)
        nested_target_symlink = os.path.join(self.system_target_dir, "nested_app")
        fake_drift_dest = os.path.join(self.drift_root, "fake_drift_dest")
        os.makedirs(fake_drift_dest, exist_ok=True)
        os.symlink(fake_drift_dest, nested_target_symlink)

        # Deploy
        run_primitive_5_install(
            self.workspace_config,
            [pkg],
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

    def test_run_full_symlink_deployment(self) -> None:
        """Verifies that plan_package_install and execute_package_actions link deployable files natively."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_symlink_full"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "file1.txt").write_text("hello 1", encoding="utf-8")
        (pkg_install_dir / "sub").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "sub" / "file2.txt").write_text("hello 2", encoding="utf-8")

        target_dir = self.system_target_dir / "symlink_test_target"
        deployable_files = [Path("file1.txt"), Path("sub/file2.txt")]

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=deployable_files,
        )
        execute_package_actions(context=context, plan=plan)

        # Target files exist as relative symlinks
        target_f1 = target_dir / "file1.txt"
        target_f2 = target_dir / "sub" / "file2.txt"

        self.assertTrue(target_f1.is_symlink())
        self.assertEqual(target_f1.read_text(encoding="utf-8"), "hello 1")
        self.assertEqual(target_f1.resolve(), (pkg_install_dir / "file1.txt").resolve())

        self.assertTrue(target_f2.is_symlink())
        self.assertEqual(target_f2.read_text(encoding="utf-8"), "hello 2")
        self.assertEqual(target_f2.resolve(), (pkg_install_dir / "sub" / "file2.txt").resolve())

    def test_collision_guard_ignored_file_deletion(self) -> None:
        """Verifies that if a staged file matches .drift_ignore,
        the collision guard will ignore its corresponding file the host system."""
        # 1. Create a package in install/ State Database
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.system_target_dir}"
            """)

        # Add physical file under install/pkg_symlink, e.g., ignored_file.txt
        with open(os.path.join(pkg_install_dir, "ignored_file.txt"), "w", encoding="utf-8") as f:
            f.write("should be ignored")

        # Write .drift_ignore to install/pkg_symlink/.drift/ telling it to ignore ignored_file.txt
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, DRIFT_IGNORE_FILE_NAME), "w", encoding="utf-8") as f:
            f.write("ignored_file.txt\n")

        # Create that file at system target (simulating it was previously deployed or exists there)
        system_file = self.system_target_dir / "ignored_file.txt"
        with open(system_file, "w", encoding="utf-8") as f:
            f.write("pre-existing on target")

        # Execute deployment
        run_primitive_5_install(self.workspace_config, [pkg])

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
        pkg = "pkg_copy"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # 1. Setup two physical files under install/pkg_copy
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
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
        run_primitive_5_install(self.workspace_config, [pkg])

        # Verify both files are deployed
        system_file1 = self.system_target_dir / "file1.txt"
        system_file2 = self.system_target_dir / "file2.txt"
        self.assertTrue(system_file1.exists() or system_file1.is_symlink())
        self.assertTrue(system_file2.exists() or system_file2.is_symlink())

        # Verify state.toml has registered them in deployed_files
        from drift.core.state_registry import load_state_registry
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        self.assertEqual(sorted(registry.get_package_deployed_files(pkg)), [Path("file1.txt"), Path("file2.txt")])

        # 2. Simulate manual deletion of file2.txt from install/pkg_copy
        os.remove(os.path.join(pkg_install_dir, "file2.txt"))

        # Re-run standalone deployment (without package_changes)
        res2 = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual([str(a.src_path.relative_to(self.system_target_dir)) for a in res2.packages[0].plan.prune_backups if a.src_path is not None], ["file2.txt"])

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

    def test_standalone_apply_prunes_stale_symlink_links(self) -> None:
        """Verifies that standalone deploy_package with install_method="symlink" unlinks/deletes stale symlinks,
        but does NOT attempt to create a backup of a broken symlink (which has no target data).
        """
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # 1. Setup config with symlink method
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.system_target_dir}"
            """)

        with open(os.path.join(pkg_install_dir, "file1.txt"), "w", encoding="utf-8") as f:
            f.write("first symlink file")
        with open(os.path.join(pkg_install_dir, "file2.txt"), "w", encoding="utf-8") as f:
            f.write("second symlink file")

        # Deploy first time using symlink
        run_primitive_5_install(self.workspace_config, [pkg])

        # Verify links are deployed
        system_file1 = self.system_target_dir / "file1.txt"
        system_file2 = self.system_target_dir / "file2.txt"
        self.assertTrue(os.path.islink(system_file1))
        self.assertTrue(os.path.islink(system_file2))

        # 2. Simulate manual deletion of file2.txt from install/pkg_symlink (which breaks its symlink)
        os.remove(os.path.join(pkg_install_dir, "file2.txt"))

        # Re-run standalone deployment
        run_primitive_5_install(self.workspace_config, [pkg])

        # 3. Assert the stale symlink was successfully pruned/deleted from the host target
        self.assertFalse(os.path.exists(system_file2))
        self.assertFalse(os.path.islink(system_file2))

        # Assert no backup was created (since the broken symlink contains no real file target content)
        backup_pruned = self.backup_dir / pkg / "deleted_files" / "file2.txt"
        self.assertFalse(backup_pruned.exists())

    def test_collision_guard_handles_internal_and_external_symlinks(self) -> None:
        """Verifies collision guard behavior with internal (dangling/valid) and external symlinks.

        - Symlink pointing into drift_root: deleted without backup.
        - Symlink pointing outside drift_root (resolvable): treated as collision, target contents backed up, and replaced.
        - Symlink pointing outside drift_root (broken): treated as collision, symlink itself backed up, and replaced.
        """
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # 1. Setup config with symlink method
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
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
        run_primitive_5_install(self.workspace_config, [pkg])

        # 2. Assertions:
        # - The internal symlink was deleted without backup:
        self.assertFalse((self.backup_dir / pkg / "overwritten" / "internal_link.txt").exists())
        # But it should be replaced by the newly created symlink pointing to pkg_install_dir:
        self.assertTrue(system_internal.is_symlink())
        
        # - The external symlink was backed up by its content because the link can be resolved:
        backup_external = self.backup_dir / pkg / "overwritten" / "external_link.txt"
        self.assertTrue(backup_external.exists())
        with open(backup_external, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, "external config source")
        # And it should be replaced by the newly created symlink pointing to pkg_install_dir:
        self.assertTrue(system_external.is_symlink())

        # - The external broken symlink was backed up as a symlink itself because it cannot be resolved:
        backup_external_broken = self.backup_dir / pkg / "overwritten" / "external_broken.txt"
        self.assertTrue(backup_external_broken.is_symlink())
        self.assertEqual(backup_external_broken.readlink(), nonexistent_external_file)
        # And it should be replaced by the newly created symlink pointing to pkg_install_dir:
        self.assertTrue(system_external_broken.is_symlink())

    def test_install_target_cannot_be_inside_drift_root(self) -> None:
        """Verifies that the installation deployment raises InstallCollisionError if the target directory is inside or equal to drift_root."""
        from drift.core.exceptions import InstallCollisionError
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # 1. Setup config with target_directory equal to drift_root
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{self.drift_root}"
            """)

        # Execute deployment and assert InstallCollisionError
        with self.assertRaises(InstallCollisionError) as ctx:
            run_primitive_5_install(self.workspace_config, [pkg])
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx.exception))

        # 2. Setup config with target_directory INSIDE drift_root (e.g. self.drift_root / "polluted_dir")
        polluted_dir = self.drift_root / "polluted_dir"
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
            target_directory = "{polluted_dir}"
            """)

        # Execute deployment and assert InstallCollisionError
        with self.assertRaises(InstallCollisionError) as ctx:
            run_primitive_5_install(self.workspace_config, [pkg])
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx.exception))

    def test_run_primitive_6_commit_install_repo(self) -> None:
        """Verifies staging and committing changes within the install state repository (Primitive 6)."""
        from drift.primitives.install_repo import run_primitive_6_commit_install_repo
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


    def test_deploy_failure_leaves_state_as_installing(self) -> None:
        """Verifies that if deployment fails midway, the package state remains 'installing' in state.toml."""
        pkg = "pkg_fail"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config with a post_install hook that will fail
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"

            [hooks]
            post_install = "drift_hooks/fail.sh"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello fail")
            
        # Write failing hook script
        hooks_dir = os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, DRIFT_INTERNAL_HOOKS_DIR_NAME)
        os.makedirs(hooks_dir, exist_ok=True)
        hook_path = os.path.join(hooks_dir, "fail.sh")
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook_path, 0o755)

        # Attempt to deploy - should fail due to hook
        with self.assertRaises(RuntimeError):
            run_primitive_5_install(self.workspace_config, [pkg])

        # Check state.toml
        state_file = os.path.join(self.install_dir, "state.toml")
        from drift.core.state_registry import load_state_registry
        registry = load_state_registry(Path(state_file))
        self.assertEqual(registry.get_package_state(pkg), "installing")
        self.assertTrue(registry.has_installing_package())

    def test_deploy_aborts_if_already_installing(self) -> None:
        """Verifies that deployment aborts if a package is already in 'installing' state."""
        pkg = "pkg_installing"
        pkg_install_dir = os.path.join(self.install_dir, pkg)
        os.makedirs(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME), exist_ok=True)

        # Write config
        with open(os.path.join(pkg_install_dir, DRIFT_INTERNAL_DIR_NAME, PACKAGE_CONFIG_FILE_NAME), "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        # Pre-set state to 'installing'
        state_file = os.path.join(self.install_dir, "state.toml")
        from drift.core.state_registry import load_state_registry, save_state_registry
        registry = load_state_registry(Path(state_file))
        registry.set_package_state(pkg, "installing")
        save_state_registry(registry)

        # Attempt to deploy - should abort with Safety Abort
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_5_install(self.workspace_config, [pkg])
        
        self.assertIn("Safety Abort", str(ctx.exception))
        self.assertIn("Package(s) in midway transaction state", str(ctx.exception))
        self.assertIn("pkg_installing", str(ctx.exception))
        assert isinstance(ctx.exception, MidwayTransactionError)
        self.assertEqual(ctx.exception.packages, [pkg])

        # Attempt with force=True - should proceed (and succeed here)
        run_primitive_5_install(self.workspace_config, [pkg], options=InstallOptions(force=True))
        
        # Verify success after force
        registry = load_state_registry(Path(state_file))
        self.assertEqual(registry.get_package_state(pkg), "installed")

    def test_install_package_name_starts_with_dot_dash(self) -> None:
        """Verifies that deploying/installing a package whose name starts with 'dot-' works exactly as expected and preserves files on the target."""
        pkg_name = "dot-my_pkg"
        pkg_install_dir = self.install_dir / pkg_name
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # Enable in workspace
        self.workspace_config.packages_enable[pkg_name] = True

        # Write config and file
        with open(pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_name}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)

        with open(pkg_install_dir / "static.txt", "w", encoding="utf-8") as f:
            f.write("deployed static content")

        # Run deployment
        run_primitive_5_install(self.workspace_config, [pkg_name])

        # Verify output target file exists under system target dir (name preserved, files copied)
        target_file = self.system_target_dir / "static.txt"
        self.assertTrue(target_file.is_file())
        self.assertEqual(target_file.read_text(encoding="utf-8"), "deployed static content")

        # Verify state in state.toml is registered under 'dot-my_pkg'
        state_file = self.install_dir / "state.toml"
        from drift.core.state_registry import load_state_registry
        registry = load_state_registry(state_file)
        self.assertEqual(registry.get_package_state(pkg_name), "installed")

    def test_skipped_package_not_set_to_installing_state(self) -> None:
        """Verifies that skipped packages (enable_install=False or missing dir) are not set to 'installing' in state.toml."""
        from drift.core.state_registry import load_state_registry

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)

        # 1. Test package with enable_install = false
        pkg_disabled = "pkg_disabled"
        pkg_dir = self.install_dir / pkg_disabled
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_disabled}"
        install_method = "copy"
        enable_install = false
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res_disabled = run_primitive_5_install(
            workspace_config=self.workspace_config,
            target_pkgs=[pkg_disabled],
            options=InstallOptions(resolve_symlinks=True, force=False),
        )
        self.assertEqual(res_disabled.packages, [])
        # Check that state.toml did not transition this package into 'installing'
        reloaded = load_state_registry(state_file)
        self.assertNotEqual(reloaded.get_package_state(pkg_disabled), "installing")

        # Also verify that force=True does NOT bypass enable_install=False
        res_forced = run_primitive_5_install(
            workspace_config=self.workspace_config,
            target_pkgs=[pkg_disabled],
            options=InstallOptions(resolve_symlinks=True, force=True),
        )
        self.assertEqual(res_forced.packages, [])
        reloaded = load_state_registry(state_file)
        self.assertNotEqual(reloaded.get_package_state(pkg_disabled), "installing")

        # 2. Test package with missing install directory (corrupted stage)
        pkg_missing = "pkg_missing_dir"
        metadata_missing = PackageConfig(
            PackageSectionConfig(
                name=pkg_missing,
                install_method=InstallMethod.COPY,
                target_directory=str(self.system_target_dir),
                enable_install=True,
            )
        )
        with self.assertRaises(PackageInstallDirMissingError) as cm:
            install_one_package(
                workspace_config=self.workspace_config,
                state_registry=registry,
                metadata=metadata_missing,
                options=InstallOptions(resolve_symlinks=True, force=False),
            )
        self.assertIn("does not exist", str(cm.exception))
        reloaded2 = load_state_registry(state_file)
        self.assertNotEqual(reloaded2.get_package_state(pkg_missing), "installing")

    def test_deploy_executes_hooks_ignored_in_drift_ignore(self) -> None:
        """Verifies that hook scripts listed in .drift_ignore are staged to install/, executed, and not deployed to host."""
        from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
        from drift.render.render_package import render_package

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
        pre_install = "drift_hooks/pre_hook.sh"
        """, encoding="utf-8")

        # 2. Hook script in drift_hooks/ and valid config file
        hooks_dir = pkg_src / DRIFT_HOOKS_DIR_NAME
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / "pre_hook.sh").write_text(f"#!/bin/sh\necho 'hook executed successfully' > '{marker_file}'\n", encoding="utf-8")
        (hooks_dir / "pre_hook.sh").chmod(0o755)
        (pkg_src / "app_setting.conf").write_text("setting = 42\n", encoding="utf-8")

        self.workspace_config.packages_enable[pkg] = True

        # Render -> Stage -> Install
        render_package(self.workspace_config, pkg_src)
        run_primitive_4_stage_render_to_install(self.workspace_config, pkg)
        run_primitive_5_install(self.workspace_config, [pkg])

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
        1. Valid symlink link pointing to this package's file is NOT removed.
        2. Ignored file on system is untouched.
        3. Rogue internal symlink pointing to another drift file is backed up and replaced.
        """
        pkg = "pkg_symlink_guard"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # 1. Package config
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "symlink"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        # 2. .drift_ignore and files
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_IGNORE_FILE_NAME).write_text("ignored_hook.sh\n", encoding="utf-8")
        (pkg_install_dir / "ignored_hook.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (pkg_install_dir / "valid_file.txt").write_text("valid content", encoding="utf-8")
        (pkg_install_dir / "rogue_link.txt").write_text("rogue target content", encoding="utf-8")

        drift_internal_target = self.drift_root / "config" / "drift_workspace.toml"
        drift_internal_target.parent.mkdir(parents=True, exist_ok=True)
        drift_internal_target.write_text("[workspace]\n", encoding="utf-8")

        # 3. Setup system target directory:
        # A. valid_file.txt already points to pkg_install_dir / valid_file.txt (valid symlink link)
        from drift.utils.path_utils import relative_path_between
        system_valid = self.system_target_dir / "valid_file.txt"
        system_valid.symlink_to(relative_path_between(self.system_target_dir, pkg_install_dir / "valid_file.txt"))

        # B. ignored_hook.sh on system is an obsolete file
        system_ignored = self.system_target_dir / "ignored_hook.sh"
        system_ignored.symlink_to(drift_internal_target)

        # C. rogue_link.txt points to wrong internal drift file
        system_rogue = self.system_target_dir / "rogue_link.txt"
        system_rogue.symlink_to(drift_internal_target)

        # 4. Execute install deployment
        res = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # 5. Assertions:
        # A. Valid symlink link is preserved and not backed up as overwritten
        self.assertTrue(system_valid.is_symlink())
        self.assertEqual(system_valid.resolve(), (pkg_install_dir / "valid_file.txt").resolve())
        self.assertFalse((self.backup_dir / pkg / "overwritten" / "valid_file.txt").exists())

        # B. Ignored file on system is ignored and stay the same.
        self.assertTrue(system_ignored.exists())
        with open(system_ignored, "r", encoding="utf-8") as f:
            system_ignored_content = f.read()
            # The system ignored_hook.sh file points to the drift_internal_target, which contains "[workspace]\n"
            self.assertEqual(system_ignored_content, "[workspace]\n")

        # C. Rogue link was backed up and replaced with the correct symlink
        self.assertTrue(system_rogue.is_symlink())
        self.assertEqual(system_rogue.resolve(), (pkg_install_dir / "rogue_link.txt").resolve())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "rogue_link.txt").exists())

    def test_symlink_link_pointing_to_different_file_in_same_pkg_updated_full_deploy(self) -> None:
        """Verifies that under full deployment, if a host symlink points to a different file in the same package's install dir,
        it is recognized as valid for this package and updated to the desired target file upon deployment.
        """
        pkg = "pkg_switch_target_full"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "symlink"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "file_a.txt").write_text("content A", encoding="utf-8")
        (pkg_install_dir / "file_b.txt").write_text("content B", encoding="utf-8")

        # Setup: file_a.txt on system currently points to file_b.txt in the same package install dir
        system_file_a = self.system_target_dir / "file_a.txt"
        system_file_a.symlink_to(pkg_install_dir / "file_b.txt")

        # Execute full deployment
        res = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert:
        # file_a.txt now points to file_a.txt in pkg_install_dir
        self.assertTrue(system_file_a.is_symlink())
        self.assertEqual(system_file_a.resolve(), (pkg_install_dir / "file_a.txt").resolve())

        # file_b.txt is also properly deployed
        system_file_b = self.system_target_dir / "file_b.txt"
        self.assertTrue(system_file_b.is_symlink())
        self.assertEqual(system_file_b.resolve(), (pkg_install_dir / "file_b.txt").resolve())

    def test_symlink_link_pointing_to_different_file_in_same_pkg_updated_partial_deploy(self) -> None:
        """Verifies that under partial/incremental deployment (via PackageStagePlan),
        a host symlink pointing to a different file in the same package is safely updated to the desired target file.
        """
        pkg = "pkg_switch_target_partial"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "symlink"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "file_a.txt").write_text("content A", encoding="utf-8")
        (pkg_install_dir / "file_b.txt").write_text("content B", encoding="utf-8")

        # Setup: file_a.txt on system currently points to file_b.txt in the same package install dir
        system_file_a = self.system_target_dir / "file_a.txt"
        system_file_a.symlink_to(pkg_install_dir / "file_b.txt")

        # Execute deployment modifying file_a.txt
        res = run_primitive_5_install(self.workspace_config, [pkg])
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
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
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

        res = run_primitive_5_install(self.workspace_config, [pkg])
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
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
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
            StateRegistry(
                packages={
                    pkg: PackageState(
                        state="installed",
                        install_method=InstallMethod.COPY,
                        last_deployed="2026-08-20T00:00:00Z",
                        deployed_files=[Path("app.conf")]
                    )
                },
                state_file=state_file
            )
        )

        # Host system has app.conf as a symlink pointing to an external file
        external_file = self.drift_root.parent / "external_target.conf"
        external_file.write_text("setting=external_original\n", encoding="utf-8")

        system_file = self.system_target_dir / "app.conf"
        system_file.symlink_to(external_file)

        # Run deployment update
        res = run_primitive_5_install(self.workspace_config, [pkg])
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
        self.assertEqual([str(a.src_path.relative_to(self.system_target_dir)) for a in res.packages[0].plan.overwritten_backups if a.src_path is not None], ["app.conf"])

    def test_switch_method_from_symlink_to_copy_backs_up_and_replaces_symlinks(self) -> None:
        """Verifies that when a package deployed with 'symlink' switches to 'copy',
        the collision handler backs up the previous symlinks/files into overwritten/
        and replaces them with physical copies.
        """
        pkg = "pkg_symlink_to_copy"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # 1. Initial deployment with 'symlink'
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "symlink"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "config.json").write_text('{"version": 1}', encoding="utf-8")
        sub_dir = pkg_install_dir / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "tool.sh").write_text("#!/bin/bash\necho hi", encoding="utf-8")

        res1 = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res1.status, "SUCCESS")

        host_config = self.system_target_dir / "config.json"
        host_tool = self.system_target_dir / "sub" / "tool.sh"
        self.assertTrue(host_config.is_symlink())
        self.assertTrue(host_tool.is_symlink())

        # 2. Switch install_method to 'copy'
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res2 = run_primitive_5_install(self.workspace_config, [pkg])
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

    def test_switch_method_from_copy_to_symlink_backs_up_and_replaces_physical_files(self) -> None:
        """Verifies that when a package deployed with 'copy' switches to 'symlink',
        the collision handler backs up the previous physical files into overwritten/
        and replaces them with symlinks.
        """
        pkg = "pkg_copy_to_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        # 1. Initial deployment with 'copy'
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "settings.ini").write_text("key=value1\n", encoding="utf-8")
        nested_dir = pkg_install_dir / "nested"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "data.txt").write_text("data payload 1\n", encoding="utf-8")

        res1 = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res1.status, "SUCCESS")

        host_settings = self.system_target_dir / "settings.ini"
        host_data = self.system_target_dir / "nested" / "data.txt"
        self.assertTrue(host_settings.is_file())
        self.assertFalse(host_settings.is_symlink())
        self.assertTrue(host_data.is_file())
        self.assertFalse(host_data.is_symlink())

        # 2. Switch install_method to 'symlink'
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "symlink"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        res2 = run_primitive_5_install(self.workspace_config, [pkg])
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
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        pre_install = "drift_hooks/missing.sh"
        """, encoding="utf-8")
        (pkg_install_dir / "app.conf").write_text("hello", encoding="utf-8")

        with self.assertRaises(FileNotFoundError) as cm:
            run_primitive_5_install(self.workspace_config, [pkg])
        self.assertIn("missing.sh", str(cm.exception))

    def test_install_fails_if_hook_file_is_directory_in_install(self) -> None:
        """Verifies that installation raises HookMissingError if a configured hook file is a directory in install/."""
        pkg = "pkg_hook_is_dir"
        self.workspace_config.packages_enable[pkg] = True

        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "hook_dir").mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        post_install = "drift_hooks/hook_dir"
        """, encoding="utf-8")
        with self.assertRaises(HookMissingError) as cm:
            run_primitive_5_install(self.workspace_config, [pkg])
        self.assertIn("not a regular file", str(cm.exception))

    def test_plan_package_install_detects_internal_symlink_conflicts(self) -> None:
        """Verifies plan_package_install detects internal ancestor symlinks and leaf symlink collisions."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_find_conflicts"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "nested").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "nested" / "app.conf").write_text("hello", encoding="utf-8")
        (pkg_install_dir / "root.conf").write_text("root", encoding="utf-8")
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
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

        # 2. 'root.conf' is a symlink pointing outside drift_root (e.g. /tmp) -> colliding external symlink
        outside_target = Path(tempfile.gettempdir()) / "outside_drift.txt"
        outside_target.write_text("outside", encoding="utf-8")
        (self.system_target_dir / "root.conf").symlink_to(outside_target)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )
        plan = plan_package_install(
            context=context,
            deployable_files=[Path("nested/app.conf"), Path("root.conf")],
        )

        # 'nested' ancestor has BACKUP_OVERWRITE (Internal ancestor symlink conflict) and ENSURE_DIR
        nested_overwrites = [a for a in plan.actions if a.src_path == self.system_target_dir / "nested" and a.action_type == FileActionType.BACKUP_OVERWRITE]
        self.assertEqual(len(nested_overwrites), 1)
        self.assertEqual(nested_overwrites[0].reason, "Internal ancestor symlink conflict")
        self.assertEqual(nested_overwrites[0].src_path, self.system_target_dir / "nested")

        # 'root.conf' leaf has BACKUP_OVERWRITE (Colliding external symlink) and CREATE_COPY
        root_overwrites = [a for a in plan.actions if a.src_path == self.system_target_dir / "root.conf" and a.action_type == FileActionType.BACKUP_OVERWRITE]
        self.assertEqual(len(root_overwrites), 1)
        self.assertEqual(root_overwrites[0].reason, "Colliding external symlink")

    def test_plan_and_execute_internal_symlink_directory_conflict(self) -> None:
        """Verifies planning and executing an internal symlink directory conflict replaces it with a real dir."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_resolve_conflict"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "sub_dir").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "sub_dir" / "file.txt").write_text("file content", encoding="utf-8")

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        fake_drift_dest = self.drift_root / "fake_drift_dest"
        fake_drift_dest.mkdir(parents=True, exist_ok=True)

        system_target = self.system_target_dir / "sub_dir"
        system_target.symlink_to(fake_drift_dest)

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("sub_dir/file.txt")],
        )
        execute_package_actions(context=context, plan=plan)

        # 1. system_target is now a physical directory
        self.assertTrue(system_target.is_dir())
        self.assertFalse(system_target.is_symlink())

        # 2. Backup path structure was created
        backup_path = self.backup_dir / pkg / "overwritten" / "sub_dir"
        self.assertTrue(backup_path.exists())

        # 3. File was created
        self.assertTrue((system_target / "file.txt").is_file())
        self.assertEqual((system_target / "file.txt").read_text(encoding="utf-8"), "file content")

    def test_plan_and_execute_ancestor_symlink_with_nested_descendants(self) -> None:
        """Verifies that when shallow ancestor 'a' is a symlink to a folder on host, only 'a' is backed up,
        subsequent deeper ancestors emit ENSURE_DIR, and leaf files emit creation actions without duplicate backups."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_ancestor_symlink_nesting"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "a" / "b").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "a" / "b" / "c.txt").write_text("package c content", encoding="utf-8")

        # Create external folder on host containing b/c.txt
        external_folder = Path(tempfile.mkdtemp(prefix="drift_foreign_dir_"))
        (external_folder / "b").mkdir(parents=True, exist_ok=True)
        (external_folder / "b" / "c.txt").write_text("foreign c content", encoding="utf-8")

        # Host system_target_dir / "a" is a symlink to external_folder
        (self.system_target_dir / "a").symlink_to(external_folder)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("a/b/c.txt")],
        )

        # 1. Exactly one BACKUP_OVERWRITE (on 'a'), ENSURE_DIR on 'a', ENSURE_DIR on 'a/b', CREATE_SYMLINK on 'a/b/c.txt'
        overwrites = [act for act in plan.actions if act.action_type == FileActionType.BACKUP_OVERWRITE]
        self.assertEqual(len(overwrites), 1)
        self.assertEqual(overwrites[0].src_path, self.system_target_dir / "a")
        self.assertEqual(overwrites[0].reason, "Symlink blocking directory")

        ensure_dirs = [act for act in plan.actions if act.action_type == FileActionType.ENSURE_DIR]
        self.assertEqual(len(ensure_dirs), 2)
        self.assertEqual(ensure_dirs[0].dst_path, self.system_target_dir / "a")
        self.assertEqual(ensure_dirs[1].dst_path, self.system_target_dir / "a" / "b")

        creations = [act for act in plan.actions if act.action_type == FileActionType.CREATE_SYMLINK]
        self.assertEqual(len(creations), 1)
        self.assertEqual(creations[0].dst_path, self.system_target_dir / "a" / "b" / "c.txt")

        # 2. Execute plan and verify host state
        execute_package_actions(context=context, plan=plan)

        self.assertTrue((self.system_target_dir / "a").is_dir())
        self.assertFalse((self.system_target_dir / "a").is_symlink())
        self.assertTrue((self.system_target_dir / "a" / "b").is_dir())
        self.assertTrue((self.system_target_dir / "a" / "b" / "c.txt").is_symlink())
        self.assertEqual((self.system_target_dir / "a" / "b" / "c.txt").read_text(encoding="utf-8"), "package c content")

        # 3. Verify backup of the original contents
        backup_dir = self.backup_dir / pkg / "overwritten" / "a"
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual((backup_dir / "b" / "c.txt").read_text(encoding="utf-8"), "foreign c content")

        shutil.rmtree(external_folder, ignore_errors=True)

    def test_plan_and_execute_dotfile_ancestor_symlink_with_nested_descendants(self) -> None:
        """Verifies that dotfile prefixes like dot-config as host symlinks trigger backup only on .config,
        with proper dot-prefix translation for backup paths and ENSURE_DIR for subpaths."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_dotfile_ancestor_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config" / "nvim" / "lua").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config" / "nvim" / "lua" / "init.lua").write_text("lua content", encoding="utf-8")

        # Create external folder on host containing nvim/lua/init.lua
        external_folder = Path(tempfile.mkdtemp(prefix="drift_foreign_dotconfig_"))
        (external_folder / "nvim" / "lua").mkdir(parents=True, exist_ok=True)
        (external_folder / "nvim" / "lua" / "init.lua").write_text("old lua content", encoding="utf-8")

        # Host system_target_dir / ".config" is a symlink to external_folder
        (self.system_target_dir / ".config").symlink_to(external_folder)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("dot-config/nvim/lua/init.lua")],
        )

        # 1. Exactly one BACKUP_OVERWRITE for dot-config
        overwrites = [act for act in plan.actions if act.action_type == FileActionType.BACKUP_OVERWRITE]
        self.assertEqual(len(overwrites), 1)
        self.assertEqual(overwrites[0].src_path, self.system_target_dir / ".config")

        # 2. ENSURE_DIR for .config, .config/nvim, .config/nvim/lua
        ensure_dirs = [act for act in plan.actions if act.action_type == FileActionType.ENSURE_DIR]
        self.assertEqual(len(ensure_dirs), 3)
        self.assertEqual([d.dst_path for d in ensure_dirs], [self.system_target_dir / ".config", self.system_target_dir / ".config/nvim", self.system_target_dir / ".config/nvim/lua"])

        # 3. CREATE_COPY for init.lua
        creations = [act for act in plan.actions if act.action_type == FileActionType.CREATE_COPY]
        self.assertEqual(len(creations), 1)
        self.assertEqual(creations[0].dst_path, self.system_target_dir / ".config/nvim/lua/init.lua")

        # 4. Execute and verify
        execute_package_actions(context=context, plan=plan)

        self.assertTrue((self.system_target_dir / ".config").is_dir())
        self.assertFalse((self.system_target_dir / ".config").is_symlink())
        self.assertTrue((self.system_target_dir / ".config" / "nvim" / "lua" / "init.lua").is_file())
        self.assertEqual((self.system_target_dir / ".config" / "nvim" / "lua" / "init.lua").read_text(encoding="utf-8"), "lua content")

        # 5. Backup translation to dot-config
        backup_dir = self.backup_dir / pkg / "overwritten" / "dot-config"
        self.assertTrue(backup_dir.is_dir())
        self.assertEqual((backup_dir / "nvim" / "lua" / "init.lua").read_text(encoding="utf-8"), "old lua content")

        shutil.rmtree(external_folder, ignore_errors=True)

    def test_plan_intermediate_ancestor_symlink_deduplication(self) -> None:
        """Verifies that when a deeper ancestor 'a/b' is a symlink (with 'a' being a regular dir),
        only 'a/b' is backed up, while other siblings of 'a' are inspected normally."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_intermediate_ancestor_symlink"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "a" / "b" / "c").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "a" / "b" / "c" / "deep.txt").write_text("deep", encoding="utf-8")
        (pkg_install_dir / "a" / "sibling.txt").write_text("sibling", encoding="utf-8")

        # Host has real directory 'a', and 'a/b' is a symlink to external_folder containing c/deep.txt
        (self.system_target_dir / "a").mkdir(parents=True, exist_ok=True)
        external_folder = Path(tempfile.mkdtemp(prefix="drift_foreign_b_"))
        (external_folder / "c").mkdir(parents=True, exist_ok=True)
        (external_folder / "c" / "deep.txt").write_text("foreign deep", encoding="utf-8")
        (self.system_target_dir / "a" / "b").symlink_to(external_folder)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("a/b/c/deep.txt"), Path("a/sibling.txt")],
        )

        # Only 'a/b' receives BACKUP_OVERWRITE
        overwrites = [act for act in plan.actions if act.action_type == FileActionType.BACKUP_OVERWRITE]
        self.assertEqual(len(overwrites), 1)
        self.assertEqual(overwrites[0].src_path, self.system_target_dir / "a/b")

        # ENSURE_DIR for 'a/b' and 'a/b/c' ('a' is already a dir on host so _inspect_single_ancestor returns [])
        ensure_dirs = [act for act in plan.actions if act.action_type == FileActionType.ENSURE_DIR]
        self.assertEqual(len(ensure_dirs), 2)
        self.assertEqual([d.dst_path for d in ensure_dirs], [self.system_target_dir / "a/b", self.system_target_dir / "a/b/c"])

        # Leaf creations for both files
        creations = [act for act in plan.actions if act.action_type == FileActionType.CREATE_SYMLINK]
        self.assertEqual(len(creations), 2)

        shutil.rmtree(external_folder, ignore_errors=True)

    def test_plan_skips_valid_relative_symlink(self) -> None:
        """Verifies that a valid relative symlink pointing to the current package is planned as SKIP_IDENTICAL."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_symlink_valid"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "valid_file.txt").write_text("valid content", encoding="utf-8")

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        # Create valid relative symlink pointing into install_pkg_dir
        system_target = self.system_target_dir / "valid_file.txt"
        rel_to_install = os.path.relpath(pkg_install_dir / "valid_file.txt", self.system_target_dir)
        os.symlink(rel_to_install, system_target)

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("valid_file.txt")],
        )

        # System target is planned as SKIP_IDENTICAL
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(plan.skipped[0].dst_path, system_target)
        self.assertEqual(plan.skipped[0].src_path, pkg_install_dir / "valid_file.txt")
        self.assertEqual(plan.skipped[0].action_type, FileActionType.SKIP_IDENTICAL)

        execute_package_actions(context=context, plan=plan)
        self.assertTrue(system_target.is_symlink())

    def test_plan_and_execute_internal_symlink_conflicts(self) -> None:
        """Verifies planning and executing detects, backs up, and removes internal symlinks."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_handle_conflicts"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "nested").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "nested" / "app.conf").write_text("hello", encoding="utf-8")
        (pkg_install_dir / "root.conf").write_text("root", encoding="utf-8")
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """)

        # Set up conflicts on system target:
        # 1. 'nested' is a symlink pointing to an internal directory in drift_root
        fake_internal_dest = self.drift_root / "fake_internal_dir"
        fake_internal_dest.mkdir(parents=True, exist_ok=True)
        (self.system_target_dir / "nested").symlink_to(fake_internal_dest)

        # 2. 'root.conf' is a symlink pointing to an internal file in drift_root
        fake_internal_file = self.drift_root / "fake_internal_file.txt"
        fake_internal_file.write_text("internal", encoding="utf-8")
        (self.system_target_dir / "root.conf").symlink_to(fake_internal_file)

        # 3. 'external.conf' is a symlink pointing outside drift_root (not in package deployable files)
        outside_target = Path(tempfile.gettempdir()) / "outside_drift.txt"
        outside_target.write_text("outside", encoding="utf-8")
        (self.system_target_dir / "external.conf").symlink_to(outside_target)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )
        plan = plan_package_install(
            context=context,
            deployable_files=[Path("nested/app.conf"), Path("root.conf")],
        )
        execute_package_actions(context=context, plan=plan)

        # root.conf is now a regular file and was backed up
        self.assertTrue((self.system_target_dir / "root.conf").is_file())
        self.assertFalse((self.system_target_dir / "root.conf").is_symlink())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "root.conf").exists())

        # nested symlink should be backed up and replaced with a physical directory
        self.assertTrue((self.system_target_dir / "nested").is_dir())
        self.assertFalse((self.system_target_dir / "nested").is_symlink())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "nested").exists())
        self.assertTrue((self.system_target_dir / "nested" / "app.conf").is_file())

        # external.conf was NOT in package files -> untouched
        self.assertTrue((self.system_target_dir / "external.conf").is_symlink())

    def test_plan_and_execute_valid_symlink_link_preserved(self) -> None:
        """Verifies that a valid relative symlink pointing to the same package install dir is preserved."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_symlink_preserve"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "file.txt").write_text("package content", encoding="utf-8")

        # Create valid relative symlink in system target
        system_target = self.system_target_dir / "file.txt"
        rel_to_install = os.path.relpath(pkg_install_dir / "file.txt", self.system_target_dir)
        os.symlink(rel_to_install, system_target)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )
        plan = plan_package_install(
            context=context,
            deployable_files=[Path("file.txt")],
        )
        self.assertEqual(len(plan.skipped), 1)
        self.assertEqual(plan.skipped[0].action_type, FileActionType.SKIP_IDENTICAL)

        execute_package_actions(context=context, plan=plan)

        # Should remain untouched
        self.assertTrue(system_target.is_symlink())
        self.assertFalse((self.backup_dir / pkg).exists())

    def test_plan_aborts_when_target_resolves_inside_drift_root(self) -> None:
        """Verifies safety abort when target_dir canonical path resolves inside drift workspace root."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_abort"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        internal_target = self.drift_root / "internal_target"
        internal_target.mkdir(parents=True, exist_ok=True)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=internal_target,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )
        with self.assertRaises(InstallCollisionError) as cm:
            plan_package_install(
                context=context,
                deployable_files=[],
            )
        self.assertIn("Safety Abort: Target directory", str(cm.exception))

    def test_plan_and_execute_internal_symlink_invariant_holds(self) -> None:
        """Verifies planning and execution handles internal symlink conflicts and valid symlinks together."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_guard_invariant"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "conflicting_link.txt").write_text("guard pkg content", encoding="utf-8")
        (pkg_install_dir / "valid_link.txt").write_text("valid content", encoding="utf-8")

        # 1. conflicting_link.txt points to an internal drift location (outside this package)
        other_internal_file = self.drift_root / "other_internal_file.txt"
        other_internal_file.write_text("other internal", encoding="utf-8")
        system_conflict = self.system_target_dir / "conflicting_link.txt"
        os.symlink(other_internal_file, system_conflict)

        # 2. valid_link.txt is already a valid relative symlink into this package
        system_valid = self.system_target_dir / "valid_link.txt"
        rel_target = os.path.relpath(pkg_install_dir / "valid_link.txt", self.system_target_dir)
        os.symlink(rel_target, system_valid)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=False,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("conflicting_link.txt"), Path("valid_link.txt")],
        )
        execute_package_actions(context=context, plan=plan)

        # Conflicting internal link was backed up and re-linked to current package
        self.assertTrue(system_conflict.is_symlink())
        self.assertEqual(system_conflict.resolve(), (pkg_install_dir / "conflicting_link.txt").resolve())
        self.assertTrue((self.backup_dir / pkg / "overwritten" / "conflicting_link.txt").exists())

        # Valid link to same package remains intact
        self.assertTrue(system_valid.is_symlink())
        self.assertEqual(system_valid.resolve(), (pkg_install_dir / "valid_link.txt").resolve())

    @patch("drift.core.file_action.create_symlink")
    def test_execute_single_action_skips_when_already_pointing_to_source(self, mock_create_symlink) -> None:
        """Verifies execute_single_action and plan_package_install skip recreating symlink if target already points to source."""
        from drift.core.ignore import DriftIgnore
        pkg = "pkg_symlink_skip"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        src_file = pkg_install_dir / "app.conf"
        src_file.write_text("config data", encoding="utf-8")

        system_target = self.system_target_dir / "app.conf"
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        # 1. Target does not exist -> plan creates CREATE_SYMLINK action
        plan1 = plan_package_install(context=context, deployable_files=[Path("app.conf")])
        self.assertEqual(len(plan1.created), 1)
        self.assertEqual(plan1.created[0].action_type, FileActionType.CREATE_SYMLINK)
        execute_single_action(context.file_action_context, plan1.created[0])
        self.assertEqual(mock_create_symlink.call_count, 1)

        # Create the actual relative symlink on filesystem
        rel_target = os.path.relpath(src_file, self.system_target_dir)
        os.symlink(rel_target, system_target)
        mock_create_symlink.reset_mock()

        # 2. Target already exists and points to src_file -> plan creates SKIP_IDENTICAL action
        plan2 = plan_package_install(context=context, deployable_files=[Path("app.conf")])
        self.assertEqual(len(plan2.skipped), 1)
        self.assertEqual(plan2.skipped[0].action_type, FileActionType.SKIP_IDENTICAL)
        execute_single_action(context.file_action_context, plan2.skipped[0])
        mock_create_symlink.assert_not_called()

        # 3. Target points to an invalid/different location -> plan creates BACKUP_OVERWRITE + CREATE_SYMLINK
        system_target.unlink()
        other_file = Path(tempfile.gettempdir()) / "other.conf"
        other_file.write_text("other", encoding="utf-8")
        os.symlink(other_file, system_target)

        plan3 = plan_package_install(context=context, deployable_files=[Path("app.conf")])
        self.assertEqual(len(plan3.overwritten_backups), 1)
        self.assertEqual(len(plan3.created), 1)
        for act in plan3.actions:
            execute_single_action(context.file_action_context, act)
        self.assertEqual(mock_create_symlink.call_count, 1)

    def test_ensure_dir_raises_on_non_directory_and_symlink(self) -> None:
        """Verifies ensure_dir and execute_single_action raise NotADirectoryError when target is a file or symlink."""
        from drift.utils.file_ops import ensure_dir

        # 1. Existing regular file
        file_path = self.system_target_dir / "existing_file.txt"
        file_path.write_text("file content", encoding="utf-8")

        with self.assertRaises(NotADirectoryError):
            ensure_dir(file_path)

        context = PackageInstallContext(
            pkg_name="pkg_test",
            install_pkg_dir=self.install_dir / "pkg_test",
            backup_pkg_dir=self.backup_dir / "pkg_test",
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )
        action = FileAction(
            action_type=FileActionType.ENSURE_DIR,
            dst_path=file_path,
        )
        with self.assertRaises(NotADirectoryError):
            execute_single_action(context.file_action_context, action)

        # 2. Existing symlink (even if pointing to a directory)
        target_dir = self.system_target_dir / "real_dir"
        target_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = self.system_target_dir / "dir_symlink"
        symlink_path.symlink_to(target_dir)

        with self.assertRaises(NotADirectoryError):
            ensure_dir(symlink_path)

        action_symlink = FileAction(
            action_type=FileActionType.ENSURE_DIR,
            dst_path=symlink_path,
        )
        with self.assertRaises(NotADirectoryError):
            execute_single_action(context.file_action_context, action_symlink)

    def test_backup_prune_backs_up_and_removes_file(self) -> None:
        """Verifies execute_single_action with BACKUP_PRUNE atomically backs up to deleted_files and deletes target."""
        pkg = "pkg_prune_test"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        target_file = self.system_target_dir / "orphan.txt"
        target_file.write_text("orphan content to be pruned", encoding="utf-8")

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=False,
            drift_root=self.workspace_config.drift_root,
        )

        action = FileAction(
            action_type=FileActionType.BACKUP_PRUNE,
            src_path=target_file,
            dst_path=self.backup_dir / pkg / "deleted_files" / "orphan.txt",
            reason="Orphaned file 'orphan.txt' prune",
        )
        execute_single_action(context.file_action_context, action)

        # 1. Target file must be physically removed
        self.assertFalse(target_file.exists())
        self.assertFalse(target_file.is_symlink())

        # 2. Backup must exist in backup/<pkg>/deleted_files/orphan.txt
        backup_file = self.backup_dir / pkg / "deleted_files" / "orphan.txt"
        self.assertTrue(backup_file.is_file())
        self.assertEqual(backup_file.read_text(encoding="utf-8"), "orphan content to be pruned")

    def test_plan_and_execute_orphan_reconciliation(self) -> None:
        """Verifies plan_package_install plans single BACKUP_PRUNE for orphaned files and execution deletes them."""
        pkg = "pkg_orphan_reconcile"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "keep.txt").write_text("keep content", encoding="utf-8")

        # Create host files
        (self.system_target_dir / "keep.txt").write_text("keep content", encoding="utf-8")
        orphan_host = self.system_target_dir / "old_deleted.txt"
        orphan_host.write_text("historical orphan content", encoding="utf-8")

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=False,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context,
            deployable_files=[Path("keep.txt")],
            deployed_files=[Path("keep.txt"), Path("old_deleted.txt")],
        )

        # Only one prune action for old_deleted.txt
        self.assertEqual(len(plan.pruned), 1)
        self.assertEqual(plan.pruned[0].action_type, FileActionType.BACKUP_PRUNE)
        self.assertEqual(plan.pruned[0].src_path, orphan_host)
        self.assertEqual(plan.pruned[0].dst_path, self.backup_dir / pkg / "deleted_files" / "old_deleted.txt")

        # Execute package actions
        execute_package_actions(context=context, plan=plan)

        # Orphan removed from host
        self.assertFalse(orphan_host.exists())

        # Backup created in deleted_files
        backup_file = self.backup_dir / pkg / "deleted_files" / "old_deleted.txt"
        self.assertTrue(backup_file.is_file())
        self.assertEqual(backup_file.read_text(encoding="utf-8"), "historical orphan content")

    def test_full_copy_deployment_translates_dot_prefixes(self) -> None:
        """Verifies full copy deployment (initial deploy and full reinstall) translates dot- prefixes to leading dots."""
        pkg = "pkg_copy_dot"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config" / "app").mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        (pkg_install_dir / "dot-bashrc").write_text("export FOO=1\n", encoding="utf-8")
        (pkg_install_dir / "dot-config" / "app" / "dot-settings.ini").write_text("setting=dark\n", encoding="utf-8")
        (pkg_install_dir / "normal.txt").write_text("plain text\n", encoding="utf-8")

        # Run full deployment (no package_changes provided -> triggers run_full_copy_deployment)
        res = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Verify translated paths exist on target
        self.assertTrue((self.system_target_dir / ".bashrc").is_file())
        self.assertEqual((self.system_target_dir / ".bashrc").read_text(encoding="utf-8"), "export FOO=1\n")

        self.assertTrue((self.system_target_dir / ".config" / "app" / ".settings.ini").is_file())
        self.assertEqual(
            (self.system_target_dir / ".config" / "app" / ".settings.ini").read_text(encoding="utf-8"),
            "setting=dark\n"
        )

        self.assertTrue((self.system_target_dir / "normal.txt").is_file())
        self.assertEqual((self.system_target_dir / "normal.txt").read_text(encoding="utf-8"), "plain text\n")

        # Verify untranslated dot- paths DO NOT exist on target
        self.assertFalse((self.system_target_dir / "dot-bashrc").exists())
        self.assertFalse((self.system_target_dir / "dot-config").exists())

    def test_plan_package_install_with_sequences(self) -> None:
        """Verifies plan_package_install processes deployable and deployed Sequence inputs."""
        from drift.core.ignore import DriftIgnore

        context = PackageInstallContext(
            pkg_name="pkg_test",
            install_pkg_dir=self.install_dir / "pkg_test",
            backup_pkg_dir=self.backup_dir / "pkg_test",
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        deployable = [Path(f"file_{i}.txt") for i in [1, 2]]
        deployed = [Path(f"file_{i}.txt") for i in [1, 2, 3]]

        plan = plan_package_install(
            context=context,
            deployable_files=deployable,
            deployed_files=deployed,
        )
        self.assertEqual(len(plan.created), 2)
        # file_3.txt is orphaned but doesn't exist on disk, so no prune actions are generated
        self.assertEqual(len(plan.pruned), 0)

    def test_dry_run_zero_host_and_state_mutations(self) -> None:
        """Verifies that InstallOptions(dry_run=True) produces a complete deployment plan
        without creating target files, making backups, triggering hooks, or modifying state.toml.
        """
        pkg = "pkg_dry_run"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / "hooks").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "app.conf").write_text("config_v1", encoding="utf-8")

        # Add a pre-install hook script that would fail or create a marker file if executed
        hook_marker = self.drift_root / "hook_executed.marker"
        hook_script = pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / "hooks" / "pre_install.sh"
        hook_script.write_text(f"#!/bin/sh\ntouch {hook_marker}\n", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        pre_install = "drift_hooks/pre_install.sh"
        """, encoding="utf-8")

        # System target has an existing app.conf that would be backed up during a live install
        target_file = self.system_target_dir / "app.conf"
        target_file.write_text("host_original", encoding="utf-8")

        # Run deployment with dry_run=True
        cfg = InstallOptions(dry_run=True)
        result = run_primitive_5_install(self.workspace_config, [pkg], options=cfg)

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(len(result.packages), 1)
        pkg_res = result.packages[0]
        self.assertEqual(pkg_res.status, "SUCCESS")
        self.assertIsNotNone(pkg_res.plan)

        # Plan contains the planned overwrite and creation
        self.assertEqual(len(pkg_res.plan.overwritten_backups), 1)
        self.assertEqual(pkg_res.plan.overwritten_backups[0].src_path, target_file)
        self.assertEqual(len(pkg_res.plan.created), 1)
        self.assertEqual(pkg_res.plan.created[0].dst_path, target_file)

        # Zero mutations on host filesystem:
        # 1. Target file remains untouched with original content
        self.assertEqual(target_file.read_text(encoding="utf-8"), "host_original")
        # 2. No backup was created
        self.assertFalse((self.backup_dir / pkg).exists())
        # 3. Hook was NOT executed
        self.assertFalse(hook_marker.exists())

        # Zero mutations on state database:
        from drift.core.state_registry import load_state_registry
        state_file = self.install_dir / "state.toml"
        if state_file.exists():
            registry = load_state_registry(state_file)
            self.assertNotIn(pkg, registry.packages)

    def test_backup_path_translates_dotfiles_into_dot_prefixes(self) -> None:
        """Verifies that backup paths translate dotfile segments into 'dot-' prefixes
        for both overwritten collisions and pruned orphans.
        """
        pkg = "pkg_dot_backup"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-bashrc").write_text("package bashrc", encoding="utf-8")
        (pkg_install_dir / "dot-config" / "dot-app").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dot-config" / "dot-app" / "dot-secret").write_text("package secret", encoding="utf-8")

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")

        # 1. Existing host dotfiles that collide and will be backed up into overwritten/
        host_bashrc = self.system_target_dir / ".bashrc"
        host_bashrc.write_text("existing host bashrc", encoding="utf-8")

        host_secret = self.system_target_dir / ".config" / ".app" / ".secret"
        host_secret.parent.mkdir(parents=True, exist_ok=True)
        host_secret.write_text("existing host secret", encoding="utf-8")

        # Deploy package
        res = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res.status, "SUCCESS")

        # Assert backups in backup/<pkg>/overwritten/ use 'dot-' prefixes (not leading dots)
        backup_bashrc = self.backup_dir / pkg / "overwritten" / "dot-bashrc"
        self.assertTrue(backup_bashrc.is_file())
        self.assertEqual(backup_bashrc.read_text(encoding="utf-8"), "existing host bashrc")
        self.assertFalse((self.backup_dir / pkg / "overwritten" / ".bashrc").exists())

        backup_secret = self.backup_dir / pkg / "overwritten" / "dot-config" / "dot-app" / "dot-secret"
        self.assertTrue(backup_secret.is_file())
        self.assertEqual(backup_secret.read_text(encoding="utf-8"), "existing host secret")
        self.assertFalse((self.backup_dir / pkg / "overwritten" / ".config").exists())

        # 2. Orphan removal: simulate deleting dot-bashrc from package, then reinstalling
        (pkg_install_dir / "dot-bashrc").unlink()
        res2 = run_primitive_5_install(self.workspace_config, [pkg])
        self.assertEqual(res2.status, "SUCCESS")

        # Assert orphan backup in backup/<pkg>/deleted_files/ uses 'dot-' prefix
        backup_pruned = self.backup_dir / pkg / "deleted_files" / "dot-bashrc"
        self.assertTrue(backup_pruned.is_file())
        self.assertEqual(backup_pruned.read_text(encoding="utf-8"), "package bashrc")
        self.assertFalse((self.backup_dir / pkg / "deleted_files" / ".bashrc").exists())

    def test_cross_package_intra_batch_conflict_collects_all(self) -> None:
        """Verifies that intra-batch cross-package destination collisions collect all conflicting paths in one report."""
        # Create pkg_a, pkg_b, pkg_c
        for pkg, files in [
            ("pkg_a", ["dot-config/app/setting.json", "shared.txt"]),
            ("pkg_b", ["dot-config/app/setting.json"]),
            ("pkg_c", ["shared.txt"]),
        ]:
            pkg_dir = self.install_dir / pkg
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """, encoding="utf-8")
            for f in files:
                f_path = pkg_dir / f
                f_path.parent.mkdir(parents=True, exist_ok=True)
                f_path.write_text(f"content of {pkg} - {f}", encoding="utf-8")
            self.workspace_config.packages_enable[pkg] = True

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)

        pkg_metadata_map = {
            pkg: PackageConfig.from_install_dir(self.install_dir / pkg, self.workspace_config)
            for pkg in ["pkg_a", "pkg_b", "pkg_c"]
        }

        with self.assertRaises(CrossPackageCollisionError) as ctx:
            assert_no_cross_package_conflicts(
                workspace_config=self.workspace_config,
                discovered_packages=["pkg_a", "pkg_b", "pkg_c"],
                pkg_metadata_map=pkg_metadata_map,
                state_registry=registry,
            )

        err_msg = str(ctx.exception)
        self.assertIn("Cross-package destination conflicts detected (2 collision(s)):", err_msg)
        self.assertIn(str(self.system_target_dir / ".config/app/setting.json"), err_msg)
        self.assertIn(str(self.system_target_dir / "shared.txt"), err_msg)
        self.assertIn("pkg_a", err_msg)
        self.assertIn("pkg_b", err_msg)
        self.assertIn("pkg_c", err_msg)
        self.assertEqual(ctx.exception.packages, ["pkg_a", "pkg_b", "pkg_c"])
        self.assertIsNotNone(ctx.exception.conflicts)
        assert ctx.exception.conflicts is not None
        self.assertEqual(len(ctx.exception.conflicts), 2)

    def test_cross_package_inter_package_conflict_excludes_reinstalling_package(self) -> None:
        """Verifies that inter-package conflicts exclude packages in the current deployment batch and report external collisions."""
        # 1. Setup pkg_installed and deploy it
        pkg_inst = "pkg_installed"
        pkg_inst_dir = self.install_dir / pkg_inst
        (pkg_inst_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_inst_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_inst}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (pkg_inst_dir / "dot-app" / "app.conf").parent.mkdir(parents=True, exist_ok=True)
        (pkg_inst_dir / "dot-app" / "app.conf").write_text("installed app conf", encoding="utf-8")
        self.workspace_config.packages_enable[pkg_inst] = True

        run_primitive_5_install(self.workspace_config, [pkg_inst])

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        self.assertEqual(registry.get_package_state(pkg_inst), "installed")

        # 2. Reinstall pkg_inst alone - should NOT conflict with itself
        res = run_primitive_5_install(self.workspace_config, [pkg_inst])
        self.assertEqual(res.status, "SUCCESS")

        # 3. Setup pkg_new attempting to claim the same destination
        pkg_new = "pkg_new"
        pkg_new_dir = self.install_dir / pkg_new
        (pkg_new_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_new_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_new}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (pkg_new_dir / "dot-app" / "app.conf").parent.mkdir(parents=True, exist_ok=True)
        (pkg_new_dir / "dot-app" / "app.conf").write_text("conflicting app conf", encoding="utf-8")
        self.workspace_config.packages_enable[pkg_new] = True

        with self.assertRaises(CrossPackageCollisionError) as ctx:
            run_primitive_5_install(self.workspace_config, [pkg_new])

        err_msg = str(ctx.exception)
        self.assertIn("Cross-package destination conflicts detected (1 collision(s)):", err_msg)
        self.assertIn("Package 'pkg_new' (current batch) collides with 'pkg_installed' (already installed)", err_msg)
        self.assertEqual(ctx.exception.packages, ["pkg_installed", "pkg_new"])
        self.assertIsNotNone(ctx.exception.conflicts)
        assert ctx.exception.conflicts is not None
        self.assertEqual(len(ctx.exception.conflicts), 1)

    def test_target_directory_migration_clean_migration(self) -> None:
        """Verifies that changing target_directory cleans up old deployed files, deploys to new target, and updates state."""
        target_1 = self.system_target_dir / "target_1"
        target_2 = self.system_target_dir / "target_2"
        target_1.mkdir(parents=True, exist_ok=True)
        target_2.mkdir(parents=True, exist_ok=True)

        pkg = "pkg_mig"
        pkg_dir = self.install_dir / pkg
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{target_1}"
        """, encoding="utf-8")
        (pkg_dir / "file1.txt").write_text("content 1", encoding="utf-8")
        (pkg_dir / "sub" / "file2.txt").parent.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "sub" / "file2.txt").write_text("content 2", encoding="utf-8")
        self.workspace_config.packages_enable[pkg] = True

        # Initial deployment to target_1
        run_primitive_5_install(self.workspace_config, [pkg])
        self.assertTrue((target_1 / "file1.txt").is_file())
        self.assertTrue((target_1 / "sub" / "file2.txt").is_file())

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        self.assertEqual(registry.get_package_target_directory(pkg), target_1)
        self.assertEqual(registry.get_target_migrated_from(pkg, target_1), None)
        self.assertEqual(registry.get_target_migrated_from(pkg, target_2), target_1)

        # Update target_directory to target_2
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{target_2}"
        """, encoding="utf-8")

        # Deploy with reinstall=False (migration automatically forces full deployment to target_2)
        res_mig = run_primitive_5_install(
            self.workspace_config,
            [pkg],
            options=InstallOptions(reinstall=False)
        )
        self.assertEqual(res_mig.status, "SUCCESS")

        # Verify target_1 files were removed
        self.assertFalse((target_1 / "file1.txt").exists())
        self.assertFalse((target_1 / "sub" / "file2.txt").exists())

        # Verify target_2 files were created
        self.assertTrue((target_2 / "file1.txt").is_file())
        self.assertTrue((target_2 / "sub" / "file2.txt").is_file())

        # Verify state.toml has recorded target_2
        registry_migrated = load_state_registry(state_file)
        self.assertEqual(registry_migrated.get_package_target_directory(pkg), target_2)
        self.assertEqual(registry_migrated.get_target_migrated_from(pkg, target_2), None)

    def test_target_directory_migration_dry_run_and_planning(self) -> None:
        """Verifies plan_package_install and dry-run accurately plan migration undeployment without host mutations."""
        target_1 = self.system_target_dir / "target_mig_1"
        target_2 = self.system_target_dir / "target_mig_2"
        target_1.mkdir(parents=True, exist_ok=True)
        target_2.mkdir(parents=True, exist_ok=True)

        pkg = "pkg_mig_dry_run"
        pkg_dir = self.install_dir / pkg
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{target_1}"
        """, encoding="utf-8")
        (pkg_dir / "config.conf").write_text("test config", encoding="utf-8")
        self.workspace_config.packages_enable[pkg] = True

        # Deploy initially to target_1
        run_primitive_5_install(self.workspace_config, [pkg])
        self.assertTrue((target_1 / "config.conf").is_file())

        # Update package config to target_2
        (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{target_2}"
        """, encoding="utf-8")

        # Dry-run deployment
        res_dry = run_primitive_5_install(
            self.workspace_config,
            [pkg],
            options=InstallOptions(dry_run=True),
        )
        self.assertEqual(res_dry.status, "SUCCESS")
        plan = res_dry.packages[0].plan

        # Verify plan contains migration message and removal action
        migration_messages = [
            a for a in plan.actions
            if a.action_type == FileActionType.INFO_MESSAGE and "MIGRATE" in (a.reason or "")
        ]
        self.assertEqual(len(migration_messages), 1)
        self.assertIsNotNone(migration_messages[0].reason)
        assert migration_messages[0].reason is not None
        self.assertIn("target_mig_1", migration_messages[0].reason)
        self.assertIn("target_mig_2", migration_messages[0].reason)

        removals = [a for a in plan.actions if a.action_type == FileActionType.DELETE_ITEM]
        self.assertEqual(len(removals), 1)
        self.assertEqual(removals[0].dst_path, target_1 / "config.conf")

        creations = [a for a in plan.actions if a.action_type == FileActionType.CREATE_COPY]
        self.assertEqual(len(creations), 1)
        self.assertEqual(creations[0].dst_path, target_2 / "config.conf")

        # Verify ZERO mutations in dry-run mode
        self.assertTrue((target_1 / "config.conf").is_file(), "Old target file must not be deleted in dry-run")
        self.assertFalse((target_2 / "config.conf").exists(), "New target file must not be created in dry-run")

    def test_state_registry_sync_deployed_files(self) -> None:
        """Verifies StateRegistry.sync_deployed_files updates target directory, install method, and deployed files."""
        registry = StateRegistry({
            "test_pkg": PackageState(
                state="installed",
                deployed_files=[Path("a.txt"), Path("b.txt")]
            )
        })

        # Synchronize with target_directory, install_method, and deployable_files
        registry.sync_deployed_files(
            "test_pkg",
            target_directory=Path("/home/user/target"),
            install_method=InstallMethod.SYMLINK,
            deployable_files=[Path("c.txt"), Path("d.txt")]
        )
        self.assertEqual(
            registry.get_package_target_directory("test_pkg"),
            Path("/home/user/target")
        )
        self.assertEqual(
            registry.get_package_install_method("test_pkg"),
            InstallMethod.SYMLINK
        )
        self.assertEqual(
            registry.get_package_deployed_files("test_pkg"),
            [Path("c.txt"), Path("d.txt")]
        )

        # Update sync with new deployable_files
        registry.sync_deployed_files(
            "test_pkg",
            target_directory=Path("/home/user/target_updated"),
            install_method=InstallMethod.COPY,
            deployable_files=[Path("d.txt"), Path("e.txt")]
        )
        self.assertEqual(
            registry.get_package_target_directory("test_pkg"),
            Path("/home/user/target_updated")
        )
        self.assertEqual(
            registry.get_package_install_method("test_pkg"),
            InstallMethod.COPY
        )
        self.assertEqual(
            registry.get_package_deployed_files("test_pkg"),
            [Path("d.txt"), Path("e.txt")]
        )

        # Test KeyError on unregistered package
        with self.assertRaises(KeyError):
            registry.sync_deployed_files("non_existent_pkg", target_directory=Path("/any"), install_method=InstallMethod.COPY)

    def test_state_registry_get_target_migrated_from(self) -> None:
        """Verifies StateRegistry.get_target_migrated_from detection under various state configurations."""
        registry = StateRegistry({
            "pkg_with_target": PackageState(
                state="installed",
                target_directory=Path("/home/user/.config")
            ),
            "pkg_no_target": PackageState(
                state="installed",
                target_directory=None
            ),
        })

        # Missing package returns None
        self.assertIsNone(registry.get_target_migrated_from("unknown", Path("/any")))

        # Package without recorded target_directory returns None
        self.assertIsNone(registry.get_target_migrated_from("pkg_no_target", Path("/any")))

        # Same target directory returns None
        self.assertIsNone(registry.get_target_migrated_from("pkg_with_target", Path("/home/user/.config")))

        # Different target directory returns the old target directory
        self.assertEqual(
            registry.get_target_migrated_from("pkg_with_target", Path("/home/user/.dotfiles")),
            Path("/home/user/.config")
        )

    def test_state_registry_build_destination_ownership_map(self) -> None:
        """Verifies StateRegistry.build_destination_ownership_map with and without exclusions."""
        target_a = Path("/target_a")
        target_b = Path("/target_b")
        registry = StateRegistry({
            "pkg_a": PackageState(
                state="installed",
                target_directory=target_a,
                deployed_files=[Path("dot-config/app.conf"), Path("readme.txt")]
            ),
            "pkg_b": PackageState(
                state="installed",
                target_directory=target_b,
                deployed_files=[Path("main.py")]
            ),
            "pkg_staging": PackageState(
                state="staging",
                target_directory=target_a,
                deployed_files=[Path("ignored.txt")]
            ),
        })

        # Full ownership map
        ownership = registry.build_destination_ownership_map()
        self.assertEqual(ownership[target_a / ".config" / "app.conf"], "pkg_a")
        self.assertEqual(ownership[target_a / "readme.txt"], "pkg_a")
        self.assertEqual(ownership[target_b / "main.py"], "pkg_b")
        self.assertNotIn(target_a / "ignored.txt", ownership)

        # Ownership map excluding pkg_a
        ownership_ex_a = registry.build_destination_ownership_map(exclude_packages=["pkg_a"])
        self.assertNotIn(target_a / ".config" / "app.conf", ownership_ex_a)
        self.assertNotIn(target_a / "readme.txt", ownership_ex_a)
        self.assertEqual(ownership_ex_a[target_b / "main.py"], "pkg_b")
        self.assertEqual(registry.get_file_owner(target_b / "main.py"), "pkg_b")

    def test_prepare_and_execute_install_pipeline(self) -> None:
        """Verifies prepare_install returns InstallPlan and execute_install executes it."""
        pkg = "pkg_copy"
        install_pkg_dir = self.install_dir / pkg
        dot_drift = install_pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (install_pkg_dir / "app.conf").write_text("setting = 1\n", encoding="utf-8")

        # 1. Prepare phase
        plan = prepare_install(self.workspace_config, [pkg])
        self.assertIsInstance(plan, InstallPlan)
        self.assertEqual(plan.packages_install_order, [pkg])
        self.assertIn(pkg, plan.pkg_metadata_map)
        self.assertEqual(plan.pkg_metadata_map[pkg].name, pkg)
        self.assertIsInstance(plan.state_registry, StateRegistry)
        self.assertIsInstance(plan.options, InstallOptions)
        self.assertIn(pkg, plan.package_plans)
        self.assertIn(pkg, plan.contexts)

        # Host system not yet modified
        self.assertFalse((self.system_target_dir / "app.conf").exists())

        # 2. Execute phase
        result = execute_install(self.workspace_config, plan=plan)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(len(result.packages), 1)
        self.assertEqual(result.packages[0].package, pkg)
        self.assertEqual(result.packages[0].status, "SUCCESS")

        # Host system has file deployed
        self.assertTrue((self.system_target_dir / "app.conf").is_file())
        self.assertEqual((self.system_target_dir / "app.conf").read_text(encoding="utf-8"), "setting = 1\n")

        # State registry recorded as installed
        registry = load_state_registry(self.install_dir / "state.toml")
        self.assertEqual(registry.get_package_state(pkg), "installed")

    def test_execute_package_install_impl_direct(self) -> None:
        """Verifies execute_package_install_impl transitions state, executes actions, and updates registry."""
        from drift.hooks.lifecycle_hooks import HookExecFlags
        pkg = "pkg_direct_impl"
        install_pkg_dir = self.install_dir / pkg
        dot_drift = install_pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        """, encoding="utf-8")
        (install_pkg_dir / "test.conf").write_text("test data\n", encoding="utf-8")

        meta = PackageConfig.from_install_dir(install_pkg_dir, self.workspace_config)
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)

        context = PackageInstallContext.from_package(
            workspace_config=self.workspace_config,
            state_registry=registry,
            metadata=meta,
        )
        plan = plan_package_install(
            context=context,
            deployable_files=[Path("test.conf")],
        )

        with context.package_envs():
            res = execute_package_install_impl(
                context=context,
                plan=plan,
                state_registry=registry,
                hook_flags=HookExecFlags(),
                options=InstallOptions(),
            )
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue((self.system_target_dir / "test.conf").is_file())
        self.assertEqual((self.system_target_dir / "test.conf").read_text(encoding="utf-8"), "test data\n")

        reloaded = load_state_registry(state_file)
        self.assertEqual(reloaded.get_package_state(pkg), "installed")
        self.assertIn("test.conf", [str(p) for p in reloaded.get_package_deployed_files(pkg)])

    def test_prepare_install_preflight_guard_failure(self) -> None:
        """Verifies prepare_install runs pre-flight checks and aborts before touching host system."""
        pkg = "pkg_copy"
        install_pkg_dir = self.install_dir / pkg
        dot_drift = install_pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"

        [hooks]
        pre_install = "drift_hooks/non_existent_hook.sh"
        """, encoding="utf-8")
        (install_pkg_dir / "app.conf").write_text("setting = 1\n", encoding="utf-8")

        with self.assertRaises(HookMissingError) as ctx:
            prepare_install(self.workspace_config, [pkg])

        self.assertEqual(ctx.exception.packages, [pkg])
        # Host system was never touched
        self.assertFalse((self.system_target_dir / "app.conf").exists())
        # State was never set to installing
        registry = load_state_registry(self.install_dir / "state.toml")
        self.assertIsNone(registry.get_package_state(pkg))

    def test_plan_and_execute_update_permission_action(self) -> None:
        """Verifies plan_package_install plans UPDATE_PERMISSION when file content matches but permissions differ,
        and execute_package_actions updates host file permissions."""
        from drift.core.ignore import DriftIgnore

        pkg = "pkg_perm_test"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        src_file = pkg_install_dir / "script.sh"
        src_file.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
        src_file.chmod(0o755)

        system_target = self.system_target_dir / "script.sh"
        self.system_target_dir.mkdir(parents=True, exist_ok=True)
        system_target.write_text("#!/bin/sh\necho hello\n", encoding="utf-8")
        system_target.chmod(0o644)

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=False,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(context=context, deployable_files=[Path("script.sh")])
        self.assertEqual(len(plan.permissions_updated), 1)
        self.assertEqual(len(plan.created), 0)
        self.assertEqual(len(plan.updated), 0)
        self.assertEqual(len(plan.skipped), 0)

        action = plan.permissions_updated[0]
        self.assertEqual(action.action_type, FileActionType.UPDATE_PERMISSION)
        self.assertIn("Permissions differ", action.reason or "")

        formatted_line = format_action_line(action)
        self.assertIn("[UPDATE_PERMISSION]", formatted_line)

        summary = format_action_summary(plan.actions)
        self.assertIn("1 permissions to update", summary)

        # Execute actions
        execute_package_actions(context, plan)

        # Verify host permissions updated
        self.assertEqual(system_target.stat().st_mode & 0o777, 0o755)
        self.assertEqual(system_target.read_text(encoding="utf-8"), "#!/bin/sh\necho hello\n")

        # Second plan should now be SKIP_IDENTICAL
        plan2 = plan_package_install(context=context, deployable_files=[Path("script.sh")])
        self.assertEqual(len(plan2.permissions_updated), 0)
        self.assertEqual(len(plan2.skipped), 1)
        self.assertEqual(plan2.skipped[0].action_type, FileActionType.SKIP_IDENTICAL)

    def test_plan_folder_delivery_updates_directory_permissions(self) -> None:
        """Verifies plan_folder_delivery generates UPDATE_PERMISSION when directory permissions differ,
        across forward install, reverse sync, and leaf directory deliveries."""
        pkg = "pkg_dir_perm"
        pkg_install_dir = self.install_dir / pkg
        src_dir = pkg_install_dir / "dot-config" / "sub"
        src_dir.mkdir(parents=True, exist_ok=True)
        src_file = src_dir / "app.conf"
        src_file.write_text("hello\n", encoding="utf-8")
        src_dir.chmod(0o700)

        host_dir = self.system_target_dir / ".config" / "sub"
        host_dir.mkdir(parents=True, exist_ok=True)
        host_file = host_dir / "app.conf"
        host_file.write_text("hello\n", encoding="utf-8")
        host_dir.chmod(0o755)

        delivery_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.workspace_config.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=self.backup_dir / pkg,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=False,
        )

        # 1. Forward installation: ancestor directory permissions differ (host 0o755 -> repo 0o700)
        actions = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("dot-config/sub/app.conf")],
            deployed_files=(),
        )

        dir_perm_actions = [
            a for a in actions
            if a.action_type == FileActionType.UPDATE_PERMISSION and a.dst_path == host_dir
        ]
        self.assertEqual(len(dir_perm_actions), 1)
        self.assertEqual(dir_perm_actions[0].src_path, src_dir)
        self.assertIn("Permissions differ", dir_perm_actions[0].reason or "")

        exec_ctx = FileActionExecutionContext(sudo=False, resolve_symlinks=True)
        execute_delivery_actions(exec_ctx, actions)
        self.assertEqual(host_dir.stat().st_mode & 0o777, 0o700)

        # 2. Reverse sync: host directory permissions differ (repo 0o755 -> host 0o700)
        host_dir.chmod(0o700)
        src_dir.chmod(0o755)

        rev_ctx = DeliveryInspectionContext(
            target_dir=pkg_install_dir,
            source_dir=self.system_target_dir,
            drift_root=self.workspace_config.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
            reverse_mode=True,
        )

        rev_actions = plan_folder_delivery(
            context=rev_ctx,
            deployable_files=[Path(".config/sub/app.conf")],
            deployed_files=[Path("dot-config/sub/app.conf")],
        )

        rev_dir_perm_actions = [
            a for a in rev_actions
            if a.action_type == FileActionType.UPDATE_PERMISSION and a.dst_path == src_dir
        ]
        self.assertEqual(len(rev_dir_perm_actions), 1)
        self.assertEqual(rev_dir_perm_actions[0].src_path, host_dir)

        execute_delivery_actions(exec_ctx, rev_actions)
        self.assertEqual(src_dir.stat().st_mode & 0o777, 0o700)

        # 3. Multi-level ancestor directory permissions differ
        deep_src = pkg_install_dir / "dot-config" / "level1" / "level2"
        deep_src.mkdir(parents=True, exist_ok=True)
        deep_src_file = deep_src / "deep.conf"
        deep_src_file.write_text("deep\n", encoding="utf-8")
        (pkg_install_dir / "dot-config" / "level1").chmod(0o750)
        deep_src.chmod(0o700)

        deep_host = self.system_target_dir / ".config" / "level1" / "level2"
        deep_host.mkdir(parents=True, exist_ok=True)
        deep_host_file = deep_host / "deep.conf"
        deep_host_file.write_text("deep\n", encoding="utf-8")
        (self.system_target_dir / ".config" / "level1").chmod(0o755)
        deep_host.chmod(0o755)

        deep_actions = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("dot-config/level1/level2/deep.conf")],
            deployed_files=(),
        )
        deep_perm_actions = [
            a for a in deep_actions
            if a.action_type == FileActionType.UPDATE_PERMISSION
        ]
        self.assertEqual(len(deep_perm_actions), 2)
        execute_delivery_actions(exec_ctx, deep_actions)
        self.assertEqual((self.system_target_dir / ".config" / "level1").stat().st_mode & 0o777, 0o750)
        self.assertEqual(deep_host.stat().st_mode & 0o777, 0o700)

    def test_plan_folder_delivery_leaf_empty_directory_handling(self) -> None:
        """Verifies plan_folder_delivery properly handles leaf directories and empty directories across all states:
        creation (ENSURE_DIR), permission sync (UPDATE_PERMISSION), and file collision replacement (BACKUP + ENSURE_DIR)."""
        pkg = "pkg_empty_dir_leaf"
        pkg_install_dir = self.install_dir / pkg
        empty_dir = pkg_install_dir / "dot-config" / "empty_sub"
        empty_dir.mkdir(parents=True, exist_ok=True)
        empty_dir.chmod(0o700)

        delivery_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.SYMLINK,
            is_first_time=False,
            backup_pkg_dir=self.backup_dir / pkg,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=False,
        )

        # 1. Target directory does not exist -> plans ENSURE_DIR for ancestor and leaf
        actions = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("dot-config/empty_sub")],
            deployed_files=(),
        )
        ensure_actions = [a for a in actions if a.action_type == FileActionType.ENSURE_DIR]
        self.assertEqual(len(ensure_actions), 2)
        self.assertEqual(ensure_actions[0].dst_path, self.system_target_dir / ".config")
        self.assertEqual(ensure_actions[1].dst_path, self.system_target_dir / ".config" / "empty_sub")

        # Execute actions to establish state on system target
        exec_ctx = FileActionExecutionContext(sudo=False, resolve_symlinks=True)
        execute_delivery_actions(exec_ctx, actions)
        target_sub = self.system_target_dir / ".config" / "empty_sub"
        self.assertTrue(target_sub.is_dir())

        # 2. Target directory exists with differing permissions -> plans UPDATE_PERMISSION
        target_sub.chmod(0o755)
        actions_perm = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("dot-config/empty_sub")],
            deployed_files=(),
        )
        perm_actions = [a for a in actions_perm if a.action_type == FileActionType.UPDATE_PERMISSION]
        self.assertEqual(len(perm_actions), 1)
        self.assertEqual(perm_actions[0].dst_path, target_sub)
        self.assertEqual(perm_actions[0].src_path, empty_dir)

        # 3. Target path is blocked by a physical file -> plans BACKUP_OVERWRITE + ENSURE_DIR
        target_sub.rmdir()
        target_sub.write_text("file blocking dir\n", encoding="utf-8")
        actions_collision = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("dot-config/empty_sub")],
            deployed_files=(),
        )
        self.assertEqual(len(actions_collision), 2)
        self.assertEqual(actions_collision[0].action_type, FileActionType.BACKUP_OVERWRITE)
        self.assertEqual(actions_collision[0].src_path, target_sub)
        self.assertEqual(actions_collision[0].reason, "File blocking directory")
        self.assertEqual(actions_collision[1].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(actions_collision[1].dst_path, target_sub)

    def test_delivery_inspection_context_derivation_and_planning(self) -> None:
        """Verifies DeliveryInspectionContext properties, backup routing, and integration with plan_folder_delivery."""
        pkg = "pkg_context_test"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "app.conf").write_text("install app conf", encoding="utf-8")

        context = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        delivery_ctx = context.delivery_context
        self.assertIsInstance(delivery_ctx, DeliveryInspectionContext)
        self.assertEqual(delivery_ctx.target_dir, self.system_target_dir)
        self.assertEqual(delivery_ctx.source_dir, pkg_install_dir)
        self.assertEqual(delivery_ctx.install_method, InstallMethod.SYMLINK)
        self.assertTrue(delivery_ctx.is_first_time)
        self.assertEqual(delivery_ctx.abs_drift_root, self.workspace_config.drift_root.resolve())

        # Test resolve_backup_path (encodes hidden dot to dot- prefix in backup store)
        backup_path = delivery_ctx.resolve_backup_path(Path(".app.conf"))
        self.assertEqual(backup_path, self.backup_dir / pkg / "overwritten" / "dot-app.conf")
        deleted_backup_path = delivery_ctx.resolve_backup_path(Path(".app.conf"), subfolder=BackupSubfolder.DELETED_FILES)
        self.assertEqual(deleted_backup_path, self.backup_dir / pkg / "deleted_files" / "dot-app.conf")

        # Test path translations in normal mode
        self.assertEqual(delivery_ctx.translate_target_rel_path(Path("dot-config/app.conf")), Path(".config/app.conf"))
        self.assertEqual(delivery_ctx.translate_source_rel_path(Path(".config/app.conf")), Path("dot-config/app.conf"))
        self.assertEqual(delivery_ctx.translate_target_path(Path("dot-config/app.conf")), self.system_target_dir / ".config/app.conf")
        self.assertEqual(delivery_ctx.translate_source_path(Path("dot-config/app.conf")), pkg_install_dir / "dot-config/app.conf")
        src_file, dst_file = delivery_ctx.translate_path(Path("dot-config/app.conf"))
        self.assertEqual(src_file, pkg_install_dir / "dot-config/app.conf")
        self.assertEqual(dst_file, self.system_target_dir / ".config/app.conf")

        # Test path translations in reverse mode
        rev_ctx = DeliveryInspectionContext(
            target_dir=pkg_install_dir,
            source_dir=self.system_target_dir,
            drift_root=Path("/nonexistent"),
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=True,
        )
        self.assertEqual(rev_ctx.translate_target_rel_path(Path(".config/app.conf")), Path("dot-config/app.conf"))
        self.assertEqual(rev_ctx.translate_source_rel_path(Path("dot-config/app.conf")), Path(".config/app.conf"))
        self.assertEqual(rev_ctx.translate_target_path(Path(".config/app.conf")), pkg_install_dir / "dot-config/app.conf")
        self.assertEqual(rev_ctx.translate_source_path(Path(".config/app.conf")), self.system_target_dir / ".config/app.conf")
        rev_src_file, rev_dst_file = rev_ctx.translate_path(Path(".config/app.conf"))
        self.assertEqual(rev_src_file, self.system_target_dir / ".config/app.conf")
        self.assertEqual(rev_dst_file, pkg_install_dir / "dot-config/app.conf")

        # Test plan_folder_delivery directly with DeliveryInspectionContext
        actions = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("app.conf")],
            deployed_files=(),
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, FileActionType.CREATE_SYMLINK)
        self.assertEqual(actions[0].src_path, pkg_install_dir / "app.conf")
        self.assertEqual(actions[0].dst_path, self.system_target_dir / "app.conf")

    def test_delivery_inspection_reverse_mode_symlink_semantics(self) -> None:
        """Verifies reverse_mode symlink handling: skip identical drift links, delete broken links, handle dir links, compare external file links."""
        pkg = "pkg_rev_symlinks"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        repo_file = pkg_install_dir / "dot-bashrc"
        repo_file.write_text("export FOO=1\n", encoding="utf-8")

        rev_ctx = DeliveryInspectionContext(
            target_dir=pkg_install_dir,
            source_dir=self.system_target_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
            reverse_mode=True,
        )

        # 1. Host symlink pointing to repo target -> SKIP_IDENTICAL
        host_link = self.system_target_dir / ".bashrc"
        host_link.symlink_to(repo_file)
        actions = plan_folder_delivery(
            context=rev_ctx,
            deployable_files=[Path(".bashrc")],
            deployed_files=[Path(".bashrc")],
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, FileActionType.SKIP_IDENTICAL)
        self.assertEqual(actions[0].reason, "Source and target are identical")
        host_link.unlink()

        # 2. Host broken symlink -> DELETE_ITEM in repo
        host_link.symlink_to(self.system_target_dir / "nonexistent_target")
        actions_broken = plan_folder_delivery(
            context=rev_ctx,
            deployable_files=[Path(".bashrc")],
            deployed_files=[Path(".bashrc")],
        )
        self.assertEqual(len(actions_broken), 1)
        self.assertEqual(actions_broken[0].action_type, FileActionType.DELETE_ITEM)
        self.assertEqual(actions_broken[0].dst_path, repo_file)
        self.assertEqual(actions_broken[0].reason, "Broken symlink on host")
        host_link.unlink()

        # 3. Host symlink pointing to a directory -> resolves colliding repo file and ensures directory
        some_dir = self.system_target_dir / "some_dir"
        some_dir.mkdir(parents=True, exist_ok=True)
        host_link.symlink_to(some_dir)
        actions_dir_link = plan_folder_delivery(
            context=rev_ctx,
            deployable_files=[Path(".bashrc")],
            deployed_files=[Path(".bashrc")],
        )
        self.assertEqual(len(actions_dir_link), 2)
        self.assertEqual(actions_dir_link[0].action_type, FileActionType.DELETE_ITEM)
        self.assertEqual(actions_dir_link[0].dst_path, repo_file)
        self.assertEqual(actions_dir_link[0].reason, "File blocking directory")
        self.assertEqual(actions_dir_link[1].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(actions_dir_link[1].dst_path, repo_file)
        host_link.unlink()

        # 4. Host symlink pointing to an external file -> compares content
        ext_file = self.system_target_dir / "external.conf"
        ext_file.write_text("export FOO=2\n", encoding="utf-8")
        host_link.symlink_to(ext_file)
        actions_ext = plan_folder_delivery(
            context=rev_ctx,
            deployable_files=[Path(".bashrc")],
            deployed_files=[Path(".bashrc")],
        )
        self.assertEqual(len(actions_ext), 1)
        self.assertEqual(actions_ext[0].action_type, FileActionType.UPDATE_COPY)
        self.assertEqual(actions_ext[0].src_path, ext_file)
        self.assertEqual(actions_ext[0].dst_path, repo_file)
        host_link.unlink()

        # 5. Normal mode: Deployable symlink in repo pointing to a directory -> ENSURE_DIR on host
        normal_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=False,
        )
        repo_dir_link = pkg_install_dir / "link_to_dir"
        repo_dir_link.symlink_to(some_dir)
        actions_norm = plan_folder_delivery(
            context=normal_ctx,
            deployable_files=[Path("link_to_dir")],
            deployed_files=(),
        )
        self.assertEqual(len(actions_norm), 1)
        self.assertEqual(actions_norm[0].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(actions_norm[0].dst_path, self.system_target_dir / "link_to_dir")

    def test_concrete_directory_blocking_leaf_file_plans_delete_tree_when_no_backup(self) -> None:
        """Verifies that a pre-existing concrete directory blocking a leaf file plans DELETE_TREE when backup is None and removes the tree upon execution."""
        pkg = "pkg_dir_collision"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "blocked_file").write_text("file content replacing directory\n", encoding="utf-8")

        # Create concrete directory on system target with nested content
        colliding_dir = self.system_target_dir / "blocked_file"
        colliding_dir.mkdir(parents=True, exist_ok=True)
        (colliding_dir / "nested_a.txt").write_text("nested a\n", encoding="utf-8")
        (colliding_dir / "sub").mkdir(parents=True, exist_ok=True)
        (colliding_dir / "sub" / "nested_b.txt").write_text("nested b\n", encoding="utf-8")

        # 1. Delivery inspection without backup (backup_pkg_dir=None): plans DELETE_TREE
        delivery_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.workspace_config.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=True,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
        )

        actions = plan_folder_delivery(
            context=delivery_ctx,
            deployable_files=[Path("blocked_file")],
        )

        # Actions should contain DELETE_TREE followed by CREATE_COPY
        delete_action = next((a for a in actions if a.action_type == FileActionType.DELETE_TREE), None)
        self.assertIsNotNone(delete_action)
        assert delete_action is not None
        self.assertEqual(delete_action.dst_path, colliding_dir)
        self.assertEqual(delete_action.reason, "Directory blocking file")

        create_action = next((a for a in actions if a.action_type == FileActionType.CREATE_COPY), None)
        self.assertIsNotNone(create_action)
        assert create_action is not None
        self.assertEqual(create_action.dst_path, colliding_dir)

        # Execute actions: colliding directory must be deleted as a tree, and leaf file created
        execute_delivery_actions(FileActionExecutionContext(), actions)
        self.assertTrue(colliding_dir.is_file())
        self.assertFalse(colliding_dir.is_dir())
        self.assertEqual(colliding_dir.read_text(encoding="utf-8"), "file content replacing directory\n")

    def test_concrete_directory_blocking_leaf_file_plans_backup_overwrite_when_backup_configured(self) -> None:
        """Verifies that a pre-existing concrete directory blocking a leaf file plans BACKUP_OVERWRITE when backup is configured."""
        pkg = "pkg_dir_backup_collision"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "blocked_file").write_text("file content replacing directory\n", encoding="utf-8")

        colliding_dir = self.system_target_dir / "blocked_file"
        colliding_dir.mkdir(parents=True, exist_ok=True)
        (colliding_dir / "nested.txt").write_text("saved content\n", encoding="utf-8")

        context_with_backup = PackageInstallContext(
            pkg_name=pkg,
            install_pkg_dir=pkg_install_dir,
            backup_pkg_dir=self.backup_dir / pkg,
            target_dir=self.system_target_dir,
            install_method=InstallMethod.COPY,
            ignore_handler=DriftIgnore(),
            sudo=False,
            is_first_time=True,
            drift_root=self.workspace_config.drift_root,
        )

        plan = plan_package_install(
            context=context_with_backup,
            deployable_files=[Path("blocked_file")],
        )

        backup_action = next((a for a in plan.actions if a.action_type == FileActionType.BACKUP_OVERWRITE), None)
        self.assertIsNotNone(backup_action)
        assert backup_action is not None
        self.assertEqual(backup_action.src_path, colliding_dir)
        self.assertEqual(backup_action.dst_path, self.backup_dir / pkg / BackupSubfolder.OVERWRITTEN.value / "blocked_file")
        self.assertEqual(backup_action.reason, "Directory blocking file")

        execute_package_actions(context_with_backup, plan)
        self.assertTrue(colliding_dir.is_file())
        self.assertEqual(colliding_dir.read_text(encoding="utf-8"), "file content replacing directory\n")
        # Verify backup directory tree was preserved
        backed_up_tree = self.backup_dir / pkg / BackupSubfolder.OVERWRITTEN.value / "blocked_file"
        self.assertTrue(backed_up_tree.is_dir())
        self.assertEqual((backed_up_tree / "nested.txt").read_text(encoding="utf-8"), "saved content\n")

    def test_install_defers_sudo_check_when_dry_run_or_no_changes(self) -> None:
        """Verifies that assert_can_escalate is deferred to execution and NOT called during planning, dry-run, or when a sudo package has no changes."""
        from unittest.mock import patch

        # 1. Setup pkg_sudo with sudo = true
        pkg_dir = self.install_dir / "pkg_sudo"
        dot_drift = pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text("""
        [package]
        name = "pkg_sudo"
        enable_install = true
        sudo = true
        """, encoding="utf-8")
        (pkg_dir / "sudo_file.txt").write_text("initial sudo content", encoding="utf-8")
        self.workspace_config.packages_enable["pkg_sudo"] = True

        # 2. Planning (prepare_install): assert_can_escalate must NOT be called even though sudo=True
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo:
            plan = prepare_install(self.workspace_config, ["pkg_sudo"])
            self.assertEqual(len(plan.packages_install_order), 1)
            self.assertEqual(plan.skipped_packages, [])
            mock_sudo.assert_not_called()

        # 3. Dry-run execution (execute_install with dry_run=True): assert_can_escalate must NOT be called
        plan_dry_run = prepare_install(
            self.workspace_config,
            ["pkg_sudo"],
            options=InstallOptions(dry_run=True),
        )
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo:
            res_dry_run = execute_install(self.workspace_config, plan_dry_run)
            self.assertTrue(res_dry_run.dry_run)
            mock_sudo.assert_not_called()

        # 4. Live execution with changes (first-time install): assert_can_escalate MUST be called
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo, \
             patch("drift.primitives.install_repo.execute_package_actions"):
            res_live = execute_install(self.workspace_config, plan)
            self.assertEqual(res_live.status, "SUCCESS")
            mock_sudo.assert_called_once()

        # 5. Second live execution with ZERO changes (reinstall=False): assert_can_escalate must NOT be called
        # Simulate that sudo_file.txt is now deployed on host matching install/
        target_file = self.system_target_dir / "sudo_file.txt"
        target_file.symlink_to(self.install_dir / "pkg_sudo" / "sudo_file.txt")

        plan2 = prepare_install(self.workspace_config, ["pkg_sudo"])
        self.assertEqual(plan2.packages_install_order, [])
        self.assertEqual(plan2.skipped_packages, ["pkg_sudo"])
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo, \
             patch("drift.primitives.install_repo.execute_package_actions"):
            res2 = execute_install(self.workspace_config, plan2)
            self.assertEqual(res2.packages[0].status, "SKIPPED")
            mock_sudo.assert_not_called()

        # 6. install_one_package: verify dry_run skips sudo, while live with reinstall calls it
        metadata = PackageConfig.from_install_dir(pkg_dir, self.workspace_config)
        state_registry = load_state_registry(self.install_dir / "state.toml")

        # install_one_package with dry_run=True: assert_can_escalate must NOT be called
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo:
            res_one_dry = install_one_package(
                self.workspace_config,
                state_registry,
                metadata,
                options=InstallOptions(dry_run=True),
            )
            mock_sudo.assert_not_called()

        # install_one_package with no changes: assert_can_escalate must NOT be called
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo:
            res_one_skip = install_one_package(
                self.workspace_config,
                state_registry,
                metadata,
                options=InstallOptions(reinstall=False),
            )
            self.assertEqual(res_one_skip.status, "SKIPPED")
            mock_sudo.assert_not_called()

        # install_one_package with reinstall=True: assert_can_escalate MUST be called
        with patch("drift.primitives.install_repo.assert_can_escalate") as mock_sudo, \
             patch("drift.primitives.install_repo.execute_package_actions"):
            res_one_live = install_one_package(
                self.workspace_config,
                state_registry,
                metadata,
                options=InstallOptions(reinstall=True),
            )
            mock_sudo.assert_called_once()

    def test_prepare_install_excludes_skipped_packages(self) -> None:
        """Verifies that prepare_install excludes packages without mutations from packages_install_order."""
        # 1. Setup pkg_a and pkg_b
        pkg_a_dir = self.install_dir / "pkg_a"
        dot_drift_a = pkg_a_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift_a.mkdir(parents=True, exist_ok=True)
        (dot_drift_a / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        enable_install = true
        """, encoding="utf-8")
        (pkg_a_dir / "file_a.txt").write_text("content a\n", encoding="utf-8")

        pkg_b_dir = self.install_dir / "pkg_b"
        dot_drift_b = pkg_b_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift_b.mkdir(parents=True, exist_ok=True)
        (dot_drift_b / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_b"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        enable_install = true
        """, encoding="utf-8")
        (pkg_b_dir / "file_b.txt").write_text("content b\n", encoding="utf-8")

        self.workspace_config.packages_enable["pkg_a"] = True
        self.workspace_config.packages_enable["pkg_b"] = True

        # Deploy pkg_a for the first time
        plan_a = prepare_install(self.workspace_config, ["pkg_a"])
        self.assertEqual(plan_a.packages_install_order, ["pkg_a"])
        self.assertEqual(plan_a.skipped_packages, [])
        res_a = execute_install(self.workspace_config, plan_a)
        self.assertEqual(res_a.status, "SUCCESS")

        # 2. Plan both packages: pkg_a has no changes on host (already deployed), pkg_b is new
        plan_both = prepare_install(self.workspace_config, ["pkg_a", "pkg_b"])
        self.assertTrue(plan_both.package_plans["pkg_a"].can_skip)
        self.assertFalse(plan_both.package_plans["pkg_a"].has_mutations)
        self.assertFalse(plan_both.package_plans["pkg_b"].can_skip)
        self.assertTrue(plan_both.package_plans["pkg_b"].has_mutations)
        self.assertEqual(plan_both.packages_install_order, ["pkg_b"])
        self.assertEqual(plan_both.skipped_packages, ["pkg_a"])

        # 3. Execution: should run pkg_b and report pkg_a as SKIPPED appended at last
        res_both = execute_install(self.workspace_config, plan_both)
        self.assertEqual(res_both.status, "SUCCESS")
        self.assertEqual(len(res_both.packages), 2)
        # Packages in packages_install_order execute first, skipped packages appended at last
        self.assertEqual([p.package for p in res_both.packages], ["pkg_b", "pkg_a"])
        self.assertEqual(res_both.packages[0].status, "SUCCESS")
        self.assertEqual(res_both.packages[1].status, "SKIPPED")

        # 4. With reinstall=True: pkg_a should NOT be skipped, and should plan real reinstall mutations
        plan_reinstall = prepare_install(
            self.workspace_config,
            ["pkg_a", "pkg_b"],
            options=InstallOptions(reinstall=True),
        )
        self.assertEqual(sorted(plan_reinstall.packages_install_order), ["pkg_a", "pkg_b"])
        self.assertEqual(plan_reinstall.skipped_packages, [])
        self.assertFalse(plan_reinstall.package_plans["pkg_a"].can_skip)
        self.assertTrue(plan_reinstall.package_plans["pkg_a"].has_mutations)
        self.assertEqual(len(plan_reinstall.package_plans["pkg_a"].skipped), 0)
        self.assertTrue(any(a.action_type == FileActionType.UPDATE_COPY for a in plan_reinstall.package_plans["pkg_a"].actions))

    def test_package_install_context_reinstall_field_and_propagation(self) -> None:
        """Verifies that PackageInstallContext encapsulates reinstall flag and propagates it to delivery_context."""
        pkg_dir = self.install_dir / "pkg_reinstall"
        dot_drift = pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_reinstall"
        install_method = "copy"
        target_directory = "{self.system_target_dir}"
        enable_install = true
        """, encoding="utf-8")
        (pkg_dir / "file.txt").write_text("reinstall content\\n", encoding="utf-8")

        self.workspace_config.packages_enable["pkg_reinstall"] = True
        state_registry = StateRegistry(state_file=self.drift_root / "state.toml")
        metadata = PackageConfig.from_install_dir(pkg_dir, self.workspace_config)

        # 1. Default reinstall is False
        ctx_default = PackageInstallContext.from_package(
            workspace_config=self.workspace_config,
            state_registry=state_registry,
            metadata=metadata,
        )
        self.assertFalse(ctx_default.reinstall)
        self.assertFalse(ctx_default.delivery_context.reinstall)

        # 2. Explicit reinstall=True
        ctx_reinstall = PackageInstallContext.from_package(
            workspace_config=self.workspace_config,
            state_registry=state_registry,
            metadata=metadata,
            reinstall=True,
        )
        self.assertTrue(ctx_reinstall.reinstall)
        self.assertTrue(ctx_reinstall.delivery_context.reinstall)


class TestInstallDependencies(unittest.TestCase):
    """Tests for package dependency ordering, validation, and DAG resolution during install."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name).resolve()
        self.drift_root = temp_root / "drift_workspace"
        self.system_target_dir = temp_root / "system_home"

        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir
            ),
            packages_enable={},
            packages_enable_default=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_install_package(
        self,
        pkg_name: str,
        dependencies: Optional[list] = None,
        target_dir: Optional[Path] = None,
    ) -> None:
        pkg_dir = self.install_dir / pkg_name
        dot_drift = pkg_dir / DRIFT_INTERNAL_DIR_NAME
        dot_drift.mkdir(parents=True, exist_ok=True)
        target_directory = target_dir if target_dir is not None else self.system_target_dir
        lines = [
            "[package]",
            f'name = "{pkg_name}"',
            'install_method = "copy"',
            f'target_directory = "{target_directory}"',
            "enable_install = true",
        ]
        if dependencies:
            import json
            formatted = []
            for item in dependencies:
                if isinstance(item, str):
                    formatted.append(f'"{item}"')
                elif isinstance(item, dict):
                    inner = ", ".join(f'{k} = {json.dumps(v)}' for k, v in item.items())
                    formatted.append(f"{{ {inner} }}")
            lines.append(f"dependencies = [{', '.join(formatted)}]")
        (dot_drift / PACKAGE_CONFIG_FILE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
        (pkg_dir / f"file_{pkg_name}.txt").write_text(f"content for {pkg_name}", encoding="utf-8")
        self.workspace_config.packages_enable[pkg_name] = True

    def test_assert_packages_install_ready_with_full_universe_deps(self) -> None:
        self._create_install_package("pkg_a")
        self._create_install_package("pkg_b")
        metadata = {
            "pkg_a": PackageConfig.from_install_dir(self.install_dir / "pkg_a", self.workspace_config),
            "pkg_b": PackageConfig.from_install_dir(self.install_dir / "pkg_b", self.workspace_config),
        }
        state_registry = load_state_registry(self.install_dir / "state.toml")
        hook_flags = HookExecFlags.resolve(None, settings=self.workspace_config.settings)

        # 1. full_universe_deps=None skips dependency DAG validation
        assert_packages_install_ready(
            workspace_config=self.workspace_config,
            discovered_packages=["pkg_a", "pkg_b"],
            pkg_metadata_map=metadata,
            hook_flags=hook_flags,
            state_registry=state_registry,
            full_universe_deps=None,
        )

        # 2. Valid full_universe_deps passes
        valid_deps = {
            "pkg_a": PackageDependencies(items=[]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
        }
        assert_packages_install_ready(
            workspace_config=self.workspace_config,
            discovered_packages=["pkg_a", "pkg_b"],
            pkg_metadata_map=metadata,
            hook_flags=hook_flags,
            state_registry=state_registry,
            full_universe_deps=valid_deps,
        )

        # 3. Cyclic dependency raises ConfigError
        cyclic_deps = {
            "pkg_a": PackageDependencies(items=[PackageDependency(name="pkg_b")]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
        }
        with self.assertRaises(ConfigError) as ctx:
            assert_packages_install_ready(
                workspace_config=self.workspace_config,
                discovered_packages=["pkg_a", "pkg_b"],
                pkg_metadata_map=metadata,
                hook_flags=hook_flags,
                state_registry=state_registry,
                full_universe_deps=cyclic_deps,
            )
        self.assertIn("Cyclic package dependency detected", str(ctx.exception))

    def test_prepare_install_topological_ordering(self) -> None:
        self._create_install_package("pkg_c", dependencies=["pkg_b"])
        self._create_install_package("pkg_b", dependencies=["pkg_a"])
        self._create_install_package("pkg_a", dependencies=[])

        plan = prepare_install(self.workspace_config)
        self.assertEqual(plan.packages_install_order, ["pkg_a", "pkg_b", "pkg_c"])

        result = execute_install(self.workspace_config, plan=plan)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual([p.package for p in result.packages], ["pkg_a", "pkg_b", "pkg_c"])

    def test_execute_install_respects_topological_write_order(self) -> None:
        """Verifies that physical file writes during installation strictly respect topological dependency order."""
        import drift.core.file_action

        # Setup 3 packages with dependency chain: pkg_c -> pkg_b -> pkg_a
        self._create_install_package("pkg_c", dependencies=["pkg_b"], target_dir=self.system_target_dir / "pkg_c")
        self._create_install_package("pkg_b", dependencies=["pkg_a"], target_dir=self.system_target_dir / "pkg_b")
        self._create_install_package("pkg_a", dependencies=[], target_dir=self.system_target_dir / "pkg_a")

        # Add payload files to each package
        (self.install_dir / "pkg_a" / "payload_a.txt").write_text("data a", encoding="utf-8")
        (self.install_dir / "pkg_b" / "payload_b.txt").write_text("data b", encoding="utf-8")
        (self.install_dir / "pkg_c" / "payload_c.txt").write_text("data c", encoding="utf-8")

        # Pass target_pkgs in reverse topological order
        plan = prepare_install(self.workspace_config, target_pkgs=["pkg_c", "pkg_b", "pkg_a"])
        self.assertEqual(plan.packages_install_order, ["pkg_a", "pkg_b", "pkg_c"])

        copied_destinations: List[Path] = []
        real_copy_file = drift.core.file_action.copy_file

        def spy_copy_file(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
            copied_destinations.append(Path(dst))
            return real_copy_file(src, dst, *args, **kwargs)

        with patch("drift.core.file_action.copy_file", side_effect=spy_copy_file):
            result = execute_install(self.workspace_config, plan=plan)

        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual([p.package for p in result.packages], ["pkg_a", "pkg_b", "pkg_c"])
        self.assertTrue(len(copied_destinations) >= 3)

        # Map each copied destination file back to its containing package target in system_target_dir
        copied_packages = [
            dst.relative_to(self.system_target_dir).parts[0]
            for dst in copied_destinations
        ]

        # Verify all pkg_a files are copied before any pkg_b files, and all pkg_b files before pkg_c files
        last_pkg_a_idx = max(i for i, p in enumerate(copied_packages) if p == "pkg_a")
        first_pkg_b_idx = min(i for i, p in enumerate(copied_packages) if p == "pkg_b")
        last_pkg_b_idx = max(i for i, p in enumerate(copied_packages) if p == "pkg_b")
        first_pkg_c_idx = min(i for i, p in enumerate(copied_packages) if p == "pkg_c")

        self.assertLess(last_pkg_a_idx, first_pkg_b_idx)
        self.assertLess(last_pkg_b_idx, first_pkg_c_idx)

    def test_prepare_install_targeted_prerequisite_subset(self) -> None:
        self._create_install_package("pkg_c", dependencies=["pkg_b"])
        self._create_install_package("pkg_b", dependencies=["pkg_a"])
        self._create_install_package("pkg_a", dependencies=[])

        # pkg_b is already installed on the machine
        registry = load_state_registry(self.install_dir / "state.toml")
        registry.set_package_state("pkg_b", "installed")
        registry.save()

        # Target only pkg_c and pkg_a, while pkg_b is already installed
        plan = prepare_install(self.workspace_config, target_pkgs=["pkg_c", "pkg_a"])
        self.assertEqual(plan.packages_install_order, ["pkg_a", "pkg_c"])

    def test_prepare_install_with_disabled_installed_prerequisite(self) -> None:
        # pkg_a is installed on machine, but has enable_install = false in its config
        self._create_install_package("pkg_a")
        (self.install_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname = "pkg_a"\ninstall_method = "copy"\ntarget_directory = "{self.system_target_dir}"\nenable_install = false\n',
            encoding="utf-8"
        )
        registry = load_state_registry(self.install_dir / "state.toml")
        registry.set_package_state("pkg_a", "installed")
        registry.save()

        # pkg_b depends on pkg_a and is enabled
        self._create_install_package("pkg_b", dependencies=["pkg_a"])

        plan = prepare_install(self.workspace_config, target_pkgs=["pkg_b"])
        self.assertEqual(plan.packages_install_order, ["pkg_b"])

    def test_prepare_install_with_missing_installed_dir(self) -> None:
        # pkg_a is recorded in state.toml as installed, but its directory is missing from install/
        registry = load_state_registry(self.install_dir / "state.toml")
        registry.set_package_state("pkg_a", "installed")
        registry.save()

        # pkg_b depends on pkg_a
        self._create_install_package("pkg_b", dependencies=["pkg_a"])

        plan = prepare_install(self.workspace_config, target_pkgs=["pkg_b"])
        self.assertEqual(plan.packages_install_order, ["pkg_b"])

    def test_prepare_install_missing_required_dependency(self) -> None:
        self._create_install_package("pkg_b", dependencies=["missing_pkg"])
        with self.assertRaises(ConfigError) as ctx:
            prepare_install(self.workspace_config, target_pkgs=["pkg_b"])
        self.assertIn("missing_pkg", str(ctx.exception))

    def test_prepare_install_optional_dependency_pruned(self) -> None:
        self._create_install_package("pkg_b", dependencies=[{"name": "missing_pkg", "optional": True}])
        plan = prepare_install(self.workspace_config, target_pkgs=["pkg_b"])
        self.assertEqual(plan.packages_install_order, ["pkg_b"])

    def test_empty_folder_lifecycle_install_mode(self) -> None:
        """Verifies empty folder delivery in normal install mode: ENSURE_DIR on host, .drift_keep never copied."""
        from drift.core.constants import DRIFT_KEEP_FILE_NAME
        pkg = "pkg_empty_install"
        pkg_install_dir = self.install_dir / pkg
        (pkg_install_dir / "empty_dir").mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "empty_dir" / DRIFT_KEEP_FILE_NAME).touch()

        # 1. filter_deployable_files(include_empty_dirs=True) transforms stub into parent dir
        ignore = DriftIgnore()
        deployable = ignore.filter_deployable_files(pkg_install_dir, include_empty_dirs=True)
        self.assertEqual(deployable, [Path("empty_dir")])

        # 2. Planning with InstallMethod.COPY: plans ENSURE_DIR
        copy_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=True,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=False,
        )
        actions_copy = plan_folder_delivery(copy_ctx, deployable_files=deployable)
        self.assertEqual(len(actions_copy), 1)
        self.assertEqual(actions_copy[0].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(actions_copy[0].dst_path, self.system_target_dir / "empty_dir")

        # 3. Planning with InstallMethod.SYMLINK: STILL plans ENSURE_DIR (never symlinks empty directory)
        symlink_ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.SYMLINK,
            is_first_time=True,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.OVERWRITTEN,
            reverse_mode=False,
        )
        actions_symlink = plan_folder_delivery(symlink_ctx, deployable_files=deployable)
        self.assertEqual(len(actions_symlink), 1)
        self.assertEqual(actions_symlink[0].action_type, FileActionType.ENSURE_DIR)
        self.assertEqual(actions_symlink[0].dst_path, self.system_target_dir / "empty_dir")

        # 4. Execute and verify host state: directory exists, .drift_keep NOT present
        exec_ctx = FileActionExecutionContext()
        execute_delivery_actions(exec_ctx, actions_copy)
        self.assertTrue((self.system_target_dir / "empty_dir").is_dir())
        self.assertFalse((self.system_target_dir / "empty_dir" / DRIFT_KEEP_FILE_NAME).exists())

        # 5. Subsequent install populating the directory suppresses orphan pruning
        actions_subsequent = plan_folder_delivery(
            copy_ctx,
            deployable_files=[Path("empty_dir/child.txt")],
            deployed_files=[Path("empty_dir")],
        )
        # Should NOT plan orphan prune for empty_dir because it is an ancestor of empty_dir/child.txt
        prune_actions = [a for a in actions_subsequent if a.action_type in DELETE_ACTION_TYPES]
        self.assertEqual(len(prune_actions), 0)

    def test_empty_folder_lifecycle_reverse_mode(self) -> None:
        """Verifies empty folder handling in reverse mode: detects empty host folder and creates .drift_keep."""
        from drift.core.constants import DRIFT_KEEP_FILE_NAME
        pkg = "pkg_empty_rev"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        host_empty = self.system_target_dir / "host_empty_dir"
        host_empty.mkdir(parents=True, exist_ok=True)

        rev_ctx = DeliveryInspectionContext(
            target_dir=pkg_install_dir,
            source_dir=self.system_target_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
            reverse_mode=True,
        )

        actions = plan_folder_delivery(
            rev_ctx,
            deployable_files=[Path("host_empty_dir")],
            deployed_files=(),
        )

        action_types = [a.action_type for a in actions]
        self.assertIn(FileActionType.ENSURE_DIR, action_types)
        self.assertIn(FileActionType.CREATE_KEEP_FILE, action_types)

        keep_action = next(a for a in actions if a.action_type == FileActionType.CREATE_KEEP_FILE)
        self.assertEqual(keep_action.dst_path, pkg_install_dir / "host_empty_dir" / DRIFT_KEEP_FILE_NAME)

        # Execute actions: verify .drift_keep created in install/
        exec_ctx = FileActionExecutionContext()
        execute_delivery_actions(exec_ctx, actions)
        keep_path = pkg_install_dir / "host_empty_dir" / DRIFT_KEEP_FILE_NAME
        self.assertTrue(keep_path.is_file())
        self.assertEqual(keep_path.stat().st_size, 0)

        # Plan again: empty folder already tracked with .drift_keep, so no CREATE_KEEP_FILE planned
        actions_repeat = plan_folder_delivery(
            rev_ctx,
            deployable_files=[Path("host_empty_dir")],
            deployed_files=(),
        )
        self.assertFalse(any(a.action_type == FileActionType.CREATE_KEEP_FILE for a in actions_repeat))

    def test_empty_folder_ancestor_prune_in_reverse_mode(self) -> None:
        """Verifies ancestor inspection prunes .drift_keep when an empty folder becomes populated in reverse mode."""
        from drift.core.constants import DRIFT_KEEP_FILE_NAME
        pkg = "pkg_ancestor_prune"
        pkg_install_dir = self.install_dir / pkg
        repo_sub = pkg_install_dir / "tracked_dir"
        repo_sub.mkdir(parents=True, exist_ok=True)
        keep_file = repo_sub / DRIFT_KEEP_FILE_NAME
        keep_file.touch()

        # Host has a new file in tracked_dir
        host_sub = self.system_target_dir / "tracked_dir"
        host_sub.mkdir(parents=True, exist_ok=True)
        (host_sub / "new_file.txt").write_text("content", encoding="utf-8")

        rev_ctx = DeliveryInspectionContext(
            target_dir=pkg_install_dir,
            source_dir=self.system_target_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
            reverse_mode=True,
        )

        actions = plan_folder_delivery(
            rev_ctx,
            deployable_files=[Path("tracked_dir/new_file.txt")],
            deployed_files=(),
        )

        # Ancestor tracked_dir must have DELETE_ITEM planned for .drift_keep
        prune_keep = next((a for a in actions if a.action_type == FileActionType.DELETE_ITEM and a.dst_path == keep_file), None)
        self.assertIsNotNone(prune_keep)
        assert prune_keep is not None
        self.assertIn("Prune .drift_keep", str(prune_keep.reason))

        # Leaf new_file.txt must have CREATE_COPY planned
        create_file = next((a for a in actions if a.action_type == FileActionType.CREATE_COPY and a.dst_path == pkg_install_dir / "tracked_dir/new_file.txt"), None)
        self.assertIsNotNone(create_file)

        # Execute actions: .drift_keep deleted, new_file.txt created
        exec_ctx = FileActionExecutionContext()
        execute_delivery_actions(exec_ctx, actions)
        self.assertFalse(keep_file.exists())
        self.assertTrue((pkg_install_dir / "tracked_dir/new_file.txt").is_file())

    def test_orphan_prune_always_uses_delete_item(self) -> None:
        """Verifies orphan pruning always plans DELETE_ITEM, never DELETE_TREE."""
        pkg = "pkg_orphan_item"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        # Host has an orphan empty directory
        host_orphan_dir = self.system_target_dir / "orphan_empty_dir"
        host_orphan_dir.mkdir(parents=True, exist_ok=True)

        ctx = DeliveryInspectionContext(
            target_dir=self.system_target_dir,
            source_dir=pkg_install_dir,
            drift_root=self.drift_root,
            install_method=InstallMethod.COPY,
            is_first_time=False,
            backup_pkg_dir=None,
            backup_subfolder=BackupSubfolder.DELETED_FILES,
            reverse_mode=False,
        )

        actions = plan_folder_delivery(
            ctx,
            deployable_files=(),
            deployed_files=[Path("orphan_empty_dir")],
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action_type, FileActionType.DELETE_ITEM)
        self.assertEqual(actions[0].dst_path, host_orphan_dir)

        # Execute: empty dir removed safely via remove_file_or_empty_dir
        exec_ctx = FileActionExecutionContext()
        execute_delivery_actions(exec_ctx, actions)
        self.assertFalse(host_orphan_dir.exists())


class TestNativeSymlinkComputation(unittest.TestCase):
    """Tests for native symlink target computation and external directory symlink handling."""

    def test_compute_relative_symlink_target_standard(self) -> None:
        source = Path("/workspace/install/pkg/config/app.conf")
        parent = Path("/home/user/.config")
        rel = compute_relative_symlink_target(source, parent)
        self.assertEqual((parent / rel).resolve(), source.resolve())

    def test_compute_relative_symlink_target_nested(self) -> None:
        source = Path("/workspace/install/pkg/nested/deep/file.txt")
        parent = Path("/home/user/.config/app/sub")
        rel = compute_relative_symlink_target(source, parent)
        self.assertEqual((parent / rel).resolve(), source.resolve())

    def test_compute_relative_symlink_target_with_external_symlinked_parent(self) -> None:
        """When link_parent_dir is a symlink pointing to an external directory, relative path must resolve from the external directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir).resolve()
            external_dir = tmp_root / "external_store" / "app"
            external_dir.mkdir(parents=True, exist_ok=True)

            system_home = tmp_root / "home"
            system_home.mkdir(parents=True, exist_ok=True)
            symlink_parent = system_home / "app"
            symlink_parent.symlink_to(external_dir)

            source_file = tmp_root / "drift_install" / "pkg" / "app" / "config.toml"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text("config", encoding="utf-8")

            rel = compute_relative_symlink_target(source_file, symlink_parent)
            # When resolved from the real external parent, it must point directly to source_file
            self.assertEqual((symlink_parent / rel).resolve(), source_file.resolve())


class TestFolderDeliveryActionLogging(unittest.TestCase):
    """Unit tests for folder delivery file action execution and logging."""

    def test_skip_identical_requires_src_path(self) -> None:
        """Verifies FileActionType.SKIP_IDENTICAL enforces src_path presence via __post_init__."""
        with self.assertRaises(ValueError) as ctx:
            FileAction(
                action_type=FileActionType.SKIP_IDENTICAL,
                dst_path=Path("/tmp/target.txt"),
            )
        self.assertIn("FileActionType.SKIP_IDENTICAL must have src_path set", str(ctx.exception))

        # Successfully instantiates when src_path is provided
        action = FileAction(
            action_type=FileActionType.SKIP_IDENTICAL,
            src_path=Path("/tmp/source.txt"),
            dst_path=Path("/tmp/target.txt"),
        )
        self.assertEqual(action.src_path, Path("/tmp/source.txt"))
        self.assertIn("/tmp/source.txt -> /tmp/target.txt", format_action_line(action))

    def test_delivery_action_logging_info_and_debug(self) -> None:
        set_test_mode(True, enable_logging=True)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_root = Path(tmp_dir)
                src_file = tmp_root / "src.txt"
                src_file.write_text("hello", encoding="utf-8")
                dst_file = tmp_root / "dst.txt"

                exec_ctx = FileActionExecutionContext()

                # 1. CREATE_COPY logs at INFO
                copy_action = FileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=src_file,
                    dst_path=dst_file,
                )
                with self.assertLogs("drift.core.file_action", level="INFO") as cm:
                    execute_single_action(exec_ctx, copy_action)
                self.assertTrue(any("➕ [CREATE_COPY]" in msg for msg in cm.output))
                self.assertTrue(dst_file.is_file())

                # 2. SKIP_IDENTICAL logs at DEBUG (and NOT at INFO)
                skip_action = FileAction(
                    action_type=FileActionType.SKIP_IDENTICAL,
                    src_path=src_file,
                    dst_path=dst_file,
                )
                with self.assertLogs("drift.core.file_action", level="DEBUG") as cm_debug:
                    execute_single_action(exec_ctx, skip_action)
                self.assertTrue(any("⏭️ [SKIP_IDENTICAL]" in msg for msg in cm_debug.output))

                with self.assertRaises(AssertionError):
                    with self.assertLogs("drift.core.file_action", level="INFO"):
                        execute_single_action(exec_ctx, skip_action)

                # 3. ENSURE_DIR conditional logging:
                new_dir = tmp_root / "new_folder"
                dir_action = FileAction(
                    action_type=FileActionType.ENSURE_DIR,
                    dst_path=new_dir,
                )
                # First execution: dir does not exist -> created -> logs at INFO
                with self.assertLogs("drift.core.file_action", level="INFO") as cm_dir:
                    execute_single_action(exec_ctx, dir_action)
                self.assertTrue(any("📁 [ENSURE_DIR]" in msg for msg in cm_dir.output))
                self.assertTrue(new_dir.is_dir())

                # Second execution: dir already exists -> logs at DEBUG (not INFO)
                with self.assertLogs("drift.core.file_action", level="DEBUG") as cm_dir_debug:
                    execute_single_action(exec_ctx, dir_action)
                self.assertTrue(any("📁 [ENSURE_DIR]" in msg for msg in cm_dir_debug.output))

                with self.assertRaises(AssertionError):
                    with self.assertLogs("drift.core.file_action", level="INFO"):
                        execute_single_action(exec_ctx, dir_action)
        finally:
            set_test_mode(True, enable_logging=False)

    def test_render_action_types_rejected_and_info_logging(self) -> None:
        """Verifies execute_single_action rejects render actions and logs INFO_MESSAGE properly."""
        exec_ctx = FileActionExecutionContext()
        render_act = FileAction(
            action_type=FileActionType.RENDER_ITEM,
            src_path=Path("/tmp/tpl"),
            dst_path=Path("/tmp/out"),
        )
        with self.assertRaises(ValueError) as cm:
            execute_single_action(exec_ctx, render_act)
        self.assertIn("cannot be executed by the host delivery engine", str(cm.exception))

        config_act = FileAction(
            action_type=FileActionType.WRITE_CONFIG,
            dst_path=Path("/tmp/cfg"),
        )
        with self.assertRaises(ValueError) as cm2:
            execute_single_action(exec_ctx, config_act)
        self.assertIn("cannot be executed by the host delivery engine", str(cm2.exception))

        # Test INFO_MESSAGE logging
        set_test_mode(True, enable_logging=True)
        try:
            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                info_act = FileAction(
                    action_type=FileActionType.INFO_MESSAGE,
                    reason="Package hook executed successfully",
                )
                with self.assertLogs("drift.core.file_action", level="INFO") as cm_info:
                    execute_single_action(exec_ctx, info_act)
                self.assertTrue(any("📢 [INFO]" in msg and "Package hook executed successfully" in msg for msg in cm_info.output))
        finally:
            set_test_mode(True, enable_logging=False)

        # Test summary formatting with render actions
        summary = format_action_summary([render_act, config_act])
        self.assertIn("1 to render", summary)
        self.assertIn("1 config", summary)


if __name__ == "__main__":
    unittest.main()

