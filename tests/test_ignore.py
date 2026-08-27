"""Tests for the DriftIgnore class."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from drift.ignore import DriftIgnore
from drift.constants import MANAGED_CONFIG_FILES, DRIFT_IGNORE_FILE_NAME


class TestDriftIgnore(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pkg_dir = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_managed_config_files_always_ignored_even_without_ignore_file(self) -> None:
        """Verifies that MANAGED_CONFIG_FILES are always ignored even if .drift_ignore is empty or missing."""
        # Create a DriftIgnore with no patterns
        ignore = DriftIgnore([])

        # Check each managed config file is matched (ignored) by match_path
        for filename in MANAGED_CONFIG_FILES:
            self.assertTrue(ignore.match_path(Path(filename)))
            self.assertTrue(ignore.match_path(Path("subdir") / filename))

        # Check that files like '.drift_ignore' are ignored, while 'xdrift_ignore' is NOT ignored
        self.assertTrue(ignore.match_path(Path(".drift_ignore")))
        self.assertTrue(ignore.match_path(Path("drift_package.toml")))
        self.assertFalse(ignore.match_path(Path("xdrift_ignore")))
        self.assertFalse(ignore.match_path(Path("xdrift_package.toml")))
        self.assertFalse(ignore.match_path(Path("x.stow-local-ignore")))

        # Check a normal file is not ignored
        self.assertFalse(ignore.match_path(Path("normal_file.txt")))
        self.assertFalse(ignore.match_path(Path("subdir/normal_file.txt")))

    def test_filter_deployable_files_excludes_managed_config_files(self) -> None:
        """Verifies that filter_deployable_files filters out MANAGED_CONFIG_FILES and ignores according to patterns."""
        # Setup files in pkg_dir
        (self.pkg_dir / "drift_package.toml").touch()
        (self.pkg_dir / ".drift_ignore").touch()
        (self.pkg_dir / ".stow-local-ignore").touch()
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
        self.assertNotIn("drift_package.toml", deployable_set)
        self.assertNotIn(".drift_ignore", deployable_set)
        self.assertNotIn(".stow-local-ignore", deployable_set)
        self.assertNotIn("ignored_pattern.txt", deployable_set)

    def test_strip_comments(self) -> None:
        """Verifies comment stripping with/without escaped hash characters."""
        self.assertEqual(DriftIgnore.strip_comments("pattern # comment"), "pattern")
        self.assertEqual(DriftIgnore.strip_comments("pattern \\# not a comment"), "pattern \\# not a comment")
        self.assertEqual(DriftIgnore.strip_comments("   pattern with spaces   "), "pattern with spaces")
        self.assertEqual(DriftIgnore.strip_comments("# whole line comment"), "")
        self.assertEqual(DriftIgnore.strip_comments(""), "")

    def test_load_from_dir_exists(self) -> None:
        """Verifies that load_from_dir correctly loads, parses, and strips patterns from .drift_ignore."""
        ignore_content = (
            "pattern1\n"
            "  # a full line comment  \n"
            "pattern2 # an inline comment\n"
            "escaped\\#hash\n"
            "\n"  # blank line
        )
        ignore_file = self.pkg_dir / DRIFT_IGNORE_FILE_NAME
        ignore_file.write_text(ignore_content, encoding="utf-8")

        ignore = DriftIgnore.load_from_dir(self.pkg_dir)
        self.assertEqual(ignore.patterns, ["pattern1", "pattern2", "escaped\\#hash"])

    def test_load_from_dir_missing_uses_default_stow_ignore_patterns(self) -> None:
        """Verifies that load_from_dir returns a DriftIgnore with default Stow ignore patterns if .drift_ignore doesn't exist."""
        from drift.constants import DEFAULT_STOW_IGNORE_PATTERNS
        # pkg_dir has no .drift_ignore
        ignore = DriftIgnore.load_from_dir(self.pkg_dir)
        self.assertEqual(ignore.patterns, DEFAULT_STOW_IGNORE_PATTERNS)

        # Ensure default patterns match common Stow ignored files
        self.assertTrue(ignore.match_path(Path("README.md")))
        self.assertTrue(ignore.match_path(Path("README.txt")))
        self.assertTrue(ignore.match_path(Path("LICENSE")))
        self.assertTrue(ignore.match_path(Path("COPYING")))
        self.assertTrue(ignore.match_path(Path(".git")))
        self.assertTrue(ignore.match_path(Path(".gitignore")))
        self.assertTrue(ignore.match_path(Path("backup.txt~")))
        self.assertTrue(ignore.match_path(Path(".#lockfile=")))

        # Subdirectory README should NOT be ignored because pattern is ^/README.*
        self.assertFalse(ignore.match_path(Path("subdir/README.md")))

        # Ensure MANAGED_CONFIG_FILES are ignored
        (self.pkg_dir / "drift_package.toml").touch()
        (self.pkg_dir / "normal.txt").touch()
        (self.pkg_dir / "README.md").touch()

        deployable = ignore.filter_deployable_files(self.pkg_dir)
        deployable_set = {p.as_posix() for p in deployable}

        self.assertIn("normal.txt", deployable_set)
        self.assertNotIn("drift_package.toml", deployable_set)
        self.assertNotIn("README.md", deployable_set)

    def test_export_stow_ignore_patterns_and_content(self) -> None:
        """Verifies that export_stow_ignore_patterns includes MANAGED_CONFIG_FILES and patterns."""
        ignore = DriftIgnore(["^/custom_file\\.txt$", "\\.log$"])
        exported = ignore.export_stow_ignore_patterns()

        # Verify MANAGED_CONFIG_FILES are present in exported list with escaped dots
        self.assertIn(r"^/drift_package\.toml$", exported)
        self.assertIn(r"^/\.drift_ignore$", exported)
        self.assertIn(r"^/\.stow-local-ignore$", exported)

        # Verify custom patterns are present
        self.assertIn("^/custom_file\\.txt$", exported)
        self.assertIn("\\.log$", exported)

        content = ignore.generate_stow_local_ignore_content()
        self.assertIn("# .stow-local-ignore - Generated by Drift", content)
        self.assertIn(r"^/drift_package\.toml$", content)
        self.assertIn(r"^/\.drift_ignore$", content)
        self.assertIn(r"^/custom_file\.txt$", content)

        # Check matching behavior of exported patterns with re.search
        import re
        self.assertTrue(bool(re.search(r"^/\.drift_ignore$", "/.drift_ignore")))
        self.assertFalse(bool(re.search(r"^/\.drift_ignore$", "/xdrift_ignore")))
        self.assertFalse(bool(re.search(r"^/\.drift_ignore$", "/x.drift_ignore")))
        self.assertFalse(bool(re.search(r"^/\.drift_ignore$", "/sub/.drift_ignore")))
        self.assertTrue(bool(re.search(r"^/drift_package\.toml$", "/drift_package.toml")))
        self.assertFalse(bool(re.search(r"^/drift_package\.toml$", "/xdrift_package.toml")))

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

    def test_match_path_invalid_regex_logs_warning_and_does_not_crash(self) -> None:
        """Verifies that invalid regex pattern doesn't crash the manager but logs warning."""
        # [invalid pattern (missing closing bracket)
        from drift.constants import set_test_mode
        set_test_mode(True, enable_logging=True)
        try:
            ignore = DriftIgnore(["[invalid_pattern"])
            with self.assertLogs("drift.ignore", level="WARNING") as cm:
                result = ignore.match_path(Path("somefile.txt"))
                self.assertFalse(result)
                self.assertTrue(any("Invalid regex pattern" in log for log in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)

    def test_load_from_dir_rejects_nested_ignores(self) -> None:
        """Verifies that load_from_dir raises ValueError when nested ignore files (.drift_ignore or .driftignore) are present in subdirectories."""
        # 1. Root-only ignore works fine
        (self.pkg_dir / DRIFT_IGNORE_FILE_NAME).write_text("root_pattern", encoding="utf-8")
        ignore = DriftIgnore.load_from_dir(self.pkg_dir)
        self.assertEqual(ignore.patterns, ["root_pattern"])

        # 2. Add a nested .drift_ignore inside a subdirectory
        nested_dir = self.pkg_dir / "subdir"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_ignore = nested_dir / DRIFT_IGNORE_FILE_NAME
        nested_ignore.write_text("nested_pattern", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            DriftIgnore.load_from_dir(self.pkg_dir)
        self.assertIn("Nested ignore files are not allowed", str(ctx.exception))
        self.assertIn(f"Found nested '{DRIFT_IGNORE_FILE_NAME}'", str(ctx.exception))

        # Clean up nested .drift_ignore and try with nested .driftignore
        nested_ignore.unlink()

        nested_driftignore = nested_dir / ".driftignore"
        nested_driftignore.write_text("nested_pattern_2", encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            DriftIgnore.load_from_dir(self.pkg_dir)
        self.assertIn("Nested ignore files are not allowed", str(ctx.exception))
        self.assertIn("Found nested '.driftignore'", str(ctx.exception))

