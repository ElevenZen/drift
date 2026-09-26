import os
import tempfile
import unittest
import subprocess
from pathlib import Path

from drift.config.package_config import PackageConfig
from drift.config.workspace_config import WorkspaceConfig
from drift.core.constants import ExitCode
from drift.core.exceptions import (
    DriftError,
    DriftDetectedError,
    HookMissingError,
    InstallCollisionError,
    CrossPackageCollisionError,
)
from drift.primitives.package_assertions import (
    assert_packages_hooks_exist,
    assert_install_pkg_dirs_clean,
)


class TestPackageAssertions(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()

        self.source_dir = self.drift_root / "src"
        self.render_dir = self.drift_root / "render"
        self.install_dir = self.drift_root / "install"

        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.render_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)

        self.workspace_config = WorkspaceConfig(
            drift_root=self.drift_root,
            packages_enable={"pkg_a": True, "pkg_b": True},
            packages_enable_default=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_assert_packages_hooks_exist_success(self) -> None:
        """Verifies assert_packages_hooks_exist passes when all hook files are present."""
        meta_a = PackageConfig.from_dict(
            {"package": {"name": "pkg_a"}, "hooks": {"post_install": "drift_hooks/post_install.sh"}},
            "pkg_a",
            self.source_dir / "pkg_a",
            workspace_config=self.workspace_config,
        )
        meta_b = PackageConfig.from_dict(
            {"package": {"name": "pkg_b"}, "hooks": {"post_install": "drift_hooks/post_install.sh"}},
            "pkg_b",
            self.source_dir / "pkg_b",
            workspace_config=self.workspace_config,
        )

        pkg_a_render = self.render_dir / "pkg_a"
        pkg_b_render = self.render_dir / "pkg_b"
        pkg_a_hooks = pkg_a_render / ".drift" / "hooks"
        pkg_b_hooks = pkg_b_render / ".drift" / "hooks"
        pkg_a_hooks.mkdir(parents=True, exist_ok=True)
        pkg_b_hooks.mkdir(parents=True, exist_ok=True)

        hook_file_a = pkg_a_hooks / "post_install.sh"
        hook_file_b = pkg_b_hooks / "post_install.sh"
        hook_file_a.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook_file_b.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        # Both packages have existing hooks -> succeeds without raising
        assert_packages_hooks_exist(
            {"pkg_a": meta_a, "pkg_b": meta_b},
            self.render_dir,
            is_source=False,
        )

    def test_assert_packages_hooks_exist_collects_all_failures(self) -> None:
        """Verifies assert_packages_hooks_exist aggregates missing hooks across all packages."""
        meta_a = PackageConfig.from_dict(
            {"package": {"name": "pkg_a"}, "hooks": {"post_install": "drift_hooks/missing_a.sh"}},
            "pkg_a",
            self.source_dir / "pkg_a",
            workspace_config=self.workspace_config,
        )
        meta_b = PackageConfig.from_dict(
            {"package": {"name": "pkg_b"}, "hooks": {"post_install": "drift_hooks/missing_b.sh"}},
            "pkg_b",
            self.source_dir / "pkg_b",
            workspace_config=self.workspace_config,
        )

        with self.assertRaises(HookMissingError) as ctx:
            assert_packages_hooks_exist(
                {"pkg_a": meta_a, "pkg_b": meta_b},
                self.render_dir,
                is_source=False,
            )

        self.assertEqual(sorted(ctx.exception.packages), ["pkg_a", "pkg_b"])
        self.assertIn("pkg_a", str(ctx.exception))
        self.assertIn("pkg_b", str(ctx.exception))
        self.assertIn("Lifecycle hook file validation failed for 2 package(s)", str(ctx.exception))

    def test_assert_packages_hooks_exist_filter_by_hook_names(self) -> None:
        """Verifies hook_names parameter restricts the validated lifecycle hooks."""
        meta = PackageConfig.from_dict(
            {
                "package": {"name": "pkg_a"},
                "hooks": {
                    "post_install": "drift_hooks/missing_install.sh",
                    "pre_uninstall": "drift_hooks/uninstall.sh",
                },
            },
            "pkg_a",
            self.source_dir / "pkg_a",
            workspace_config=self.workspace_config,
        )

        pkg_a_hooks = self.install_dir / "pkg_a" / ".drift" / "hooks"
        pkg_a_hooks.mkdir(parents=True, exist_ok=True)
        (pkg_a_hooks / "uninstall.sh").write_text("#!/bin/sh\n", encoding="utf-8")

        # Checking only pre_uninstall succeeds because uninstall.sh exists, even though post_install is missing
        assert_packages_hooks_exist(
            {"pkg_a": meta},
            self.install_dir,
            is_source=False,
            hook_names=["pre_uninstall"],
        )

        # Checking post_install raises HookMissingError
        with self.assertRaises(HookMissingError) as ctx:
            assert_packages_hooks_exist(
                {"pkg_a": meta},
                self.install_dir,
                is_source=False,
                hook_names=["post_install"],
            )
        self.assertEqual(ctx.exception.packages, ["pkg_a"])

    def test_assert_install_pkg_dirs_clean_passes_when_clean_or_nonexistent(self) -> None:
        """Verifies assert_install_pkg_dirs_clean passes when directories are clean or do not exist."""
        # Nonexistent packages
        assert_install_pkg_dirs_clean(self.install_dir, "nonexistent")
        assert_install_pkg_dirs_clean(self.install_dir, ["nonexistent_1", "nonexistent_2"])

        # Clean git repository
        subprocess.run(["git", "init"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), check=True)

        pkg_dir = self.install_dir / "pkg_clean"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "file.txt").write_text("clean content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.install_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.install_dir), check=True, capture_output=True)

        assert_install_pkg_dirs_clean(self.install_dir, "pkg_clean")
        assert_install_pkg_dirs_clean(self.install_dir, ["pkg_clean"])

    def test_assert_install_pkg_dirs_clean_collects_all_dirty_packages(self) -> None:
        """Verifies assert_install_pkg_dirs_clean aggregates all dirty packages across the batch."""
        subprocess.run(["git", "init"], cwd=str(self.install_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.install_dir), check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.install_dir), check=True)

        pkg_a = self.install_dir / "pkg_a"
        pkg_b = self.install_dir / "pkg_b"
        pkg_a.mkdir(parents=True, exist_ok=True)
        pkg_b.mkdir(parents=True, exist_ok=True)

        (pkg_a / "dirty_a.txt").write_text("dirty a", encoding="utf-8")
        (pkg_b / "dirty_b.txt").write_text("dirty b", encoding="utf-8")

        # Single string dirty
        with self.assertRaises(DriftDetectedError) as ctx_single:
            assert_install_pkg_dirs_clean(self.install_dir, "pkg_a")
        self.assertEqual(ctx_single.exception.packages, ["pkg_a"])
        self.assertIn("'pkg_a'", str(ctx_single.exception))

        # Iterable multiple dirty
        with self.assertRaises(DriftDetectedError) as ctx_multi:
            assert_install_pkg_dirs_clean(self.install_dir, ["pkg_a", "pkg_b"])
        self.assertEqual(sorted(ctx_multi.exception.packages), ["pkg_a", "pkg_b"])
        self.assertIn("'pkg_a'", str(ctx_multi.exception))
        self.assertIn("'pkg_b'", str(ctx_multi.exception))
        self.assertIn("Package(s) 'pkg_a', 'pkg_b' in install directory has uncommitted local modifications", str(ctx_multi.exception))

    def test_cross_package_collision_error_properties(self) -> None:
        """Verifies CrossPackageCollisionError fields, aliases, and inheritance hierarchy."""
        # 1. Instantiation with packages parameter
        err1 = CrossPackageCollisionError("collision message", packages=["pkg_a", "pkg_b"])
        self.assertEqual(err1.packages, ["pkg_a", "pkg_b"])
        self.assertEqual(err1.conflicting_packages, ["pkg_a", "pkg_b"])
        self.assertEqual(err1.exit_code, ExitCode.COLLISION_ERROR)
        self.assertIsInstance(err1, InstallCollisionError)
        self.assertIsInstance(err1, DriftError)
        self.assertIsInstance(err1, RuntimeError)

        # 2. Instantiation with conflicting_packages parameter
        err2 = CrossPackageCollisionError("collision message", conflicting_packages=["pkg_c", "pkg_d"])
        self.assertEqual(err2.packages, ["pkg_c", "pkg_d"])
        self.assertEqual(err2.conflicting_packages, ["pkg_c", "pkg_d"])

        # 3. Property setter
        err2.conflicting_packages = ["pkg_e"]
        self.assertEqual(err2.packages, ["pkg_e"])
        self.assertEqual(err2.conflicting_packages, ["pkg_e"])

        # 4. Optional conflicts map
        fake_conflicts = {Path("/fake/path"): [("pkg_a", "batch"), ("pkg_b", "installed")]}
        err3 = CrossPackageCollisionError("collision", packages=["pkg_a", "pkg_b"], conflicts=fake_conflicts)
        self.assertEqual(err3.conflicts, fake_conflicts)


if __name__ == "__main__":
    unittest.main()
