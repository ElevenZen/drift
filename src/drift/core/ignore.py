"""Handles drift ignore filtering based on GNU Stow-derived regex matching logic using pathlib."""

import re
import logging
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable, Sequence

from .constants import (
    MANAGED_CONFIG_FILES,
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_LEGACY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    DRIFT_KEEP_FILE_NAME,
    DRIFT_INTERNAL_DIR_NAME,
    DEFAULT_IGNORE_PATTERNS,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class IgnoreHandler(Protocol):
    """Protocol for ignore path matching."""

    def match_path(self, rel_path: Path, is_dir: bool = False) -> bool:
        """Determines whether a relative path should be ignored.

        Args:
            rel_path: Relative Path object to check against ignore rules.
            is_dir: Whether rel_path represents a directory / folder.

        Returns:
            True if the path should be ignored, False otherwise.
        """
        return False


def _resolve_package_ignore_file(package_dir: Path, is_source: bool) -> Optional[Path]:
    """Resolves the ignore configuration file path for a package directory."""
    if is_source:
        canonical_source = package_dir / DRIFT_IGNORE_FILE_NAME
        if canonical_source.is_file():
            return canonical_source
        legacy_source = package_dir / DRIFT_IGNORE_LEGACY_FILE_NAME
        if legacy_source.is_file():
            return legacy_source
        return None

    # For render sandbox / install state packages:
    canonical_internal = package_dir / DRIFT_INTERNAL_DIR_NAME / DRIFT_IGNORE_FILE_NAME
    if canonical_internal.is_file():
        return canonical_internal

    legacy_root = package_dir / DRIFT_IGNORE_FILE_NAME
    if legacy_root.is_file():
        logger.warning(
            f"⚠️ [DEPRECATION] Package at '{package_dir}' contains legacy root ignore file '{DRIFT_IGNORE_FILE_NAME}'. "
            f"Please run 'drift repair' to migrate metadata into '{DRIFT_INTERNAL_DIR_NAME}/'."
        )
        return legacy_root

    return None


def _validate_no_other_ignore_files(package_dir: Path, resolved_path: Optional[Path]) -> None:
    """Validates that only the single resolved ignore file exists in the package."""
    for name in DRIFT_IGNORE_FILE_NAME_LIST:
        for path in package_dir.rglob(name):
            if resolved_path is not None and path == resolved_path:
                continue
            raise ValueError(
                f"Nested ignore files are not allowed. "
                f"Found nested '{name}' inside subdirectory: "
                f"{path.parent.relative_to(package_dir)}"
            )


def resolve_deployable_paths_with_empty_dirs(
    paths: Sequence[Path],
    ignore_handler: Optional[IgnoreHandler] = None,
) -> List[Path]:
    """Transforms raw file paths into deployable paths, converting .drift_keep stubs into empty directory leaf paths.

    1. Filters regular files (excluding .drift_keep and ignored paths).
    2. Identifies ancestors of regular files.
    3. Converts .drift_keep stubs whose parent is not an ancestor of regular files into directory leaf paths.
    4. Validates that the directory parent is not ignored by ignore_handler.
    """
    regular_files = [
        p for p in paths
        if p.name != DRIFT_KEEP_FILE_NAME
        and (ignore_handler is None or not ignore_handler.match_path(p, is_dir=False))
    ]
    regular_ancestors = {
        parent
        for p in regular_files
        for parent in p.parents
        if parent != Path(".")
    }
    empty_dirs = [
        p.parent
        for p in paths
        if p.name == DRIFT_KEEP_FILE_NAME
        and p.parent != Path(".")
        and p.parent not in regular_ancestors
        and (ignore_handler is None or not ignore_handler.match_path(p.parent, is_dir=True))
    ]
    return sorted(regular_files + empty_dirs)


class DriftIgnore(IgnoreHandler):
    """Handles parsing and match evaluation of drift ignore patterns."""

    def __init__(
        self,
        patterns: Optional[Sequence[str]] = None,
        ignore_keep_file: bool = True,
    ) -> None:
        if patterns is None:
            self.patterns = list(DEFAULT_IGNORE_PATTERNS)
        else:
            self.patterns = list(patterns)
        self.ignore_keep_file = ignore_keep_file
        # Pre-divide patterns depending on whether they contain '/'
        self.set_with_slash = []
        self.set_without_slash = []
        for pattern in self.patterns:
            if "/" in pattern:
                self.set_with_slash.append(pattern)
            else:
                self.set_without_slash.append(pattern)

    def set_ignore_keep_file(self, ignore: bool) -> None:
        """Sets whether DRIFT_KEEP_FILE_NAME is ignored by match_path in-place."""
        self.ignore_keep_file = ignore

    def with_ignore_keep_file(self, ignore: bool) -> "DriftIgnore":
        """Returns a new DriftIgnore instance with ignore_keep_file set to the given boolean."""
        return DriftIgnore(patterns=list(self.patterns), ignore_keep_file=ignore)

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
    def load_from_dir(cls, package_dir: Path, is_source: bool) -> "DriftIgnore":
        """Loads ignore PCRE regex patterns from .drift_ignore inside package_dir.

        Args:
            package_dir: Directory path of the package (source, render sandbox, or install base).
            is_source: If True, loads .drift_ignore directly from package root (src/<pkg>/.drift_ignore).
                If False, loads .drift_ignore from the internal control plane (.drift/.drift_ignore)
                with fallback to package root for backward compatibility.

        Returns:
            An instance of DriftIgnore with loaded patterns, or default ignore patterns if missing.

        Raises:
            ValueError: If a nested ignore file is detected in an unauthorized subdirectory.
        """
        if not package_dir.exists() or not package_dir.is_dir():
            return cls(None)

        ignore_path = _resolve_package_ignore_file(package_dir, is_source=is_source)
        _validate_no_other_ignore_files(package_dir, resolved_path=ignore_path)
        if not ignore_path:
            return cls(None)

        with ignore_path.open("r", encoding="utf-8") as f:
            patterns = [
                stripped
                for line in f
                if (stripped := cls.strip_comments(line))
            ]
        return cls(patterns)

    def filter_deployable_files(
        self,
        install_pkg_dir: Path,
        include_empty_dirs: bool = True,
    ) -> List[Path]:
        """Returns relative paths for all deployable items in a package.

        When include_empty_dirs is True, empty folders tracked via .drift_keep
        are transformed into their parent directory relative paths as leaf nodes.
        When include_empty_dirs is False, empty directories and .drift_keep stubs
        are excluded (used for cross-package collision checks).
        """
        from ..utils.file_inspect import tree_files

        raw_files = tree_files(install_pkg_dir)
        if not include_empty_dirs:
            return sorted([
                p for p in raw_files
                if p.name != DRIFT_KEEP_FILE_NAME and not self.match_path(p, is_dir=False)
            ])

        return resolve_deployable_paths_with_empty_dirs(raw_files, ignore_handler=self)

    def match_path(self, rel_path: Path, is_dir: bool = False) -> bool:
        """Implements regex ignore matching algorithm on a relative path."""
        # Special exception: always ignore internal drift directories, ignore-related files, config files, and keep file stubs
        if rel_path.parts and rel_path.parts[0] == DRIFT_INTERNAL_DIR_NAME:
            return True

        filename = rel_path.name
        if filename in MANAGED_CONFIG_FILES or (self.ignore_keep_file and filename == DRIFT_KEEP_FILE_NAME):
            return True

        normalized_rel_path = rel_path.as_posix()
        path_with_slash = "/" + normalized_rel_path
        path_with_trailing_slash = path_with_slash + "/" if is_dir else None
        basename = rel_path.name

        # Match Step 1: Check patterns containing '/' against path_with_slash
        for pattern in self.set_with_slash:
            try:
                if re.search(pattern, path_with_slash):
                    return True
                if is_dir and path_with_trailing_slash and re.search(pattern, path_with_trailing_slash):
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

