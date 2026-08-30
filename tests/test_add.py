import unittest
import os
import shutil
import tempfile
from pathlib import Path
from drift.workspace_config import WorkspaceConfig
from drift.add_resource import run_primitive_11_add_resources
from drift.constants import PACKAGE_CONFIG_FILE_NAME

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

        from drift.workspace_config import RenderEngineConfig
        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
            backup_directory=Path("backup"),
            default_target_directory=self.system_target_dir,
            packages_enable={},
            render_engine_config={
                "envsubst": RenderEngineConfig(
                    name="envsubst",
                    input_file=Path("env.sh"),
                    suffix="envst",
                    render_command="bash -c 'source %i && envsubst < %s'"
                )
            }
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
        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
        
        # 3. Verify translation in src/
        imported_file = pkg_src_dir / "dot-bashrc"
        self.assertTrue(imported_file.exists())
        self.assertEqual(imported_file.read_text(), "alias hi='echo hello'")

    def test_add_directory_recursive(self):
        """Verifies importing a directory recursively with translation."""
        pkg = "pkg_dir"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)
        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(f'[package]\nname="{pkg}"')
        
        # 1. Create directory structure on system
        target_dir = self.system_target_dir / ".config" / "nvim"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "init.vim").write_text("set number")
        (target_dir / ".hidden").write_text("secret")
        
        # 2. Add to drift
        run_primitive_11_add_resources(self.workspace_config, pkg, [self.system_target_dir / ".config"])
        
        # 3. Verify recursive translation
        self.assertTrue((pkg_src_dir / "dot-config" / "nvim" / "init.vim").exists())
        self.assertTrue((pkg_src_dir / "dot-config" / "nvim" / "dot-hidden").exists())
        self.assertEqual((pkg_src_dir / "dot-config" / "nvim" / "init.vim").read_text(), "set number")

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

        scripts_dir = pkg_src_dir / "scripts"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "pre_add.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'STATIC_HOOK_RAN' > add_hook_out.txt\n"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="scripts/pre_add.sh"\n'
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # Hook must have run and generated add_hook_out.txt
        hook_out = pkg_src_dir / "add_hook_out.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text().strip(), "STATIC_HOOK_RAN")

        # Copied static hook must exist in render/
        rendered_hook = self.render_dir / pkg / "scripts" / "pre_add.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn("STATIC_HOOK_RAN", rendered_hook.read_text())

    def test_add_triggers_pre_source_hook_templated(self):
        """Verifies that a templated pre_source hook is rendered and triggered in src/pkg before importing resources."""
        if not shutil.which("envsubst"):
            self.skipTest("envsubst command is not available on this system")

        pkg = "pkg_add_template_hook"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = pkg_src_dir / "scripts"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "pre_add.envst.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo \"ADD_HOOK_RAN_${drift_package_name}\" > add_hook_out.txt\n"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="scripts/pre_add.envst.sh"\n'
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # Hook must have run and generated add_hook_out.txt
        hook_out = pkg_src_dir / "add_hook_out.txt"
        self.assertTrue(hook_out.is_file())
        self.assertEqual(hook_out.read_text().strip(), f"ADD_HOOK_RAN_{pkg}")

        # Rendered hook must exist in render/
        rendered_hook = self.render_dir / pkg / "scripts" / "pre_add.sh"
        self.assertTrue(rendered_hook.is_file())
        self.assertIn(f"ADD_HOOK_RAN_{pkg}", rendered_hook.read_text())

    def test_add_pre_source_hook_failure_aborts_add(self):
        """Verifies that an error in pre_source hook is not suppressed and aborts add."""
        pkg = "pkg_add_failing_hook"
        pkg_src_dir = self.source_dir / pkg
        pkg_src_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = pkg_src_dir / "scripts"
        scripts_dir.mkdir()
        hook_script = scripts_dir / "failing.sh"
        hook_script.write_text(
            "#!/bin/bash\n"
            "echo 'Fatal add hook error' >&2\n"
            "exit 1\n"
        )
        hook_script.chmod(0o755)

        (pkg_src_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname="{pkg}"\n\n[hooks]\npre_source="scripts/failing.sh"\n'
        )

        target_file = self.system_target_dir / "imported_file.txt"
        target_file.write_text("imported content")

        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])
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
        target_file.write_text("sub_app_setting=1\n")

        run_primitive_11_add_resources(self.workspace_config, pkg, [target_file])

        # File must be imported inside src/pkg_add_subfolder/dotfiles/dot-config/sub_app.conf
        imported_file = subfolder_dir / "dot-config" / "sub_app.conf"
        self.assertTrue(imported_file.is_file())
        self.assertEqual(imported_file.read_text(), "sub_app_setting=1\n")
        # Ensure it was not imported at root of package
        self.assertFalse((pkg_src_dir / "dot-config" / "sub_app.conf").exists())


if __name__ == "__main__":
    unittest.main()
