import unittest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from drift.workspace_config import WorkspaceConfig
from drift.state_registry import load_state_registry, save_state_registry, PackageState
from drift.uninstall_repo import run_primitive_7_uninstall_packages
from drift.constants import PACKAGE_CONFIG_FILE_NAME

class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        
        # Override HOME environment variable for the duration of the test
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.system_target_dir)

        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"
        
        for d in [self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.system_target_dir,
            packages_enable={"pkg_a": True}
        )
        # Ensure it doesn't expand to real home during test
        self.workspace_config.default_target_directory = self.system_target_dir

        
        # Initialize Git in install_dir for Primitive 6 commit
        subprocess.run(["git", "init"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), capture_output=True, check=True)

    def tearDown(self):
        if self._old_home:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)
        self.temp_dir.cleanup()

    def test_uninstall_basic_stow(self):
        """Verifies basic uninstallation of a stowed package."""
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/drift_package.toml
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)
            
        # 2. Setup system target with a symlink (simulating deployment)
        src_file = pkg_install_dir / "dot-bashrc"
        src_file.write_text("pkg content")
        
        system_target = self.system_target_dir / ".bashrc"
        os.symlink(src_file, system_target)
        
        # 3. Setup state.toml
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="stow")
        registry.set_package_deployed_files(pkg, [Path("dot-bashrc")])
        save_state_registry(state_file, registry)
        
        # Commit initial state so git tracks it
        # or it will say nothing to commit when we try to commit the uninstall changes
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)
        
        # 4. Run uninstall
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg])
        
        # 5. Verify results
        self.assertFalse(system_target.exists())
        self.assertFalse(system_target.is_symlink())
        self.assertFalse(pkg_install_dir.exists())
        
        updated_registry = load_state_registry(state_file)
        self.assertNotIn(pkg, updated_registry.packages)
        
        # Verify commit happened
        res = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertIn(f"Uninstall: Removed package(s) {pkg}", res.stdout)

    def test_uninstall_with_backup_restore(self):
        """Verifies that uninstallation restores overwritten files from backup."""
        pkg = "pkg_copy"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/drift_package.toml
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "copy"
            target_directory = "{self.system_target_dir}"
            """)
            
        # 2. Setup system target (simulating deployed file)
        system_target = self.system_target_dir / "config.txt"
        system_target.write_text("deployed content")
        
        # 3. Setup backup (simulating overwritten file)
        backup_pkg_overwritten = self.backup_dir / pkg / "overwritten"
        backup_pkg_overwritten.mkdir(parents=True, exist_ok=True)
        backup_file = backup_pkg_overwritten / "config.txt"
        backup_file.write_text("original user content")
        
        # 4. Setup state.toml
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="copy")
        registry.set_package_deployed_files(pkg, [Path("config.txt")])
        save_state_registry(state_file, registry)

        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 5. Run uninstall
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg])
        
        # 6. Verify restoration
        self.assertTrue(system_target.exists())
        self.assertEqual(system_target.read_text(), "original user content")
        
        # Verify cleanup
        self.assertFalse(backup_pkg_overwritten.exists())
        self.assertFalse((self.backup_dir / pkg).exists())
        self.assertFalse(pkg_install_dir.exists())

    def test_uninstall_safeguard_abort(self):
        """Verifies that uninstall aborts if package is enabled in workspace config."""
        pkg = "pkg_active"
        
        # 1. Enable package in workspace config
        self.workspace_config.packages_enable[pkg] = True
        
        # 2. Run uninstall - should raise RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, [pkg])
        self.assertIn("Safeguard abort", str(ctx.exception))
        
        # 3. Run with force=True - should NOT raise RuntimeError (it might skip if not in state, but won't abort on safeguard)
        # To make it proceed, we need it in state and the folder to exist
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / "dummy.txt").write_text("untracked file")
        
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        save_state_registry(state_file, registry)
        
        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)
        
        # Verify it proceeded
        updated_registry = load_state_registry(state_file)
        self.assertNotIn(pkg, updated_registry.packages)

    def test_uninstall_detach_stow(self):
        """Verifies that detaching a stowed package replaces the symlink with a copy, and keeps backup folders intact."""
        pkg = "pkg_stow"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/drift_package.toml
        with open(pkg_install_dir / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "stow"
            target_directory = "{self.system_target_dir}"
            """)
            
        # 2. Setup system target with a symlink (simulating deployment)
        src_file = pkg_install_dir / "dot-bashrc"
        src_file.write_text("pkg content")
        
        system_target = self.system_target_dir / ".bashrc"
        os.symlink(src_file, system_target)
        
        # 3. Setup backup (should remain intact during detach)
        backup_pkg_overwritten = self.backup_dir / pkg / "overwritten"
        backup_pkg_overwritten.mkdir(parents=True, exist_ok=True)
        backup_file = backup_pkg_overwritten / "dot-bashrc"
        backup_file.write_text("original backup user content")
        
        # 4. Setup state.toml
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="stow")
        registry.set_package_deployed_files(pkg, [Path("dot-bashrc")])
        save_state_registry(state_file, registry)
        
        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)
        
        # 5. Run uninstall with detach=True
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], detach=True)
        
        # 6. Verify results
        # Target file is NO LONGER a symlink, but a physical copy of "pkg content"
        self.assertTrue(system_target.exists())
        self.assertFalse(system_target.is_symlink())
        self.assertEqual(system_target.read_text(encoding="utf-8"), "pkg content")
        
        # Backup folder and backup file remain completely intact!
        self.assertTrue(backup_file.exists())
        self.assertEqual(backup_file.read_text(encoding="utf-8"), "original backup user content")
        
        # install/pkg dir is removed
        self.assertFalse(pkg_install_dir.exists())
        
        updated_registry = load_state_registry(state_file)
        self.assertNotIn(pkg, updated_registry.packages)
        
        # Verify commit happened with "Detach" message
        res = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertIn(f"Detach: Removed package(s) {pkg}", res.stdout)

    def test_uninstall_triggers_pre_and_post_uninstall_hooks(self):
        """Verifies that pre_uninstall and post_uninstall hooks run in the correct order with correct environments."""
        pkg = "pkg_hooks"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        system_target = self.system_target_dir / "app.conf"
        system_target.write_text("deployed config", encoding="utf-8")

        pre_hook_out = self.system_target_dir / "pre_uninstall_out.txt"
        post_hook_out = self.system_target_dir / "post_uninstall_out.txt"

        pre_hook = scripts_dir / "pre_uninstall.sh"
        pre_hook.write_text(f"""#!/bin/sh
if [ -f "{system_target}" ]; then
    echo "PRE_UNINSTALL_${{drift_package_name}}_FILE_EXISTS" > "{pre_hook_out}"
fi
""", encoding="utf-8")
        pre_hook.chmod(0o755)

        post_hook = scripts_dir / "post_uninstall.sh"
        post_hook.write_text(f"""#!/bin/sh
if [ ! -f "{system_target}" ]; then
    echo "POST_UNINSTALL_${{drift_package_name}}_FILE_REMOVED_IN_$(pwd)" > "{post_hook_out}"
fi
""", encoding="utf-8")
        post_hook.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "scripts/pre_uninstall.sh"
        post_uninstall = "scripts/post_uninstall.sh"
        """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="copy")
        registry.set_package_deployed_files(pkg, [Path("app.conf")])
        save_state_registry(state_file, registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        res = run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)
        self.assertEqual(res.status, "SUCCESS")

        # 1. Target file was removed
        self.assertFalse(system_target.exists())

        # 2. pre_uninstall executed before file removal
        self.assertTrue(pre_hook_out.is_file())
        self.assertEqual(pre_hook_out.read_text(encoding="utf-8").strip(), f"PRE_UNINSTALL_{pkg}_FILE_EXISTS")

        # 3. post_uninstall executed after file removal with cwd=target_dir
        self.assertTrue(post_hook_out.is_file())
        self.assertEqual(
            post_hook_out.read_text(encoding="utf-8").strip(),
            f"POST_UNINSTALL_{pkg}_FILE_REMOVED_IN_{self.system_target_dir}"
        )

    def test_uninstall_fails_if_hook_file_missing_in_install(self):
        """Verifies that uninstall pre-flight checks fail if a configured hook file is missing."""
        pkg = "pkg_missing_hook"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "scripts/non_existent.sh"
        """, encoding="utf-8")

        system_target = self.system_target_dir / "sample.txt"
        system_target.write_text("sample")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="copy")
        registry.set_package_deployed_files(pkg, [Path("sample.txt")])
        save_state_registry(state_file, registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        with self.assertRaises(FileNotFoundError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)

        self.assertIn("pre_uninstall", str(ctx.exception))
        # System target was not touched
        self.assertTrue(system_target.exists())

    def test_uninstall_pre_hook_failure_aborts_uninstall(self):
        """Verifies that a failure in pre_uninstall aborts the uninstall process."""
        pkg = "pkg_failing_hook"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_install_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "scripts/failing.sh"
        """, encoding="utf-8")

        system_target = self.system_target_dir / "sample.txt"
        system_target.write_text("sample")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed", install_method="copy")
        registry.set_package_deployed_files(pkg, [Path("sample.txt")])
        save_state_registry(state_file, registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)

        self.assertIn("pre_uninstall", str(ctx.exception))
        # System target was not removed
        self.assertTrue(system_target.exists())


if __name__ == "__main__":
    unittest.main()
