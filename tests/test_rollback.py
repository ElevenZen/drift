import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from io import StringIO
from pathlib import Path

from unittest.mock import patch
from drift.core.constants import (
    PACKAGE_CONFIG_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DRIFT_INTERNAL_HOOKS_DIR_NAME,
    InstallMethod,
    set_test_mode,
)
from drift.core.file_action import (
    FileActionExecutionContext,
    FileActionType,
    NormalFileAction,
    BackupFileAction,
    execute_delivery_actions,
)
from drift.config.workspace_config import WorkspaceConfig, WorkspaceSectionConfig
from drift.core.state_registry import load_state_registry, save_state_registry
from drift.primitives.rollback_repo import run_primitive_8_rollback_recovery

set_test_mode(True, enable_logging=False)


class TestRollback(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_root = Path(self.temp_dir.name).resolve()
        self.drift_root = temp_root / "drift_workspace"
        self.system_target_dir = temp_root / "system_home"

        # Create workspace structures
        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"
        self.backup_dir = self.drift_root / "backup"

        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.system_target_dir.mkdir(parents=True, exist_ok=True)

        # Initialize install as an empty git repo
        subprocess.run(["git", "init"], cwd=self.install_dir, check=True, capture_output=True)
        # Configure dummy git user
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.install_dir, check=True, capture_output=True)

        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir
            ),
            packages_enable={
                "pkg_a": True,
            },
            packages_enable_default=False,
        )

        # 1. Setup pkg_a in source
        self.pkg_a_src = self.source_dir / "pkg_a"
        self.pkg_a_src.mkdir(parents=True, exist_ok=True)
        with open(self.pkg_a_src / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            """)

        # 2. Setup initial committed state in install/
        self.pkg_a_install = self.install_dir / "pkg_a"
        (self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        with open(self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "pkg_a"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            """)
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("clean content")

        # Initial state registry
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.sync_deployed_files(
            "pkg_a",
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            deployable_files=[Path("file.txt")],
        )
        save_state_registry(registry)

        # Initial commit in install repo
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial deploy"], cwd=self.install_dir, check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rollback_reverts_uncommitted_changes_and_reinstalls_clean_state(self) -> None:
        # Simulate a failed midway deployment/staging:
        # 1. We modify file.txt in install/ to represent dirty uncommitted state
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("dirty failed content")
            
        # 2. We add an untracked file to install/pkg_a/
        with open(self.pkg_a_install / "untracked.txt", "w", encoding="utf-8") as f:
            f.write("untracked")

        # 3. We update state.toml to "installing" (representing midway failure)
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        save_state_registry(registry)

        # 4. We also dirty the target system file
        with open(self.system_target_dir / "file.txt", "w", encoding="utf-8") as f:
            f.write("system dirty content")

        # Run rollback
        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.restored_packages, ["pkg_a"])

        # Verify:
        # 1. install/pkg_a/file.txt is restored to HEAD ("clean content")
        self.assertEqual((self.pkg_a_install / "file.txt").read_text(encoding="utf-8"), "clean content")

        # 2. install/pkg_a/untracked.txt is deleted (git clean)
        self.assertFalse((self.pkg_a_install / "untracked.txt").exists())

        # 3. target system file is re-deployed and matches "clean content"
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")

        # 4. state.toml is restored to "installed" state
        reloaded_registry = load_state_registry(state_file)
        self.assertEqual(reloaded_registry.get_package_state("pkg_a"), "installed")

    def test_rollback_first_time_package_uninstalls_and_restores_backups(self) -> None:
        """Verifies that a first-time package failing midway is cleanly uninstalled and backups restored on rollback."""
        pkg_first = "pkg_first_time"

        # 1. Setup in src/
        pkg_src = self.source_dir / pkg_first
        pkg_src.mkdir(parents=True, exist_ok=True)
        with open(pkg_src / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_first}"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            """)

        # 2. Setup in install/ (uncommitted, first-time stage)
        pkg_install = self.install_dir / pkg_first
        (pkg_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        with open(pkg_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
            f.write(f"""
            [package]
            name = "{pkg_first}"
            install_method = "copy"
            target_directory = "{self.system_target_dir.as_posix()}"
            """)
        with open(pkg_install / "app_config.json", "w", encoding="utf-8") as f:
            f.write('{"installed": true}')

        # 3. Simulate state.toml having recorded the package as "installing" with deployed_files
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg_first, "installing")
        registry.sync_deployed_files(
            pkg_first,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            deployable_files=[Path("app_config.json")],
        )
        save_state_registry(registry)

        # 4. Simulate an existing host file and execute delivery actions with WAL enabled
        (self.system_target_dir / "existing_host_file.txt").write_text("original user content", encoding="utf-8")
        pkg_backup_root = self.backup_dir / pkg_first
        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                BackupFileAction(
                    action_type=FileActionType.BACKUP_OVERWRITE,
                    src_path=self.system_target_dir / "existing_host_file.txt",
                    dst_path=pkg_backup_root / "overwritten" / "existing_host_file.txt",
                    reason="Colliding regular file",
                    backup_pkg_root=pkg_backup_root,
                ),
                NormalFileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=pkg_install / "app_config.json",
                    dst_path=self.system_target_dir / "app_config.json",
                ),
            ],
        )

        # Enable in workspace config
        workspace_cfg = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir
            ),
            packages_enable={
                pkg_first: True,
            },
            packages_enable_default=False,
        )

        # Execute rollback on first-time package
        res = run_primitive_8_rollback_recovery(workspace_cfg, [pkg_first], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn(pkg_first, res.restored_packages)

        # Verify:
        # 1. Newly delivered file on host target is removed
        self.assertFalse((self.system_target_dir / "app_config.json").exists())

        # 2. Original backed up file is restored to the host target
        self.assertTrue((self.system_target_dir / "existing_host_file.txt").exists())
        self.assertEqual(
            (self.system_target_dir / "existing_host_file.txt").read_text(encoding="utf-8"),
            "original user content"
        )

        # 3. install/pkg_first directory is removed
        self.assertFalse(pkg_install.exists())

        # 4. state.toml does not contain pkg_first
        reloaded_registry = load_state_registry(state_file)
        self.assertNotIn(pkg_first, reloaded_registry.packages)

    def test_rollback_mixed_first_time_and_existing_packages(self) -> None:
        """Verifies rollback correctly redeploys existing packages from HEAD while uninstalling first-time packages."""
        pkg_first = "pkg_brand_new"

        # Setup first-time package in src & install
        pkg_src = self.source_dir / pkg_first
        pkg_src.mkdir(parents=True, exist_ok=True)
        (pkg_src / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_first}"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")

        pkg_install = self.install_dir / pkg_first
        (pkg_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_first}"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"
        """, encoding="utf-8")
        (pkg_install / "brand_new.txt").write_text("brand new", encoding="utf-8")

        # Dirty existing pkg_a
        (self.pkg_a_install / "file.txt").write_text("corrupted pkg_a", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("corrupted pkg_a on host", encoding="utf-8")

        # Deliver new file on host with WAL enabled
        pkg_backup_root = self.backup_dir / pkg_first
        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                NormalFileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=pkg_install / "brand_new.txt",
                    dst_path=self.system_target_dir / "brand_new.txt",
                )
            ],
        )

        # Set both packages to "installing"
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        registry.set_package_state(pkg_first, "installing")
        registry.sync_deployed_files(
            pkg_first,
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            deployable_files=[Path("brand_new.txt")],
        )
        save_state_registry(registry)

        workspace_cfg = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(
                default_target_directory=self.system_target_dir
            ),
            packages_enable={
                "pkg_a": True,
                pkg_first: True,
            },
            packages_enable_default=False,
        )

        res = run_primitive_8_rollback_recovery(workspace_cfg, ["pkg_a", pkg_first], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(set(res.restored_packages), {"pkg_a", pkg_first})

        # Verify pkg_a is restored to clean committed state ("clean content")
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded = load_state_registry(state_file)
        self.assertEqual(reloaded.get_package_state("pkg_a"), "installed")

        # Verify pkg_first is uninstalled
        self.assertFalse((self.system_target_dir / "brand_new.txt").exists())
        self.assertFalse(pkg_install.exists())
        self.assertNotIn(pkg_first, reloaded.packages)

    def test_rollback_non_midway_package_raises_error_without_force(self) -> None:
        """Verifies that rollback raises RuntimeError when package is not in midway state unless force=True."""
        # pkg_a is currently in 'installed' state in setUp
        with self.assertRaises(RuntimeError) as ctx:
            run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertIn("The following packages are not in a failed midway/conflict state", str(ctx.exception))
        self.assertIn("pkg_a", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))

        # With force=True, it should proceed cleanly
        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=True)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.restored_packages, ["pkg_a"])

    def test_rollback_not_found_package_graceful_noop(self) -> None:
        """Verifies that rollback gracefully no-ops when target package is not found in install/."""
        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_never_staged"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.target_packages, ["pkg_never_staged"])
        self.assertEqual(res.restored_packages, [])

    def test_rollback_mixed_found_and_not_found_packages(self) -> None:
        """Verifies that rollback restores found midway packages while gracefully skipping not found packages."""
        # Dirty pkg_a and set to installing
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("dirty failed content")
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        save_state_registry(registry)

        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a", "pkg_never_staged"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.target_packages, ["pkg_a", "pkg_never_staged"])
        self.assertEqual(res.restored_packages, ["pkg_a"])

        # Verify pkg_a is restored
        self.assertEqual((self.pkg_a_install / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded_registry = load_state_registry(state_file)
        self.assertEqual(reloaded_registry.get_package_state("pkg_a"), "installed")

    def test_rollback_staged_package_succeeds_without_force(self) -> None:
        """Verifies that a package in 'staged' state (e.g. after Step 4a failure) can be rolled back without --force."""
        # Dirty pkg_a and set to 'staged'
        with open(self.pkg_a_install / "file.txt", "w", encoding="utf-8") as f:
            f.write("dirty staged content")
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "staged")
        save_state_registry(registry)

        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.restored_packages, ["pkg_a"])
        self.assertEqual((self.pkg_a_install / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded = load_state_registry(state_file)
        self.assertEqual(reloaded.get_package_state("pkg_a"), "installed")

    @patch("drift.primitives.rollback_repo.rollback_uninstalled_first_time_package")
    @patch("drift.primitives.rollback_repo.rollback_reinstall_committed_package")
    def test_rollback_unified_reverse_topological_order(self, mock_reinstall, mock_uninstall) -> None:
        """Verifies rollback executes in unified reverse topological order across mixed committed and first-time packages."""
        # Setup:
        # pkg_a is committed in HEAD (setUp already committed pkg_a)
        # pkg_b is a first-time package that depends on pkg_a
        # pkg_c is a first-time package that depends on pkg_b
        # All three failed midway in 'installing' state
        pkg_b_install = self.install_dir / "pkg_b"
        (pkg_b_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_b_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
            '[package]\nname = "pkg_b"\ndependencies = ["pkg_a"]\n',
            encoding="utf-8",
        )

        pkg_c_install = self.install_dir / "pkg_c"
        (pkg_c_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_c_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
            '[package]\nname = "pkg_c"\ndependencies = ["pkg_b"]\n',
            encoding="utf-8",
        )

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        registry.set_package_state("pkg_b", "installing")
        registry.set_package_state("pkg_c", "installing")
        save_state_registry(registry)

        call_order = []
        mock_reinstall.side_effect = lambda ws, pkg, flags=None: call_order.append(("reinstall", pkg))
        mock_uninstall.side_effect = lambda ws, pkg, flags=None: call_order.append(("uninstall", pkg))

        ws_cfg = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(default_target_directory=self.system_target_dir),
            packages_enable={"pkg_a": True, "pkg_b": True, "pkg_c": True},
            packages_enable_default=False,
        )

        # Call rollback with random order in arguments: ["pkg_a", "pkg_c", "pkg_b"]
        res = run_primitive_8_rollback_recovery(ws_cfg, ["pkg_a", "pkg_c", "pkg_b"], force=False)
        self.assertEqual(res.status, "SUCCESS")

        # Dependency chain: pkg_c -> pkg_b -> pkg_a
        # Unified reverse topological rollback order must be: pkg_c (uninstall) -> pkg_b (uninstall) -> pkg_a (reinstall)
        expected_calls = [
            ("uninstall", "pkg_c"),
            ("uninstall", "pkg_b"),
            ("reinstall", "pkg_a"),
        ]
        self.assertEqual(call_order, expected_calls)

    def test_rollback_sequential_commits_prevent_ping_pong_staged_state(self) -> None:
        """Verifies that rollback commits each restoration so subsequent rollbacks do not revert to HEAD."""
        # 1. Setup pkg_b and pkg_c in install/
        for pkg in ("pkg_b", "pkg_c"):
            pkg_install = self.install_dir / pkg
            (pkg_install / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
            with open(pkg_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME, "w", encoding="utf-8") as f:
                f.write(f'[package]\nname = "{pkg}"\ninstall_method = "copy"\ntarget_directory = "{self.system_target_dir.as_posix()}"\n')
            with open(pkg_install / f"{pkg}.txt", "w", encoding="utf-8") as f:
                f.write(f"{pkg} clean")

        # Initial commit of pkg_a, pkg_b, pkg_c in install repo
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_b", "installed")
        registry.set_package_state("pkg_c", "installed")
        save_state_registry(registry)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit all pkgs"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Stage pkg_a and pkg_b -> both become "staged"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "staged")
        registry.set_package_state("pkg_b", "staged")
        save_state_registry(registry)

        # 3. Simulate deploying pkg_c successfully, which committed pkg_c and state.toml (with pkg_a and pkg_b "staged")
        subprocess.run(["git", "add", "pkg_c", "state.toml"], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Deploy pkg_c"], cwd=self.install_dir, check=True, capture_output=True)

        # Verify HEAD now has pkg_a and pkg_b as "staged"
        head_state = subprocess.run(
            ["git", "-C", self.install_dir.as_posix(), "show", "HEAD:state.toml"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn('[packages.pkg_a]\nstate = "staged"', head_state)
        self.assertIn('[packages.pkg_b]\nstate = "staged"', head_state)

        ws_cfg = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(default_target_directory=self.system_target_dir),
            packages_enable={"pkg_a": True, "pkg_b": True, "pkg_c": True},
            packages_enable_default=False,
        )

        # 4. Rollback pkg_a only
        res_a = run_primitive_8_rollback_recovery(ws_cfg, ["pkg_a"], force=False)
        self.assertEqual(res_a.status, "SUCCESS")

        # Verify pkg_a is now committed to HEAD as "installed", and pkg_b remains "staged" in HEAD
        head_after_a = subprocess.run(
            ["git", "-C", self.install_dir.as_posix(), "show", "HEAD:state.toml"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn('[packages.pkg_a]\nstate = "installed"', head_after_a)
        self.assertIn('[packages.pkg_b]\nstate = "staged"', head_after_a)

        # 5. Now rollback pkg_b only
        res_b = run_primitive_8_rollback_recovery(ws_cfg, ["pkg_b"], force=False)
        self.assertEqual(res_b.status, "SUCCESS")

        # Verify BOTH pkg_a and pkg_b are now "installed" in HEAD (pkg_a did NOT get reverted!)
        head_after_b = subprocess.run(
            ["git", "-C", self.install_dir.as_posix(), "show", "HEAD:state.toml"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn('[packages.pkg_a]\nstate = "installed"', head_after_b)
        self.assertIn('[packages.pkg_b]\nstate = "installed"', head_after_b)

        # Disk state is also in sync
        current_reg = load_state_registry(state_file)
        self.assertEqual(current_reg.get_package_state("pkg_a"), "installed")
        self.assertEqual(current_reg.get_package_state("pkg_b"), "installed")

    def test_rollback_committed_package_preserves_original_backup_via_wal_and_incremental_reinstall(self) -> None:
        """Verifies 3-step rollback restores UPDATE_COPY, CREATE_COPY, BACKUP_PRUNE, and BACKUP_OVERWRITE without clobbering original backups."""
        from drift.primitives.install_repo import run_primitive_5_install

        # 1. Establish original user backup in backup/pkg_a/overwritten/file.txt and committed v1 with file.txt + extra.txt
        pkg_backup_root = self.backup_dir / "pkg_a"
        orig_backup = pkg_backup_root / "overwritten" / "file.txt"
        orig_backup.parent.mkdir(parents=True, exist_ok=True)
        orig_backup.write_text("original user host file v0", encoding="utf-8")

        (self.pkg_a_install / "file.txt").write_text("clean v1", encoding="utf-8")
        (self.pkg_a_install / "extra.txt").write_text("extra v1", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("clean v1", encoding="utf-8")
        (self.system_target_dir / "extra.txt").write_text("extra v1", encoding="utf-8")

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed")
        registry.sync_deployed_files(
            "pkg_a",
            target_directory=self.system_target_dir,
            install_method=InstallMethod.COPY,
            deployable_files=[Path("file.txt"), Path("extra.txt")],
        )
        save_state_registry(registry)
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit v1 with extra"], cwd=self.install_dir, check=True, capture_output=True)

        # 2. Simulate uncommitted v2 deployment that modifies file.txt, deletes extra.txt, adds new_dir/added.txt,
        #    collides with host file collide_v2 (blocking directory collide_v2/nested.txt), and fails in post_install hook
        (self.pkg_a_install / "file.txt").write_text("dirty v2", encoding="utf-8")
        (self.pkg_a_install / "extra.txt").unlink()
        (self.pkg_a_install / "new_dir").mkdir(parents=True, exist_ok=True)
        (self.pkg_a_install / "new_dir" / "added.txt").write_text("added in v2", encoding="utf-8")
        (self.pkg_a_install / "collide_v2").mkdir(parents=True, exist_ok=True)
        (self.pkg_a_install / "collide_v2" / "nested.txt").write_text("pkg collide v2", encoding="utf-8")
        (self.system_target_dir / "collide_v2").write_text("user collide v2 on host", encoding="utf-8")

        hooks_dir = self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_script = hooks_dir / "post_install.py"
        hook_script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        hook_script.chmod(0o755)
        (self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        post_install = "drift_hooks/post_install.py"
        post_update = "drift_hooks/post_install.py"
        """, encoding="utf-8")

        from drift.core.exceptions import HookExecutionError
        with self.assertRaises(HookExecutionError):
            run_primitive_5_install(self.workspace_config, ["pkg_a"])

        # Verify dirty midway state on host and in WAL before rollback
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "dirty v2")
        self.assertFalse((self.system_target_dir / "extra.txt").exists())
        self.assertTrue((self.system_target_dir / "new_dir" / "added.txt").is_file())
        self.assertTrue((self.system_target_dir / "collide_v2").is_dir())
        self.assertEqual((self.system_target_dir / "collide_v2" / "nested.txt").read_text(encoding="utf-8"), "pkg collide v2")
        self.assertTrue((pkg_backup_root / "wal" / "actions.json").is_file())

        # 3. Execute 3-step rollback
        res_rb = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res_rb.status, "SUCCESS")
        self.assertEqual(res_rb.restored_packages, ["pkg_a"])

        # Verify host state is 100% restored to v1:
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean v1")
        self.assertEqual((self.system_target_dir / "extra.txt").read_text(encoding="utf-8"), "extra v1")
        self.assertFalse((self.system_target_dir / "new_dir").exists())
        self.assertTrue((self.system_target_dir / "collide_v2").is_file())
        self.assertEqual((self.system_target_dir / "collide_v2").read_text(encoding="utf-8"), "user collide v2 on host")

        # Crucial invariant: original user backup in overwritten/file.txt was NEVER overwritten by "dirty v2"!
        self.assertEqual(orig_backup.read_text(encoding="utf-8"), "original user host file v0")
        self.assertFalse((pkg_backup_root / "wal").exists())

    def test_rollback_committed_package_restores_stashed_overwritten_and_deleted_backups(self) -> None:
        """Verifies that stashed backups in backup/<pkg>/wal/stash/{overwritten,deleted}/ are restored on rollback."""
        pkg_backup_root = self.backup_dir / "pkg_a"
        prior_overwritten = pkg_backup_root / "overwritten" / "shared.conf"
        prior_deleted = pkg_backup_root / "deleted" / "pruned.conf"
        prior_overwritten.parent.mkdir(parents=True, exist_ok=True)
        prior_deleted.parent.mkdir(parents=True, exist_ok=True)
        prior_overwritten.write_text("v0 overwritten backup", encoding="utf-8")
        prior_deleted.write_text("v0 deleted backup", encoding="utf-8")

        # Simulate midway transaction that overwrites both backup slots
        host_shared = self.system_target_dir / "shared.conf"
        host_pruned = self.system_target_dir / "pruned.conf"
        host_shared.write_text("v1 host shared", encoding="utf-8")
        host_pruned.write_text("v1 host pruned", encoding="utf-8")

        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                BackupFileAction(
                    action_type=FileActionType.BACKUP_OVERWRITE,
                    src_path=host_shared,
                    dst_path=prior_overwritten,
                    reason="Colliding file",
                    backup_pkg_root=pkg_backup_root,
                ),
                BackupFileAction(
                    action_type=FileActionType.BACKUP_PRUNE,
                    src_path=host_pruned,
                    dst_path=prior_deleted,
                    reason="Orphan prune",
                    backup_pkg_root=pkg_backup_root,
                ),
            ],
        )

        # Mark pkg_a as installing
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        save_state_registry(registry)

        # Verify stash exists before rollback
        self.assertTrue((pkg_backup_root / "wal" / "stash" / "overwritten" / "shared.conf").is_file())
        self.assertTrue((pkg_backup_root / "wal" / "stash" / "deleted" / "pruned.conf").is_file())

        # Rollback
        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")

        # Verify host files restored AND original v0 backups restored from stash
        self.assertEqual(host_shared.read_text(encoding="utf-8"), "v1 host shared")
        self.assertEqual(host_pruned.read_text(encoding="utf-8"), "v1 host pruned")
        self.assertEqual(prior_overwritten.read_text(encoding="utf-8"), "v0 overwritten backup")
        self.assertEqual(prior_deleted.read_text(encoding="utf-8"), "v0 deleted backup")
        self.assertFalse((pkg_backup_root / "wal").exists())

    def test_rollback_first_time_package_skips_all_hooks_and_cleans_empty_backup_dir(self) -> None:
        """Verifies first-time package rollback executes zero lifecycle hooks and cleans up empty backup/<pkg>/."""
        pkg_first = "pkg_first_no_hooks"
        pkg_install = self.install_dir / pkg_first
        hooks_dir = pkg_install / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        hooks_dir.mkdir(parents=True, exist_ok=True)

        marker_file = self.system_target_dir / "hook_ran_marker.txt"
        hook_script = hooks_dir / "any_hook.py"
        hook_script.write_text(
            f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({repr(marker_file.as_posix())}).write_text('ran')\n",
            encoding="utf-8",
        )
        hook_script.chmod(0o755)

        (pkg_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "{pkg_first}"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        pre_install = "drift_hooks/any_hook.py"
        post_install = "drift_hooks/any_hook.py"
        pre_uninstall = "drift_hooks/any_hook.py"
        post_uninstall = "drift_hooks/any_hook.py"
        """, encoding="utf-8")
        (pkg_install / "first.txt").write_text("first content", encoding="utf-8")

        pkg_backup_root = self.backup_dir / pkg_first
        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                NormalFileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=pkg_install / "first.txt",
                    dst_path=self.system_target_dir / "first.txt",
                )
            ],
        )

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg_first, "installing")
        save_state_registry(registry)

        ws_cfg = WorkspaceConfig(
            drift_root=self.drift_root,
            workspace=WorkspaceSectionConfig(default_target_directory=self.system_target_dir),
            packages_enable={pkg_first: True},
            packages_enable_default=False,
        )

        res = run_primitive_8_rollback_recovery(ws_cfg, [pkg_first], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse(marker_file.exists())
        self.assertFalse((self.system_target_dir / "first.txt").exists())
        self.assertFalse(pkg_install.exists())
        self.assertFalse(pkg_backup_root.exists())

    def test_rollback_committed_package_ignores_hook_errors_by_default(self) -> None:
        """Verifies Step 3 reinstall for committed packages ignores hook failures by default (raise_on_error=False)."""
        hooks_dir = self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        hooks_dir.mkdir(parents=True, exist_ok=True)
        failing_hook = hooks_dir / "fail_hook.py"
        failing_hook.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(42)\n", encoding="utf-8")
        failing_hook.chmod(0o755)

        (self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        pre_install = "drift_hooks/fail_hook.py"
        post_install = "drift_hooks/fail_hook.py"
        pre_update = "drift_hooks/fail_hook.py"
        post_update = "drift_hooks/fail_hook.py"
        """, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit failing hook in HEAD"], cwd=self.install_dir, check=True, capture_output=True)

        # Dirty pkg_a and mark installing
        (self.pkg_a_install / "file.txt").write_text("dirty", encoding="utf-8")
        (self.system_target_dir / "file.txt").write_text("dirty on host", encoding="utf-8")
        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        save_state_registry(registry)

        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.restored_packages, ["pkg_a"])
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded = load_state_registry(state_file)
        self.assertEqual(reloaded.get_package_state("pkg_a"), "installed")

    def test_rollback_committed_package_runs_hooks_when_wal_restores_all_files(self) -> None:
        """Verifies Step 3 executes pre_update/post_update hooks via run_hooks_when_unchanged=True even when Step 1 WAL restores all host files."""
        hooks_dir = self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / DRIFT_INTERNAL_HOOKS_DIR_NAME
        hooks_dir.mkdir(parents=True, exist_ok=True)
        marker_file = self.system_target_dir / "rollback_hook_ran.txt"
        update_hook = hooks_dir / "post_update.py"
        update_hook.write_text(
            f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({repr(marker_file.as_posix())}).write_text('hook_executed')\n",
            encoding="utf-8",
        )
        update_hook.chmod(0o755)

        (self.pkg_a_install / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(f"""
        [package]
        name = "pkg_a"
        install_method = "copy"
        target_directory = "{self.system_target_dir.as_posix()}"

        [hooks]
        post_update = "drift_hooks/post_update.py"
        """, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.install_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit post_update hook in HEAD"], cwd=self.install_dir, check=True, capture_output=True)

        # Simulate a failed deploy where WAL recorded a newly added file (so Step 1 WAL rollback
        # deletes the added file and leaves host files 100% identical to HEAD before Step 3 runs)
        pkg_backup_root = self.backup_dir / "pkg_a"
        added_src = self.pkg_a_install / "added_v2.txt"
        added_src.write_text("v2 added", encoding="utf-8")
        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                NormalFileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=added_src,
                    dst_path=self.system_target_dir / "added_v2.txt",
                )
            ],
        )

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installing")
        save_state_registry(registry)

        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertFalse((self.system_target_dir / "added_v2.txt").exists())
        self.assertTrue(marker_file.is_file())
        self.assertEqual(marker_file.read_text(encoding="utf-8"), "hook_executed")

    def test_rollback_falls_back_to_default_order_on_cyclic_dependencies_or_corrupt_config(self) -> None:
        """Verifies rollback succeeds using fallback order when uncommitted packages have cyclic dependencies or corrupt config."""
        for pkg, dep in (("pkg_cyc_1", "pkg_cyc_2"), ("pkg_cyc_2", "pkg_cyc_1")):
            pkg_dir = self.install_dir / pkg / DRIFT_INTERNAL_DIR_NAME
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / PACKAGE_CONFIG_FILE_NAME).write_text(
                f'[package]\nname = "{pkg}"\ndependencies = ["{dep}"]\n',
                encoding="utf-8",
            )
            self.workspace_config.packages_enable[pkg] = True

        pkg_corrupt = "pkg_corrupt"
        corrupt_dir = self.install_dir / pkg_corrupt / DRIFT_INTERNAL_DIR_NAME
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_dir / PACKAGE_CONFIG_FILE_NAME).write_text("[[[invalid toml", encoding="utf-8")
        self.workspace_config.packages_enable[pkg_corrupt] = True

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)
        for pkg in ("pkg_cyc_1", "pkg_cyc_2", pkg_corrupt):
            registry.set_package_state(pkg, "staging")
        save_state_registry(registry)

        res = run_primitive_8_rollback_recovery(
            self.workspace_config,
            ["pkg_cyc_1", "pkg_cyc_2", pkg_corrupt],
            force=False,
        )
        self.assertEqual(res.status, "SUCCESS")
        self.assertCountEqual(res.restored_packages, ["pkg_cyc_1", "pkg_cyc_2", pkg_corrupt])
        reloaded = load_state_registry(state_file)
        for pkg in ("pkg_cyc_1", "pkg_cyc_2", pkg_corrupt):
            self.assertIsNone(reloaded.get_package_state(pkg))
            self.assertFalse((self.install_dir / pkg).exists())

    def test_rollback_uncommitted_installed_package_without_force(self) -> None:
        """Verifies check_is_package_rollback_eligible and rollback without --force for uncommitted 'installed' packages."""
        from drift.core.state_registry import (
            check_is_package_rollback_eligible,
            check_is_package_uncommitted_in_install,
        )

        state_file = self.install_dir / "state.toml"
        registry = load_state_registry(state_file)

        # 1. Clean committed 'installed' package is NOT rollback-eligible without force
        self.assertFalse(check_is_package_uncommitted_in_install("pkg_a", registry, self.install_dir))
        self.assertFalse(check_is_package_rollback_eligible("pkg_a", registry, self.install_dir))
        with self.assertRaises(RuntimeError):
            run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)

        # 2. Simulate pkg_a completing update in a multi-package batch before install-commit:
        #    state is 'installed', but install/pkg_a/file.txt has uncommitted modifications and WAL exists.
        pkg_backup_root = self.backup_dir / "pkg_a"
        (self.pkg_a_install / "file.txt").write_text("v2 uncommitted", encoding="utf-8")
        exec_ctx = FileActionExecutionContext(sudo=False, wal_dir=pkg_backup_root / "wal")
        execute_delivery_actions(
            exec_ctx,
            [
                NormalFileAction(
                    action_type=FileActionType.UPDATE_COPY,
                    src_path=self.pkg_a_install / "file.txt",
                    dst_path=self.system_target_dir / "file.txt",
                )
            ],
        )
        registry = load_state_registry(state_file)
        registry.set_package_state("pkg_a", "installed", last_deployed="2026-10-19T12:00:00Z")
        save_state_registry(registry)

        self.assertTrue(check_is_package_uncommitted_in_install("pkg_a", registry, self.install_dir))
        self.assertTrue(check_is_package_rollback_eligible("pkg_a", registry, self.install_dir))
        self.assertIn(
            ("pkg_a", "installed"),
            registry.get_rollback_eligible_packages(install_base=self.install_dir),
        )

        # Rollback with force=False succeeds and restores pkg_a to committed HEAD
        res = run_primitive_8_rollback_recovery(self.workspace_config, ["pkg_a"], force=False)
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.restored_packages, ["pkg_a"])
        self.assertEqual((self.system_target_dir / "file.txt").read_text(encoding="utf-8"), "clean content")
        self.assertEqual((self.pkg_a_install / "file.txt").read_text(encoding="utf-8"), "clean content")
        reloaded_after_a = load_state_registry(state_file)
        self.assertFalse(check_is_package_uncommitted_in_install("pkg_a", reloaded_after_a, self.install_dir))

        # 3. Simulate a first-time package (not in HEAD:state.toml) that reached 'installed' before batch crash
        pkg_first = "pkg_first_installed"
        pkg_first_dir = self.install_dir / pkg_first
        (pkg_first_dir / DRIFT_INTERNAL_DIR_NAME).mkdir(parents=True, exist_ok=True)
        (pkg_first_dir / DRIFT_INTERNAL_DIR_NAME / PACKAGE_CONFIG_FILE_NAME).write_text(
            f'[package]\nname = "{pkg_first}"\ninstall_method = "copy"\ntarget_directory = "{self.system_target_dir.as_posix()}"\n',
            encoding="utf-8",
        )
        (pkg_first_dir / "new.txt").write_text("new pkg file", encoding="utf-8")
        first_backup = self.backup_dir / pkg_first
        execute_delivery_actions(
            FileActionExecutionContext(sudo=False, wal_dir=first_backup / "wal"),
            [
                NormalFileAction(
                    action_type=FileActionType.CREATE_COPY,
                    src_path=pkg_first_dir / "new.txt",
                    dst_path=self.system_target_dir / "new.txt",
                )
            ],
        )
        registry = load_state_registry(state_file)
        registry.set_package_state(pkg_first, "installed")
        registry.sync_deployed_files(pkg_first, self.system_target_dir, InstallMethod.COPY, [Path("new.txt")])
        save_state_registry(registry)
        self.workspace_config.packages_enable[pkg_first] = True

        self.assertTrue(check_is_package_uncommitted_in_install(pkg_first, registry, self.install_dir))
        self.assertTrue(check_is_package_rollback_eligible(pkg_first, registry, self.install_dir))

        res_first = run_primitive_8_rollback_recovery(self.workspace_config, [pkg_first], force=False)
        self.assertEqual(res_first.status, "SUCCESS")
        self.assertEqual(res_first.restored_packages, [pkg_first])
        self.assertFalse((self.system_target_dir / "new.txt").exists())
        self.assertFalse(pkg_first_dir.exists())
        reloaded = load_state_registry(state_file)
        self.assertIsNone(reloaded.get_package_state(pkg_first))


if __name__ == "__main__":
    unittest.main()

