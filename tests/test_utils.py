"""Reusable utility functions and mixins for drift test suites."""

import os
import re
import unittest
from pathlib import Path
from typing import Union, Optional


def strip_ansi(text: str) -> str:
    """Strips ANSI escape codes from the given string."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def normalize_whitespace(text: str) -> str:
    """Collapses consecutive whitespace sequences into a single space and strips ends."""
    return re.sub(r'\s+', ' ', text).strip()


def assert_in_stripped(expected: str, actual: str, test_case: Optional[unittest.TestCase] = None) -> None:
    """
    Asserts that `expected` is contained within `actual` after stripping ANSI codes
    and normalizing whitespace.
    """
    stripped_actual = re.sub(r'\s+', ' ', strip_ansi(actual))
    expected_norm = re.sub(r'\s+', ' ', expected)
    if test_case is not None:
        test_case.assertIn(expected_norm, stripped_actual)
    else:
        assert expected_norm in stripped_actual, f"'{expected_norm}' not found in '{stripped_actual}'"


def add_template_suffix(filename: Union[str, Path], suffix: str = "envst") -> str:
    """
    Inserts a template suffix before the file extension.
    Example: 'drift_package.toml' -> 'drift_package.envst.toml'
    """
    filename_str = str(filename)
    stem, ext = os.path.splitext(filename_str)
    return f"{stem}.{suffix}{ext}"


def add_envst(filename: Union[str, Path]) -> str:
    """Convenience helper to insert '.envst' suffix before file extension."""
    return add_template_suffix(filename, suffix="envst")


class TestCaseUtilityMixin:
    """Mixin for unittest.TestCase classes providing shared assertion helpers."""

    def assertIn_stripped(self, expected: str, actual: str) -> None:
        assert_in_stripped(expected, actual, test_case=self)


class TestTestUtils(TestCaseUtilityMixin, unittest.TestCase):
    """Unit tests for test utility helpers."""

    def test_strip_ansi(self) -> None:
        colored = "\x1b[31mRed text\x1b[0m and \x1b[1;32mBold Green\x1b[0m"
        self.assertEqual(strip_ansi(colored), "Red text and Bold Green")

    def test_normalize_whitespace(self) -> None:
        messy = "  hello   \n\t world  \n"
        self.assertEqual(normalize_whitespace(messy), "hello world")

    def test_assert_in_stripped(self) -> None:
        actual = "\x1b[32m✨ Initialized\n  workspace!\x1b[0m"
        self.assertIn_stripped("Initialized workspace!", actual)

    def test_add_template_suffix_and_add_envst(self) -> None:
        self.assertEqual(add_template_suffix("drift_package.toml", "mustache"), "drift_package.mustache.toml")
        self.assertEqual(add_envst("drift_package.toml"), "drift_package.envst.toml")
        self.assertEqual(add_envst(Path("config.bash")), "config.envst.bash")


if __name__ == "__main__":
    unittest.main()
