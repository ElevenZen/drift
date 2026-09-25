"""Comprehensive unit and integration tests for drift_hooks/ lifecycle directory and .drift/hooks/ isolation."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from drift.core.constants import (
    DRIFT_HOOKS_DIR_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    FORBIDDEN_RENDER_ENGINE_SUFFIXES,
)
from drift.config.workspace_config import WorkspaceConfig, RenderEngineConfig
from drift.config.render_engine_config import RenderEngineRegistry
from drift.config.package_config import PackageConfig, PackageHooks
from drift.core.ignore import DriftIgnore
from drift.render.render_package import render_package
from drift.primitives.stage_repo import run_primitive_4_stage_render_to_install
from drift.primitives.install_repo import run_primitive_5_install_deployment, DeployOptions
from drift.core.exceptions import ConfigError
from drift.hooks.lifecycle_hooks import HookExecFlags, trigger_pre_source_hook


class TestDriftHooksIsolation(unittest.TestCase):
    """Tests for the dedicated drift_hooks/ source directory and .drift/hooks/ compilation sandbox."""

    def setUp(self) -> None:
        self.test_dir = tempfile.TemporaryDirectory()
        base_path = Path(self.test_dir.name).resolve()
        self.drift_root = base_path / "drift_workspace"
        self.target_dir = base_path / "target_home"

        # Create basic directory structure
        self.src_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.config_dir = self.drift_root / "config"

        self.src_dir.mkdir(parents=True)
        self.render_dir.mkdir(parents=True)
        self.install_dir.mkdir(parents=True)
        self.config_dir.mkdir(parents=True)
        self.target_dir.mkdir(parents=True)

        # Initialize git repositories for render and install
        from drift.utils.git_utils import git_init_repo
        git_init_repo(self.render_dir, "render")
        git_init_repo(self.install_dir, "install")

        # Create workspace config
        workspace_toml = self.config_dir / "drift_workspace.toml"
        workspace_toml.write_text(
            f"""[workspace]
default_target_directory = "{self.target_dir}"
default_install_method = "stow"

[packages.enable]
DEFAULT = true

[render.envst]
render_command = "internal"
suffix = "envst"
""",
            encoding="utf-8"
        )
        self.workspace_config = WorkspaceConfig.from_workspace_dir(self.drift_root)

    def tearDown(self) -> None:
        self.test_dir.cleanup()

    def test_drift_ignore_match_path_hardcodes_drift_internal_dir(self) -> None:
        """Verifies DriftIgnore.match_path hardcodes DRIFT_INTERNAL_DIR_NAME (.drift) as ignored."""
        ignore = DriftIgnore()
        self.assertTrue(ignore.match_path(Path(f"{DRIFT_INTERNAL_DIR_NAME}/hooks/post_install.sh")))
        self.assertTrue(ignore.match_path(Path(f"{DRIFT_INTERNAL_DIR_NAME}/sub/file.txt")))
        self.assertTrue(ignore.match_path(Path(DRIFT_INTERNAL_DIR_NAME)))
        self.assertFalse(ignore.match_path(Path("dot-config/app.conf")))
        self.assertFalse(ignore.match_path(Path("bin/my_tool")))

    def test_filter_deployable_files_skips_drift_internal_dir(self) -> None:
        """Verifies filter_deployable_files automatically excludes all files inside .drift/."""
        pkg_install_dir = self.install_dir / "test_pkg"
        pkg_install_dir.mkdir(parents=True)

        # Create payload files
        (pkg_install_dir / "dot-config").mkdir(parents=True)
        (pkg_install_dir / "dot-config" / "app.conf").write_text("setting=1\n", encoding="utf-8")
        (pkg_install_dir / "bin").mkdir(parents=True)
        (pkg_install_dir / "bin" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")

        # Create internal .drift/hooks files
        internal_hooks = pkg_install_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        internal_hooks.mkdir(parents=True)
        (internal_hooks / "post_install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (internal_hooks / "helper.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        ignore = DriftIgnore()
        deployable = ignore.filter_deployable_files(pkg_install_dir)
        deployable_posix = [p.as_posix() for p in deployable]

        self.assertIn("dot-config/app.conf", deployable_posix)
        self.assertIn("bin/tool", deployable_posix)
        self.assertNotIn(f"{DRIFT_INTERNAL_DIR_NAME}/hooks/post_install.sh", deployable_posix)
        self.assertNotIn(f"{DRIFT_INTERNAL_DIR_NAME}/hooks/helper.sh", deployable_posix)

    def test_forbidden_render_engine_suffixes(self) -> None:
        """Verifies defining a render engine with forbidden reserved suffixes raises ConfigError."""
        for suffix in FORBIDDEN_RENDER_ENGINE_SUFFIXES:
            engine = RenderEngineConfig(name="test", suffix=suffix, render_command="internal")
            with self.assertRaises(ConfigError) as ctx:
                engine.validate()
            self.assertIn(f"Render engine suffix '{suffix}' is a forbidden reserved keyword.", str(ctx.exception))

    def test_package_hooks_from_dict_and_assert_hooks_exist(self) -> None:
        """Verifies PackageHooks resolves drift_hooks/ paths into .drift/hooks/ and assert_hooks_exist validates both stages."""
        pkg_src = self.src_dir / "pkg_a"
        pkg_src.mkdir(parents=True)
        dh_src = pkg_src / DRIFT_HOOKS_DIR_NAME
        dh_src.mkdir(parents=True)
        (dh_src / "post_install.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        # 1. from_dict with workspace_config
        hooks = PackageHooks.from_dict(
            {"post_install": "drift_hooks/post_install.sh", "pre_source": "drift_hooks/pre_source.sh"},
            package_name="pkg_a",
            workspace_config=self.workspace_config
        )
        self.assertEqual(
            hooks.post_install,
            (self.install_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "post_install.sh").resolve()
        )
        self.assertEqual(
            hooks.pre_source,
            (self.render_dir / "pkg_a" / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "pre_source.sh").resolve()
        )
        self.assertEqual(hooks.get_relative_path("post_install"), Path("drift_hooks/post_install.sh"))

        # 2. assert_hooks_exist with is_source=True
        hooks.assert_hooks_exist(pkg_src, is_source=True, hook_names=["post_install"])

        # 3. assert_hooks_exist with is_source=False before staging raises FileNotFoundError
        pkg_install = self.install_dir / "pkg_a"
        pkg_install.mkdir(parents=True)
        with self.assertRaises(FileNotFoundError):
            hooks.assert_hooks_exist(pkg_install, is_source=False, hook_names=["post_install"])

        # Create staged file in .drift/hooks/
        dh_install = pkg_install / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        dh_install.mkdir(parents=True)
        (dh_install / "post_install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        hooks.assert_hooks_exist(pkg_install, is_source=False, hook_names=["post_install"])

    def test_render_and_deploy_drift_hooks_end_to_end(self) -> None:
        """End-to-end integration test:
        1. Package contains payload file and drift_hooks/ with entrypoint + sibling helper.
        2. Templated hook is rendered and non-templated helper is copied to .drift/hooks/.
        3. Hook executes with CWD as hook_path.parent, sources sibling helper, and writes to $drift_package_target_dir.
        4. Staging stages .drift/hooks/ to install/.
        5. Deployment applies payload to target_dir while completely omitting .drift/ and drift_hooks/.
        """
        pkg_src = self.src_dir / "my_app"
        pkg_src.mkdir(parents=True)

        # 1. Payload file
        (pkg_src / "dot-config").mkdir(parents=True)
        (pkg_src / "dot-config" / "my_app.conf").write_text("theme=dark\n", encoding="utf-8")

        # 2. drift_hooks source directory with helper and templated hook
        dh_src = pkg_src / DRIFT_HOOKS_DIR_NAME
        (dh_src / "lib").mkdir(parents=True)
        (dh_src / "lib" / "common.sh").write_text(
            '#!/bin/sh\nget_greeting() {\n    echo "HELLO_FROM_HELPER"\n}\n',
            encoding="utf-8"
        )
        (dh_src / "post_install.sh.envst").write_text(
            """#!/bin/sh
# Source sibling helper relative to hook script
. ./lib/common.sh
GREET=$(get_greeting)
# Write output into target directory via drift_package_target_dir
echo "\\$GREET - prefix: ${APP_PREFIX} - target is \\$drift_package_target_dir - name is \\$drift_package_name" > "\\$drift_package_target_dir/hook_output.txt"
""",
            encoding="utf-8"
        )

        # 3. Package config
        (pkg_src / "drift_package.toml").write_text(
            """[package]
name = "my_app"

[env.override]
APP_PREFIX = "APP_PREFIX_VAL"

[hooks]
post_install = "drift_hooks/post_install.sh"
""",
            encoding="utf-8"
        )

        # Step A: Render package
        render_res = render_package(self.workspace_config, pkg_src)
        self.assertEqual(render_res.status, "SUCCESS")

        render_pkg = self.render_dir / "my_app"
        self.assertTrue((render_pkg / "dot-config" / "my_app.conf").exists())
        self.assertTrue((render_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "post_install.sh").exists())
        self.assertTrue((render_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "lib" / "common.sh").exists())
        self.assertFalse((render_pkg / "drift_hooks").exists())

        # Step B: Stage package to install repo
        stage_res = run_primitive_4_stage_render_to_install(self.workspace_config)
        self.assertIn("my_app", stage_res)

        install_pkg = self.install_dir / "my_app"
        self.assertTrue((install_pkg / "dot-config" / "my_app.conf").exists())
        self.assertTrue((install_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "post_install.sh").exists())
        self.assertTrue((install_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "lib" / "common.sh").exists())

        # Step C: Deploy package to target
        deploy_res = run_primitive_5_install_deployment(
            self.workspace_config,
            packages_to_redeploy=["my_app"],
            options=DeployOptions(flags=HookExecFlags(streaming=False)),
        )
        self.assertEqual(deploy_res.status, "SUCCESS")

        # Step D: Verify target directory
        # Payload deployed
        self.assertTrue((self.target_dir / ".config" / "my_app.conf").exists())
        self.assertEqual((self.target_dir / ".config" / "my_app.conf").read_text(encoding="utf-8"), "theme=dark\n")

        # Hook output generated by post_install.sh
        self.assertTrue((self.target_dir / "hook_output.txt").exists())
        output_content = (self.target_dir / "hook_output.txt").read_text(encoding="utf-8").strip()
        self.assertIn("HELLO_FROM_HELPER", output_content)
        self.assertIn("prefix: APP_PREFIX_VAL", output_content)
        self.assertIn(f"target is {self.target_dir}", output_content)
        self.assertIn("name is my_app", output_content)

        # Control plane / lifecycle scripts are NOT deployed to target
        self.assertFalse((self.target_dir / ".drift").exists())
        self.assertFalse((self.target_dir / "drift_hooks").exists())
        self.assertFalse((self.target_dir / "post_install.sh").exists())

    def test_backward_compatibility_non_drift_hooks_scripts(self) -> None:
        """Verifies scripts in payload (e.g. bin/) render and deploy normally, and can be hooked via symlink."""
        pkg_src = self.src_dir / "cli_tool"
        pkg_src.mkdir(parents=True)
        (pkg_src / "bin").mkdir(parents=True)
        cli_script = pkg_src / "bin" / "my_cli"
        cli_script.write_text("#!/bin/sh\necho 'cli running'\n", encoding="utf-8")
        cli_script.chmod(0o755)

        # Create symlink inside drift_hooks/ to payload script
        dh_src = pkg_src / DRIFT_HOOKS_DIR_NAME
        dh_src.mkdir(parents=True)
        os.symlink("../bin/my_cli", dh_src / "post_install.sh")

        (pkg_src / "drift_package.toml").write_text(
            """[package]
name = "cli_tool"

[hooks]
post_install = "drift_hooks/post_install.sh"
""",
            encoding="utf-8"
        )

        render_res = render_package(self.workspace_config, pkg_src)
        self.assertEqual(render_res.status, "SUCCESS")

        render_pkg = self.render_dir / "cli_tool"
        self.assertTrue((render_pkg / "bin" / "my_cli").exists())
        self.assertTrue((render_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "post_install.sh").exists())

        stage_res = run_primitive_4_stage_render_to_install(self.workspace_config)
        self.assertIn("cli_tool", stage_res)

        deploy_res = run_primitive_5_install_deployment(
            self.workspace_config,
            packages_to_redeploy=["cli_tool"],
            options=DeployOptions(flags=HookExecFlags(streaming=False)),
        )
        self.assertEqual(deploy_res.status, "SUCCESS")

        # Payload script in bin/ IS deployed to target directory as a payload binary
        self.assertTrue((self.target_dir / "bin" / "my_cli").exists())
        # .drift is NOT deployed
        self.assertFalse((self.target_dir / ".drift").exists())

    def test_source_directory_subfolder_rendering_and_deployment(self) -> None:
        """End-to-end integration test with package source_directory subfolder:
        1. Package defines source_directory = "dotfiles".
        2. Payload resides in src/<pkg>/dotfiles/ (e.g. dot-config/app.conf, bin/tool).
        3. Lifecycle hooks reside in src/<pkg>/drift_hooks/ (not inside dotfiles/).
        4. render_package compiles dotfiles/ directly to render/<pkg>/ root and drift_hooks/ to .drift/hooks/.
        5. Deployment applies payload without dotfiles/ prefix while running post_install hook.
        """
        pkg_src = self.src_dir / "custom_subfolder_pkg"
        pkg_src.mkdir(parents=True)

        # 1. Subfolder payload
        dotfiles_dir = pkg_src / "dotfiles"
        (dotfiles_dir / "dot-config").mkdir(parents=True)
        (dotfiles_dir / "dot-config" / "sub_app.conf").write_text("mode=advanced\n", encoding="utf-8")
        (dotfiles_dir / "bin").mkdir(parents=True)
        (dotfiles_dir / "bin" / "sub_tool").write_text("#!/bin/sh\necho sub_tool\n", encoding="utf-8")

        # 2. drift_hooks source directory with sibling helper
        dh_src = pkg_src / DRIFT_HOOKS_DIR_NAME
        (dh_src / "lib").mkdir(parents=True)
        (dh_src / "lib" / "helper.sh").write_text(
            '#!/bin/sh\nwrite_marker() {\n    echo "SUBFOLDER_HOOK_RAN" > "$drift_package_target_dir/sub_marker.txt"\n}\n',
            encoding="utf-8"
        )
        (dh_src / "post_install.sh").write_text(
            """#!/bin/sh
. ./lib/helper.sh
write_marker
""",
            encoding="utf-8"
        )

        # 3. drift_package.toml
        (pkg_src / "drift_package.toml").write_text(
            """[package]
name = "custom_subfolder_pkg"
source_directory = "dotfiles"

[hooks]
post_install = "drift_hooks/post_install.sh"
""",
            encoding="utf-8"
        )

        # Render
        render_res = render_package(self.workspace_config, pkg_src)
        self.assertEqual(render_res.status, "SUCCESS")

        render_pkg = self.render_dir / "custom_subfolder_pkg"
        self.assertTrue((render_pkg / "dot-config" / "sub_app.conf").exists())
        self.assertTrue((render_pkg / "bin" / "sub_tool").exists())
        self.assertFalse((render_pkg / "dotfiles").exists())
        self.assertTrue((render_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "post_install.sh").exists())
        self.assertTrue((render_pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "lib" / "helper.sh").exists())

        # Stage
        stage_res = run_primitive_4_stage_render_to_install(self.workspace_config)
        self.assertIn("custom_subfolder_pkg", stage_res)

        install_pkg = self.install_dir / "custom_subfolder_pkg"
        self.assertTrue((install_pkg / "dot-config" / "sub_app.conf").exists())
        self.assertFalse((install_pkg / "dotfiles").exists())

        # Deploy
        deploy_res = run_primitive_5_install_deployment(
            self.workspace_config,
            packages_to_redeploy=["custom_subfolder_pkg"],
            options=DeployOptions(flags=HookExecFlags(streaming=False)),
        )
        self.assertEqual(deploy_res.status, "SUCCESS")

        # Verify deployed files in target_dir
        self.assertTrue((self.target_dir / ".config" / "sub_app.conf").exists())
        self.assertEqual((self.target_dir / ".config" / "sub_app.conf").read_text(encoding="utf-8"), "mode=advanced\n")
        self.assertTrue((self.target_dir / "bin" / "sub_tool").exists())
        self.assertTrue((self.target_dir / "sub_marker.txt").exists())
        self.assertEqual((self.target_dir / "sub_marker.txt").read_text(encoding="utf-8").strip(), "SUBFOLDER_HOOK_RAN")
        self.assertFalse((self.target_dir / "dotfiles").exists())
        self.assertFalse((self.target_dir / ".drift").exists())

    def test_from_workspace_and_package_dir_factories(self) -> None:
        """Verifies WorkspaceConfig.from_workspace_dir and PackageConfig.from_*_dir factory classmethods."""
        ws_cfg = WorkspaceConfig.from_workspace_dir(self.drift_root)
        self.assertEqual(ws_cfg.drift_root, self.drift_root)

        pkg_src = self.src_dir / "pkg_factories"
        pkg_src.mkdir(parents=True)
        (pkg_src / "drift_package.toml").write_text(
            """[package]
name = "pkg_factories"
install_method = "copy"
target_directory = "~/pkg_factories"
""",
            encoding="utf-8"
        )

        # 1. PackageConfig.from_source_dir
        pkg_src_cfg = PackageConfig.from_source_dir(pkg_src, ws_cfg)
        self.assertEqual(pkg_src_cfg.name, "pkg_factories")
        self.assertEqual(pkg_src_cfg.install_method, "copy")

        # 2. PackageConfig.from_render_dir
        render_res = render_package(ws_cfg, pkg_src)
        self.assertEqual(render_res.status, "SUCCESS")
        pkg_render = self.render_dir / "pkg_factories"
        pkg_render_cfg = PackageConfig.from_render_dir(pkg_render, self.workspace_config)
        self.assertEqual(pkg_render_cfg.name, "pkg_factories")
        self.assertEqual(pkg_render_cfg.install_method, "copy")

        # 3. PackageConfig.from_install_dir
        stage_res = run_primitive_4_stage_render_to_install(ws_cfg)
        self.assertIn("pkg_factories", stage_res)
        pkg_install = self.install_dir / "pkg_factories"
        pkg_install_cfg = PackageConfig.from_install_dir(pkg_install, ws_cfg)
        self.assertEqual(pkg_install_cfg.name, "pkg_factories")
        self.assertEqual(pkg_install_cfg.install_method, "copy")

    def test_drift_package_src_dir_alias(self) -> None:
        """Verifies drift_package_src_dir alias is present in envs and facts alongside drift_package_source_dir."""
        pkg_src = self.src_dir / "pkg_alias"
        pkg_src.mkdir(parents=True)
        (pkg_src / "drift_package.toml").write_text(
            """[package]
name = "pkg_alias"
""",
            encoding="utf-8"
        )
        pkg_cfg = PackageConfig.from_source_dir(pkg_src, self.workspace_config)

        with pkg_cfg.package_envs():
            self.assertIn("drift_package_src_dir", os.environ)
            self.assertIn("drift_package_source_dir", os.environ)
            self.assertEqual(os.environ["drift_package_src_dir"], os.environ["drift_package_source_dir"])
            self.assertEqual(os.environ["drift_package_src_dir"], str(pkg_src))

        # Test PackageHookContext facts
        from drift.hooks.package_hook import PackageHookContext
        ctx = PackageHookContext(
            config={},
            package_name="pkg_alias",
            package_dir=pkg_src,
            workspace_config=self.workspace_config,
            env=self.workspace_config.get_drift_package_facts("pkg_alias")
        )
        self.assertIn("drift_package_src_dir", ctx.package_facts)
        self.assertEqual(ctx.package_facts["drift_package_src_dir"], str(pkg_src))
        self.assertEqual(ctx.package_facts["drift_package_src_dir"], ctx.package_facts["drift_package_source_dir"])


if __name__ == "__main__":
    unittest.main()
