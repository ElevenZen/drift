"""Tests for package requirements checking and probe hooks in Drift."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drift.package_config import PackageConfig, PackageRequirements, PackageHooks
from drift.workspace_config import WorkspaceConfig
from drift.render_package import render_package
from drift.exceptions import ConfigError
from drift.toml_utils import parse_toml


class TestPackageRequirements(unittest.TestCase):
    """Unit tests for declarative PackageRequirements."""

    def test_empty_requirements_satisfied(self) -> None:
        req = PackageRequirements()
        is_met, reason = req.check_requirements()
        self.assertTrue(is_met)
        self.assertIsNone(reason)

    def test_os_requirement_matching(self) -> None:
        req = PackageRequirements(os=["linux", "darwin"])
        with patch.dict(os.environ, {"drift_os": "linux"}):
            is_met, reason = req.check_requirements()
            self.assertTrue(is_met)
            self.assertIsNone(reason)

        with patch.dict(os.environ, {"drift_os": "windows"}):
            is_met, reason = req.check_requirements()
            self.assertFalse(is_met)
            self.assertIn("Host OS 'windows' not in required list", reason)

    def test_arch_requirement_matching(self) -> None:
        req = PackageRequirements(arch=["x86_64", "aarch64"])
        with patch.dict(os.environ, {"drift_arch": "x86_64"}):
            is_met, reason = req.check_requirements()
            self.assertTrue(is_met)
            self.assertIsNone(reason)

        with patch.dict(os.environ, {"drift_arch": "armv7l"}):
            is_met, reason = req.check_requirements()
            self.assertFalse(is_met)
            self.assertIn("Host architecture 'armv7l' not in required list", reason)

    def test_distro_requirement_matching(self) -> None:
        req = PackageRequirements(distro=["arch", "ubuntu"])
        with patch.dict(os.environ, {"drift_distro": "arch"}):
            is_met, reason = req.check_requirements()
            self.assertTrue(is_met)
            self.assertIsNone(reason)

        with patch.dict(os.environ, {"drift_distro": "fedora"}):
            is_met, reason = req.check_requirements()
            self.assertFalse(is_met)
            self.assertIn("Linux distribution 'fedora' not in required list", reason)

    def test_binaries_requirement_matching(self) -> None:
        req = PackageRequirements(binaries=["git", "nonexistent_binary_xyz_123"])
        with patch("shutil.which", side_effect=lambda b: "/usr/bin/" + b if b == "git" else None):
            is_met, reason = req.check_requirements()
            self.assertFalse(is_met)
            self.assertIn("Required binary 'nonexistent_binary_xyz_123' not found in PATH", reason)

        with patch("shutil.which", return_value="/usr/bin/tool"):
            is_met, reason = req.check_requirements()
            self.assertTrue(is_met)
            self.assertIsNone(reason)

    def test_env_requirement_matching(self) -> None:
        req = PackageRequirements(env=["WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"])
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"}):
            is_met, reason = req.check_requirements()
            self.assertTrue(is_met)
            self.assertIsNone(reason)

        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "", "XDG_RUNTIME_DIR": "/run/user/1000"}):
            is_met, reason = req.check_requirements()
            self.assertFalse(is_met)
            self.assertIn("Required environment variable 'WAYLAND_DISPLAY' is unset or empty", reason)

    def test_from_dict_parsing(self) -> None:
        data = {
            "os": ["linux"],
            "arch": "x86_64",
            "distro": ["arch", "ubuntu"],
            "binaries": ["sway", "waybar"],
            "env": ["WAYLAND_DISPLAY"]
        }
        req = PackageRequirements.from_dict(data, package_name="sway")
        self.assertEqual(req.os, ["linux"])
        self.assertEqual(req.arch, ["x86_64"])
        self.assertEqual(req.distro, ["arch", "ubuntu"])
        self.assertEqual(req.binaries, ["sway", "waybar"])
        self.assertEqual(req.env, ["WAYLAND_DISPLAY"])

    def test_from_dict_validation_unknown_keys(self) -> None:
        with self.assertRaises(ConfigError):
            PackageRequirements.from_dict({"unknown_key": "val"}, package_name="test")


class TestPackageProbeAndRenderPipeline(unittest.TestCase):
    """Integration tests for requirement evaluation in render_package."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.drift_root = Path(self.temp_dir.name).resolve()
        (self.drift_root / "config").mkdir(parents=True)
        (self.drift_root / "src").mkdir(parents=True)
        (self.drift_root / "render").mkdir(parents=True)
        (self.drift_root / "install").mkdir(parents=True)

        self.workspace_config = WorkspaceConfig(
            drift_root_path=self.drift_root,
            source_directory=Path("src"),
            render_directory=Path("render"),
            install_directory=Path("install"),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_render_package_skipped_due_to_declarative_requirement(self) -> None:
        pkg_dir = self.drift_root / "src" / "sway_pkg"
        pkg_dir.mkdir(parents=True)

        (pkg_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [package.requirements]
        os = ["darwin"]
        """, encoding="utf-8")

        (pkg_dir / "config.txt").write_text("Sway Config", encoding="utf-8")

        with patch.dict(os.environ, {"drift_os": "linux"}):
            res = render_package(self.workspace_config, pkg_dir)
            self.assertEqual(res.status, "SKIPPED")
            self.assertIn("Host OS 'linux' not in required list", res.skip_reason)
            # Ensure render directory was not populated with config.txt
            render_dest = self.drift_root / "render" / "sway_pkg" / "config.txt"
            self.assertFalse(render_dest.exists())

    def test_render_package_probe_hook_success(self) -> None:
        pkg_dir = self.drift_root / "src" / "probe_pkg"
        pkg_dir.mkdir(parents=True)

        probe_script = pkg_dir / "probe.sh"
        probe_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        probe_script.chmod(0o755)

        (pkg_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [hooks]
        probe = "probe.sh"
        """, encoding="utf-8")

        (pkg_dir / "app.conf").write_text("app settings", encoding="utf-8")

        res = render_package(self.workspace_config, pkg_dir)
        self.assertEqual(res.status, "SUCCESS")
        self.assertIsNone(res.skip_reason)
        render_dest = self.drift_root / "render" / "probe_pkg" / "app.conf"
        self.assertTrue(render_dest.exists())

    def test_render_package_probe_hook_failure_skips_package(self) -> None:
        pkg_dir = self.drift_root / "src" / "failed_probe_pkg"
        pkg_dir.mkdir(parents=True)

        probe_script = pkg_dir / "probe.sh"
        probe_script.write_text("#!/bin/sh\necho 'Wayland session not found' >&2\nexit 1\n", encoding="utf-8")
        probe_script.chmod(0o755)

        (pkg_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [hooks]
        probe = "probe.sh"
        """, encoding="utf-8")

        (pkg_dir / "app.conf").write_text("app settings", encoding="utf-8")

        res = render_package(self.workspace_config, pkg_dir)
        self.assertEqual(res.status, "SKIPPED")
        self.assertIn("Probe hook failed", res.skip_reason)
        self.assertIn("Wayland session not found", res.skip_reason)

        render_dest = self.drift_root / "render" / "failed_probe_pkg" / "app.conf"
        self.assertFalse(render_dest.exists())

    def test_no_hooks_flag_bypasses_probe_hook(self) -> None:
        pkg_dir = self.drift_root / "src" / "bypassed_probe_pkg"
        pkg_dir.mkdir(parents=True)

        probe_script = pkg_dir / "probe.sh"
        probe_script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        probe_script.chmod(0o755)

        (pkg_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [hooks]
        probe = "probe.sh"
        """, encoding="utf-8")

        (pkg_dir / "app.conf").write_text("app settings", encoding="utf-8")

        # Running with no_hooks=True should ignore failing probe hook
        res = render_package(self.workspace_config, pkg_dir, no_hooks=True)
        self.assertEqual(res.status, "SUCCESS")

    def test_render_package_templated_probe_hook(self) -> None:
        """Verifies that probe hooks matching template engines (e.g. probe.sh.envst) are rendered before execution."""
        from drift.workspace_config import RenderEngineConfig

        self.workspace_config.render_engine_configs = {
            "envst": RenderEngineConfig(name="envst", suffix="envst", render_command="internal")
        }

        pkg_dir = self.drift_root / "src" / "templated_probe_pkg"
        pkg_dir.mkdir(parents=True)

        # Template probe script checking injected variable
        probe_template = pkg_dir / "check.sh.envst"
        probe_template.write_text("""#!/bin/sh
if [ "$PROBE_EXPECTED" = "allow" ]; then
    exit 0
else
    echo "Expected 'allow', got '$PROBE_EXPECTED'" >&2
    exit 1
fi
""", encoding="utf-8")
        probe_template.chmod(0o755)

        (pkg_dir / "drift_package.toml").write_text("""
        [package]
        install_method = "stow"

        [env.override]
        PROBE_EXPECTED = "allow"

        [hooks]
        probe = "check.sh"
        """, encoding="utf-8")

        (pkg_dir / "app.conf").write_text("app settings", encoding="utf-8")

        res = render_package(self.workspace_config, pkg_dir)
        self.assertEqual(res.status, "SUCCESS")

        # Verify rendered probe script exists in render directory
        render_probe = self.drift_root / "render" / "templated_probe_pkg" / "check.sh"
        self.assertTrue(render_probe.exists())
        self.assertIn('"allow" = "allow"', render_probe.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
