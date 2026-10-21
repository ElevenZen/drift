"""Package enablement primitive backing `drift enable` / `drift disable`.

Flips `[packages.enable]` entries in `config/drift_workspace.toml` (or the machine-local
`config/drift_workspace.local.toml` with `local=True`) using a format-preserving,
re-parse-verified edit. This is configuration authoring only: it never renders, deploys,
or uninstalls packages.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Composite Entry Point
    run_primitive_packages_enable(workspace_config, packages, options) -> PackageEnableResult
        - prepare_packages_enable -> execute_packages_enable

Layer 2: Read-Only Preparation & Mutation Execution
    prepare_packages_enable(workspace_config, packages, options) -> WorkspaceEnableEdit
        - normalize_enable_package_names()
        - assert_packages_enable_targets_exist()
        - plan_workspace_enable_edit() (config layer: edit + re-parse verification)
    execute_packages_enable(workspace_config, edit, options) -> PackageEnableResult
        - Dry run: unified diff only; no changes: UNCHANGED; else atomic write_file() (mode preserved)

Layer 1: Package Name Validation
    normalize_enable_package_names(packages) -> List[str]
    gather_known_package_names(workspace_config, enabled) -> Set[str]
    assert_packages_enable_targets_exist(workspace_config, packages, enabled) -> None
===============================================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Sequence, Set

from ..config.workspace_config import WorkspaceConfig
from ..config.workspace_enable_edit import (
    WorkspaceEnableEdit,
    plan_workspace_enable_edit,
    render_workspace_enable_diff,
)
from ..core.exceptions import ConfigError
from ..models import PackageEnableResult
from ..utils.config_utils import choose
from ..utils.file_ops import write_file
from ..utils.path_utils import to_relative_posix

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageEnableOptions:
    """Options for `drift enable` / `drift disable`."""
    enabled: bool
    local: bool = False
    dry_run: bool = False

    @property
    def command_name(self) -> str:
        return choose(self.enabled, "enable", "disable")


# =============================================================================
# Layer 1: Package Name Validation
# =============================================================================


def normalize_enable_package_names(packages: Sequence[str]) -> List[str]:
    """Strips names, rejects empty ones, and de-duplicates while preserving order.

    Raises:
        ConfigError: If no packages are given or any name is empty.
    """
    names = [str(p).strip() for p in packages]
    if not names:
        raise ConfigError("No packages specified.")
    empty = [p for p in names if not p]
    if empty:
        raise ConfigError("Package names must be non-empty.")
    return list(dict.fromkeys(names))


def gather_known_package_names(workspace_config: WorkspaceConfig, enabled: bool) -> Set[str]:
    """Returns package names accepted as targets.

    `enable` accepts packages present in `src/`. `disable` additionally accepts packages that
    only exist in `render/`, `install/`, or the merged `[packages.enable]` table, so stale
    packages can still be disabled after their sources were removed.
    """
    source = set(workspace_config.get_package_names_from_source_dir())
    if enabled:
        return source
    return (
        source
        | set(WorkspaceConfig.get_package_names_from_dir(workspace_config.render_path))
        | set(WorkspaceConfig.get_package_names_from_dir(workspace_config.install_path))
        | set(workspace_config.packages_enable)
    )


def assert_packages_enable_targets_exist(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    enabled: bool,
) -> None:
    """Raises ConfigError if any target (other than `DEFAULT`) is unknown."""
    known = gather_known_package_names(workspace_config, enabled) | {WorkspaceConfig.PACKAGES_ENABLE_DEFAULT_KEY}
    missing = [p for p in packages if p not in known]
    if not missing:
        return
    where = choose(
        enabled,
        f"in source directory '{workspace_config.source_path}'",
        "in src/, render/, install/, or [packages.enable]",
    )
    raise ConfigError(f"Package(s) not found {where}: {', '.join(missing)}")


# =============================================================================
# Layer 2: Read-Only Preparation & Mutation Execution
# =============================================================================


def prepare_packages_enable(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    options: PackageEnableOptions,
) -> WorkspaceEnableEdit:
    """Validates targets and plans a verified edit. Performs no writes."""
    names = normalize_enable_package_names(packages)
    assert_packages_enable_targets_exist(workspace_config, names, options.enabled)
    return plan_workspace_enable_edit(
        workspace_config.drift_root,
        {name: options.enabled for name in names},
        local=options.local,
    )


def execute_packages_enable(
    workspace_config: WorkspaceConfig,
    edit: WorkspaceEnableEdit,
    options: PackageEnableOptions,
) -> PackageEnableResult:
    """Writes the planned edit atomically (unless dry-run or unchanged) and builds the result."""
    drift_root = workspace_config.drift_root
    diff = render_workspace_enable_diff(edit, drift_root)
    should_write = edit.has_changes and not options.dry_run
    if should_write:
        # permission=None: write_file preserves the existing mode, or uses 0o666 & ~umask for new files.
        write_file(edit.config_file, edit.after_text.encode("utf-8"))
        logger.debug(f"Updated '{edit.config_file}' for 'drift {options.command_name}'.")

    return PackageEnableResult(
        command=options.command_name,
        status=choose(edit.has_changes, "SUCCESS", "UNCHANGED"),
        enabled=options.enabled,
        config_file=to_relative_posix(edit.config_file, drift_root),
        created_file=edit.is_new_file and edit.has_changes,
        dry_run=options.dry_run,
        actions={pkg: action.value for pkg, action in edit.actions.items()},
        shadowed_packages=list(edit.shadowed_packages),
        diff=diff,
    )


# =============================================================================
# Layer 3: Composite Entry Point
# =============================================================================


def run_primitive_packages_enable(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    options: PackageEnableOptions,
) -> PackageEnableResult:
    """Enables or disables packages in the workspace configuration (`drift enable` / `drift disable`)."""
    edit = prepare_packages_enable(workspace_config, packages, options)
    return execute_packages_enable(workspace_config, edit, options)
