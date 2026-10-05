import unittest
import os
import shutil
import tempfile
from pathlib import Path
from drift.config.workspace_config import WorkspaceConfig
from drift.hooks.lifecycle_hooks import HookExecFlags
from drift.primitives.add_resource import (
    run_primitive_11_add_resources,
    prepare_add_resources,
    execute_add_resources,
    plan_add_resources,
    AddResourcePlan,
)
from drift.core.file_action import FileActionType
from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
)

class TestAddResource(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name).resolve()
        
        self.drift_root = self.base_path / "drift_workspace"
        self.system_target_dir = self.base_path / "system_home"
        
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"
        
        for d in [self.source_dir, self.render_dir, self.install_dir, self.backup_dir, self.system_target_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        config_dir = self.drift_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "env.sh").write_text("#!/bin/bash\n", encoding="utf-8")

        from drift.config.workspace_config import WorkspaceSectionConfig
        from drift.config.render_engine_config import RenderEngineConfig, RenderEngineRegistry
        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir,
            ),
            packages_enable={},
            render_engine_configs=RenderEngineRegistry({
                "envsubst": RenderEngineConfig(
                    name="envsubst",
                    input_file=config_dir / "env.sh",
                    suffix="envst",
                    render_command="bash -c 'source %i && envsubst < %s'"
                )
            })
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_basic_file(self):
        """Verifies importing a basic file with dot-prefix translation."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create file on system
        target_file = self.system_target_dir / ".bashrc"
        target_file.write_text("alias hi='echo hello'")
        
        # 2. Add to drift
        res = run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.package, pkg)
        self.assertEqual(res.imported_files, [str(target_file.resolve())])
        self.assertFalse(res.dry_run)
        self.assertIsInstance(res.plan, AddResourcePlan)
        self.assertEqual(res.plan.package, pkg)
        self.assertEqual(len(res.plan.actions), 1)
        
        # 3. Verify translation in src/
        imported_file = pkg_src_dir / "dot-bashrc"
        self.assertTrue(imported_file.exists())
        self.assertEqual(imported_file.read_text(encoding="utf-8"), "alias hi='echo hello'")

    def test_add_directory_recursive(self):
        """Verifies importing a directory recursively with translation."""
        pkg = "pkg_dir"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"', encoding="utf-8")
        
        # 1. Create directory structure on system
        target_dir = self.system_target_dir / ".config" / "nvim"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "init.vim").write_text("set number", encoding="utf-8")
        (target_dir / ".hidden").write_text("secret", encoding="utf-8")
        
        # 2. Add to drift
        res = run_primitive_11_add_resources(self.workspace_config, pkg, [self.system_target_dir / ".config"])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.package, pkg)
        self.assertEqual(len(res.imported_files), 2)
        self.assertFalse(res.dry_run)
        
        # 3. Verify recursive translation
        self.assertTrue((pkg_src_dir / "dot-config" / "nvim" / "init.vim").exists())
        self.assertTrue((pkg_src_dir / "dot-config" / "nvim" / "dot-hidden").exists())
        self.assertEqual((pkg_src_dir / "dot-config" / "nvim" / "init.vim").read_text(encoding="utf-8"), "set number")

    def test_add_conflict_detection(self):
        """Verifies that add fails if a conflicting source already exists."""
        pkg = "pkg_conflict"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create existing template in src/
        (pkg_src_dir / "dot-bashrc.envst").write_text("template content")
        
        # 2. Create file on system
        target_file = self.system_target_dir / ".bashrc"
        target_file.write_text("system content")
        
        # 3. Add should raise RuntimeError due to conflict with .envst template
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-bashrc.envst", str(ctx.exception))

    def test_add_outside_target_dir(self):
        """Verifies that add fails for paths outside the target directory."""
        pkg = "pkg_a"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        
        outside_file = self.base_path / "outside.txt"
        outside_file.write_text("I am outside")
        
        with self.assertRaises(ValueError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [outside_file])
        self.assertIn("not inside package target directory", str(ctx.exception))

    def test_add_parent_blocked_by_template(self):
        """Verifies that add fails if a parent directory of the import is blocked by a template file in src/."""
        pkg = "pkg_blocked"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create a template in src/ that renders to a file that blocks our directory import
        # e.g. src/pkg_blocked/dot-config.envst -> renders to ~/.config (file)
        (pkg_src_dir / "dot-config.envst").write_text("template content")
        
        # 2. Try to import a file inside ~/.config/
        target_file = self.system_target_dir / ".config" / "nvim" / "init.vim"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("content")
        
        # 3. Add should raise RuntimeError because dot-config.envst (file) blocks .config/ (directory)
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-config.envst", str(ctx.exception))

    def test_add_directory_blocked_by_template_file(self):
        """Verifies that add fails if a directory being imported is blocked by a template file in src/."""
        pkg = "pkg_dir_blocked"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create a template in src/ that renders to a file that blocks our directory import
        # e.g. src/pkg_dir_blocked/dot-ssh.envst -> renders to ~/.ssh (file)
        (pkg_src_dir / "dot-ssh.envst").write_text("template content")
        
        # 2. Try to import a directory ~/.ssh/
        target_dir = self.system_target_dir / ".ssh"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "id_rsa.pub").write_text("public key")
        
        # 3. Add should raise RuntimeError because dot-ssh.envst (file) blocks .ssh/ (directory)
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_dir])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-ssh.envst", str(ctx.exception))

    def test_add_directory_blocked_by_static_file(self):
        """Verifies that add fails if a directory being imported is blocked by a static file in src/."""
        pkg = "pkg_static_blocked"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create a static file in src/ that blocks our directory import
        (pkg_src_dir / "dot-vim").write_text("static file")
        
        # 2. Try to import a directory ~/.vim/
        target_dir = self.system_target_dir / ".vim"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "vimrc").write_text("vim config")
        
        # 3. Add should raise RuntimeError because dot-vim (file) blocks .vim/ (directory)
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_dir])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-vim", str(ctx.exception))

    def test_add_file_blocked_by_directory(self):
        """Verifies that add fails if a file being imported is blocked by a directory in src/."""
        pkg = "pkg_dir_blocks_file"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create a directory in src/
        (pkg_src_dir / "dot-bashrc").mkdir(parents=True, exist_ok=True)
        
        # 2. Try to import a file ~/.bashrc
        target_file = self.system_target_dir / ".bashrc"
        target_file.write_text("system content")
        
        # 3. Add should raise RuntimeError because dot-bashrc (directory) blocks .bashrc (file)
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-bashrc", str(ctx.exception))

    def test_add_triggers_pre_source_hook_static(self):
        """Verifies that a static pre_source hook is triggered in src/pkg before importing resources."""
        pkg = "pkg_add_static_hook"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = pkg_src_dir / "drift_hooks"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "pre_add.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'STATIC_HOOK_RAN' > add_hook_out.txt\n"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="drift_hooks/pre_add.sh"\n'
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # Hook must have run and generated add_hook_out.txt
        hook_out = self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "add_hook_out.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), "STATIC_HOOK_RAN")

        # Copied static hook must exist in render/.drift/hooks/
        rendered_hook = self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "pre_add.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn("STATIC_HOOK_RAN", rendered_hook.read_text(encoding="utf-8"))

    def test_add_triggers_pre_source_hook_templated(self):
        """Verifies that a templated pre_source hook is rendered and triggered in src/pkg before importing resources."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        pkg = "pkg_add_template_hook"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = pkg_src_dir / "drift_hooks"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "pre_add.envst.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo \"ADD_HOOK_RAN_${drift_package_name}\" > add_hook_out.txt\n",
            encoding="utf-8"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="drift_hooks/pre_add.sh"\n',
            encoding="utf-8"
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content", encoding="utf-8")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # Hook must have run and generated add_hook_out.txt
        hook_out = self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "add_hook_out.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text(encoding="utf-8").strip(), f"ADD_HOOK_RAN_{pkg}")

        # Rendered hook must exist in render/.drift/hooks/
        rendered_hook = self.render_dir / pkg / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME / "pre_add.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn(f"ADD_HOOK_RAN_{pkg}", rendered_hook.read_text(encoding="utf-8"))

    def test_add_pre_source_hook_failure_aborts_add(self):
        """Verifies that an error in pre_source hook is not suppressed and aborts add."""
        pkg = "pkg_add_failing_hook"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = pkg_src_dir / "drift_hooks"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'Fatal add hook error' >&2\n"
            "exit 1\n"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="drift_hooks/failing.sh"\n'
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content")

        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(
                self.workspace_config, pkg, [target_file], flags=HookExecFlags(streaming=False)
            )
        self.assertIn("failed with exit code 1", str(ctx.exception))

    def test_add_with_subfolder_source_directory(self):
        """Verifies that adding a resource imports files into the configured source_directory subfolder."""
        pkg = "pkg_add_subfolder"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        subfolder_dir = pkg_src_dir / "dotfiles"
        subfolder_dir.mkdir(parents=True, exist_ok=True)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\nsource_directory="dotfiles"\n'
        )

        target_file = self.system_target_dir / ".config" / "sub_app.conf"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("sub_app_setting=1\n", encoding="utf-8")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # File must be imported inside src/pkg_add_subfolder/dotfiles/dot-config/sub_app.conf
        imported_file = subfolder_dir / "dot-config" / "sub_app.conf"
        self.assertTrue(imported_file.is_file())
        self.assertEqual(imported_file.read_text(encoding="utf-8"), "sub_app_setting=1\n")
        # Ensure it was not imported at root of package
        self.assertFalse((pkg_src_dir / "dot-config" / "sub_app.conf").exists())

    def test_add_conflict_detection_with_package_level_render_engine(self):
        """Verifies that add detects conflicts with templates matching package-level-only render engines."""
        pkg = "pkg_pkg_engine_conflict"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        data_file = pkg_src_dir / "custom.json"
        data_file.write_text('{"key": "val"}', encoding="utf-8")

        # Package defines [render.custom] which is NOT present in workspace_config
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
[package]
name = "{pkg}"
enable_render = true

[render.custom]
input_file = "custom.json"
suffix = "custom"
render_command = "bash -c 'cat %i %s'"
""", encoding="utf-8")
        # Existing template in src matching package-level engine
        (pkg_src_dir / "dot-bashrc.custom").write_text("templated custom bashrc", encoding="utf-8")

        # Create target file on system
        target_file = self.system_target_dir / ".bashrc"
        target_file.write_text("system bashrc content", encoding="utf-8")

        # Add must detect conflict with dot-bashrc.custom
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertIn("Conflict detected", str(ctx.exception))
        self.assertIn("dot-bashrc.custom", str(ctx.exception))

    def test_add_with_package_level_render_engine_success(self):
        """Verifies that add succeeds in importing non-conflicting files into a package with package-level render engines."""
        pkg = "pkg_pkg_engine_success"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        data_file = pkg_src_dir / "custom.json"
        data_file.write_text('{"key": "val"}', encoding="utf-8")

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
[package]
name = "{pkg}"
enable_render = true

[render.custom]
input_file = "custom.json"
suffix = "custom"
render_command = "bash -c 'cat %i %s'"
""", encoding="utf-8")
        target_file = self.system_target_dir / ".zshrc"
        target_file.write_text("alias z='echo zsh'", encoding="utf-8")

        res = run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.package, pkg)
        self.assertTrue((pkg_src_dir / "dot-zshrc").is_file())
        self.assertEqual((pkg_src_dir / "dot-zshrc").read_text(encoding="utf-8"), "alias z='echo zsh'")

    def test_prepare_and_execute_plan_decomposition(self):
        """Verifies decoupled plan preparation and execution sub-stages."""
        pkg = "pkg_plan_exec"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"', encoding="utf-8")

        target_file = self.system_target_dir / ".vimrc"
        target_file.write_text("set nocompatible\n", encoding="utf-8")

        # 1. Prepare Plan (Read-Only)
        plan = prepare_add_resources(self.workspace_config, pkg, [target_file])
        self.assertIsInstance(plan, AddResourcePlan)
        self.assertEqual(plan.package, pkg)
        self.assertTrue(plan.has_changes)
        self.assertEqual(len(plan), 1)

        action = plan.actions[0]
        self.assertEqual(action.action_type, FileActionType.CREATE_COPY)
        self.assertEqual(action.src_path, target_file.resolve())
        self.assertEqual(action.dst_path, pkg_src_dir / "dot-vimrc")

        # Plan formatting check
        plan_text = plan.format_text()
        self.assertIn(f"Package '{pkg}':", plan_text)
        self.assertIn("CREATE_COPY", plan_text)
        self.assertIn("dot-vimrc", plan_text)

        # Ensure no mutations occurred during planning
        self.assertFalse((pkg_src_dir / "dot-vimrc").exists())

        # 2. Execute Plan (Physical State Mutation)
        res = execute_add_resources(self.workspace_config, plan)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.package, pkg)
        self.assertEqual(res.imported_files, [str(target_file.resolve())])
        self.assertFalse(res.dry_run)
        self.assertIs(res.plan, plan)

        # Verify physical file in src/
        imported_dest = pkg_src_dir / "dot-vimrc"
        self.assertTrue(imported_dest.is_file())
        self.assertEqual(imported_dest.read_text(encoding="utf-8"), "set nocompatible\n")

        # Formatted output check
        formatted = res.format_text()
        self.assertIn(f"Successfully imported 1 file(s) into package '{pkg}'", formatted)

    def test_add_dry_run_simulation(self):
        """Verifies dry-run simulation mode leaves source repository untouched."""
        pkg = "pkg_dry_run"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"', encoding="utf-8")

        target_file = self.system_target_dir / ".tmux.conf"
        target_file.write_text("set -g mouse on\n", encoding="utf-8")

        # Run with dry_run=True
        res = run_primitive_11_add_resources(self.workspace_config, pkg, [target_file], dry_run=True)
        self.assertEqual(res.status, "SUCCESS")
        self.assertTrue(res.dry_run)
        self.assertEqual(res.imported_files, [str(target_file.resolve())])

        # Verify zero filesystem mutations in src/
        self.assertFalse((pkg_src_dir / "dot-tmux.conf").exists())

        # Check dry-run formatted output
        text = res.format_text()
        self.assertIn("[DRY-RUN]", text)
        self.assertIn("CREATE_COPY", text)
        self.assertIn("dot-tmux.conf", text)
        self.assertIn("1 file(s) would be imported", text)

    def test_add_empty_worklist_plan_and_result(self):
        """Verifies handling when no files match the import path."""
        pkg = "pkg_empty"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"', encoding="utf-8")

        # Empty directory on system
        empty_dir = self.system_target_dir / "empty_dir"
        empty_dir.mkdir(parents=True, exist_ok=True)

        plan = prepare_add_resources(self.workspace_config, pkg, [empty_dir])
        self.assertFalse(plan.has_changes)
        self.assertEqual(len(plan), 0)
        self.assertIn("No resources to import", plan.format_text())

        res = execute_add_resources(self.workspace_config, plan)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.imported_files, [])
        self.assertIn("No resources to import", res.format_text())

    def test_cli_execute_add_dry_run(self):
        """Verifies CLI execute_add with dry_run prints dry run plan."""
        from io import StringIO
        from unittest.mock import patch
        from drift.cli.actions import execute_add

        pkg = "pkg_cli_dry_run"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"', encoding="utf-8")

        target_file = self.system_target_dir / ".gitconfig"
        target_file.write_text("[user]\nname = Test\n", encoding="utf-8")

        with patch("drift.cli.actions.assert_workspace_healthy"), \
             patch("drift.cli.actions.load_workspace_config_default", return_value=self.workspace_config), \
             patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            execute_add(
                drift_root=self.drift_root,
                package_name=pkg,
                import_paths=[str(target_file)],
                dry_run=True,
            )

        output = mock_stdout.getvalue()
        self.assertIn("[DRY-RUN]", output)
        self.assertIn("CREATE_COPY", output)
        self.assertIn("dot-gitconfig", output)
        self.assertFalse((pkg_src_dir / "dot-gitconfig").exists())


if __name__ == "__main__":
    unittest.main()

