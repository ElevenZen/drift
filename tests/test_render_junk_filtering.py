"""Tests for render-time junk filtering (`list_folder_paths(ignore_junk_files=True)`).

Covers:
    * Constant split: `DEFAULT_DRIFT_IGNORE_CONTENT` is unchanged, `DEFAULT_JUNK_IGNORE_PATTERNS` is a strict
      subset of `DEFAULT_IGNORE_PATTERNS` that excludes deploy-policy patterns (README/LICENSE/COPYING).
    * `list_folder_paths` flag semantics: default-off, junk files and junk directories skipped, policy files
      kept, `.drift_keep` stubs and empty-dir placeholders preserved, root-relative matching independent of
      `base_rel` (a workspace living under e.g. `venv/` is not swallowed), composition with `ignore_handler`,
      and zero filesystem side effects.
    * Render integration across input variants (static, `.envst` template, 0-byte file, empty dir, Python
      package hook that writes `__pycache__`, `drift_hooks/` with junk): cold pass renders no junk, warm pass
      is idempotent, and stale junk rendered by older versions is pruned from `render/`.
"""

import hashlib
import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Dict, Iterator, List
from unittest.mock import patch

from drift.config.workspace_config import WorkspaceConfig
from drift.core.constants import (
    DEFAULT_DRIFT_IGNORE_CONTENT,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_JUNK_IGNORE_PATTERNS,
    DRIFT_KEEP_FILE_NAME,
    DirMode,
)
from drift.core.folder_diff import build_junk_ignore_handler, list_folder_paths, strip_root_rel_prefix
from drift.core.ignore import DriftIgnore
from drift.models import RenderOptions
from drift.primitives.render_repo import run_primitive_2_render_packages
from drift.primitives.workspace_init import init_drift_workspace

JUNK_FILES = [
    "__pycache__/mod.cpython-311.pyc",
    "sub/__pycache__/deep.cpython-39.pyc",
    "stray.pyc",
    "sub/stray.pyo",
    "notes.txt~",
    "Thumbs.db",
    "venv/bin/python",
    "CVS/Entries",
]
KEPT_FILES = [
    "README.md",
    "LICENSE",
    "COPYING.txt",
    "dot-bashrc",
    "sub/app.conf",
    "python_helper.py",
]


def _write_tree(root: Path, rel_paths: List[str]) -> None:
    for rel in rel_paths:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {rel}\n", encoding="utf-8")


def _snapshot_tree(root: Path) -> Dict[str, str]:
    """Returns {relative_path: sha256-or-'<dir>'} for every entry under root, skipping `.git` internals."""
    def _digest(p: Path) -> str:
        return "<dir>" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()

    def _entries() -> Iterator[Path]:
        return (p for p in root.rglob("*") if ".git" not in p.relative_to(root).parts)

    return {str(p.relative_to(root)): _digest(p) for p in _entries()}


def _junk_paths_under(root: Path) -> List[str]:
    """Lists entries under root having any path component that matches a junk pattern.

    Uses the raw regexes (not DriftIgnore.match_path, which always treats the '.drift/' control plane as ignored).
    """
    import re

    def _is_junk_part(part: str) -> bool:
        return any(re.search(p, part) for p in DEFAULT_JUNK_IGNORE_PATTERNS if "/" not in p)

    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
        and any(_is_junk_part(part) for part in p.relative_to(root).parts)
    )


# =============================================================================
# Constants
# =============================================================================

class TestJunkPatternConstants(unittest.TestCase):

    def test_junk_patterns_are_strict_subset_without_policy_patterns(self) -> None:
        self.assertTrue(set(DEFAULT_JUNK_IGNORE_PATTERNS) < set(DEFAULT_IGNORE_PATTERNS))
        for policy in ("^/README.*", "^/LICENSE.*", "^/COPYING.*"):
            with self.subTest(policy=policy):
                self.assertIn(policy, DEFAULT_IGNORE_PATTERNS)
                self.assertNotIn(policy, DEFAULT_JUNK_IGNORE_PATTERNS)
        self.assertIn("__pycache__", DEFAULT_JUNK_IGNORE_PATTERNS)
        self.assertIn("\\.py[cod]$", DEFAULT_JUNK_IGNORE_PATTERNS)

    def test_default_ignore_file_content_still_contains_all_sections(self) -> None:
        for marker in ("# Python bytecode and cache files", "# OS metadata",
                       "# Package documentation and licenses", "^/\\.drift/"):
            with self.subTest(marker=marker):
                self.assertIn(marker, DEFAULT_DRIFT_IGNORE_CONTENT)
        self.assertEqual(
            DEFAULT_IGNORE_PATTERNS,
            [ln.strip() for ln in DEFAULT_DRIFT_IGNORE_CONTENT.splitlines()
             if ln.strip() and not ln.strip().startswith("#")],
        )

    def test_junk_handler_keeps_drift_keep_stubs(self) -> None:
        handler = build_junk_ignore_handler()
        self.assertFalse(handler.match_path(Path("empty") / DRIFT_KEEP_FILE_NAME))
        self.assertTrue(handler.match_path(Path("a/__pycache__"), is_dir=True))


# =============================================================================
# list_folder_paths(ignore_junk_files=...)
# =============================================================================

class TestListFolderPathsJunkFlag(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "pkg"
        _write_tree(self.root, JUNK_FILES + KEPT_FILES)
        (self.root / "empty_dir").mkdir()
        _write_tree(self.root, [f"kept_empty/{DRIFT_KEEP_FILE_NAME}"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _listed(self, **kwargs) -> List[str]:
        return sorted(p.as_posix() for p in list_folder_paths(self.root, dir_mode=DirMode.ONLY_EMPTY_DIR, **kwargs))

    def test_flag_defaults_to_off(self) -> None:
        listed = self._listed()
        for rel in JUNK_FILES:
            with self.subTest(rel=rel):
                self.assertIn(rel, listed)

    def test_flag_skips_junk_files_and_directories(self) -> None:
        listed = self._listed(ignore_junk_files=True)
        for rel in JUNK_FILES:
            with self.subTest(rel=rel):
                self.assertNotIn(rel, listed)
        self.assertFalse(any("__pycache__" in p or p.startswith("venv") for p in listed))

    def test_flag_keeps_policy_and_payload_files(self) -> None:
        listed = self._listed(ignore_junk_files=True)
        for rel in KEPT_FILES:
            with self.subTest(rel=rel):
                self.assertIn(rel, listed)

    def test_flag_preserves_empty_dirs_and_keep_stubs(self) -> None:
        listed = self._listed(ignore_junk_files=True)
        self.assertIn("empty_dir", listed)
        self.assertIn(f"kept_empty/{DRIFT_KEEP_FILE_NAME}", listed)

    def test_matching_is_relative_to_src_dir_not_base_rel(self) -> None:
        # The package lives under ancestors named like junk ("venv", "__pycache__"); with an absolute
        # base_rel those ancestors must not cause every entry to be treated as junk.
        nested = Path(self._tmp.name).resolve() / "venv" / "__pycache__" / "pkg"
        _write_tree(nested, ["dot-vimrc", "__pycache__/x.pyc"])
        listed = list_folder_paths(nested, base_rel=nested, dir_mode=DirMode.ONLY_EMPTY_DIR, ignore_junk_files=True)
        self.assertEqual(listed, [nested / "dot-vimrc"])

    def test_strip_root_rel_prefix(self) -> None:
        self.assertEqual(strip_root_rel_prefix(Path("/a/b/c"), Path("/a")), Path("b/c"))
        self.assertEqual(strip_root_rel_prefix(Path("b/c"), Path("")), Path("b/c"))
        self.assertEqual(strip_root_rel_prefix(Path("/x/y"), Path("/a")), Path("/x/y"))

    def test_composes_with_ignore_handler(self) -> None:
        handler = DriftIgnore(patterns=["^/LICENSE$"])
        listed = self._listed(ignore_junk_files=True, ignore_handler=handler)
        self.assertNotIn("LICENSE", listed)
        self.assertNotIn("stray.pyc", listed)
        self.assertIn("README.md", listed)

    def test_listing_is_read_only(self) -> None:
        before = _snapshot_tree(self.root)
        self._listed(ignore_junk_files=True)
        self._listed(ignore_junk_files=True)
        self.assertEqual(_snapshot_tree(self.root), before)


# =============================================================================
# Render integration: cold / warm passes across input variants
# =============================================================================

class TestRenderSkipsJunk(unittest.TestCase):

    PKG = "junk_pkg"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.drift_root = Path(self._tmp.name).resolve() / "ws"
        self.drift_root.mkdir()
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            init_drift_workspace(self.drift_root)
        (self.drift_root / "config" / "drift_workspace.local.toml").write_text(
            f"[packages.enable]\n{self.PKG} = true\n", encoding="utf-8"
        )
        self.pkg_src = self.drift_root / "src" / self.PKG
        self.render_pkg = self.drift_root / "render" / self.PKG
        self.pkg_src.mkdir(parents=True)
        (self.pkg_src / "drift_package.toml").write_text(
            f'[package]\nname = "{self.PKG}"\ninstall_method = "copy"\n'
            f'target_directory = "{(Path(self._tmp.name).resolve() / "home").as_posix()}"\n\n'
            '[env.default]\nGREETING = "hello"\n',
            encoding="utf-8",
        )
        # Input variants: static file, envst template, 0-byte file, empty dir, Python package hook,
        # drift_hooks/ script, plus junk at every level (including in drift_hooks/).
        (self.pkg_src / "dot-static").write_text("static=1\n", encoding="utf-8")
        (self.pkg_src / "dot-greeting.envst.conf").write_text("greeting=${GREETING}\n", encoding="utf-8")
        (self.pkg_src / "dot-empty").write_text("", encoding="utf-8")
        (self.pkg_src / "empty_dir").mkdir()
        (self.pkg_src / "drift_package.py").write_text(
            "def configure_package(context):\n    return context.config\n", encoding="utf-8"
        )
        (self.pkg_src / "drift_hooks").mkdir()
        (self.pkg_src / "drift_hooks" / "post_install.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        _write_tree(self.pkg_src, [
            "__pycache__/drift_package.cpython-311.pyc",
            "sub/__pycache__/x.pyc",
            "dot-static~",
            "drift_hooks/__pycache__/helper.cpython-311.pyc",
        ])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _render(self, **opts) -> None:
        cfg = WorkspaceConfig.from_workspace_dir(self.drift_root, quiet=True)
        with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
            res = run_primitive_2_render_packages(cfg, target_pkgs=[self.PKG], options=RenderOptions(silent=True, **opts))
        self.assertEqual(res.status, "SUCCESS", res.error_message)
        return res

    def test_cold_pass_renders_payload_without_junk(self) -> None:
        self._render()
        self.assertEqual((self.render_pkg / "dot-static").read_text(encoding="utf-8"), "static=1\n")
        self.assertEqual((self.render_pkg / "dot-greeting.conf").read_text(encoding="utf-8"), "greeting=hello\n")
        self.assertEqual((self.render_pkg / "dot-empty").read_bytes(), b"")
        self.assertTrue((self.render_pkg / ".drift" / "hooks" / "post_install.sh").is_file())
        self.assertEqual(_junk_paths_under(self.render_pkg), [])

    def test_warm_pass_is_idempotent(self) -> None:
        self._render()
        after_cold = _snapshot_tree(self.render_pkg)
        res = self._render()
        self.assertEqual(_snapshot_tree(self.render_pkg), after_cold)
        self.assertEqual(res.packages[0].status, "UP_TO_DATE")
        self.assertEqual(_junk_paths_under(self.render_pkg), [])

    def test_dry_run_plans_no_junk_actions(self) -> None:
        res = self._render(dry_run=True)
        planned = [str(a.dst_path) for a in res.packages[0].actions if a.dst_path is not None]
        self.assertFalse(any("__pycache__" in p or p.endswith((".pyc", "~")) for p in planned), planned)
        self.assertFalse(self.render_pkg.exists() and _junk_paths_under(self.render_pkg))

    def test_stale_junk_from_older_versions_is_pruned(self) -> None:
        # Simulate a render produced before junk filtering existed.
        with patch("drift.core.folder_diff.build_junk_ignore_handler", return_value=None):
            self._render()
        self.assertTrue((self.render_pkg / "__pycache__").exists())

        self._render()
        self.assertEqual(_junk_paths_under(self.render_pkg), [])
        self.assertTrue((self.render_pkg / "dot-static").is_file())

    def test_removing_junk_from_source_does_not_change_render(self) -> None:
        self._render()
        after_with_junk = _snapshot_tree(self.render_pkg)
        shutil.rmtree(self.pkg_src / "__pycache__")
        (self.pkg_src / "dot-static~").unlink()
        self._render()
        self.assertEqual(_snapshot_tree(self.render_pkg), after_with_junk)


if __name__ == "__main__":
    unittest.main()
