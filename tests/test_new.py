import unittest
import tempfile
from pathlib import Path
from drift.workspace_config import WorkspaceConfig
from drift.new_package import run_primitive_10_create_new_package
from drift.constants import PACKAGE_CONFIG_FILE_NAME

class TestNewPackage(unittest.TestCase):
    def test_run_primitive_10_create_new_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root_path=drift_root, source_directory=Path("src"))
            
            pkg_name = "test_pkg"
            pkg_dir = run_primitive_10_create_new_package(config, pkg_name)
            
            self.assertTrue(pkg_dir.exists())
            self.assertTrue(pkg_dir.is_dir())
            self.assertEqual(pkg_dir.name, pkg_name)
            
            # Default should be drift_package.toml per spec
            config_file = pkg_dir / "drift_package.toml"
            self.assertTrue(config_file.exists())
            self.assertTrue(config_file.is_file())
            
            content = config_file.read_text()
            self.assertIn(f'name = "{pkg_name}"', content)
            self.assertIn('# target_directory = "~"', content)

    def test_run_primitive_10_create_new_package_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root_path=drift_root, source_directory=Path("src"))
            
            pkg_name = "existing_pkg"
            pkg_dir = src_dir / pkg_name
            pkg_dir.mkdir()
            (pkg_dir / "drift_package.toml").touch()
            
            with self.assertRaises(FileExistsError):
                run_primitive_10_create_new_package(config, pkg_name)
            
            # Should succeed with force
            run_primitive_10_create_new_package(config, pkg_name, force=True)
            content = (pkg_dir / "drift_package.toml").read_text()
            self.assertIn(f'name = "{pkg_name}"', content)

    def test_run_primitive_10_create_new_package_custom_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root_path=drift_root, source_directory=Path("src"))
            
            pkg_name = "custom_pkg"
            target_dir = "~/.config/nvim"
            pkg_dir = run_primitive_10_create_new_package(config, pkg_name, target_directory=target_dir)
            
            config_file = pkg_dir / "drift_package.toml"
            self.assertTrue(config_file.exists())
            
            content = config_file.read_text()
            self.assertIn(f'target_directory = "{target_dir}"', content)

if __name__ == "__main__":
    unittest.main()
