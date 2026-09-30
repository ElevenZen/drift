import unittest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from drift.core.exceptions import ConfigError
from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.core.state_registry import load_state_registry, save_state_registry, PackageState
from drift.primitives.uninstall_repo import (
    run_primitive_7_uninstall_packages,
    UninstallConfig,
    UninstallPlan,
    prepare_uninstall_packages,
    execute_uninstall_packages,
    uninstall_missing_package,
    assert_packages_uninstall_ready,
)
from drift.core.constants import PACKAGE_CONFIG_FILE_NAME, DRIFT_INTERNAL_DIR_NAME, InstallMethod

class TestUninstall(unittest.TestCase):
    def setUp(self):
        self.original_environ = dict(os.environ)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        
        # Override HOME environment variable for the duration of the test
        os.environ["HOME"] = str(self.system_target_dir)

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

        
        # Initialize Git in install_dir for Primitive 6 commit
        subprocess.run(["git", "init"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), capture_output=True, check=True)

    def tearDown(self):
        self.temp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_uninstall_basic_symlink(self):
        """Verifies basic uninstallation of a symlinked package."""
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/.drift/drift_package.toml
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        with open(pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
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
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            redeploy=True,
            deployable_files=[Path("dot-bashrc")],
        )
        save_state_registry(registry)
        
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
        self.assertIn(f"Uninstall: Removed package {pkg}", res.stdout)

    def test_uninstall_with_backup_restore(self):
        """Verifies that uninstallation restores overwritten files from backup."""
        pkg = "pkg_copy"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/.drift/drift_package.toml
        with open(pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
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
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            redeploy=True,
            deployable_files=[Path("config.txt")],
        )
        save_state_registry(registry)

        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 5. Run uninstall
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg])
        
        # 6. Verify restoration
        self.assertTrue(system_target.exists())
        self.assertEqual(system_target.read_text(encoding="utf-8"), "original user content")
        
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
        save_state_registry(registry)
        
        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], config=UninstallConfig(force=True))
        
        # Verify it proceeded
        updated_registry = load_state_registry(state_file)
        self.assertNotIn(pkg, updated_registry.packages)

    def test_uninstall_detach_symlink(self):
        """Verifies that detaching a symlinked package replaces the symlink with a copy, and keeps backup folders intact."""
        pkg = "pkg_symlink"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        
        # 1. Setup install/pkg/.drift/drift_package.toml
        with open(pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg}"
            install_method = "symlink"
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
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
            redeploy=True,
            deployable_files=[Path("dot-bashrc")],
        )
        save_state_registry(registry)
        
        # Commit initial state so git tracks it
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)
        
        # 5. Run uninstall with detach=True
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], config=UninstallConfig(detach=True))
        
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
        self.assertIn(f"Detach: Removed package {pkg}", res.stdout)

    def test_uninstall_triggers_pre_and_post_uninstall_hooks(self):
        """Verifies that pre_uninstall and post_uninstall hooks run in the correct order with correct environments."""
        pkg = "pkg_hooks"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / "hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        system_target = self.system_target_dir / "app.conf"
        system_target.write_text("deployed config", encoding="utf-8")

        pre_hook_out = self.drift_root / "pre_uninstall_out.txt"
        post_hook_out = self.drift_root / "post_uninstall_out.txt"

        pre_hook = scripts_dir / "pre_uninstall.sh"
        pre_hook.write_text(f"""#!/bin/sh
if [ -f "{system_target}" ]; then
    echo "PRE_UNINSTALL_${{drift_package_name}}_IN_$(pwd)" > "{pre_hook_out}"
fi
""", encoding="utf-8")
        pre_hook.chmod(0o755)

        post_hook = scripts_dir / "post_uninstall.sh"
        post_hook.write_text(f"""#!/bin/sh
if [ ! -f "{system_target}" ]; then
    echo "POST_UNINSTALL_${{drift_package_name}}_IN_$(pwd)" > "{post_hook_out}"
fi
""", encoding="utf-8")
        post_hook.chmod(0o755)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "drift_hooks/pre_uninstall.sh"
        post_uninstall = "drift_hooks/post_uninstall.sh"
        """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            redeploy=True,
            deployable_files=[Path("app.conf")],
        )
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        res = run_primitive_7_uninstall_packages(self.workspace_config, [pkg], config=UninstallConfig(force=True))
        self.assertEqual(res.status, "SUCCESS")

        # 1. Target file was removed
        self.assertFalse(system_target.exists())

        # 2. pre_uninstall executed before file removal with cwd=hook_path.parent
        self.assertTrue(pre_hook_out.is_file())
        self.assertEqual(
            pre_hook_out.read_text(encoding="utf-8").strip(),
            f"PRE_UNINSTALL_{pkg}_IN_{pre_hook.parent}"
        )

        # 3. post_uninstall executed after file removal with cwd=install_dir
        self.assertTrue(post_hook_out.is_file())
        self.assertEqual(
            post_hook_out.read_text(encoding="utf-8").strip(),
            f"POST_UNINSTALL_{pkg}_IN_{post_hook.parent}"
        )

    def test_uninstall_fails_if_hook_file_missing_in_install(self):
        """Verifies that uninstall pre-flight checks fail if a configured hook file is missing."""
        pkg = "pkg_missing_hook"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "drift_hooks/non_existent.sh"
        """, encoding="utf-8")

        system_target = self.system_target_dir / "sample.txt"
        system_target.write_text("sample")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            redeploy=True,
            deployable_files=[Path("sample.txt")],
        )
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        with self.assertRaises(FileNotFoundError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, [pkg], config=UninstallConfig(force=True))

        self.assertIn("pre_uninstall", str(ctx.exception))
        # System target was not touched
        self.assertTrue(system_target.exists())

    def test_uninstall_pre_hook_failure_aborts_uninstall(self):
        """Verifies that a failure in pre_uninstall aborts the uninstall process."""
        pkg = "pkg_failing_hook"
        pkg_install_dir = self.install_dir / pkg
        pkg_install_dir.mkdir(parents=True, exist_ok=True)
        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        scripts_dir = pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / "hooks"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook_script.chmod(0o755)

        (pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg}"
        install_method = "copy"

        [hooks]
        pre_uninstall = "drift_hooks/failing.sh"
        """, encoding="utf-8")

        system_target = self.system_target_dir / "sample.txt"
        system_target.write_text("sample")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        registry.sync_deployed_files(
            pkg,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            redeploy=True,
            deployable_files=[Path("sample.txt")],
        )
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial install"], cwd=str(self.install_dir), check=True, capture_output=True)

        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, [pkg], config=UninstallConfig(force=True))

        self.assertIn("pre_uninstall", str(ctx.exception))
        # System target was not removed
        self.assertTrue(system_target.exists())

    def test_uninstall_blocked_when_remaining_package_requires_target(self):
        """Verifies that uninstalling a package required by another remaining installed package raises ConfigError."""
        self.workspace_config.packages_enable["pkg_a"] = False
        self.workspace_config.packages_enable["pkg_b"] = False
        # Setup pkg_a and pkg_b, where pkg_b requires pkg_a
        for pkg, deps_text in [("pkg_a", ""), ("pkg_b", 'dependencies = ["pkg_a"]')]:
            pkg_dir = self.install_dir / pkg
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "{pkg}"
            {deps_text}
            """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.set_package_state("pkg_b", "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Install pkg_a and pkg_b"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Attempting to uninstall pkg_a while pkg_b remains installed should raise ConfigError
        with self.assertRaises(ConfigError) as ctx:
            run_primitive_7_uninstall_packages(self.workspace_config, ["pkg_a"])

        self.assertIn("Remaining package 'pkg_b' requires uninstalled package(s): ['pkg_a']", str(ctx.exception))

    def test_uninstall_dependency_blocked_bypassed_with_force_or_no_deps(self):
        """Verifies that dependency check on uninstallation is bypassed with force=True or no_deps=True."""
        self.workspace_config.packages_enable["pkg_a"] = False
        self.workspace_config.packages_enable["pkg_b"] = False
        for pkg, deps_text in [("pkg_a", ""), ("pkg_b", 'dependencies = ["pkg_a"]')]:
            pkg_dir = self.install_dir / pkg
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "{pkg}"
            {deps_text}
            """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.set_package_state("pkg_b", "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Install pkg_a and pkg_b"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 1. With no_deps=True (pkg_a is disabled in workspace_config, so safeguard passes)
        res = run_primitive_7_uninstall_packages(
            self.workspace_config,
            ["pkg_a"],
            config=UninstallConfig(no_deps=True),
        )
        self.assertEqual(res.status, "SUCCESS")
        updated_reg = load_state_registry(state_file)
        self.assertNotIn("pkg_a", updated_reg.packages)
        self.assertIn("pkg_b", updated_reg.packages)

        # Re-install pkg_a and enable both to test force=True bypasses active check AND dependency check
        updated_reg.set_package_state("pkg_a", "installed")
        save_state_registry(updated_reg)
        self.workspace_config.packages_enable["pkg_a"] = True
        self.workspace_config.packages_enable["pkg_b"] = True
        res_force = run_primitive_7_uninstall_packages(
            self.workspace_config,
            ["pkg_a"],
            config=UninstallConfig(force=True),
        )
        self.assertEqual(res_force.status, "SUCCESS")
        updated_reg2 = load_state_registry(state_file)
        self.assertNotIn("pkg_a", updated_reg2.packages)
        self.assertIn("pkg_b", updated_reg2.packages)

    def test_uninstall_multiple_packages_reverse_topological_order(self):
        """Verifies that multiple packages are uninstalled in reverse topological order (dependents first)."""
        # pkg_c requires pkg_b, pkg_b requires pkg_a
        for pkg, deps_text in [
            ("pkg_a", ""),
            ("pkg_b", 'dependencies = ["pkg_a"]'),
            ("pkg_c", 'dependencies = ["pkg_b"]'),
        ]:
            pkg_dir = self.install_dir / pkg
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (pkg_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
            [package]
            name = "{pkg}"
            {deps_text}
            """, encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        for pkg in ["pkg_a", "pkg_b", "pkg_c"]:
            registry.set_package_state(pkg, "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Install all 3 packages"], cwd=str(self.install_dir), check=True, capture_output=True)

        # Uninstall all three in random order in argument: ["pkg_a", "pkg_c", "pkg_b"]
        res = run_primitive_7_uninstall_packages(
            self.workspace_config,
            ["pkg_a", "pkg_c", "pkg_b"],
            config=UninstallConfig(force=True),
        )
        self.assertEqual(res.status, "SUCCESS")
        uninstalled_order = [p.package for p in res.packages]
        # Must be reverse dependency order: pkg_c first, then pkg_b, then pkg_a
        self.assertEqual(uninstalled_order, ["pkg_c", "pkg_b", "pkg_a"])

    def test_uninstall_graceful_missing_install_dir(self):
        """Verifies that uninstalling a registered package whose directory is missing in install/ succeeds and cleans state."""
        pkg = "ghost_pkg"
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg, "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Record ghost package"], cwd=str(self.install_dir), check=True, capture_output=True)

        # install_dir / pkg does NOT exist on disk
        self.assertFalse((self.install_dir / pkg).exists())

        res = run_primitive_7_uninstall_packages(
            self.workspace_config,
            [pkg],
            config=UninstallConfig(force=True),
        )
        self.assertEqual(res.status, "SUCCESS")
        updated_reg = load_state_registry(state_file)
        self.assertNotIn(pkg, updated_reg.packages)

    def test_uninstall_commit_message_plural(self):
        """Verifies that uninstalling multiple packages uses 'packages' instead of 'package(s)' in commit message."""
        pkgs = ["pkg_mult1", "pkg_mult2"]
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        for p in pkgs:
            p_dir = self.install_dir / p
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (p_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
                f"[package]\nname = '{p}'\ndefault_install_method = 'copy'\n",
                encoding="utf-8"
            )
            registry.set_package_state(p, "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Record multiple packages"], cwd=str(self.install_dir), check=True, capture_output=True)

        res = run_primitive_7_uninstall_packages(
            self.workspace_config,
            pkgs,
            config=UninstallConfig(force=True),
        )
        self.assertEqual(res.status, "SUCCESS")

        log_res = subprocess.run(["git", "log", "-1", "--pretty=%B"], cwd=str(self.install_dir), capture_output=True, text=True)
        self.assertEqual(log_res.stdout.strip(), "Uninstall: Removed packages pkg_mult2, pkg_mult1")

    def test_prepare_and_execute_uninstall_pipeline(self):
        """Verifies that prepare_uninstall_packages and execute_uninstall_packages work as modular sub-stages."""
        # Setup pkg_a and pkg_b (pkg_b depends on pkg_a)
        for pkg, deps_text in [("pkg_a", ""), ("pkg_b", 'dependencies = ["pkg_a"]')]:
            p_dir = self.install_dir / pkg
            p_dir.mkdir(parents=True, exist_ok=True)
            (p_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            (p_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
                f"[package]\nname = '{pkg}'\n{deps_text}\n", encoding="utf-8"
            )
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.set_package_state("pkg_b", "installed")
        save_state_registry(registry)

        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Install pkg_a and pkg_b"], cwd=str(self.install_dir), check=True, capture_output=True)

        # 1. Test prepare_uninstall_packages (read-only plan preparation)
        plan = prepare_uninstall_packages(
            self.workspace_config,
            ["pkg_a", "pkg_b"],
            config=UninstallConfig(force=True),
        )
        self.assertIsInstance(plan, UninstallPlan)
        # Reverse dependency order: pkg_b must be uninstalled before pkg_a
        self.assertEqual(plan.ordered_packages, ["pkg_b", "pkg_a"])
        self.assertIn("pkg_a", plan.pkg_config_map)
        self.assertIn("pkg_b", plan.pkg_config_map)
        # Registry and install directories must still exist unmodified
        self.assertTrue((self.install_dir / "pkg_a").is_dir())
        self.assertTrue((self.install_dir / "pkg_b").is_dir())

        # 2. Test execute_uninstall_packages (state-mutating execution)
        result = execute_uninstall_packages(self.workspace_config, plan)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual([p.package for p in result.packages], ["pkg_b", "pkg_a"])
        self.assertFalse((self.install_dir / "pkg_a").exists())
        self.assertFalse((self.install_dir / "pkg_b").exists())

        updated_reg = load_state_registry(state_file)
        self.assertNotIn("pkg_a", updated_reg.packages)
        self.assertNotIn("pkg_b", updated_reg.packages)

    def test_prepare_uninstall_safeguard_error(self):
        """Verifies that prepare_uninstall_packages raises RuntimeError on active packages without force."""
        self.workspace_config.packages_enable["pkg_a"] = True
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        save_state_registry(registry)

        with self.assertRaises(RuntimeError) as ctx:
            prepare_uninstall_packages(self.workspace_config, ["pkg_a"])

        self.assertIn("Safeguard abort", str(ctx.exception))

    def test_uninstall_missing_package_unit(self):
        """Verifies uninstall_missing_package helper cleans up directories and returns proper result."""
        pkg_state = PackageState(
            state="installed",
            target_directory=self.system_target_dir,
            install_method=InstallMethod.SYMLINK,
        )
        # Setup empty backup directory for pkg
        pkg_backup = self.backup_dir / "pkg_ghost"
        pkg_backup.mkdir(parents=True, exist_ok=True)

        res = uninstall_missing_package(
            workspace_config=self.workspace_config,
            pkg="pkg_ghost",
            pkg_state=pkg_state,
            dry_run=False,
            detach=False,
        )
        self.assertEqual(res.package, "pkg_ghost")
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.removed_files, [])
        # Empty backup dir should have been pruned
        self.assertFalse(pkg_backup.exists())


if __name__ == "__main__":
    unittest.main()
