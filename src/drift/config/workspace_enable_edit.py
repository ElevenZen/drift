"""Planning and verification of `[packages.enable]` edits in workspace configuration files.

Backs `drift enable` / `drift disable`. Edits are format-preserving (line-surgical via
`utils.toml_edit`) and verified by a parse -> edit -> re-parse round trip: the second
parse must equal the first parse with exactly the intended `packages.enable` changes,
otherwise a `ConfigError` is raised and nothing may be written.

This module only reads files; writing is performed by the primitive layer.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Planning Entry Point
    plan_workspace_enable_edit(drift_root, updates, local) -> WorkspaceEnableEdit
        1. resolve_workspace_enable_target()     : static > .envst > new (--local only)
        2. read_config_text() + first parse      : parse_workspace_config_content()
        3. assert_packages_enable_header_editable(): reject inline/dotted [packages.enable]
        4. set_toml_table_bools()                : line-surgical text edit
        5. second parse + assert_workspace_enable_edit_verified()
        6. check_shadowed_packages()             : base edits overridden by .local layer

Layer 2: Verification & Inspection Helpers
    build_expected_enable_dict(before, updates) -> dict
    assert_workspace_enable_edit_verified(before, after, updates) -> None
    check_shadowed_packages(drift_root, updates, local) -> Tuple[str, ...]
    render_workspace_enable_diff(edit, drift_root) -> str

Layer 1: Target Resolution & Raw I/O
    WorkspaceEnableTarget (Dataclass)
    resolve_workspace_enable_target(drift_root, local) -> WorkspaceEnableTarget
    read_config_text(path) -> str  (preserves CRLF line endings)
===============================================================================
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from ..core.constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
)
from ..core.exceptions import ConfigError
from ..utils.config_utils import branch, choose, parse_bool_value, when_none
from ..utils.path_utils import to_relative_posix
from ..utils.toml_edit import (
    TomlEditAction,
    find_toml_table_span,
    scan_toml_logical_lines,
    set_toml_table_bools,
)
from .workspace_loader import (
    locate_workspace_config_file,
    parse_workspace_config_content,
)

PACKAGES_ENABLE_TABLE_KEYS: Tuple[str, str] = ("packages", "enable")
PACKAGES_ENABLE_TABLE_NAME = ".".join(PACKAGES_ENABLE_TABLE_KEYS)


# =============================================================================
# Layer 1: Target Resolution & Raw I/O
# =============================================================================


@dataclass(frozen=True)
class WorkspaceEnableTarget:
    """The workspace config file to edit."""
    config_file: Path
    exists: bool
    is_local: bool


def resolve_workspace_enable_target(drift_root: Path, local: bool) -> WorkspaceEnableTarget:
    """Resolves which workspace config file `drift enable/disable` edits.

    Uses the loader precedence (static file > .envst.toml template). With `local`, a missing
    `drift_workspace.local.toml` is allowed and will be created.

    Raises:
        ConfigError: If the base workspace configuration file does not exist (non-local).
    """
    file_name = choose(local, WORKSPACE_CONFIG_LOCAL_FILE_NAME, WORKSPACE_CONFIG_FILE_NAME)
    default_path = Path(drift_root) / CONFIG_DIR_NAME / file_name
    located = locate_workspace_config_file(default_path)
    if located is None and not local:
        raise ConfigError(
            f"Workspace configuration file not found: '{default_path}' (or its .envst.toml template)."
        )
    return WorkspaceEnableTarget(
        config_file=when_none(located, default_path),
        exists=located is not None,
        is_local=local,
    )


def read_config_text(path: Path) -> str:
    """Reads a config file as UTF-8 text without newline translation (preserves CRLF)."""
    return path.read_bytes().decode("utf-8")


# =============================================================================
# Layer 2: Verification & Inspection Helpers
# =============================================================================


@dataclass(frozen=True)
class WorkspaceEnableEdit:
    """A fully verified, not-yet-written `[packages.enable]` edit."""
    config_file: Path
    is_new_file: bool
    before_text: str
    after_text: str
    actions: Dict[str, TomlEditAction] = field(default_factory=dict)
    shadowed_packages: Tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return self.before_text != self.after_text


def _get_enable_table(data: Mapping[str, Any]) -> Any:
    packages = data.get("packages")
    return branch(isinstance(packages, dict), lambda: packages.get("enable"), lambda: None)


def build_expected_enable_dict(before: Mapping[str, Any], updates: Mapping[str, bool]) -> dict:
    """Returns a deep copy of `before` with `packages.enable[k] = v` applied for each update."""
    expected = copy.deepcopy(dict(before))
    packages = expected.setdefault("packages", {})
    if not isinstance(packages, dict):
        raise ConfigError("'packages' in workspace configuration must be a TOML table.")
    enable = packages.setdefault("enable", {})
    if not isinstance(enable, dict):
        raise ConfigError(f"'{PACKAGES_ENABLE_TABLE_NAME}' in workspace configuration must be a TOML table.")
    enable.update(updates)
    return expected


def assert_workspace_enable_edit_verified(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    updates: Mapping[str, bool],
) -> None:
    """Asserts that `after` equals `before` plus exactly the intended `packages.enable` updates.

    Raises:
        ConfigError: If any other value changed, or an intended update is missing.
    """
    expected = build_expected_enable_dict(before, updates)
    if after != expected:
        raise ConfigError(
            f"Internal edit verification failed: re-parsed configuration does not match the intended "
            f"'[{PACKAGES_ENABLE_TABLE_NAME}]' changes {dict(updates)}. The file was not modified; "
            "please edit it manually."
        )


def assert_packages_enable_header_editable(text: str, data: Mapping[str, Any], config_file: Path) -> None:
    """Rejects configs defining `packages.enable` without a literal `[packages.enable]` header.

    Inline-table (`enable = { ... }`) or dotted-key (`packages.enable.x = true`) forms cannot be
    edited line-surgically; appending a new header would produce a duplicate table.

    Raises:
        ConfigError: If `packages.enable` exists in data but has no `[packages.enable]` header.
    """
    try:
        span = find_toml_table_span(scan_toml_logical_lines(text), PACKAGES_ENABLE_TABLE_KEYS)
    except ValueError as exc:
        raise ConfigError(f"Failed to scan '{config_file}': {exc}") from exc
    if span is None and _get_enable_table(data) is not None:
        raise ConfigError(
            f"'{config_file}' defines '{PACKAGES_ENABLE_TABLE_NAME}' without a '[{PACKAGES_ENABLE_TABLE_NAME}]' "
            "section header (inline table or dotted keys). Please edit the configuration file manually."
        )


def _check_is_shadowed(local_enable: Mapping[str, Any], key: str, value: bool) -> bool:
    if key not in local_enable:
        return False
    try:
        return parse_bool_value(local_enable[key], strict=True) != value
    except (TypeError, ValueError):
        return True


def check_shadowed_packages(drift_root: Path, updates: Mapping[str, bool], local: bool) -> Tuple[str, ...]:
    """Returns updated keys whose base-file value is overridden by the `.local` layer.

    Always empty for `--local` edits, since the local layer has the highest precedence.
    """
    if local:
        return ()
    local_file = locate_workspace_config_file(
        Path(drift_root) / CONFIG_DIR_NAME / WORKSPACE_CONFIG_LOCAL_FILE_NAME
    )
    if local_file is None:
        return ()
    local_data = parse_workspace_config_content(read_config_text(local_file), local_file)
    local_enable = _get_enable_table(local_data)
    if not isinstance(local_enable, dict):
        return ()
    return tuple(k for k, v in updates.items() if _check_is_shadowed(local_enable, k, v))


def render_workspace_enable_diff(edit: WorkspaceEnableEdit, drift_root: Path) -> str:
    """Renders a unified diff of the edit (empty string when there are no changes)."""
    if not edit.has_changes:
        return ""
    rel = to_relative_posix(edit.config_file, Path(drift_root))
    from_file = choose(edit.is_new_file, "/dev/null", f"a/{rel}")
    lines = difflib.unified_diff(
        [line.rstrip("\r\n") for line in edit.before_text.splitlines(keepends=True)],
        [line.rstrip("\r\n") for line in edit.after_text.splitlines(keepends=True)],
        fromfile=from_file,
        tofile=f"b/{rel}",
        lineterm="",
    )
    return "\n".join(lines)


# =============================================================================
# Layer 3: Planning Entry Point
# =============================================================================


def plan_workspace_enable_edit(
    drift_root: Path,
    updates: Mapping[str, bool],
    local: bool = False,
) -> WorkspaceEnableEdit:
    """Plans and verifies a `[packages.enable]` edit without writing anything.

    Section policy:
        - A literal `[packages.enable]` header is edited in place.
        - A missing section is appended at EOF (for both base and `--local` files).
        - A commented-out `# [packages.enable]` is never uncommented: commented lines below it
          are often examples, and reviving them would be guessing at user intent. Such a file
          simply gets a new section appended.
        - Inline-table / dotted-key forms are rejected (`assert_packages_enable_header_editable`).

    Raises:
        ConfigError: If the target file is missing (non-local), malformed, not editable
            line-surgically, or the re-parse verification fails.
    """
    if not updates:
        raise ConfigError("No packages specified.")
    target = resolve_workspace_enable_target(drift_root, local)
    before_text = branch(target.exists, lambda: read_config_text(target.config_file), lambda: "")
    before_data = parse_workspace_config_content(before_text, target.config_file)
    assert_packages_enable_header_editable(before_text, before_data, target.config_file)

    try:
        edit_result = set_toml_table_bools(
            before_text,
            PACKAGES_ENABLE_TABLE_KEYS,
            updates,
            create_table_if_missing=True,
        )
    except ValueError as exc:
        raise ConfigError(f"Cannot edit '{target.config_file}': {exc}") from exc

    after_data = parse_workspace_config_content(edit_result.text, target.config_file)
    assert_workspace_enable_edit_verified(before_data, after_data, updates)

    return WorkspaceEnableEdit(
        config_file=target.config_file,
        is_new_file=not target.exists,
        before_text=before_text,
        after_text=edit_result.text,
        actions=dict(edit_result.actions),
        shadowed_packages=check_shadowed_packages(drift_root, updates, local),
    )
