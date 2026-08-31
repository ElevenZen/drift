import unittest
import tempfile
from pathlib import Path
from drift.workspace_config import WorkspaceConfig
from drift.new_package import run_primitive_10_create_new_package
from drift.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DEFAULT_PACKAGE_CONFIG_TEMPLATE,
    get_default_package_config_content,
    get_default_drift_ignore_content,
)

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
            self.assertIn(f'# src/{pkg_name}/drift_package.toml', content)
            self.assertIn('install_method = "stow"', content)
            self.assertIn('# target_directory = "~"', content)
            self.assertIn('# target_directory_windows = "~"', content)
            self.assertIn('# source_directory = "."', content)
            self.assertIn('[hooks.windows]', content)

            # Default .drift_ignore should be generated
            ignore_file = pkg_dir / DRIFT_IGNORE_FILE_NAME
            self.assertTrue(ignore_file.exists())
            self.assertTrue(ignore_file.is_file())
            self.assertEqual(ignore_file.read_text(encoding="utf-8"), get_default_drift_ignore_content())

    def test_run_primitive_10_create_new_package_preserves_existing_drift_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root_path=drift_root, source_directory=Path("src"))
            
            pkg_name = "pkg_with_custom_ignore"
            pkg_dir = src_dir / pkg_name
            pkg_dir.mkdir()
            
            custom_ignore = pkg_dir / DRIFT_IGNORE_FILE_NAME
            custom_ignore.write_text("# Custom ignore rules\ncustom_rule/\n", encoding="utf-8")
            
            run_primitive_10_create_new_package(config, pkg_name)
            
            # Existing .drift_ignore should be preserved
            self.assertEqual(custom_ignore.read_text(encoding="utf-8"), "# Custom ignore rules\ncustom_rule/\n")

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
            self.assertIn(f'# src/{pkg_name}/drift_package.toml', content)

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

    def test_get_default_package_config_content_helper_direct(self) -> None:
        """Directly verifies get_default_package_config_content with default and custom arguments."""
        content_default = get_default_package_config_content("my_app")
        self.assertIn("# src/my_app/drift_package.toml", content_default)
        self.assertIn('install_method = "stow"', content_default)
        self.assertIn('# target_directory = "~"', content_default)

        content_custom = get_default_package_config_content(
            package_name="zsh_pkg",
            install_method="copy",
            target_directory="/etc/zsh"
        )
        self.assertIn("# src/zsh_pkg/drift_package.toml", content_custom)
        self.assertIn('install_method = "copy"', content_custom)
        self.assertIn('target_directory = "/etc/zsh"', content_custom)


if __name__ == "__main__":
    unittest.main()
