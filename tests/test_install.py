import os
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.package_config import PackageConfig
from drift.state_registry import load_state_registry, save_state_registry, StateRegistry, PackageState
from drift.install_repo import (
    resolve_system_target,
    is_stow_linked_parent,
    run_primitive_5_install_deployment,
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

    def test_directory_writability_check(self) -> None:
        """Tests writability check helper logic."""
        # Nonexistent nested path with writable base directory should succeed
        ensure_directory_writable(self.system_target_dir / "nonexistent" / "nested", sudo=False)
        
        # Read-only path (if it exists) should raise PermissionError, but we skip system-level permissions testing in normal environments
        # We can mock os.access to return False
        with patch("os.access", return_value=False):
            with self.assertRaises(PermissionError):
                ensure_directory_writable(self.system_target_dir, sudo=False)
                
        # Sudo elevation should bypass check
        with patch("os.access", return_value=False):
            ensure_directory_writable(self.system_target_dir, sudo=True)

    def test_infinite_loop_protection(self) -> None:
        """Verifies incremental stow skips individual links if parent directory is already symlinked to install/."""
        metadata = PackageConfig(
            name="pkg_stow",
            target_directory=self.system_target_dir,
            install_method="stow"
        )
        
        # Mock system target nvim config
        nvim_target_dir = self.system_target_dir / ".config" / "nvim"
        os.makedirs(os.path.dirname(nvim_target_dir), exist_ok=True)
        
        # Make ~/.config/nvim a symlink to install/pkg_stow/dot-config/nvim/
        install_nvim_dir = self.install_dir / "pkg_stow" / "dot-config" / "nvim"
        os.makedirs(install_nvim_dir, exist_ok=True)
        
        os.symlink(install_nvim_dir, nvim_target_dir)

        # Check if individual subfiles of nvim are recognized as having symlinked parents
        subfile_target = nvim_target_dir / "init.lua"
        self.assertTrue(is_stow_linked_parent(subfile_target, self.drift_root))

        # Check a regular file which doesn't have symlinked parent
        regular_target = self.system_target_dir / ".bashrc"
        self.assertFalse(is_stow_linked_parent(regular_target, self.drift_root))

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
            on_install = "on-install.sh"
            on_update = "on-update.sh"
            """)

        # Add physical file in install
        with open(os.path.join(pkg_install_dir, "test.txt"), "w", encoding="utf-8") as f:
            f.write("hello copy")

        # Write hooks in src/pkg_copy/
        with open(os.path.join(pkg_install_dir, "on-install.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook installed' > hook_ran.txt\n")
        with open(os.path.join(pkg_install_dir, "on-update.sh"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'hook updated' > hook_ran.txt\n")

        # Simulation 1: First-Time Deploy (triggers collision guard and on_install)
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

        # on_install hook ran (check file created in src/pkg_copy/)
        hook_marker = os.path.join(pkg_install_dir, "hook_ran.txt")
        self.assertTrue(os.path.isfile(hook_marker))
        with open(hook_marker, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hook installed")

        # Simulation 2: Update/Redeploy (bypasses collision guard, triggers on_update)
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

        # on_update hook ran
        self.assertTrue(os.path.isfile(hook_marker))
        with open(hook_marker, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hook updated")

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

    def test_load_active_install_packages(self) -> None:
        """Verifies that load_active_install_packages correctly resolves packages for deployment."""
        from drift.install_repo import load_active_install_packages
        
        discovered = ["pkg_a", "pkg_b"]
        
        # 1. No package list but they are disabled -> returns []
        res = load_active_install_packages(discovered, None, self.workspace_config)
        self.assertEqual(res, [])

        # 2. Enable them by setting packages_enable_default = True
        self.workspace_config.packages_enable_default = True
        
        # Now no package list fallback to discovered (all enabled)
        res = load_active_install_packages(discovered, None, self.workspace_config)
        self.assertEqual(res, ["pkg_a", "pkg_b"])
        
        # 3. Empty package list fallback to discovered (all enabled)
        res = load_active_install_packages(discovered, [], self.workspace_config)
        self.assertEqual(res, ["pkg_a", "pkg_b"])
        
        # 4. Explicit list with valid packages (even if some might be disabled, explicit selection is always allowed)
        self.workspace_config.packages_enable_default = False
        res = load_active_install_packages(discovered, ["pkg_a"], self.workspace_config)
        self.assertEqual(res, ["pkg_a"])
        
        # 5. Explicit list with single package string
        res = load_active_install_packages(discovered, "pkg_b", self.workspace_config)
        self.assertEqual(res, ["pkg_b"])

        # 6. Invalid package (raises ValueError)
        with self.assertRaises(ValueError):
            load_active_install_packages(discovered, ["invalid_pkg"], self.workspace_config)

        # 7. Invalid package but with force (allowed)
        res = load_active_install_packages(discovered, ["invalid_pkg"], self.workspace_config, force=True)
        self.assertEqual(res, ["invalid_pkg"])

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
        """Verifies that if a staged file matches .drift_ignore, the collision guard backs it up to deleted_files/ and removes it from the host system."""
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
            f.write("should be deleted")

        # Write .drift_ignore to install/pkg_stow telling it to ignore ignored_file.txt
        with open(os.path.join(pkg_install_dir, ".drift_ignore"), "w", encoding="utf-8") as f:
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

        # 1. The ignored_file.txt should be deleted from self.system_target_dir
        self.assertFalse(system_file.exists())

        # 2. It should be backed up under backup/pkg_stow/deleted_files (preserving structure)
        backup_deleted = self.backup_dir / pkg / "deleted_files" / "ignored_file.txt"
        self.assertTrue(backup_deleted.exists())
        with open(backup_deleted, "r", encoding="utf-8") as f:
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


if __name__ == "__main__":
    unittest.main()
