"""Drift workspace root discovery.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

A directory is a drift workspace root when it contains any workspace config
marker under ``config/`` (see ``WORKSPACE_ROOT_MARKER_FILE_NAMES``). Discovery
walks upward from a start directory and the nearest marked ancestor wins, so
workspaces nested inside unrelated Git repositories (e.g. ``samples/<name>/``)
are recognized independently of the enclosing Git toplevel.

Layers (ordered bottom-up by dependency):
    Layer 1: Pure Predicates & Path Enumeration
        - check_is_drift_workspace_root(dir_path): Marker presence predicate
        - iter_self_and_ancestor_dirs(start_dir): start_dir followed by its parents
    Layer 2: Discovery
        - find_drift_workspace_root(start_dir): Nearest marked ancestor or None
    Layer 3: Raising Resolver (CLI entry boundary)
        - resolve_drift_workspace_root(start_dir): Nearest marked ancestor or
          WorkspaceNotFoundError with an actionable hint
===============================================================================
"""

from itertools import chain
from pathlib import Path
from typing import Iterator, Optional

from ..core.constants import CONFIG_DIR_NAME, WORKSPACE_CONFIG_FILE_NAME, WORKSPACE_ROOT_MARKER_FILE_NAMES
from ..core.exceptions import WorkspaceNotFoundError


def check_is_drift_workspace_root(dir_path: Path) -> bool:
    """Returns True if ``dir_path/config/`` contains any workspace config marker file."""
    config_dir = Path(dir_path) / CONFIG_DIR_NAME
    return any((config_dir / name).is_file() for name in WORKSPACE_ROOT_MARKER_FILE_NAMES)


def iter_self_and_ancestor_dirs(start_dir: Path) -> Iterator[Path]:
    """Yields the resolved ``start_dir`` followed by each of its ancestors up to the filesystem root."""
    resolved = Path(start_dir).resolve()
    return chain((resolved,), resolved.parents)


def find_drift_workspace_root(start_dir: Path) -> Optional[Path]:
    """Returns the nearest directory at or above ``start_dir`` that is a drift workspace root, or None."""
    return next(filter(check_is_drift_workspace_root, iter_self_and_ancestor_dirs(start_dir)), None)


def resolve_drift_workspace_root(start_dir: Path) -> Path:
    """Resolves the drift workspace root for ``start_dir``.

    Raises:
        WorkspaceNotFoundError: If neither ``start_dir`` nor any ancestor is a drift workspace root.
    """
    root = find_drift_workspace_root(start_dir)
    if root is not None:
        return root
    raise WorkspaceNotFoundError(
        f"No Drift workspace found in '{Path(start_dir).resolve()}' or any of its parent directories "
        f"(looked for '{CONFIG_DIR_NAME}/{WORKSPACE_CONFIG_FILE_NAME}' or its template/local variants).\n\n"
        "To create a new workspace here, run:  drift init\n"
        "To clone an existing workspace, run:  drift clone <url>\n"
        "To target another workspace, pass:    -C <workspace_dir>"
    )
