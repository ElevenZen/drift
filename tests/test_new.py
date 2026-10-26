import unittest
import tempfile
from pathlib import Path
from drift.config.workspace_config import WorkspaceConfig
from drift.primitives.new_package import run_primitive_10_create_new_package
from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    InstallMethod,
    get_default_package_config_content,
    get_default_drift_ignore_content,
)

class TestNewPackage(unittest.TestCase):
    def test_run_primitive_10_create_new_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root=drift_root)
            
            pkg_name = "test_pkg"
            res = run_primitive_10_create_new_package(config, pkg_name)
            pkg_dir = Path(res.package_dir)
            
            self.assertEqual(res.status, "SUCCESS")
            self.assertEqual(res.package, pkg_name)
            self.assertTrue(pkg_dir.exists())
            self.assertTrue(pkg_dir.is_dir())
            self.assertEqual(pkg_dir.name, pkg_name)
            
            # Default should be drift_package.toml per spec
            config_file = Path(res.config_file)
            self.assertTrue(config_file.exists())
            self.assertTrue(config_file.is_file())
            
            content = config_file.read_text(encoding="utf-8")
            self.assertIn(f'# src/{pkg_name}/drift_package.toml', content)
            self.assertIn('install_method = "symlink"', content)
            self.assertIn('# target_directory = "~"', content)
            self.assertIn('# target_directory_windows = "~"', content)
            self.assertIn('# source_directory = "."', content)
            self.assertIn('[hooks.windows]', content)

            # Default .drift_ignore should be generated
            ignore_file = pkg_dir / DRIFT_IGNORE_FILE_NAME
            self.assertTrue(ignore_file.exists())
            self.assertTrue(ignore_file.is_file())
            self.assertEqual(ignore_file.read_text(encoding="utf-8"), get_default_drift_ignore_content())

            # Default drift_package.py should be generated
            hook_file = pkg_dir / "drift_package.py"
            self.assertTrue(hook_file.exists())
            self.assertTrue(hook_file.is_file())
            hook_content = hook_file.read_text(encoding="utf-8")
            self.assertIn("def configure_package(context: PackageHookContext)", hook_content)
            self.assertIn("PackageHookContext", hook_content)

    def test_run_primitive_10_create_new_package_preserves_existing_drift_ignore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root=drift_root)
            
            pkg_name = "pkg_with_custom_ignore"
            pkg_dir = src_dir / pkg_name
            pkg_dir.mkdir()
            
            custom_ignore = pkg_dir / DRIFT_IGNORE_FILE_NAME
            custom_ignore.write_text("# Custom ignore rules\ncustom_rule/\n", encoding="utf-8")
            
            res = run_primitive_10_create_new_package(config, pkg_name)
            self.assertEqual(res.status, "SUCCESS")
            
            # Existing .drift_ignore should be preserved
            self.assertEqual(custom_ignore.read_text(encoding="utf-8"), "# Custom ignore rules\ncustom_rule/\n")

    def test_run_primitive_10_create_new_package_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root=drift_root)
            
            pkg_name = "existing_pkg"
            pkg_dir = src_dir / pkg_name
            pkg_dir.mkdir()
            (pkg_dir / "drift_package.toml").touch()
            
            with self.assertRaises(FileExistsError):
                run_primitive_10_create_new_package(config, pkg_name)
            
            # Should succeed with force
            res = run_primitive_10_create_new_package(config, pkg_name, force=True)
            self.assertEqual(res.status, "SUCCESS")
            content = (pkg_dir / "drift_package.toml").read_text(encoding="utf-8")
            self.assertIn(f'# src/{pkg_name}/drift_package.toml', content)

    def test_run_primitive_10_create_new_package_custom_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            src_dir = drift_root / "src"
            src_dir.mkdir()
            
            config = WorkspaceConfig(drift_root=drift_root)
            
            pkg_name = "custom_pkg"
            target_dir = "~/.config/nvim"
            res = run_primitive_10_create_new_package(config, pkg_name, target_directory=target_dir)
            pkg_dir = Path(res.package_dir)
            self.assertEqual(res.target_directory, target_dir)
            
            config_file = pkg_dir / "drift_package.toml"
            self.assertTrue(config_file.exists())
            
            content = config_file.read_text(encoding="utf-8")
            self.assertIn(f'target_directory = "{target_dir}"', content)

    def test_get_default_package_config_content_helper_direct(self) -> None:
        """Directly verifies get_default_package_config_content with default and custom arguments."""
        content_default = get_default_package_config_content("my_app")
        self.assertIn("# src/my_app/drift_package.toml", content_default)
        self.assertIn('install_method = "symlink"', content_default)
        self.assertIn('# target_directory = "~"', content_default)

        content_custom = get_default_package_config_content(
            package_name="zsh_pkg",
            install_method=InstallMethod.COPY,
            target_directory=Path("/etc/zsh")
        )
        self.assertIn("# src/zsh_pkg/drift_package.toml", content_custom)
        self.assertIn('install_method = "copy"', content_custom)
        self.assertIn('target_directory = "/etc/zsh"', content_custom)

    def test_default_toml_templates_have_zero_envsubst_vars_in_comments_and_render_cleanly(self) -> None:
        """Verifies that default TOML templates contain zero $VAR/${VAR} tokens in comments,
        preventing comment-breakout TOML injection and missing-variable RenderErrors when
        used as .envst.toml templates across cold and warm render passes.
        """
        import subprocess
        from drift.core.constants import (
            get_default_drift_workspace_toml_content,
            set_test_mode,
        )
        from drift.config.package_config import PackageConfig
        from drift.primitives.render_repo import RenderOptions, run_primitive_2_render_packages
        from drift.utils.env_utils import ENV_VAR_PATTERN, env_scope, python_envsubst
        from drift.core.exceptions import RenderError

        set_test_mode(True)

        pkg_template = get_default_package_config_content("envst_pkg", config_filename="drift_package.envst.toml")
        ws_template = get_default_drift_workspace_toml_content()

        # 1. Assert zero ENV_VAR_PATTERN matches in any '#' comment line of both default TOML templates
        pkg_comment_matches = [
            (idx + 1, line)
            for idx, line in enumerate(pkg_template.splitlines())
            if line.lstrip().startswith("#") and ENV_VAR_PATTERN.search(line)
        ]
        ws_comment_matches = [
            (idx + 1, line)
            for idx, line in enumerate(ws_template.splitlines())
            if line.lstrip().startswith("#") and ENV_VAR_PATTERN.search(line)
        ]
        self.assertEqual(pkg_comment_matches, [], f"Found $VAR in drift_package_default.toml comments: {pkg_comment_matches}")
        self.assertEqual(ws_comment_matches, [], f"Found $VAR in drift_workspace_default.toml comments: {ws_comment_matches}")

        # 2. Verify python_envsubst on drift_package.envst.toml with an empty environment and with malicious multiline env vars
        rendered_empty_env = python_envsubst(pkg_template, error_cls=RenderError, env={})
        self.assertEqual(rendered_empty_env, pkg_template)

        malicious_env = {
            "PATH": '/usr/bin\n[package]\nsudo = true\nhook_file = "evil.py"',
            "VAR": 'injected\n[package]\nsudo = true',
            "HOME": '/tmp/home\n[package]\nsudo = true',
            "THEME": 'dark\n[package]\nsudo = true',
        }
        rendered_malicious = python_envsubst(pkg_template, error_cls=RenderError, env=malicious_env)
        self.assertEqual(rendered_malicious, pkg_template)

        # 3. End-to-end two-pass (cold + warm) render test for a package created with template_suffix="envst"
        with tempfile.TemporaryDirectory() as temp_dir:
            drift_root = Path(temp_dir).resolve()
            list(map(lambda d: (drift_root / d).mkdir(), ("src", "config", "render", "install")))

            def _init_git(repo_dir: Path) -> None:
                subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)

            list(map(_init_git, (drift_root, drift_root / "render", drift_root / "install")))

            (drift_root / "config" / "drift_workspace.toml").write_text(
                ws_template.replace("DEFAULT = false", "DEFAULT = true"),
                encoding="utf-8",
            )
            ws_config = WorkspaceConfig.from_workspace_dir(drift_root)

            res_new = run_primitive_10_create_new_package(ws_config, "envst_pkg")
            envst_cfg_path = drift_root / "src" / "envst_pkg" / "drift_package.envst.toml"
            Path(res_new.config_file).rename(envst_cfg_path)
            (drift_root / "src" / "envst_pkg" / "app.conf").write_text("ok = true\n", encoding="utf-8")
            ws_config = WorkspaceConfig.from_workspace_dir(drift_root)

            with env_scope(malicious_env, overwrite=True):
                pkg_cfg = PackageConfig.from_source_dir(drift_root / "src" / "envst_pkg", ws_config)
                self.assertFalse(pkg_cfg.package.sudo)

                # Pass 1 (Cold): renders drift_package.envst.toml -> .drift/render/package/drift_package.toml & .drift/drift_package.toml
                res_cold = run_primitive_2_render_packages(ws_config, ["envst_pkg"], RenderOptions())
                self.assertEqual(res_cold.status, "SUCCESS")
                self.assertEqual(res_cold.packages[0].status, "SUCCESS")
                engine_rendered_toml = drift_root / "render" / "envst_pkg" / ".drift" / "render" / "package" / "drift_package.toml"
                stitched_pkg_toml = drift_root / "render" / "envst_pkg" / ".drift" / "drift_package.toml"
                self.assertTrue(engine_rendered_toml.is_file())
                self.assertTrue(stitched_pkg_toml.is_file())
                self.assertNotIn("evil.py", engine_rendered_toml.read_text(encoding="utf-8"))
                self.assertNotIn("evil.py", stitched_pkg_toml.read_text(encoding="utf-8"))

                # Pass 2 (Warm / Idempotent): returns UP_TO_DATE with zero re-renders
                res_warm = run_primitive_2_render_packages(ws_config, ["envst_pkg"], RenderOptions())
                self.assertEqual(res_warm.status, "SUCCESS")
                self.assertEqual(res_warm.packages[0].status, "UP_TO_DATE")


if __name__ == "__main__":
    unittest.main()
