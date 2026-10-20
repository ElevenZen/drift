"""Shared workspace root marker snippets for shell completion generators.

Every shell's dynamic package completer must locate the drift workspace root exactly like the
CLI does (``utils.workspace_root.resolve_drift_workspace_root``): walk upward from the start
directory and stop at the nearest directory containing any ``WORKSPACE_ROOT_MARKER_FILE_NAMES``
file under ``config/``. These helpers derive each shell's marker test from that single constant so
the Python discovery and the generated scripts can never diverge.
"""

from typing import Tuple

from ...core.constants import CONFIG_DIR_NAME, WORKSPACE_ROOT_MARKER_FILE_NAMES


def get_workspace_marker_rel_paths() -> Tuple[str, ...]:
    """Returns marker file paths relative to a workspace root (e.g. ``config/drift_workspace.toml``)."""
    return tuple(f"{CONFIG_DIR_NAME}/{name}" for name in WORKSPACE_ROOT_MARKER_FILE_NAMES)


def render_bracket_marker_test(dir_var: str) -> str:
    """Renders a Bash/Zsh ``[[ ... ]]`` test that succeeds when ``$dir_var`` is a workspace root."""
    clauses = " || ".join(f'-f "${dir_var}/{rel}"' for rel in get_workspace_marker_rel_paths())
    return f"[[ {clauses} ]]"


def render_fish_marker_test(dir_var: str) -> str:
    """Renders a Fish ``test`` command that succeeds when ``$dir_var`` is a workspace root."""
    clauses = " -o ".join(f'-f "${dir_var}/{rel}"' for rel in get_workspace_marker_rel_paths())
    return f"test {clauses}"


def render_nu_marker_list() -> str:
    """Renders a Nushell list literal of marker paths relative to a workspace root."""
    return "[" + " ".join(f'"{rel}"' for rel in get_workspace_marker_rel_paths()) + "]"
