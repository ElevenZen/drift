import unittest
import os
import shutil
import tempfile
import subprocess
from pathlib import Path
from drift.workspace_config import WorkspaceConfig, load_workspace_config
from drift.state_registry import load_state_registry
from drift.constants import PACKAGE_CONFIG_FILE_NAME, CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME

class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        self.system_target_dir.mkdir(parents=True, exist_ok=True)
        
        # Override HOME environment variable for the duration of the test
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.system_target_dir)
        
        # 1. Initialize drift workspace via CLI logic
        from drift.init_repo import init_drift_workspace
        init_drift_workspace(self.drift_root)
        
        # 2. Load workspace config
        config_path = self.drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
        # We need to manually inject the enable for tests since drift.toml is generated fresh
        self.workspace_config = load_workspace_config(config_path)
        self.workspace_config.default_target_directory = self.system_target_dir
        
        self.source_dir = self.workspace_config.source_path
        self.install_dir = self.workspace_config.install_path
        self.render_dir = self.workspace_config.render_path

    def tearDown(self):
        if self._old_home:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)
        self.temp_dir.cleanup()

    def test_lifecycle_stow_basic(self):
        """Scenario: Basic stow deployment, drift detection, and uninstallation."""
        from drift.new_package import create_new_package
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment
        from drift.uninstall_repo import run_primitive_7_uninstall_packages
        
        pkg = "pkg_stow"
        # Manually enable the package in the loaded config object
        self.workspace_config.packages_enable[pkg] = True
        
        # 1. Create & Setup
        create_new_package(self.workspace_config, pkg)
        (self.source_dir / pkg / "bashrc").write_text("alias ll='ls -l'")
        
        # 2. Render & Stage & Apply
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        
        # Verify deployment
        target_file = self.system_target_dir / "bashrc"
        self.assertTrue(target_file.is_symlink())
        self.assertEqual(target_file.read_text(), "alias ll='ls -l'")
        
        # 3. Simulate Drift
        target_file.unlink()
        target_file.write_text("drifted content") # Replace symlink with physical drifted file
        
        # 4. Reverse Sync
        from drift.reverse_sync import run_primitive_1_reverse_sync
        run_primitive_1_reverse_sync(self.workspace_config, package_names=[pkg])
        
        # Verify install/ state updated
        install_file = self.install_dir / pkg / "bashrc"
        self.assertEqual(install_file.read_text(), "drifted content")
        
        # 5. Uninstall
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)
        self.assertFalse(target_file.exists())
        self.assertFalse((self.install_dir / pkg).exists())

    def test_lifecycle_copy_with_backup_restore(self):
        """Scenario: Copy deployment overwriting existing system file, then restoring it."""
        from drift.new_package import create_new_package
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment
        from drift.uninstall_repo import run_primitive_7_uninstall_packages
        
        pkg = "pkg_copy"
        self.workspace_config.packages_enable[pkg] = True
        
        # 1. Pre-existing system file
        target_file = self.system_target_dir / "config.ini"
        target_file.write_text("original config")
        
        # 2. Setup package with copy method
        create_new_package(self.workspace_config, pkg)
        with open(self.source_dir / pkg / PACKAGE_CONFIG_FILE_NAME, "a") as f:
            f.write('install_method = "copy"\n')
        (self.source_dir / pkg / "config.ini").write_text("drift managed config")
        
        # 3. Full Deployment
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        
        # Verify overwritten and backed up
        self.assertEqual(target_file.read_text(), "drift managed config")
        backup_file = self.workspace_config.backup_path / pkg / "overwritten" / "config.ini"
        self.assertTrue(backup_file.exists())
        self.assertEqual(backup_file.read_text(), "original config")
        
        # 4. Uninstall
        run_primitive_7_uninstall_packages(self.workspace_config, [pkg], force=True)
        
        # Verify restoration
        self.assertTrue(target_file.exists())
        self.assertFalse(target_file.is_symlink())
        self.assertEqual(target_file.read_text(), "original config")

    def test_orphan_garbage_collection(self):
        """Scenario: Deploying a package, then disabling it and running gc to trigger cleanup."""
        from drift.new_package import create_new_package
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment
        from drift.gc import run_primitive_9_garbage_collect_orphans
        
        pkg = "pkg_to_be_orphan"
        self.workspace_config.packages_enable[pkg] = True
        
        # 1. Deploy
        create_new_package(self.workspace_config, pkg)
        (self.source_dir / pkg / "orphaned_file.txt").write_text("I will be gone")
        
        run_primitive_2_render_packages(self.workspace_config)
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        
        target_file = self.system_target_dir / "orphaned_file.txt"
        self.assertTrue(target_file.exists())
        
        # 2. Disable in config
        self.workspace_config.packages_enable[pkg] = False
        
        # 3. Run GC (explicit primitive call)
        run_primitive_9_garbage_collect_orphans(self.workspace_config)
        
        # 4. Verify uninstalled
        self.assertFalse(target_file.exists())
        registry = load_state_registry(self.install_dir / "state.toml")
        self.assertNotIn(pkg, registry.packages)

    def test_template_engine_dependency_chain(self):
        """Scenario: mustache template depends on envsubst-rendered JSON input."""
        from drift.render_package import run_primitive_2_render_packages
        from drift.stage_repo import run_primitive_4_stage_render_to_install
        from drift.install_repo import run_primitive_5_install_deployment
        
        pkg = "pkg_templating"
        self.workspace_config.packages_enable[pkg] = True
        
        # 1. Setup envsubst template for mustache input
        # Note: drift_root/config/ contains the source templates for engine inputs
        envsubst_input_src = self.drift_root / "config" / "mustache.envst.json"
        # mustache requires valid JSON.
        envsubst_input_src.write_text('{"user": "$USER"}')
        
        # 2. Setup mustache template that uses this input
        pkg_src = self.source_dir / pkg
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"\n')
        (pkg_src / "greet.mustache.txt").write_text("Hello {{user}}!")
        
        # 3. Set environment variable
        os.environ["USER"] = "drift_tester"
        
        # 4. Deploy
        run_primitive_2_render_packages(self.workspace_config)
        
        # DEBUG: Check rendered mustache input
        mustache_json = self.render_dir / "config" / "mustache.json"
        if mustache_json.exists():
            print(f"DEBUG: rendered mustache.json content: {mustache_json.read_text()}")
        else:
            print("DEBUG: rendered mustache.json DOES NOT EXIST")
            print(f"DEBUG: render/config contains: {os.listdir(self.render_dir / 'config') if (self.render_dir / 'config').exists() else 'N/A'}")
            
        run_primitive_4_stage_render_to_install(self.workspace_config)
        run_primitive_5_install_deployment(self.workspace_config)
        
        # 5. Verify result
        target_file = self.system_target_dir / "greet.txt"
        self.assertTrue(target_file.exists())
        self.assertEqual(target_file.read_text(), "Hello drift_tester!")

if __name__ == "__main__":
    unittest.main()
