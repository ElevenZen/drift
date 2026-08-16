import os
import shutil
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from drift.constants import PACKAGE_CONFIG_FILE_NAME
from drift.workspace_config import WorkspaceConfig
from drift.package_config import PackageConfig
from drift.state_registry import load_state_registry, save_state_registry, StateRegistry
from drift.install_repo import (
    resolve_system_target,
    is_stow_linked_parent,
    run_primitive_5_install_deployment,
    ensure_directory_writable,
)


class TestInstallRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = os.path.abspath(self.temp_dir.name)

        # Create workspace structures
        self.source_dir = os.path.join(self.drift_root, "src")
        self.render_dir = os.path.join(self.drift_root, "render")
        self.install_dir = os.path.join(self.drift_root, "install")
        self.backup_dir = os.path.join(self.drift_root, "backup")
        self.system_target_dir = os.path.join(self.drift_root, "system_home")

        os.makedirs(self.source_dir, exist_ok=True)
        os.makedirs(self.render_dir, exist_ok=True)
        os.makedirs(self.install_dir, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)
        os.makedirs(self.system_target_dir, exist_ok=True)

        self.workspace_config = WorkspaceConfig(
            source_directory="src",
            render_directory="render",
            install_directory="install",
            backup_directory="backup",
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
        state_file = os.path.join(self.install_dir, "state.toml")
        
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

    def test_resolve_system_target(self) -> None:
        """Verifies resolves system targets correctly, translating dot- prefixes to dot."""
        # Simple file resolution
        resolved = resolve_system_target("dot-bashrc", self.system_target_dir)
        self.assertEqual(resolved, os.path.join(self.system_target_dir, ".bashrc"))

        # Nested folder and file resolution
        resolved = resolve_system_target("dot-config/nvim/dot-init.lua", self.system_target_dir)
        self.assertEqual(resolved, os.path.join(self.system_target_dir, ".config", "nvim", ".init.lua"))

        # Non-prefixed parts remain untouched
        resolved = resolve_system_target("regular_dir/regular_file.txt", self.system_target_dir)
        self.assertEqual(resolved, os.path.join(self.system_target_dir, "regular_dir", "regular_file.txt"))

    def test_directory_writability_check(self) -> None:
        """Tests writability check helper logic."""
        # Nonexistent nested path with writable base directory should succeed
        ensure_directory_writable(os.path.join(self.system_target_dir, "nonexistent", "nested"), sudo=False)
        
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
        nvim_target_dir = os.path.join(self.system_target_dir, ".config", "nvim")
        os.makedirs(os.path.dirname(nvim_target_dir), exist_ok=True)
        
        # Make ~/.config/nvim a symlink to install/pkg_stow/dot-config/nvim/
        install_nvim_dir = os.path.join(self.install_dir, "pkg_stow", "dot-config", "nvim")
        os.makedirs(install_nvim_dir, exist_ok=True)
        
        os.symlink(install_nvim_dir, nvim_target_dir)

        # Check if individual subfiles of nvim are recognized as having symlinked parents
        subfile_target = os.path.join(nvim_target_dir, "init.lua")
        self.assertTrue(is_stow_linked_parent(subfile_target, self.install_dir))

        # Check a regular file which doesn't have symlinked parent
        regular_target = os.path.join(self.system_target_dir, ".bashrc")
        self.assertFalse(is_stow_linked_parent(regular_target, self.install_dir))

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
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=["dot-bashrc"])])

        # Verify symlink is created
        target_file = os.path.join(self.system_target_dir, ".bashrc")
        self.assertTrue(os.path.islink(target_file))
        link_target = os.readlink(target_file)
        abs_link_target = os.path.abspath(os.path.join(os.path.dirname(target_file), link_target))
        self.assertEqual(abs_link_target, os.path.join(pkg_install_dir, "dot-bashrc"))

        # Verify state.toml transition
        state_file = os.path.join(self.install_dir, "state.toml")
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
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=["dot-bashrc"])])

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
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, added_files=["test.txt"])])

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
        run_primitive_5_install_deployment(self.workspace_config, [pkg], package_changes=[PackageStageChanges(package_name=pkg, modified_files=["test.txt"])])

        # Target file should be directly overwritten
        with open(target_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "updated hello copy")

        # on_update hook ran
        self.assertTrue(os.path.isfile(hook_marker))
        with open(hook_marker, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "hook updated")


if __name__ == "__main__":
    unittest.main()
