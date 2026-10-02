import os
import tempfile
import unittest
import subprocess
from pathlib import Path

from drift.config.package_config import (
    PackageConfig,
    PackageDependencies,
    PackageDependency,
    PackageSectionConfig,
)
from drift.config.workspace_config import WorkspaceConfig
from drift.core.constants import ExitCode
from drift.core.state_registry import StateRegistry
from drift.core.exceptions import (
    DriftError,
    ConfigError,
    DriftDetectedError,
    HookMissingError,
    MidwayTransactionError,
    PackageInstallDirMissingError,
    TargetPermissionError,
    InstallCollisionError,
    CrossPackageCollisionError,
)
from drift.primitives.package_assertions import (
    assert_packages_hooks_exist,
    assert_install_pkg_dirs_clean,
    assert_packages_not_in_midway_state,
    assert_packages_install_dirs_exist,
    assert_packages_target_dirs_valid,
    assert_packages_target_dirs_writable,
    assert_no_cross_package_conflicts,
    assert_required_package_dependencies_exist,
    assert_no_cyclic_package_dependencies,
    assert_no_broken_dependencies_on_uninstall,
    resolve_package_install_order,
    resolve_package_uninstall_order,
    resolve_target_package_order,
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
        """Verifies CrossPackageCollisionError fields, packages attribute, and inheritance hierarchy."""
        # 1. Instantiation with packages parameter
        err1 = CrossPackageCollisionError("collision message", packages=["pkg_a", "pkg_b"])
        self.assertEqual(err1.packages, ["pkg_a", "pkg_b"])
        self.assertEqual(err1.exit_code, ExitCode.COLLISION_ERROR)
        self.assertIsInstance(err1, InstallCollisionError)
        self.assertIsInstance(err1, DriftError)
        self.assertIsInstance(err1, RuntimeError)

        # 2. Optional conflicts map
        fake_conflicts = {Path("/fake/path"): [("pkg_a", "batch"), ("pkg_b", "installed")]}
        err2 = CrossPackageCollisionError("collision", packages=["pkg_a", "pkg_b"], conflicts=fake_conflicts)
        self.assertEqual(err2.packages, ["pkg_a", "pkg_b"])
        self.assertEqual(err2.conflicts, fake_conflicts)

    def test_assert_packages_not_in_midway_state(self) -> None:
        """Verifies assert_packages_not_in_midway_state passes on clean states and aggregates midway packages."""
        registry = StateRegistry()
        registry.set_package_state("pkg_clean", "installed")
        assert_packages_not_in_midway_state(["pkg_clean"], registry)

        registry.set_package_state("pkg_staging", "staging")
        registry.set_package_state("pkg_installing", "installing")

        with self.assertRaises(MidwayTransactionError) as ctx:
            assert_packages_not_in_midway_state(["pkg_clean", "pkg_staging", "pkg_installing"], registry)
        self.assertEqual(sorted(ctx.exception.packages), ["pkg_installing", "pkg_staging"])
        self.assertIn("pkg_staging", str(ctx.exception))
        self.assertIn("pkg_installing", str(ctx.exception))

    def test_assert_packages_install_dirs_exist(self) -> None:
        """Verifies assert_packages_install_dirs_exist passes when directories exist and raises PackageInstallDirMissingError when missing."""
        (self.install_dir / "pkg_exists").mkdir(parents=True, exist_ok=True)
        assert_packages_install_dirs_exist(self.install_dir, ["pkg_exists"])

        # Also verifies catching as FileNotFoundError works due to inheritance
        with self.assertRaises(FileNotFoundError) as ctx_fnf:
            assert_packages_install_dirs_exist(self.install_dir, ["pkg_exists", "pkg_missing1", "pkg_missing2"])
        self.assertIsInstance(ctx_fnf.exception, PackageInstallDirMissingError)
        assert isinstance(ctx_fnf.exception, PackageInstallDirMissingError)
        self.assertIsInstance(ctx_fnf.exception, DriftError)
        self.assertEqual(sorted(ctx_fnf.exception.packages), ["pkg_missing1", "pkg_missing2"])
        self.assertIn("pkg_missing1", str(ctx_fnf.exception))
        self.assertIn("pkg_missing2", str(ctx_fnf.exception))

    def test_assert_packages_target_dirs_valid(self) -> None:
        """Verifies assert_packages_target_dirs_valid validates absolute paths and outside drift_root."""
        system_target = Path(self.temp_dir.name).parent / "system_target"
        meta_valid = PackageConfig.from_dict(
            {"package": {"name": "pkg_valid", "target_directory": system_target}},
            "pkg_valid",
            self.source_dir / "pkg_valid",
            workspace_config=self.workspace_config,
        )
        assert_packages_target_dirs_valid({"pkg_valid": meta_valid}, self.workspace_config)

        # Non-absolute target directory
        meta_relative = PackageConfig.from_dict(
            {"package": {"name": "pkg_rel", "target_directory": "relative/path"}},
            "pkg_rel",
            self.source_dir / "pkg_rel",
            workspace_config=self.workspace_config,
        )
        with self.assertRaises(ValueError) as ctx_val:
            assert_packages_target_dirs_valid({"pkg_rel": meta_relative}, self.workspace_config)
        self.assertIsInstance(ctx_val.exception, ConfigError)
        assert isinstance(ctx_val.exception, ConfigError)
        self.assertIsInstance(ctx_val.exception, DriftError)
        self.assertEqual(ctx_val.exception.packages, ["pkg_rel"])
        self.assertIn("must be absolute", str(ctx_val.exception))

        # Target directory inside drift_root
        meta_inside = PackageConfig.from_dict(
            {"package": {"name": "pkg_inside", "target_directory": str(self.drift_root / "nested")}},
            "pkg_inside",
            self.source_dir / "pkg_inside",
            workspace_config=self.workspace_config,
        )
        with self.assertRaises(InstallCollisionError) as ctx_col:
            assert_packages_target_dirs_valid({"pkg_inside": meta_inside}, self.workspace_config)
        self.assertIsInstance(ctx_col.exception, DriftError)
        self.assertEqual(ctx_col.exception.packages, ["pkg_inside"])
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx_col.exception))

        # Target directory equal to drift_root
        meta_exact = PackageConfig.from_dict(
            {"package": {"name": "pkg_exact", "target_directory": str(self.drift_root)}},
            "pkg_exact",
            self.source_dir / "pkg_exact",
            workspace_config=self.workspace_config,
        )
        with self.assertRaises(InstallCollisionError) as ctx_exact:
            assert_packages_target_dirs_valid({"pkg_exact": meta_exact}, self.workspace_config)
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx_exact.exception))

        # Target directory is a symlink pointing into drift_root
        symlink_to_drift = Path(self.temp_dir.name) / "symlink_target_dir"
        symlink_to_drift.symlink_to(self.drift_root / "nested")
        meta_symlink = PackageConfig.from_dict(
            {"package": {"name": "pkg_symlink", "target_directory": str(symlink_to_drift)}},
            "pkg_symlink",
            self.source_dir / "pkg_symlink",
            workspace_config=self.workspace_config,
        )
        with self.assertRaises(InstallCollisionError) as ctx_symlink:
            assert_packages_target_dirs_valid({"pkg_symlink": meta_symlink}, self.workspace_config)
        self.assertIn("cannot be inside or equal to the drift workspace root", str(ctx_symlink.exception))
        self.assertIn("resolved to", str(ctx_symlink.exception))

    def test_assert_packages_target_dirs_writable(self) -> None:
        """Verifies assert_packages_target_dirs_writable checks permissions on target directories."""
        writable_target = Path(self.temp_dir.name).parent / "writable_dir"
        writable_target.mkdir(parents=True, exist_ok=True)
        meta = PackageConfig.from_dict(
            {"package": {"name": "pkg_write", "target_directory": str(writable_target)}},
            "pkg_write",
            self.source_dir / "pkg_write",
            workspace_config=self.workspace_config,
        )
        assert_packages_target_dirs_writable({"pkg_write": meta}, self.workspace_config)

    def test_assert_packages_target_dirs_writable_collects_all_failures(self) -> None:
        """Verifies assert_packages_target_dirs_writable catches lower-level errors and aggregates all unwritable packages."""
        from unittest import mock

        meta_ok = PackageConfig.from_dict(
            {"package": {"name": "pkg_ok", "target_directory": "/dummy/ok"}},
            "pkg_ok",
            self.source_dir / "pkg_ok",
            workspace_config=self.workspace_config,
        )
        meta_bad1 = PackageConfig.from_dict(
            {"package": {"name": "pkg_bad1", "target_directory": "/dummy/bad1"}},
            "pkg_bad1",
            self.source_dir / "pkg_bad1",
            workspace_config=self.workspace_config,
        )
        meta_bad2 = PackageConfig.from_dict(
            {"package": {"name": "pkg_bad2", "target_directory": "/dummy/bad2"}},
            "pkg_bad2",
            self.source_dir / "pkg_bad2",
            workspace_config=self.workspace_config,
        )

        def mock_assert_writable(target_path: Path, sudo: bool = False) -> None:
            if "bad" in str(target_path):
                raise PermissionError(f"Directory '{target_path}' is not writable.")

        with mock.patch("drift.primitives.package_assertions.assert_writable", side_effect=mock_assert_writable):
            with self.assertRaises(PermissionError) as ctx:
                assert_packages_target_dirs_writable(
                    {"pkg_ok": meta_ok, "pkg_bad1": meta_bad1, "pkg_bad2": meta_bad2},
                    self.workspace_config,
                )
            self.assertIsInstance(ctx.exception, TargetPermissionError)
            assert isinstance(ctx.exception, TargetPermissionError)
            self.assertIsInstance(ctx.exception, DriftError)
            self.assertEqual(sorted(ctx.exception.packages), ["pkg_bad1", "pkg_bad2"])
            self.assertIn("pkg_bad1", str(ctx.exception))
            self.assertIn("pkg_bad2", str(ctx.exception))
            self.assertIn("Target directory permission check failed for 2 package(s)", str(ctx.exception))

    def test_assert_no_cross_package_conflicts(self) -> None:
        """Verifies assert_no_cross_package_conflicts detects collisions across packages and populates packages field."""
        system_target = Path(self.temp_dir.name).parent / "system_home"
        system_target.mkdir(parents=True, exist_ok=True)

        meta_a = PackageConfig.from_dict(
            {"package": {"name": "pkg_a", "target_directory": str(system_target)}},
            "pkg_a",
            self.source_dir / "pkg_a",
            workspace_config=self.workspace_config,
        )
        meta_b = PackageConfig.from_dict(
            {"package": {"name": "pkg_b", "target_directory": str(system_target)}},
            "pkg_b",
            self.source_dir / "pkg_b",
            workspace_config=self.workspace_config,
        )

        pkg_a_install = self.install_dir / "pkg_a"
        pkg_b_install = self.install_dir / "pkg_b"
        pkg_a_install.mkdir(parents=True, exist_ok=True)
        pkg_b_install.mkdir(parents=True, exist_ok=True)

        # 1. Distinct files -> no conflict
        (pkg_a_install / "file_a.txt").write_text("a", encoding="utf-8")
        (pkg_b_install / "file_b.txt").write_text("b", encoding="utf-8")
        registry = StateRegistry()

        assert_no_cross_package_conflicts(
            self.workspace_config,
            ["pkg_a", "pkg_b"],
            {"pkg_a": meta_a, "pkg_b": meta_b},
            registry,
        )

        # 2. Conflicting identical file -> raises CrossPackageCollisionError
        (pkg_b_install / "file_a.txt").write_text("b_collision", encoding="utf-8")

        with self.assertRaises(CrossPackageCollisionError) as ctx:
            assert_no_cross_package_conflicts(
                self.workspace_config,
                ["pkg_a", "pkg_b"],
                {"pkg_a": meta_a, "pkg_b": meta_b},
                registry,
            )

        self.assertIsInstance(ctx.exception, InstallCollisionError)
        self.assertIsInstance(ctx.exception, DriftError)
        self.assertEqual(sorted(ctx.exception.packages), ["pkg_a", "pkg_b"])
        self.assertIn("file_a.txt", str(ctx.exception))
        self.assertIn("pkg_a", str(ctx.exception))
        self.assertIn("pkg_b", str(ctx.exception))

    def test_assert_no_cross_package_conflicts_allows_shared_empty_directories(self) -> None:
        """Verifies that multiple packages can share the same empty directory without cross-package collision."""
        from drift.core.constants import DRIFT_KEEP_FILE_NAME

        system_target = Path(self.temp_dir.name).parent / "system_home_shared"
        system_target.mkdir(parents=True, exist_ok=True)

        meta_a = PackageConfig.from_dict(
            {"package": {"name": "pkg_a", "target_directory": str(system_target)}},
            "pkg_a",
            self.source_dir / "pkg_a",
            workspace_config=self.workspace_config,
        )
        meta_b = PackageConfig.from_dict(
            {"package": {"name": "pkg_b", "target_directory": str(system_target)}},
            "pkg_b",
            self.source_dir / "pkg_b",
            workspace_config=self.workspace_config,
        )

        pkg_a_install = self.install_dir / "pkg_a"
        pkg_b_install = self.install_dir / "pkg_b"
        (pkg_a_install / "shared_empty").mkdir(parents=True, exist_ok=True)
        (pkg_a_install / "shared_empty" / DRIFT_KEEP_FILE_NAME).touch()
        (pkg_a_install / "file_a.txt").write_text("a", encoding="utf-8")

        (pkg_b_install / "shared_empty").mkdir(parents=True, exist_ok=True)
        (pkg_b_install / "shared_empty" / DRIFT_KEEP_FILE_NAME).touch()
        (pkg_b_install / "file_b.txt").write_text("b", encoding="utf-8")

        registry = StateRegistry()

        # Both packages declare the identical empty folder 'shared_empty'; this must NOT raise CrossPackageCollisionError
        assert_no_cross_package_conflicts(
            self.workspace_config,
            ["pkg_a", "pkg_b"],
            {"pkg_a": meta_a, "pkg_b": meta_b},
            registry,
        )

    def test_resolve_package_install_order_dag(self) -> None:
        deps_map = {
            "pkg_c": PackageDependencies(items=[PackageDependency(name="pkg_b")]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
            "pkg_a": PackageDependencies(items=[]),
        }
        order = resolve_package_install_order(deps_map)
        self.assertEqual(order, ["pkg_a", "pkg_b", "pkg_c"])

    def test_resolve_package_install_order_deterministic_lexical(self) -> None:
        """Verifies that topological sorting resolves ties deterministically using lexical order."""
        # 1. Independent packages passed in reverse alphabetical order
        deps_map_independent = {
            "pkg_z": PackageDependencies(items=[]),
            "pkg_m": PackageDependencies(items=[]),
            "pkg_a": PackageDependencies(items=[]),
        }
        order = resolve_package_install_order(deps_map_independent)
        self.assertEqual(order, ["pkg_a", "pkg_m", "pkg_z"])

        # 2. Dependency constraints combined with lexical tie-breaking:
        # pkg_z depends on pkg_b; pkg_a and pkg_b have in-degree 0
        deps_map_branching = {
            "pkg_z": PackageDependencies(items=[PackageDependency(name="pkg_b")]),
            "pkg_a": PackageDependencies(items=[]),
            "pkg_b": PackageDependencies(items=[]),
        }
        order_branching = resolve_package_install_order(deps_map_branching)
        # pkg_a and pkg_b both have in-degree 0 -> pkg_a is chosen first, then pkg_b unlocks pkg_z
        self.assertEqual(order_branching, ["pkg_a", "pkg_b", "pkg_z"])
        # Reverse uninstall order reverses the forward install order
        self.assertEqual(resolve_package_uninstall_order(deps_map_branching), ["pkg_z", "pkg_b", "pkg_a"])

    def test_assert_required_package_dependencies_exist_success(self) -> None:
        deps_map = {
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
            "pkg_a": PackageDependencies(items=[]),
        }
        # Should not raise
        assert_required_package_dependencies_exist(deps_map)

    def test_assert_required_package_dependencies_exist_missing(self) -> None:
        deps_map = {
            "pkg_b": PackageDependencies(items=[PackageDependency(name="missing_pkg")]),
            "pkg_c": PackageDependencies(items=[PackageDependency(name="missing_pkg2")]),
        }
        with self.assertRaises(ConfigError) as ctx:
            assert_required_package_dependencies_exist(deps_map)
        self.assertIn("missing_pkg", str(ctx.exception))
        self.assertIn("missing_pkg2", str(ctx.exception))

    def test_resolve_package_install_order_prunes_absent_deps(self) -> None:
        # resolve_package_install_order is a pure sorter and prunes absent dependencies
        deps_map = {
            "pkg_b": PackageDependencies(items=[PackageDependency(name="absent_dep")]),
        }
        order = resolve_package_install_order(deps_map)
        self.assertEqual(order, ["pkg_b"])

    def test_resolve_package_install_order_prune_optional(self) -> None:
        deps_map = {
            "pkg_b": PackageDependencies(items=[PackageDependency(name="missing", optional=True)]),
        }
        order = resolve_package_install_order(deps_map)
        self.assertEqual(order, ["pkg_b"])

    def test_resolve_package_install_order_cycle(self) -> None:
        deps_map = {
            "pkg_a": PackageDependencies(items=[PackageDependency(name="pkg_b")]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
        }
        with self.assertRaises(ConfigError) as ctx:
            resolve_package_install_order(deps_map)
        self.assertIn("Cyclic package dependency detected", str(ctx.exception))

    def test_resolve_package_uninstall_order(self) -> None:
        # If pkg_c depends on pkg_b, and pkg_b depends on pkg_a:
        # Forward install order is: pkg_a -> pkg_b -> pkg_c
        # Reverse uninstall order must be: pkg_c -> pkg_b -> pkg_a
        deps_map = {
            "pkg_c": PackageDependencies(items=[PackageDependency(name="pkg_b")]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
            "pkg_a": PackageDependencies(items=[]),
        }
        uninstall_order = resolve_package_uninstall_order(deps_map)
        self.assertEqual(uninstall_order, ["pkg_c", "pkg_b", "pkg_a"])

    def test_assert_no_cyclic_package_dependencies(self) -> None:
        valid_deps = {
            "pkg_a": PackageDependencies(items=[]),
            "pkg_b": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
            # Missing dependency should NOT cause cycle assertion to fail
            "pkg_c": PackageDependencies(items=[PackageDependency(name="missing_external")]),
        }
        # Should not raise on valid DAG or missing dependencies
        assert_no_cyclic_package_dependencies(valid_deps)

        cyclic_deps = {
            "pkg_a": PackageDependencies(items=[PackageDependency(name="pkg_a")]),
        }
        with self.assertRaises(ConfigError):
            assert_no_cyclic_package_dependencies(cyclic_deps)

    def test_assert_no_broken_dependencies_on_uninstall(self) -> None:
        meta_b = PackageConfig(
            PackageSectionConfig(
                name="pkg_b",
                dependencies=PackageDependencies(items=[PackageDependency(name="pkg_a")]),
            )
        )
        remaining = {"pkg_b": meta_b}

        # Attempting to uninstall pkg_a while pkg_b remains raises ConfigError
        with self.assertRaises(ConfigError) as ctx:
            assert_no_broken_dependencies_on_uninstall(
                packages_to_uninstall=["pkg_a"],
                remaining_metadata=remaining,
            )
        self.assertIn("Remaining package 'pkg_b' requires uninstalled package(s): ['pkg_a']", str(ctx.exception))

        # Uninstalling an unrelated package passes without error
        assert_no_broken_dependencies_on_uninstall(
            packages_to_uninstall=["pkg_c"],
            remaining_metadata=remaining,
        )

    def test_resolve_target_package_order_empty(self) -> None:
        registry = StateRegistry()
        res = resolve_target_package_order({}, registry, self.workspace_config)
        self.assertEqual(res, [])

    def test_resolve_target_package_order_with_installed_and_pruning(self) -> None:
        # Create an installed package recorded in state registry
        meta_installed = PackageConfig(
            PackageSectionConfig(name="pkg_base")
        )
        pkg_base_dir = self.install_dir / "pkg_base"
        dot_drift = pkg_base_dir / ".drift"
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / "drift_package.toml").write_text('[package]\nname = "pkg_base"\n', encoding="utf-8")

        registry = StateRegistry()
        registry.set_package_state("pkg_base", "installed")

        # Target package depends on pkg_base and an optional missing package
        meta_target = PackageConfig(
            PackageSectionConfig(
                name="pkg_app",
                dependencies=PackageDependencies(items=[
                    PackageDependency(name="pkg_base"),
                    PackageDependency(name="opt_missing", optional=True),
                ]),
            )
        )

        res = resolve_target_package_order(
            target_metadata={"pkg_app": meta_target},
            state_registry=registry,
            workspace_config=self.workspace_config,
        )
        self.assertEqual(res, ["pkg_app"])

    def test_resolve_target_package_order_missing_deps_guard_and_bypass(self) -> None:
        registry = StateRegistry()
        meta_target = PackageConfig(
            PackageSectionConfig(
                name="pkg_app",
                dependencies=PackageDependencies(items=[
                    PackageDependency(name="non_existent_dep"),
                ]),
            )
        )

        # By default (no_deps=False), raises ConfigError
        with self.assertRaises(ConfigError) as ctx:
            resolve_target_package_order(
                target_metadata={"pkg_app": meta_target},
                state_registry=registry,
                workspace_config=self.workspace_config,
                no_deps=False,
            )
        self.assertIn("non_existent_dep", str(ctx.exception))

        # With no_deps=True, bypasses missing check and prunes
        res = resolve_target_package_order(
            target_metadata={"pkg_app": meta_target},
            state_registry=registry,
            workspace_config=self.workspace_config,
            no_deps=True,
        )
        self.assertEqual(res, ["pkg_app"])


if __name__ == "__main__":
    unittest.main()
