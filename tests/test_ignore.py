"""Tests for the DriftIgnore class."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from drift.core.ignore import DriftIgnore
from drift.core.constants import MANAGED_CONFIG_FILES, DRIFT_IGNORE_FILE_NAME


class TestDriftIgnore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pkg_dir = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_managed_config_files_always_ignored_even_without_ignore_file(self) -> None:
        """Verifies that MANAGED_CONFIG_FILES and .drift/ control plane paths are always ignored."""
        # Create a DriftIgnore with no patterns
        ignore = DriftIgnore([])

        # Check that files in .drift/ are always ignored
        self.assertTrue(ignore.match_path(Path(".drift/drift_package.toml")))
        self.assertTrue(ignore.match_path(Path(".drift/.drift_ignore")))
        self.assertTrue(ignore.match_path(Path(".drift/hooks/post_install.sh")))
        self.assertTrue(ignore.match_path(Path(".drift/render/template.json")))

        # Check that regular files resembling drift names outside .drift/ are NOT ignored
        self.assertFalse(ignore.match_path(Path("xdrift_ignore")))
        self.assertFalse(ignore.match_path(Path("xdrift_package.toml")))

        # Check normal files are not ignored
        self.assertFalse(ignore.match_path(Path("normal_file.txt")))
        self.assertFalse(ignore.match_path(Path("subdir/normal_file.txt")))

    def test_filter_deployable_files_excludes_managed_config_files(self) -> None:
        """Verifies that filter_deployable_files filters out MANAGED_CONFIG_FILES, .drift/, and ignored patterns."""
        # Setup files in pkg_dir
        (self.pkg_dir / ".drift").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / ".drift" / "drift_package.toml").touch()
        (self.pkg_dir / ".drift" / ".drift_ignore").touch()
        (self.pkg_dir / "xdrift_ignore").touch()
        (self.pkg_dir / "xdrift_package.toml").touch()
        (self.pkg_dir / "allowed.txt").touch()
        (self.pkg_dir / "ignored_pattern.txt").touch()

        # Let's ignore files ending in _pattern.txt
        ignore = DriftIgnore(["_pattern\\.txt$"])

        deployable = ignore.filter_deployable_files(self.pkg_dir)
        # Convert to set of posix paths for easy comparison
        deployable_set = {p.as_posix() for p in deployable}

        self.assertIn("allowed.txt", deployable_set)
        self.assertIn("xdrift_ignore", deployable_set)
        self.assertIn("xdrift_package.toml", deployable_set)
        self.assertNotIn(".drift/drift_package.toml", deployable_set)
        self.assertNotIn(".drift/.drift_ignore", deployable_set)
        self.assertNotIn("ignored_pattern.txt", deployable_set)

    def test_strip_comments(self) -> None:
        """Verifies comment stripping with/without escaped hash characters."""
        self.assertEqual(DriftIgnore.strip_comments("pattern # comment"), "pattern")
        self.assertEqual(DriftIgnore.strip_comments("pattern \\# not a comment"), "pattern \\# not a comment")
        self.assertEqual(DriftIgnore.strip_comments("   pattern with spaces   "), "pattern with spaces")
        self.assertEqual(DriftIgnore.strip_comments("# whole line comment"), "")
        self.assertEqual(DriftIgnore.strip_comments(""), "")

    def test_load_from_dir_exists(self) -> None:
        """Verifies that load_from_dir correctly loads from root (source) and from .drift/ (compiled/staged)."""
        ignore_content = (
            "pattern1\n"
            "  # a full line comment  \n"
            "pattern2 # an inline comment\n"
            "escaped\\#hash\n"
            "\n"  # blank line
        )
        # 1. Source package directory (is_source=True)
        ignore_file = self.pkg_dir / DRIFT_IGNORE_FILE_NAME
        ignore_file.write_text(ignore_content, encoding="utf-8")

        ignore_src = DriftIgnore.load_from_dir(self.pkg_dir, is_source=True)
        self.assertEqual(ignore_src.patterns, ["pattern1", "pattern2", "escaped\\#hash"])

        # 2. Render / install package directory (is_source=False)
        ignore_file.unlink()
        dot_drift_dir = self.pkg_dir / ".drift"
        dot_drift_dir.mkdir(parents=True, exist_ok=True)
        (dot_drift_dir / DRIFT_IGNORE_FILE_NAME).write_text("internal_pattern\n", encoding="utf-8")

        ignore_internal = DriftIgnore.load_from_dir(self.pkg_dir, is_source=False)
        self.assertEqual(ignore_internal.patterns, ["internal_pattern"])

    def test_load_from_dir_missing_uses_default_ignore_patterns(self) -> None:
        """Verifies that load_from_dir returns a DriftIgnore with default ignore patterns if .drift_ignore doesn't exist."""
        from drift.core.constants import DEFAULT_IGNORE_PATTERNS
        # pkg_dir has no .drift_ignore
        ignore = DriftIgnore.load_from_dir(self.pkg_dir, is_source=True)
        self.assertEqual(ignore.patterns, DEFAULT_IGNORE_PATTERNS)

        # Ensure default patterns match common ignored files
        self.assertTrue(ignore.match_path(Path("README.md")))
        self.assertTrue(ignore.match_path(Path("README.txt")))
        self.assertTrue(ignore.match_path(Path("LICENSE")))
        self.assertTrue(ignore.match_path(Path("COPYING")))
        self.assertTrue(ignore.match_path(Path(".git")))
        self.assertTrue(ignore.match_path(Path(".gitignore")))
        self.assertTrue(ignore.match_path(Path("backup.txt~")))
        self.assertTrue(ignore.match_path(Path(".#lockfile=")))

        # Ensure Python cache, bytecode, and virtual environments are ignored
        self.assertTrue(ignore.match_path(Path("__pycache__")))
        self.assertTrue(ignore.match_path(Path("__pycache__/mod.cpython-312.pyc")))
        self.assertTrue(ignore.match_path(Path("subdir/__pycache__/mod.pyc")))
        self.assertTrue(ignore.match_path(Path("foo.pyc")))
        self.assertTrue(ignore.match_path(Path("foo.pyo")))
        self.assertTrue(ignore.match_path(Path("foo.pyd")))
        self.assertTrue(ignore.match_path(Path("foo$py.class")))
        self.assertTrue(ignore.match_path(Path(".pytest_cache/v/cache")))
        self.assertTrue(ignore.match_path(Path(".mypy_cache/3.10/mod.data.json")))
        self.assertTrue(ignore.match_path(Path(".ruff_cache/content")))
        self.assertTrue(ignore.match_path(Path(".venv/bin/activate")))
        self.assertTrue(ignore.match_path(Path("venv/bin/activate")))
        self.assertTrue(ignore.match_path(Path("subdir/.venv/bin/python")))
        self.assertTrue(ignore.match_path(Path("subdir/venv/bin/python")))
        self.assertFalse(ignore.match_path(Path("venv_helper.py")))

        # Subdirectory README should NOT be ignored because pattern is ^/README.*
        self.assertFalse(ignore.match_path(Path("subdir/README.md")))

        # Ensure .drift/ is ignored
        (self.pkg_dir / ".drift").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / ".drift" / "drift_package.toml").touch()
        (self.pkg_dir / "normal.txt").touch()
        (self.pkg_dir / "README.md").touch()

        deployable = ignore.filter_deployable_files(self.pkg_dir)
        deployable_set = {p.as_posix() for p in deployable}

        self.assertIn("normal.txt", deployable_set)
        self.assertNotIn(".drift/drift_package.toml", deployable_set)
        self.assertNotIn("README.md", deployable_set)

    def test_match_path_regex_matching_logic(self) -> None:
        """Verifies that step 1 (with slash) and step 2 (without slash) matching logic works correctly."""
        # Pattern with slash: matching path_with_slash (/normalized_path)
        # Let's say we have pattern "/sub/" which should match "/sub/file.txt"
        ignore = DriftIgnore(["/sub/"])
        self.assertTrue(ignore.match_path(Path("sub/file.txt")))
        self.assertFalse(ignore.match_path(Path("file_sub.txt")))

        # Pattern without slash: matching only basename
        ignore2 = DriftIgnore(["^file_.*\\.txt$"])
        self.assertTrue(ignore2.match_path(Path("sub/file_abc.txt")))
        self.assertTrue(ignore2.match_path(Path("file_xyz.txt")))
        self.assertFalse(ignore2.match_path(Path("sub/abc_file.txt")))

    def test_match_path_trailing_slash_only_matches_directories(self) -> None:
        """Verifies that trailing slash patterns match when is_dir=True, but do not match files when is_dir=False."""
        # 1. Directory pattern with trailing slash: /cache/
        ignore = DriftIgnore(["/cache/"])
        # Should match directory 'cache' when is_dir=True
        self.assertTrue(ignore.match_path(Path("cache"), is_dir=True))
        # Should match nested directory 'themes/cache' when is_dir=True
        self.assertTrue(ignore.match_path(Path("themes/cache"), is_dir=True))
        # Should NOT match regular file 'cache' or 'themes/cache' when is_dir=False
        self.assertFalse(ignore.match_path(Path("cache"), is_dir=False))
        self.assertFalse(ignore.match_path(Path("themes/cache"), is_dir=False))
        # Files INSIDE cache/ should match because path_with_slash is '/cache/item.txt'
        self.assertTrue(ignore.match_path(Path("cache/item.txt"), is_dir=False))

        # 2. Anchored root directory pattern: ^/dot-config/cache/
        ignore_anchored = DriftIgnore(["^/dot-config/cache/"])
        self.assertTrue(ignore_anchored.match_path(Path("dot-config/cache"), is_dir=True))
        self.assertFalse(ignore_anchored.match_path(Path("dot-config/cache"), is_dir=False))

        # 3. Simple pattern with trailing slash: themes/
        ignore_trailing = DriftIgnore(["themes/"])
        self.assertTrue(ignore_trailing.match_path(Path("themes"), is_dir=True))
        self.assertFalse(ignore_trailing.match_path(Path("themes"), is_dir=False))

    def test_match_path_invalid_regex_logs_warning_and_does_not_crash(self) -> None:
        """Verifies that invalid regex pattern doesn't crash the manager but logs warning."""
        # [invalid pattern (missing closing bracket)
        from drift.core.constants import set_test_mode
        set_test_mode(True, enable_logging=True)
        try:
            ignore = DriftIgnore(["[invalid_pattern"])
            with self.assertLogs("drift.core.ignore", level="WARNING") as cm:
                result = ignore.match_path(Path("somefile.txt"))
                self.assertFalse(result)
                self.assertTrue(any("Invalid regex pattern" in log for log in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_from_dir_rejects_nested_ignores(self) -> None:
        """Verifies that load_from_dir raises ValueError when nested ignore files are present in unauthorized subdirectories."""
        # 1. Root-only ignore in source mode works fine
        (self.pkg_dir / DRIFT_IGNORE_FILE_NAME).write_text("root_pattern", encoding="utf-8")
        ignore = DriftIgnore.load_from_dir(self.pkg_dir, is_source=True)
        self.assertEqual(ignore.patterns, ["root_pattern"])

        # 2. Add a nested .drift_ignore inside a subdirectory
        nested_dir = self.pkg_dir / "subdir"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_ignore = nested_dir / DRIFT_IGNORE_FILE_NAME
        nested_ignore.write_text("nested_pattern", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            DriftIgnore.load_from_dir(self.pkg_dir, is_source=True)
        self.assertIn("Nested ignore files are not allowed", str(ctx.exception))
        self.assertIn(f"Found nested '{DRIFT_IGNORE_FILE_NAME}'", str(ctx.exception))

        # 3. In non-source mode (is_source=False), .drift/.drift_ignore is valid
        nested_ignore.unlink()
        (self.pkg_dir / DRIFT_IGNORE_FILE_NAME).unlink()

        dot_drift = self.pkg_dir / ".drift"
        dot_drift.mkdir(parents=True, exist_ok=True)
        (dot_drift / DRIFT_IGNORE_FILE_NAME).write_text("drift_pattern", encoding="utf-8")
        ignore_compiled = DriftIgnore.load_from_dir(self.pkg_dir, is_source=False)
        self.assertEqual(ignore_compiled.patterns, ["drift_pattern"])

        # But a nested ignore inside another subdirectory in non-source mode raises ValueError
        nested_sub_ignore = nested_dir / DRIFT_IGNORE_FILE_NAME
        nested_sub_ignore.write_text("nested_drift", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            DriftIgnore.load_from_dir(self.pkg_dir, is_source=False)
        self.assertIn("Nested ignore files are not allowed", str(ctx.exception))

    def test_ignore_handler_protocol_compliance(self) -> None:
        """Verifies that DriftIgnore and custom matchers satisfy the IgnoreHandler protocol."""
        from drift.core.ignore import IgnoreHandler

        # 1. DriftIgnore instance satisfies IgnoreHandler
        drift_ignore = DriftIgnore()
        self.assertTrue(isinstance(drift_ignore, IgnoreHandler))

        # 2. Custom duck-typed class satisfying match_path protocol
        class CustomIgnore:
            def match_path(self, rel_path: Path, is_dir: bool = False) -> bool:
                return rel_path.name.endswith(".tmp")

        custom_ignore = CustomIgnore()
        self.assertTrue(isinstance(custom_ignore, IgnoreHandler))
        self.assertTrue(custom_ignore.match_path(Path("test.tmp")))
        self.assertFalse(custom_ignore.match_path(Path("test.txt")))

    def test_load_from_dir_legacy_fallback(self) -> None:
        """Verifies that load_from_dir with is_source=False falls back to root .drift_ignore with deprecation warning."""
        from drift.core.constants import set_test_mode
        set_test_mode(True, enable_logging=True)
        try:
            legacy_ignore = self.pkg_dir / DRIFT_IGNORE_FILE_NAME
            legacy_ignore.write_text("legacy_pattern_1\nlegacy_pattern_2\n", encoding="utf-8")

            with self.assertLogs("drift.core.ignore", level="WARNING") as cm:
                ignore = DriftIgnore.load_from_dir(self.pkg_dir, is_source=False)
                self.assertEqual(ignore.patterns, ["legacy_pattern_1", "legacy_pattern_2"])
                self.assertTrue(any("DEPRECATION" in msg and ".drift_ignore" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_drift_keep_matching_and_deployable_filtering(self) -> None:
        """Verifies that .drift_keep is matched by match_path and handled by filter_deployable_files."""
        from drift.core.constants import DRIFT_KEEP_FILE_NAME

        ignore = DriftIgnore([])

        # 1. match_path should always return True for .drift_keep
        self.assertTrue(ignore.match_path(Path(DRIFT_KEEP_FILE_NAME)))
        self.assertTrue(ignore.match_path(Path("nested") / DRIFT_KEEP_FILE_NAME))
        self.assertTrue(ignore.match_path(Path("a/b/c") / DRIFT_KEEP_FILE_NAME))

        # Setup directory structure:
        # - empty_dir/.drift_keep
        # - populated_dir/.drift_keep + populated_dir/file.txt
        # - nested/empty_sub/.drift_keep
        # - normal.txt
        (self.pkg_dir / "empty_dir").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "empty_dir" / DRIFT_KEEP_FILE_NAME).touch()

        (self.pkg_dir / "populated_dir").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "populated_dir" / DRIFT_KEEP_FILE_NAME).touch()
        (self.pkg_dir / "populated_dir" / "file.txt").touch()

        (self.pkg_dir / "nested" / "empty_sub").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "nested" / "empty_sub" / DRIFT_KEEP_FILE_NAME).touch()

        (self.pkg_dir / "normal.txt").touch()

        # 2. filter_deployable_files with include_empty_dirs=True (default)
        deployable_all = ignore.filter_deployable_files(self.pkg_dir, include_empty_dirs=True)
        self.assertIn(Path("empty_dir"), deployable_all)
        self.assertIn(Path("nested/empty_sub"), deployable_all)
        self.assertIn(Path("populated_dir/file.txt"), deployable_all)
        self.assertIn(Path("normal.txt"), deployable_all)
        # populated_dir has children, so populated_dir itself is not emitted as a leaf
        self.assertNotIn(Path("populated_dir"), deployable_all)
        # .drift_keep stubs are never in deployable_files
        self.assertFalse(any(p.name == DRIFT_KEEP_FILE_NAME for p in deployable_all))

        # 3. filter_deployable_files with include_empty_dirs=False
        deployable_no_empty = ignore.filter_deployable_files(self.pkg_dir, include_empty_dirs=False)
        self.assertNotIn(Path("empty_dir"), deployable_no_empty)
        self.assertNotIn(Path("nested/empty_sub"), deployable_no_empty)
        self.assertIn(Path("populated_dir/file.txt"), deployable_no_empty)
        self.assertIn(Path("normal.txt"), deployable_no_empty)
        self.assertFalse(any(p.name == DRIFT_KEEP_FILE_NAME for p in deployable_no_empty))
