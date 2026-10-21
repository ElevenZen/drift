"""Tests for `drift enable` / `drift disable` (config planning, primitive, and CLI backends)."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Dict
from unittest.mock import patch

from drift.config.workspace_config import WorkspaceConfig
from drift.config.workspace_enable_edit import (
    assert_workspace_enable_edit_verified,
    build_expected_enable_dict,
    plan_workspace_enable_edit,
    render_workspace_enable_diff,
    resolve_workspace_enable_target,
)
from drift.config.workspace_loader import (
    locate_workspace_config_file,
    parse_workspace_config_content,
)
from drift.core.exceptions import ConfigError
from drift.primitives.workspace_enable import (
    PackageEnableOptions,
    normalize_enable_package_names,
    run_primitive_packages_enable,
)
from drift.utils.toml_edit import TomlBoolEditResult
from drift.utils.toml_utils import parse_toml
from tests.test_utils import TestCaseUtilityMixin

BASE_CONFIG = """# Workspace config
[env.default]
THEME = "nord"  # keep me

[packages.enable]
DEFAULT = false
pkg_a = true   # primary
pkg_b = false

# ---------------------------------------------------------------------
# Render Engines Configuration
# ---------------------------------------------------------------------
[render.envsubst]
suffix = "envst"
render_command = "internal"

[workspace]
source_directory = "src"
"""

ENVST_CONFIG = """[packages.enable]
DEFAULT = false
pkg_a = true

[workspace]
source_directory = "src"
default_target_directory = "${DRIFT_ENABLE_TEST_TARGET}"
"""


class _WorkspaceFixture(unittest.TestCase):
    """Builds a minimal workspace: config/, src/pkg_a, src/pkg_b, src/pkg_c."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve() / "ws"
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(parents=True)
        for pkg in ("pkg_a", "pkg_b", "pkg_c"):
            (self.root / "src" / pkg).mkdir(parents=True)
        self.base = self.config_dir / "drift_workspace.toml"
        self.local = self.config_dir / "drift_workspace.local.toml"
        self.base.write_text(BASE_CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def load_config(self) -> WorkspaceConfig:
        return WorkspaceConfig.from_workspace_dir(self.root, quiet=True)

    def run_enable(self, packages, enabled=True, local=False, dry_run=False):
        return run_primitive_packages_enable(
            self.load_config(), packages, PackageEnableOptions(enabled=enabled, local=local, dry_run=dry_run)
        )


class TestLoaderLocateAndParse(_WorkspaceFixture):
    def test_locate_precedence_static_over_envst_over_none(self) -> None:
        self.assertEqual(locate_workspace_config_file(self.base), self.base)
        envst = self.config_dir / "drift_workspace.envst.toml"
        envst.write_text(ENVST_CONFIG, encoding="utf-8")
        self.assertEqual(locate_workspace_config_file(self.base), self.base)
        self.base.unlink()
        self.assertEqual(locate_workspace_config_file(self.base), envst)
        envst.unlink()
        self.assertIsNone(locate_workspace_config_file(self.base))

    def test_locate_ignores_directories(self) -> None:
        self.local.mkdir()
        self.assertIsNone(locate_workspace_config_file(self.local))

    def test_parse_content_renders_only_envst(self) -> None:
        with patch.dict(os.environ, {"DRIFT_ENABLE_TEST_TARGET": "/opt/x"}):
            envst = parse_workspace_config_content(ENVST_CONFIG, Path("drift_workspace.envst.toml"))
            self.assertEqual(envst["workspace"]["default_target_directory"], "/opt/x")
            static = parse_workspace_config_content(ENVST_CONFIG, Path("drift_workspace.toml"))
            self.assertEqual(static["workspace"]["default_target_directory"], "${DRIFT_ENABLE_TEST_TARGET}")

    def test_parse_content_wraps_errors(self) -> None:
        with self.assertRaises(ConfigError):
            parse_workspace_config_content("[broken\n", Path("drift_workspace.toml"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DRIFT_ENABLE_TEST_UNSET", None)
            with self.assertRaises(ConfigError):
                parse_workspace_config_content('a = "${DRIFT_ENABLE_TEST_UNSET}"', Path("x.envst.toml"))


class TestEditVerification(unittest.TestCase):
    def test_expected_dict_is_deep_copy(self) -> None:
        before: Dict = {"packages": {"enable": {"a": True}}, "workspace": {"x": 1}}
        expected = build_expected_enable_dict(before, {"b": False})
        self.assertEqual(expected["packages"]["enable"], {"a": True, "b": False})
        self.assertEqual(before["packages"]["enable"], {"a": True})

    def test_expected_dict_creates_missing_tables(self) -> None:
        self.assertEqual(build_expected_enable_dict({}, {"a": True}), {"packages": {"enable": {"a": True}}})

    def test_expected_dict_rejects_non_table(self) -> None:
        with self.assertRaises(ConfigError):
            build_expected_enable_dict({"packages": 1}, {"a": True})
        with self.assertRaises(ConfigError):
            build_expected_enable_dict({"packages": {"enable": []}}, {"a": True})

    def test_verification_rejects_extra_missing_or_wrong_changes(self) -> None:
        before = {"packages": {"enable": {"a": False}}, "w": {"x": 1}}
        ok = {"packages": {"enable": {"a": True}}, "w": {"x": 1}}
        assert_workspace_enable_edit_verified(before, ok, {"a": True})
        for bad in (
            {"packages": {"enable": {"a": False}}, "w": {"x": 1}},            # missing update
            {"packages": {"enable": {"a": True}}, "w": {"x": 2}},             # unrelated change
            {"packages": {"enable": {"a": True, "z": True}}, "w": {"x": 1}},  # extra key
            {"packages": {"enable": {"a": "true"}}, "w": {"x": 1}},           # wrong type
        ):
            with self.assertRaises(ConfigError):
                assert_workspace_enable_edit_verified(before, bad, {"a": True})


class TestPlanWorkspaceEnableEdit(_WorkspaceFixture):
    def test_resolve_target(self) -> None:
        t = resolve_workspace_enable_target(self.root, local=False)
        self.assertEqual((t.config_file, t.exists, t.is_local), (self.base, True, False))
        t = resolve_workspace_enable_target(self.root, local=True)
        self.assertEqual((t.config_file, t.exists, t.is_local), (self.local, False, True))
        self.base.unlink()
        with self.assertRaises(ConfigError):
            resolve_workspace_enable_target(self.root, local=False)

    def test_plan_does_not_write(self) -> None:
        before = self.base.read_bytes()
        edit = plan_workspace_enable_edit(self.root, {"pkg_c": True})
        self.assertTrue(edit.has_changes)
        self.assertEqual(self.base.read_bytes(), before)

    def test_corrupted_edit_is_rejected_before_write(self) -> None:
        before = self.base.read_bytes()
        bad_text = BASE_CONFIG.replace('THEME = "nord"', 'THEME = "evil"') + "\n"

        def fake_edit(text, table_keys, updates, create_table_if_missing=False):
            return TomlBoolEditResult(text=bad_text, actions={})

        with patch("drift.config.workspace_enable_edit.set_toml_table_bools", side_effect=fake_edit):
            with self.assertRaises(ConfigError) as cm:
                self.run_enable(["pkg_c"])
        self.assertIn("verification failed", str(cm.exception))
        self.assertEqual(self.base.read_bytes(), before)

    def test_editor_value_error_becomes_config_error(self) -> None:
        self.base.write_text(BASE_CONFIG.replace("pkg_b = false", "pkg_b = [\n  false,\n]"), encoding="utf-8")
        with self.assertRaises(ConfigError):
            plan_workspace_enable_edit(self.root, {"pkg_b": True})

    def test_inline_enable_table_without_header_is_rejected(self) -> None:
        self.base.write_text('[packages]\nenable = { pkg_a = true }\n\n[workspace]\nsource_directory = "src"\n',
                             encoding="utf-8")
        before = self.base.read_bytes()
        with self.assertRaises(ConfigError) as cm:
            plan_workspace_enable_edit(self.root, {"pkg_c": True})
        self.assertIn("manually", str(cm.exception))
        self.assertEqual(self.base.read_bytes(), before)

    def test_malformed_config_is_rejected(self) -> None:
        self.base.write_text("[packages.enable\n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            plan_workspace_enable_edit(self.root, {"pkg_a": True})

    def test_empty_updates_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            plan_workspace_enable_edit(self.root, {})

    def test_diff_labels(self) -> None:
        edit = plan_workspace_enable_edit(self.root, {"pkg_b": True})
        diff = render_workspace_enable_diff(edit, self.root)
        self.assertIn("--- a/config/drift_workspace.toml", diff)
        self.assertIn("+++ b/config/drift_workspace.toml", diff)
        self.assertIn("-pkg_b = false", diff)
        self.assertIn("+pkg_b = true", diff)

        local_edit = plan_workspace_enable_edit(self.root, {"pkg_b": True}, local=True)
        local_diff = render_workspace_enable_diff(local_edit, self.root)
        self.assertIn("--- /dev/null", local_diff)
        self.assertIn("+++ b/config/drift_workspace.local.toml", local_diff)

        noop = plan_workspace_enable_edit(self.root, {"pkg_a": True})
        self.assertEqual(render_workspace_enable_diff(noop, self.root), "")


class TestPrimitivePackagesEnable(_WorkspaceFixture):
    def assert_effective(self, pkg: str, expected: bool) -> None:
        self.assertEqual(self.load_config().is_package_enabled(pkg), expected)

    def test_two_pass_static_insert(self) -> None:
        res1 = self.run_enable(["pkg_c"])
        self.assertEqual(res1.status, "SUCCESS")
        self.assertEqual(res1.actions, {"pkg_c": "inserted"})
        self.assertEqual(res1.config_file, "config/drift_workspace.toml")
        self.assertFalse(res1.created_file)
        self.assert_effective("pkg_c", True)
        text1 = self.base.read_text(encoding="utf-8")
        self.assertEqual(text1, BASE_CONFIG.replace("pkg_b = false\n", "pkg_b = false\npkg_c = true\n", 1))
        mtime1 = self.base.stat().st_mtime_ns

        res2 = self.run_enable(["pkg_c"])
        self.assertEqual(res2.status, "UNCHANGED")
        self.assertEqual(res2.actions, {"pkg_c": "unchanged"})
        self.assertEqual(res2.diff, "")
        self.assertEqual(self.base.read_text(encoding="utf-8"), text1)
        self.assertEqual(self.base.stat().st_mtime_ns, mtime1)

    def test_two_pass_static_update_preserves_comment(self) -> None:
        res = self.run_enable(["pkg_a"], enabled=False)
        self.assertEqual(res.command, "disable")
        self.assertEqual(res.actions, {"pkg_a": "updated"})
        self.assertIn("pkg_a = false   # primary", self.base.read_text(encoding="utf-8"))
        self.assert_effective("pkg_a", False)
        self.assertEqual(self.run_enable(["pkg_a"], enabled=False).status, "UNCHANGED")

    def test_two_pass_envst_template(self) -> None:
        self.base.unlink()
        envst = self.config_dir / "drift_workspace.envst.toml"
        envst.write_text(ENVST_CONFIG, encoding="utf-8")
        with patch.dict(os.environ, {"DRIFT_ENABLE_TEST_TARGET": (self.root / "home").as_posix()}):
            res = self.run_enable(["pkg_b"])
            self.assertEqual(res.config_file, "config/drift_workspace.envst.toml")
            text = envst.read_text(encoding="utf-8")
            self.assertIn('"${DRIFT_ENABLE_TEST_TARGET}"', text)
            self.assertEqual(text, ENVST_CONFIG.replace("pkg_a = true\n", "pkg_a = true\npkg_b = true\n", 1))
            self.assertFalse(self.base.exists())
            self.assert_effective("pkg_b", True)
            self.assertEqual(self.run_enable(["pkg_b"]).status, "UNCHANGED")
            self.assertEqual(envst.read_text(encoding="utf-8"), text)

    def test_two_pass_local_created(self) -> None:
        base_before = self.base.read_bytes()
        res = self.run_enable(["pkg_a"], enabled=False, local=True)
        self.assertTrue(res.created_file)
        self.assertEqual(res.config_file, "config/drift_workspace.local.toml")
        self.assertEqual(self.local.read_text(encoding="utf-8"), "[packages.enable]\npkg_a = false\n")
        self.assertEqual(self.base.read_bytes(), base_before)
        self.assert_effective("pkg_a", False)

        res2 = self.run_enable(["pkg_a"], enabled=False, local=True)
        self.assertEqual(res2.status, "UNCHANGED")
        self.assertFalse(res2.created_file)

    def test_two_pass_local_without_section(self) -> None:
        self.local.write_text("# local\n[settings]\nrollback = \"never\"\n", encoding="utf-8")
        res = self.run_enable(["pkg_c"], local=True)
        self.assertEqual(res.actions, {"pkg_c": "inserted"})
        self.assertEqual(
            self.local.read_text(encoding="utf-8"),
            "# local\n[settings]\nrollback = \"never\"\n\n[packages.enable]\npkg_c = true\n",
        )
        self.assertEqual(self.run_enable(["pkg_c"], local=True).status, "UNCHANGED")

    def test_two_pass_local_envst(self) -> None:
        local_envst = self.config_dir / "drift_workspace.local.envst.toml"
        local_envst.write_text('[packages.enable]\npkg_b = false\n# ${NOT_A_REF_IN_COMMENT}\n', encoding="utf-8")
        with patch.dict(os.environ, {"NOT_A_REF_IN_COMMENT": "x"}):
            res = self.run_enable(["pkg_b"], local=True)
            self.assertEqual(res.config_file, "config/drift_workspace.local.envst.toml")
            self.assertFalse(self.local.exists())
            self.assertIn("pkg_b = true", local_envst.read_text(encoding="utf-8"))
            self.assertEqual(self.run_enable(["pkg_b"], local=True).status, "UNCHANGED")

    def test_crlf_file_preserved(self) -> None:
        self.base.write_bytes(BASE_CONFIG.replace("\n", "\r\n").encode("utf-8"))
        self.run_enable(["pkg_c"])
        data = self.base.read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        self.assertIn(b"pkg_b = false\r\npkg_c = true\r\n", data)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_file_mode_preserved(self) -> None:
        self.base.chmod(0o640)
        self.run_enable(["pkg_c"])
        self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o640)

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_new_local_file_uses_umask_default(self) -> None:
        old_umask = os.umask(0o022)
        try:
            self.run_enable(["pkg_a"], enabled=False, local=True)
            self.assertEqual(stat.S_IMODE(self.local.stat().st_mode), 0o644)
            self.local.chmod(0o600)
            self.run_enable(["pkg_b"], local=True)
            self.assertEqual(stat.S_IMODE(self.local.stat().st_mode), 0o600)
        finally:
            os.umask(old_umask)

    def test_commented_header_is_not_uncommented(self) -> None:
        self.local.write_text("# [packages.enable]\n# gui_apps = false\n", encoding="utf-8")
        res = self.run_enable(["pkg_c"], local=True)
        self.assertEqual(res.actions, {"pkg_c": "inserted"})
        self.assertEqual(
            self.local.read_text(encoding="utf-8"),
            "# [packages.enable]\n# gui_apps = false\n\n[packages.enable]\npkg_c = true\n",
        )
        self.assertEqual(self.run_enable(["pkg_c"], local=True).status, "UNCHANGED")

    def test_missing_section_in_base_is_appended(self) -> None:
        self.local.write_text("[packages.enable]\nDEFAULT = false\n", encoding="utf-8")
        self.base.write_text('[workspace]\nsource_directory = "src"\n', encoding="utf-8")
        res = self.run_enable(["pkg_a"])
        self.assertEqual(res.actions, {"pkg_a": "inserted"})
        self.assertTrue(self.base.read_text(encoding="utf-8").endswith("\n[packages.enable]\npkg_a = true\n"))
        self.assert_effective("pkg_a", True)

    def test_dry_run_writes_nothing(self) -> None:
        before = self.base.read_bytes()
        res = self.run_enable(["pkg_b", "pkg_c"], dry_run=True)
        self.assertTrue(res.dry_run)
        self.assertEqual(res.status, "SUCCESS")
        self.assertIn("+pkg_b = true", res.diff)
        self.assertIn("+pkg_c = true", res.diff)
        self.assertEqual(self.base.read_bytes(), before)

        local_res = self.run_enable(["pkg_c"], local=True, dry_run=True)
        self.assertIn("--- /dev/null", local_res.diff)
        self.assertFalse(self.local.exists())

    def test_shadowed_by_local(self) -> None:
        self.local.write_text("[packages.enable]\npkg_b = false\npkg_c = true\n", encoding="utf-8")
        res = self.run_enable(["pkg_b", "pkg_c"])
        self.assertEqual(res.shadowed_packages, ["pkg_b"])
        self.assert_effective("pkg_b", False)
        self.assertEqual(self.run_enable(["pkg_b"], local=True).shadowed_packages, [])

    def test_enable_requires_source_dir(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            self.run_enable(["ghost"])
        self.assertIn("ghost", str(cm.exception))

    def test_disable_accepts_stale_packages(self) -> None:
        # Listed in [packages.enable] but removed from src/
        import shutil
        shutil.rmtree(self.root / "src" / "pkg_a")
        self.assertEqual(self.run_enable(["pkg_a"], enabled=False).actions, {"pkg_a": "updated"})
        # Present only in install/
        (self.root / "install" / "orphan").mkdir(parents=True)
        self.assertEqual(self.run_enable(["orphan"], enabled=False).actions, {"orphan": "inserted"})
        # Present only in render/
        (self.root / "render" / "rendered_only").mkdir(parents=True)
        self.assertEqual(self.run_enable(["rendered_only"], enabled=False).actions, {"rendered_only": "inserted"})
        with self.assertRaises(ConfigError):
            self.run_enable(["nowhere"], enabled=False)
        with self.assertRaises(ConfigError):
            self.run_enable(["orphan"], enabled=True)

    def test_default_key(self) -> None:
        res = self.run_enable(["DEFAULT"])
        self.assertEqual(res.actions, {"DEFAULT": "updated"})
        self.assertTrue(self.load_config().packages_enable_default)
        self.assert_effective("pkg_c", True)
        self.run_enable(["DEFAULT"], enabled=False)
        self.assertFalse(self.load_config().packages_enable_default)

    def test_names_normalized_and_deduplicated(self) -> None:
        self.assertEqual(normalize_enable_package_names([" a ", "b", "a"]), ["a", "b"])
        with self.assertRaises(ConfigError):
            normalize_enable_package_names([])
        with self.assertRaises(ConfigError):
            normalize_enable_package_names(["a", "  "])
        res = self.run_enable(["pkg_c", "pkg_c", "pkg_b"])
        self.assertEqual(list(res.actions), ["pkg_c", "pkg_b"])

    def test_no_environment_leak(self) -> None:
        env_before = dict(os.environ)
        self.run_enable(["pkg_c"])
        self.assertEqual(dict(os.environ), env_before)

    def test_result_is_valid_toml_for_stdlib_shape(self) -> None:
        self.run_enable(["pkg_c"])
        data = parse_toml(self.base.read_text(encoding="utf-8"))
        self.assertEqual(data["packages"]["enable"],
                         {"DEFAULT": False, "pkg_a": True, "pkg_b": False, "pkg_c": True})


class TestPackageEnableResultFormatText(unittest.TestCase):
    def _res(self, **kwargs):
        from drift.models import PackageEnableResult
        base = dict(command="enable", enabled=True, config_file="config/drift_workspace.toml")
        base.update(kwargs)
        return PackageEnableResult(**base)

    def test_dry_run_diff_and_noop(self) -> None:
        self.assertEqual(self._res(dry_run=True, diff="--- a\n+++ b").format_text(), "--- a\n+++ b")
        self.assertIn("already enabled", self._res(dry_run=True, actions={"a": "unchanged"}).format_text())
        self.assertIn("already disabled", self._res(dry_run=True, enabled=False, command="disable").format_text())

    def test_real_run_lines_and_hints(self) -> None:
        text = self._res(
            actions={"a": "inserted", "b": "unchanged", "DEFAULT": "updated"},
            created_file=True,
            shadowed_packages=["a"],
        ).format_text()
        lines = text.splitlines()
        self.assertEqual(lines[0], "📝 Created config/drift_workspace.toml")
        self.assertIn("✅ Enabled 'a' in config/drift_workspace.toml", lines)
        self.assertIn("✅ Enabled 'DEFAULT' in config/drift_workspace.toml", lines)
        self.assertIn("ℹ️  'b' is already enabled in config/drift_workspace.toml", lines)
        self.assertTrue(any("'a' is overridden by drift_workspace.local.toml" in ln for ln in lines))
        self.assertEqual(lines[-1], "💡 Run 'drift deploy a' to deploy.")

    def test_disable_hint_and_default_only(self) -> None:
        text = self._res(command="disable", enabled=False, actions={"a": "updated"}).format_text()
        self.assertIn("✅ Disabled 'a'", text)
        self.assertIn("drift uninstall a", text)
        only_default = self._res(actions={"DEFAULT": "updated"}).format_text()
        self.assertNotIn("💡", only_default)
        self.assertEqual(self._res(actions={"a": "unchanged"}).format_text(),
                         "ℹ️  'a' is already enabled in config/drift_workspace.toml")


try:
    import rich  # noqa: F401
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

SAMPLE_DIFF = (
    "--- a/config/drift_workspace.toml\n"
    "+++ b/config/drift_workspace.toml\n"
    "@@ -1,2 +1,3 @@\n"
    " [packages.enable]\n"
    "-pkg_b = false\n"
    "+pkg_b = true"
)


class TestDiffColoring(unittest.TestCase):
    def test_style_line_prefix_precedence(self) -> None:
        from drift.cli.actions import _style_unified_diff_line
        ident = lambda x: x  # noqa: E731
        self.assertEqual(_style_unified_diff_line("+++ b/x", ident), "[bold]+++ b/x[/bold]")
        self.assertEqual(_style_unified_diff_line("--- a/x", ident), "[bold]--- a/x[/bold]")
        self.assertEqual(_style_unified_diff_line("+a = true", ident), "[green]+a = true[/green]")
        self.assertEqual(_style_unified_diff_line("-a = false", ident), "[red]-a = false[/red]")
        self.assertEqual(_style_unified_diff_line("@@ -1 +1 @@", ident), "[cyan]@@ -1 +1 @@[/cyan]")
        self.assertEqual(_style_unified_diff_line(" context", ident), " context")
        self.assertEqual(_style_unified_diff_line("", ident), "")

    def test_plain_fallback_is_verbatim(self) -> None:
        from drift.cli.actions import print_unified_diff
        out = StringIO()
        with patch("sys.stdout", out):
            print_unified_diff(SAMPLE_DIFF, use_rich=False)
        self.assertEqual(out.getvalue(), SAMPLE_DIFF + "\n")

    def test_missing_rich_falls_back_to_plain(self) -> None:
        from drift.cli.actions import print_unified_diff
        out = StringIO()
        with patch.dict(sys.modules, {"rich.console": None, "rich.markup": None}), patch("sys.stdout", out):
            print_unified_diff(SAMPLE_DIFF, use_rich=True)
        self.assertEqual(out.getvalue(), SAMPLE_DIFF + "\n")

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_rich_non_tty_output_has_no_ansi_and_keeps_brackets(self) -> None:
        from drift.cli.actions import print_unified_diff
        out = StringIO()
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=False), patch("sys.stdout", out):
            os.environ.pop("FORCE_COLOR", None)
            print_unified_diff(SAMPLE_DIFF, use_rich=True)
        text = out.getvalue()
        self.assertNotIn("\x1b[", text)
        self.assertEqual(text.splitlines(), SAMPLE_DIFF.splitlines())

    @unittest.skipUnless(HAS_RICH, "rich not installed")
    def test_rich_forced_color_emits_ansi(self) -> None:
        from drift.cli.actions import print_unified_diff
        out = StringIO()
        with patch.dict(os.environ, {"FORCE_COLOR": "1", "TERM": "xterm-256color"}), patch("sys.stdout", out):
            os.environ.pop("NO_COLOR", None)
            print_unified_diff(SAMPLE_DIFF, use_rich=True)
        text = out.getvalue()
        self.assertIn("\x1b[32m", text)  # green additions
        self.assertIn("\x1b[31m", text)  # red removals
        self.assertIn("[packages.enable]", text)  # markup-like text escaped, not swallowed


class TestCLIEnableDisable(TestCaseUtilityMixin, _WorkspaceFixture):
    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        from drift.primitives.workspace_repair import repair_drift_workspace
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            repair_drift_workspace(self.root)
        self.base.write_text(BASE_CONFIG, encoding="utf-8")

    def _run(self, runner, argv):
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            runner(["-C", self.root.as_posix()] + argv)
        return out.getvalue()

    def _backends(self):
        from drift.cli import main, run_argparse_cli
        return (("typer", main), ("argparse", run_argparse_cli))

    def test_enable_disable_both_backends(self) -> None:
        for name, runner in self._backends():
            with self.subTest(backend=name):
                self.base.write_text(BASE_CONFIG, encoding="utf-8")
                out = self._run(runner, ["enable", "pkg_c"])
                self.assertIn_stripped("Enabled 'pkg_c'", out)
                self.assertIn_stripped("drift deploy pkg_c", out)
                self.assertIn("pkg_c = true", self.base.read_text(encoding="utf-8"))

                out = self._run(runner, ["enable", "pkg_c"])
                self.assertIn_stripped("already enabled", out)

                out = self._run(runner, ["disable", "pkg_c"])
                self.assertIn_stripped("Disabled 'pkg_c'", out)
                self.assertIn_stripped("drift uninstall pkg_c", out)
                self.assertIn("pkg_c = false", self.base.read_text(encoding="utf-8"))

    def test_local_and_dry_run_both_backends(self) -> None:
        for name, runner in self._backends():
            with self.subTest(backend=name):
                if self.local.exists():
                    self.local.unlink()
                out = self._run(runner, ["disable", "--local", "-n", "pkg_a"])
                self.assertIn("+pkg_a = false", out)
                self.assertIn("/dev/null", out)
                self.assertFalse(self.local.exists())

                out = self._run(runner, ["disable", "--local", "pkg_a"])
                self.assertIn_stripped("Created", out)
                self.assertEqual(self.local.read_text(encoding="utf-8"), "[packages.enable]\npkg_a = false\n")

                out = self._run(runner, ["enable", "pkg_a"])
                self.assertIn_stripped("overridden by drift_workspace.local.toml", out)

    def test_json_output_both_backends(self) -> None:
        for name, runner in self._backends():
            with self.subTest(backend=name):
                self.base.write_text(BASE_CONFIG, encoding="utf-8")
                out = self._run(runner, ["--json", "enable", "--dry-run", "pkg_b"])
                payload = json.loads(out)
                self.assertEqual(payload["command"], "enable")
                self.assertEqual(payload["actions"], {"pkg_b": "updated"})
                self.assertTrue(payload["dry_run"])
                self.assertIn("+pkg_b = true", payload["diff"])
                self.assertEqual(self.base.read_text(encoding="utf-8"), BASE_CONFIG)

    def test_error_exit_codes_both_backends(self) -> None:
        for name, runner in self._backends():
            with self.subTest(backend=name):
                with self.assertRaises(SystemExit) as cm:
                    self._run(runner, ["enable", "ghost"])
                self.assertNotEqual(cm.exception.code, 0)
                with self.assertRaises(SystemExit) as cm:
                    self._run(runner, ["enable"])
                self.assertEqual(cm.exception.code, 2)

    def test_handler_routing(self) -> None:
        from drift.cli import run_argparse_cli
        with patch("drift.cli.cli_handlers.execute_packages_enable") as mock_exec:
            run_argparse_cli(["-C", self.root.as_posix(), "disable", "--local", "--dry-run", "pkg_a", "pkg_b"])
        args, kwargs = mock_exec.call_args
        self.assertEqual(args[1], ["pkg_a", "pkg_b"])
        self.assertEqual(kwargs["enabled"], False)
        self.assertEqual(kwargs["local"], True)
        self.assertEqual(kwargs["dry_run"], True)
        self.assertEqual(kwargs["use_rich"], False)  # argparse backend is plain


if __name__ == "__main__":
    unittest.main()
