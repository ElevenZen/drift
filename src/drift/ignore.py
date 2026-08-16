"""Handles drift ignore filtering based on GNU Stow matching logic."""

import os
import re
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


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

    @classmethod
    def load_from_dir(cls, render_pkg_dir: str) -> "DriftIgnore":
        """Loads ignore PCRE regex patterns from .drift_ignore inside render_pkg_dir."""
        ignore_path = os.path.join(render_pkg_dir, ".drift_ignore")
        if not os.path.exists(ignore_path) or not os.path.isfile(ignore_path):
            return cls([])
        patterns = []
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line_stripped = cls.strip_comments(line)
                if line_stripped:
                    patterns.append(line_stripped)
        return cls(patterns)

    def match_path(self, rel_path: str) -> bool:
        """Implements GNU Stow's ignore matching algorithm on a relative path."""
        # Special exception: always ignore ignore-related files and config files
        from .constants import IGNORED_FILENAMES
        filename = os.path.basename(rel_path)
        if filename in IGNORED_FILENAMES:
            return True

        normalized_rel_path = rel_path.replace(os.sep, "/")
        path_with_slash = "/" + normalized_rel_path
        basename = os.path.basename(normalized_rel_path)

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
