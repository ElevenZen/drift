"""Handles drift ignore filtering based on GNU Stow matching logic using pathlib."""

import re
import logging
from pathlib import Path
from typing import List

from .constants import MANAGED_CONFIG_FILES, DRIFT_IGNORE_FILE_NAME

logger = logging.getLogger(__name__)


# TODO: add test that MANAGED_CONFIG_FILES are always ignored, even if not in .drift_ignore
class DriftIgnore:
    """Handles parsing and match evaluation of drift ignore patterns."""

    def __init__(self, patterns: List[str]) -> None:
        self.patterns = patterns
        # Pre-divide patterns depending on whether they contain '/'
        self.set_with_slash = []
        self.set_without_slash = []
        for pattern in self.patterns:
            if "/" in pattern:
                self.set_with_slash.append(pattern)
            else:
                self.set_without_slash.append(pattern)

    @staticmethod
    def strip_comments(line: str) -> str:
        """Strips out comments unless '#' is escaped with a backslash."""
        result = []
        escaped = False
        for char in line:
            if char == "\\" and not escaped:
                escaped = True
                result.append(char)
                continue
            if char == "#" and not escaped:
                break
            escaped = False
            result.append(char)
        return "".join(result).strip()

    # TODO: add test to ensure that this method is called and works correctly when loading from .drift_ignore
    # And returns an empty object if not found, which can ignore MANAGED_CONFIG_FILES when called with filter_deployable_files.
    @classmethod
    def load_from_dir(cls, render_pkg_dir: Path) -> "DriftIgnore":
        """
        Loads ignore PCRE regex patterns from .drift_ignore inside render_pkg_dir.
        Returns an instance of DriftIgnore with the loaded patterns.
        If the file does not exist, returns an instance with an empty pattern list.
        """
        ignore_path = render_pkg_dir / DRIFT_IGNORE_FILE_NAME
        if not ignore_path.exists() or not ignore_path.is_file():
            return cls([])
        patterns = []
        with ignore_path.open("r", encoding="utf-8") as f:
            for line in f:
                line_stripped = cls.strip_comments(line)
                if line_stripped:
                    patterns.append(line_stripped)
        return cls(patterns)


    def filter_deployable_files(self, install_pkg_dir: Path) -> List[Path]:
        """Returns a list of relative Path objects for all deployable files in a package."""
        from .file_utils import tree_relative_files
        return [ rel_file for rel_file in tree_relative_files(install_pkg_dir)
                if rel_file.name not in MANAGED_CONFIG_FILES
                    and not self.match_path(rel_file) ]


    def match_path(self, rel_path: Path) -> bool:
        """Implements GNU Stow's ignore matching algorithm on a relative path."""
        # Special exception: always ignore ignore-related files and config files
        filename = rel_path.name
        if filename in MANAGED_CONFIG_FILES:
            return True

        normalized_rel_path = rel_path.as_posix()
        path_with_slash = "/" + normalized_rel_path
        basename = rel_path.name

        # Match Step 1: Check patterns containing '/' against path_with_slash
        for pattern in self.set_with_slash:
            try:
                if re.search(pattern, path_with_slash):
                    return True
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")

        # Match Step 2: Check remaining patterns against basename
        for pattern in self.set_without_slash:
            try:
                if re.search(pattern, basename):
                    return True
            except re.error as e:
                logger.warning(f"Invalid regex pattern '{pattern}': {e}")

        return False
