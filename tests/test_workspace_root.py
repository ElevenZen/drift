"""Tests for marker-based drift workspace root discovery.

Covers:
    * Pure predicates (`check_is_drift_workspace_root`) across every marker variant and negative layouts.
    * Upward discovery (`find_drift_workspace_root` / `resolve_drift_workspace_root`): nearest-wins nesting,
      Git independence, relative / symlinked / non-existent start paths, purity and idempotency.
    * CLI integration across both Typer (`main`) and Argparse (`run_argparse_cli`) backends: workspaces nested
      in a parent Git repository (e.g. `samples/<name>/`), cwd vs `-C` start dirs, not-found exit codes,
      removal of `--no-git-root`, literal `init`, and a two-pass cold/warm `render` from a subdirectory.
"""

import hashlib
import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, Iterator, List
from unittest.mock import patch

from drift.cli import main
from drift.cli.argparse_backend import run_argparse_cli
from drift.cli.completion import generate_completion_script
from drift.cli.completion.workspace_markers import get_workspace_marker_rel_paths
from drift.core.constants import (
    CONFIG_DIR_NAME,
    CURRENT_WORKSPACE_CONFIG_FILE_NAMES,
    LEGACY_WORKSPACE_CONFIG_FILE_NAMES,
    WORKSPACE_ROOT_MARKER_FILE_NAMES,
    ExitCode,
)
from drift.core.exceptions import ConfigError, DriftError, WorkspaceNotFoundError
from drift.primitives.workspace_init import init_drift_workspace
from drift.utils.workspace_root import (
    check_is_drift_workspace_root,
    find_drift_workspace_root,
    iter_self_and_ancestor_dirs,
    resolve_drift_workspace_root,
)
from tests.test_utils import TestCaseUtilityMixin

CLI_BACKENDS: Dict[str, Callable[[List[str]], None]] = {
    "typer": main,
    "argparse": run_argparse_cli,
}


def _make_marker(root: Path, name: str = "drift_workspace.toml") -> Path:
    """Creates `root/config/<name>` (an empty but valid TOML file) and returns `root`."""
    (root / CONFIG_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (root / CONFIG_DIR_NAME / name).write_text("", encoding="utf-8")
    return root


def _snapshot_tree(root: Path) -> Dict[str, str]:
    """Returns {relative_path: sha256-or-'<dir>'} for every entry under root, skipping `.git` internals."""
    def _digest(p: Path) -> str:
        return "<dir>" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()

    def _entries() -> Iterator[Path]:
        return (p for p in root.rglob("*") if ".git" not in p.relative_to(root).parts)

    return {p.relative_to(root).as_posix(): _digest(p) for p in _entries()}


class _TempDirTestCase(unittest.TestCase):
    """Provides an isolated, resolved temporary directory that is not itself inside a workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        if find_drift_workspace_root(self.tmp.parent) is not None:
            self.skipTest("Temporary directory is nested inside an existing drift workspace.")

    def tearDown(self) -> None:
        self._tmp.cleanup()


# =============================================================================
# Layer 1: Pure predicates & ancestor enumeration
# =============================================================================

class TestCheckIsDriftWorkspaceRoot(_TempDirTestCase):

    def test_marker_set_covers_current_and_legacy_names(self) -> None:
        self.assertEqual(
            set(WORKSPACE_ROOT_MARKER_FILE_NAMES),
            set(CURRENT_WORKSPACE_CONFIG_FILE_NAMES) | set(LEGACY_WORKSPACE_CONFIG_FILE_NAMES),
        )
        self.assertIn("drift_workspace.toml", WORKSPACE_ROOT_MARKER_FILE_NAMES)
        self.assertIn("drift_workspace.envst.toml", WORKSPACE_ROOT_MARKER_FILE_NAMES)

    def test_each_marker_variant_alone_marks_root(self) -> None:
        for name in WORKSPACE_ROOT_MARKER_FILE_NAMES:
            with self.subTest(marker=name):
                root = _make_marker(self.tmp / name.replace(".", "_"), name)
                self.assertTrue(check_is_drift_workspace_root(root))

    def test_negative_layouts_are_not_roots(self) -> None:
        cases: Dict[str, Callable[[Path], None]] = {
            "empty_dir": lambda d: None,
            "src_only": lambda d: (d / "src").mkdir(),
            "render_and_install_only": lambda d: [(d / x).mkdir() for x in ("render", "install")],
            "empty_config_dir": lambda d: (d / CONFIG_DIR_NAME).mkdir(),
            "marker_is_directory": lambda d: (d / CONFIG_DIR_NAME / "drift_workspace.toml").mkdir(parents=True),
            "unrelated_config_files": lambda d: [
                (d / CONFIG_DIR_NAME).mkdir(),
                (d / CONFIG_DIR_NAME / "secrets.env").write_text(""),
                (d / CONFIG_DIR_NAME / "drift_workspace.py").write_text(""),
                (d / CONFIG_DIR_NAME / "drift_workspace.backup.toml").write_text(""),
            ],
            "marker_outside_config_dir": lambda d: (d / "drift_workspace.toml").write_text(""),
        }
        for label, build in cases.items():
            with self.subTest(layout=label):
                d = self.tmp / label
                d.mkdir()
                build(d)
                self.assertFalse(check_is_drift_workspace_root(d))

    def test_nonexistent_dir_is_not_root(self) -> None:
        self.assertFalse(check_is_drift_workspace_root(self.tmp / "missing"))


class TestIterSelfAndAncestorDirs(_TempDirTestCase):

    def test_yields_resolved_start_then_parents_to_anchor(self) -> None:
        start = self.tmp / "a" / "b"
        start.mkdir(parents=True)
        dirs = list(iter_self_and_ancestor_dirs(start))
        self.assertEqual(dirs[0], start)
        self.assertEqual(dirs[1], self.tmp / "a")
        self.assertEqual(dirs[-1], Path(start.anchor))
        self.assertEqual(len(dirs), len(start.parts))

    def test_relative_start_is_resolved_against_cwd(self) -> None:
        (self.tmp / "rel").mkdir()
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            self.assertEqual(next(iter(iter_self_and_ancestor_dirs(Path("rel")))), self.tmp / "rel")
        finally:
            os.chdir(old_cwd)


# =============================================================================
# Layer 2/3: Discovery & raising resolver
# =============================================================================

class TestFindAndResolveWorkspaceRoot(_TempDirTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.outer = _make_marker(self.tmp / "outer")
        self.inner = _make_marker(self.outer / "samples" / "pkgit")
        (self.inner / "src" / "pkg" / "deep").mkdir(parents=True)
        (self.outer / "src" / "other").mkdir(parents=True)

    def test_root_itself_resolves_to_root(self) -> None:
        self.assertEqual(find_drift_workspace_root(self.inner), self.inner)
        self.assertEqual(resolve_drift_workspace_root(self.outer), self.outer)

    def test_deep_subdirectories_resolve_to_nearest_root(self) -> None:
        cases = {
            self.inner / "src" / "pkg" / "deep": self.inner,
            self.inner / CONFIG_DIR_NAME: self.inner,
            self.outer / "samples": self.outer,
            self.outer / "src" / "other": self.outer,
        }
        for start, expected in cases.items():
            with self.subTest(start=start.relative_to(self.tmp).as_posix()):
                self.assertEqual(resolve_drift_workspace_root(start), expected)

    def test_nonexistent_start_dir_resolves_via_existing_ancestors(self) -> None:
        self.assertEqual(resolve_drift_workspace_root(self.inner / "src" / "not" / "yet"), self.inner)

    def test_relative_start_dir(self) -> None:
        old_cwd = os.getcwd()
        try:
            os.chdir(self.outer)
            self.assertEqual(resolve_drift_workspace_root(Path("samples/pkgit/src")), self.inner)
            self.assertEqual(resolve_drift_workspace_root(Path(".")), self.outer)
        finally:
            os.chdir(old_cwd)

    def test_symlinked_start_dir_resolves_to_real_root(self) -> None:
        link = self.tmp / "link_to_pkg"
        try:
            link.symlink_to(self.inner / "src" / "pkg", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are not supported on this platform.")
        self.assertEqual(resolve_drift_workspace_root(link), self.inner)

    def test_not_found_returns_none_and_resolver_raises(self) -> None:
        lone = self.tmp / "lonely" / "dir"
        lone.mkdir(parents=True)
        self.assertIsNone(find_drift_workspace_root(lone))
        with self.assertRaises(WorkspaceNotFoundError) as cm:
            resolve_drift_workspace_root(lone)
        err = cm.exception
        self.assertIsInstance(err, DriftError)
        self.assertIsInstance(err, ConfigError)
        self.assertIsInstance(err, FileNotFoundError)
        self.assertEqual(err.exit_code, ExitCode.CONFIG_ERROR)
        self.assertIn(f"'{lone}'", str(err))
        self.assertIn("drift init", str(err))
        self.assertIn("-C", str(err))

    def test_discovery_is_independent_of_git(self) -> None:
        # Workspace nested inside a Git repo resolves to the workspace, not the Git toplevel.
        repo = self.tmp / "repo"
        ws = _make_marker(repo / "samples" / "demo")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        self.assertEqual(resolve_drift_workspace_root(ws / CONFIG_DIR_NAME), ws)
        # A Git toplevel without a marker is not a workspace.
        self.assertIsNone(find_drift_workspace_root(repo))
        # A workspace without any Git repository is still discovered.
        self.assertEqual(resolve_drift_workspace_root(self.inner), self.inner)

    def test_discovery_is_pure_and_idempotent(self) -> None:
        before_tree = _snapshot_tree(self.tmp)
        before_env = dict(os.environ)
        before_cwd = os.getcwd()
        start = self.inner / "src" / "pkg" / "deep"
        results = [resolve_drift_workspace_root(start) for _ in range(3)]
        self.assertEqual(results, [self.inner] * 3)
        self.assertEqual(_snapshot_tree(self.tmp), before_tree)
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(os.getcwd(), before_cwd)

    def test_removing_inner_marker_falls_back_to_outer(self) -> None:
        start = self.inner / "src" / "pkg"
        self.assertEqual(resolve_drift_workspace_root(start), self.inner)
        (self.inner / CONFIG_DIR_NAME / "drift_workspace.toml").unlink()
        self.assertEqual(resolve_drift_workspace_root(start), self.outer)


# =============================================================================
# CLI integration (both backends)
# =============================================================================

class TestCliWorkspaceDiscovery(TestCaseUtilityMixin, _TempDirTestCase):
    """A drift workspace at `<git repo>/samples/pkgit` must be usable from anywhere inside it."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        for cmd in (["git", "init"], ["git", "config", "user.name", "test_user"],
                    ["git", "config", "user.email", "test_user@example.com"]):
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True)
        (self.repo / "src" / "not_a_package").mkdir(parents=True)  # mimics the drift source repo itself
        self.ws = self.repo / "samples" / "pkgit"
        self.ws.mkdir(parents=True)
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            init_drift_workspace(self.ws)
        self._old_cwd = os.getcwd()

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        super().tearDown()

    def _run_silenced(self, runner: Callable[[List[str]], None], argv: List[str]) -> None:
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            runner(argv)

    def _captured_status_root(self, runner: Callable[[List[str]], None], argv: List[str]) -> Path:
        with patch("drift.cli.cli_handlers.execute_status") as mock_status:
            self._run_silenced(runner, argv)
        self.assertEqual(mock_status.call_count, 1)
        return mock_status.call_args.args[0]

    def test_dash_c_start_dirs_resolve_to_nested_workspace(self) -> None:
        starts = [self.ws, self.ws / "src", self.ws / CONFIG_DIR_NAME, self.ws / "render", self.ws / "install"]
        for backend, runner in CLI_BACKENDS.items():
            for start in starts:
                with self.subTest(backend=backend, start=start.relative_to(self.repo).as_posix()):
                    self.assertEqual(self._captured_status_root(runner, ["-C", start.as_posix(), "status"]), self.ws)

    def test_cwd_start_dir_resolves_to_nested_workspace(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                os.chdir(self.ws / "src")
                self.assertEqual(self._captured_status_root(runner, ["status"]), self.ws)

    def test_new_package_from_subdir_lands_in_nested_workspace(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                pkg = f"pkg_{backend}"
                self._run_silenced(runner, ["-C", (self.ws / CONFIG_DIR_NAME).as_posix(), "new", pkg])
                self.assertTrue((self.ws / "src" / pkg / "drift_package.toml").is_file())
                self.assertFalse((self.repo / "src" / pkg).exists())

    def test_git_root_without_marker_exits_with_config_error(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                stderr = StringIO()
                with patch("sys.stdout", StringIO()), patch("sys.stderr", stderr), \
                        patch("drift.cli.cli_handlers.execute_status") as mock_status:
                    with self.assertRaises(SystemExit) as cm:
                        runner(["-C", (self.repo / "src").as_posix(), "status"])
                self.assertEqual(cm.exception.code, ExitCode.CONFIG_ERROR)
                mock_status.assert_not_called()
                self.assertIn_stripped("No Drift workspace found", stderr.getvalue())

    def test_no_git_root_flag_is_removed(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        runner(["-C", self.ws.as_posix(), "--no-git-root", "status"])
                self.assertEqual(cm.exception.code, 2)

    def test_init_from_cwd_is_literal_inside_git_repo(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                target = self.repo / "samples" / f"fresh_{backend}"
                target.mkdir()
                os.chdir(target)
                self._run_silenced(runner, ["init"])
                self.assertTrue(check_is_drift_workspace_root(target))
                self.assertTrue((target / "render" / ".git").is_dir())
                self.assertFalse((target / ".git").exists())
                self.assertFalse(check_is_drift_workspace_root(self.repo))

    def test_two_pass_render_from_subdir_is_idempotent(self) -> None:
        for backend, runner in CLI_BACKENDS.items():
            with self.subTest(backend=backend):
                pkg = f"render_{backend}"
                start = (self.ws / "src").as_posix()
                self._run_silenced(runner, ["-C", start, "new", pkg])
                (self.ws / "src" / pkg / "dot-samplerc").write_text("sample=1\n", encoding="utf-8")

                # Pass 1 (cold): renders into the nested workspace's render/ sandbox only.
                self._run_silenced(runner, ["-C", start, "render", pkg])
                rendered = self.ws / "render" / pkg / "dot-samplerc"
                self.assertEqual(rendered.read_text(encoding="utf-8"), "sample=1\n")
                self.assertFalse((self.repo / "render").exists())
                after_cold = _snapshot_tree(self.ws / "render")

                # Pass 2 (warm): identical re-run produces zero filesystem changes.
                self._run_silenced(runner, ["-C", start, "render", pkg])
                self.assertEqual(_snapshot_tree(self.ws / "render"), after_cold)


# =============================================================================
# Completion scripts share the same marker definition
# =============================================================================

class TestCompletionWorkspaceMarkers(unittest.TestCase):

    def test_marker_rel_paths_derive_from_constant(self) -> None:
        self.assertEqual(
            get_workspace_marker_rel_paths(),
            tuple(f"{CONFIG_DIR_NAME}/{n}" for n in WORKSPACE_ROOT_MARKER_FILE_NAMES),
        )

    def test_every_shell_script_embeds_all_markers_and_no_loose_heuristics(self) -> None:
        for shell in ("bash", "zsh", "fish", "nu"):
            with self.subTest(shell=shell):
                script = generate_completion_script(shell)
                for rel in get_workspace_marker_rel_paths():
                    self.assertIn(rel, script)
                self.assertNotIn('-d "$cur_path/render"', script)
                self.assertNotIn('-d "$root/render"', script)
                self.assertNotIn("--no-git-root", script)


@unittest.skipUnless(
    subprocess.run(["bash", "--version"], capture_output=True).returncode == 0 if os.name != "nt" else False,
    "bash is required for functional completion tests",
)
class TestBashCompletionDiscovery(_TempDirTestCase):
    """Executes the generated Bash package completer against real directory layouts."""

    def setUp(self) -> None:
        super().setUp()
        self.script = self.tmp / "drift.bash"
        self.script.write_text(generate_completion_script("bash"), encoding="utf-8")
        self.repo = self.tmp / "repo"
        (self.repo / "src" / "not_a_package").mkdir(parents=True)
        self.ws = _make_marker(self.repo / "samples" / "pkgit")
        for pkg in ("alpha", "beta"):
            (self.ws / "src" / pkg / "deep").mkdir(parents=True)

    def _complete(self, cwd: Path, words: str = "", cword: int = 0) -> List[str]:
        snippet = f"source '{self.script}' 2>/dev/null; COMP_WORDS=({words}); COMP_CWORD={cword}; _drift_packages"
        res = subprocess.run(["bash", "-c", snippet], cwd=cwd, capture_output=True, text=True, check=True)
        return sorted(filter(None, res.stdout.splitlines()))

    def test_nested_workspace_packages_from_deep_cwd(self) -> None:
        self.assertEqual(self._complete(self.ws / "src" / "alpha" / "deep"), ["alpha", "beta"])

    def test_src_without_marker_yields_nothing(self) -> None:
        self.assertEqual(self._complete(self.repo), [])

    def test_dash_c_relative_start_dir(self) -> None:
        self.assertEqual(self._complete(self.repo, "drift -C samples/pkgit/src/beta deploy ''", 4), ["alpha", "beta"])

    def test_dash_c_missing_dir_yields_nothing(self) -> None:
        self.assertEqual(self._complete(self.repo, "drift -C does/not/exist deploy ''", 4), [])


if __name__ == "__main__":
    unittest.main()
