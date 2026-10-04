"""Primitive 11: Resource Import (Add files/folders to package).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Pre-flight Preparation & Planning (Read-Only except pre_source hook):
        prepare_add_resources(workspace_config, package_name, import_paths, dry_run=False, flags=None) [Layer 4]
            - Package Source Validation (assert_package_source_exists [Layer 1])
            - Pre-Source Lifecycle Hook (trigger_pre_source_hook)
            - Package Import Context Resolution (resolve_package_import_context [Layer 1])
            - Ignore Rules Resolution (DriftIgnore.load_from_dir)
            - Worklist Resolution (generate_import_worklist [Layer 2])
            - Global Conflict Assertion (assert_no_import_conflicts [Layer 2])
            - Plan Compilation (plan_add_resources [Layer 3])
            -> Returns AddResourcePlan(package, src_dir_to_render, target_base, actions, dry_run)

    2. Resource Import Execution (State-Mutating / Simulated):
        execute_add_resources(workspace_config, plan: AddResourcePlan) [Layer 4]
            - If dry_run -> simulates imports, logs operations, performs zero filesystem mutations
            - If live -> executes execute_delivery_actions with FileActionExecutionContext [Layer 3]
            -> Returns AddResourceResult(command="add", status="SUCCESS", package=plan.package, imported_files=..., dry_run=plan.dry_run, plan=plan)

    3. Public Composite Primitive Entry Point:
        run_primitive_11_add_resources(workspace_config, package_name, import_paths, dry_run=False, flags=None) [Layer 5]
            = prepare_add_resources >> execute_add_resources

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Pre-flight Verification & Context Resolution
        assert_package_source_exists
        resolve_package_import_context
    Layer 2: Worklist Resolution & Conflict Detection
        ScopedPackageIgnore
        resolve_single_import_worklist
        generate_import_worklist
        check_import_conflict
        assert_no_import_conflicts
    Layer 3: Plan Compilation & Action Mapping
        plan_resource_import
        plan_add_resources
    Layer 4: Pipeline Sub-stages (Preparation & Execution)
        prepare_add_resources
        execute_add_resources
    Layer 5: Public Composite Primitive Entry Point
        run_primitive_11_add_resources
===============================================================================
"""

import logging
from pathlib import Path
from typing import List, Optional, Tuple, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..config.render_engine_config import RenderEngineRegistry
from ..core.folder_delivery import (
    FileActionExecutionContext,
    FileActionType,
    FileAction,
    execute_delivery_actions,
)
from ..core.result_models import (
    AddResourceResult,
    AddResourcePlan,
)
from ..utils.path_utils import (
    decode_dot_prefix,
    is_relative_to,
    to_relative_path,
)
from ..core.ignore import DriftIgnore, IgnoreHandler
from ..core.constants import DirMode
from ..core.folder_diff import list_folder_paths
from ..hooks.lifecycle_hooks import HookExecFlags, trigger_pre_source_hook

logger = logging.getLogger(__name__)


# =============================================================================
# Layer 1: Pre-flight Verification & Context Resolution
# =============================================================================

def assert_package_source_exists(workspace_config: WorkspaceConfig, package_name: str) -> Path:
    """Validates that the source directory for a package exists, raising FileNotFoundError otherwise."""
    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists():
        raise FileNotFoundError(f"Package '{package_name}' source directory not found: {src_pkg_dir}")
    return src_pkg_dir


def resolve_package_import_context(
    workspace_config: WorkspaceConfig,
    src_pkg_dir: Path,
) -> Tuple[Path, Path, RenderEngineRegistry]:
    """Resolves source directory to render, host target directory, and effective render engines for a package."""
    try:
        pkg_config = PackageConfig.from_source_dir(
            package_dir=src_pkg_dir,
            workspace_config=workspace_config
        )
        src_dir_to_render = pkg_config.get_source_directory_to_render(src_pkg_dir)
        target_base = pkg_config.get_target_directory(workspace_config)
        render_engines = pkg_config.package_render_engines(workspace_config)
    except Exception as e:
        logger.warning(f"Failed to load package configuration in {src_pkg_dir}: {e}. Using defaults.")
        src_dir_to_render = src_pkg_dir
        target_base = workspace_config.default_target_path
        render_engines = workspace_config.render_engine_configs

    return src_dir_to_render.resolve(), target_base.resolve(), render_engines


# =============================================================================
# Layer 2: Worklist Resolution & Conflict Detection
# =============================================================================

class ScopedPackageIgnore(IgnoreHandler):
    """Scoped ignore handler that offsets paths by repository prefix to match package-root rules."""

    def __init__(self, base_ignore: IgnoreHandler, repo_prefix: Path) -> None:
        self._base_ignore = base_ignore
        self._repo_prefix = repo_prefix

    def match_path(self, rel_path: Path, is_dir: bool = False) -> bool:
        return self._base_ignore.match_path(self._repo_prefix / rel_path, is_dir=is_dir)


def resolve_single_import_worklist(
    target_base: Path,
    import_path: Path,
    ignore_handler: IgnoreHandler,
) -> List[Tuple[Path, Path]]:
    """Resolves a single import path (file or folder) into concrete (system_abs, rel_target) pairs."""
    abs_import = import_path.resolve()
    if not abs_import.exists():
        raise FileNotFoundError(f"Import path does not exist: {import_path}")

    if not is_relative_to(abs_import, target_base):
        raise ValueError(f"Import path '{abs_import}' is not inside package target directory '{target_base}'")

    rel_root_target = abs_import.relative_to(target_base)
    repo_prefix = decode_dot_prefix(rel_root_target)
    scoped_ignore = ScopedPackageIgnore(ignore_handler, repo_prefix)

    file_paths = list_folder_paths(
        abs_import,
        ignore_handler=scoped_ignore,
        resolve_symlinks=True,
        translate_mode="reverse",
        dir_mode=DirMode.NO_DIR,
    )

    return [
        (
            abs_import / rel_path,
            rel_root_target / rel_path if rel_path != Path("") else rel_root_target,
        )
        for rel_path in file_paths
    ]


def generate_import_worklist(
    target_base: Path,
    import_paths: Sequence[Path],
    ignore_handler: IgnoreHandler,
) -> List[Tuple[Path, Path]]:
    """Generates a combined list of (absolute_source_path, target_relative_path) across all import paths."""
    return [
        item
        for path in import_paths
        for item in resolve_single_import_worklist(target_base, path, ignore_handler)
    ]


def check_import_conflict(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    src_on_system: Path,
    rel_target: Path,
) -> Optional[Tuple[Path, Path]]:
    """Checks if a single import path conflicts with existing source templates/files."""
    match = render_engines.find_conflict_in_source_dir(src_dir_to_render, rel_target)
    return (src_on_system, match.path) if match else None


def assert_no_import_conflicts(
    drift_root: Path,
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    worklist: Sequence[Tuple[Path, Path]],
) -> None:
    """Validates that no imported resource conflicts with an existing template or blocking file."""
    conflicts = list(
        filter(
            None,
            (
                check_import_conflict(render_engines, src_dir_to_render, src, rel_tgt)
                for src, rel_tgt in worklist
            ),
        )
    )
    if conflicts:
        src_on_system, conflict_path = conflicts[0]
        rel_conflict = to_relative_path(conflict_path, drift_root)
        raise RuntimeError(f"Conflict detected: '{src_on_system}' would overwrite existing source '{rel_conflict}'")


# =============================================================================
# Layer 3: Plan Compilation & Action Mapping
# =============================================================================

def plan_resource_import(
    src_dir_to_render: Path,
    src_on_system: Path,
    rel_target: Path,
) -> FileAction:
    """Compiles a single planned resource import action."""
    rel_src = decode_dot_prefix(rel_target)
    dest_path = src_dir_to_render / rel_src
    return FileAction(
        action_type=FileActionType.CREATE_COPY,
        src_path=src_on_system,
        dst_path=dest_path,
        reason="Resource import",
    )


def plan_add_resources(
    package_name: str,
    src_dir_to_render: Path,
    target_base: Path,
    worklist: Sequence[Tuple[Path, Path]],
    dry_run: bool = False,
) -> AddResourcePlan:
    """Compiles the declarative import plan containing all planned resource import actions."""
    planned_actions = [
        plan_resource_import(src_dir_to_render, src, rel_tgt)
        for src, rel_tgt in worklist
    ]
    return AddResourcePlan(
        package=package_name,
        src_dir_to_render=src_dir_to_render,
        target_base=target_base,
        actions=planned_actions,
        dry_run=dry_run,
    )


# =============================================================================
# Layer 4: Pipeline Sub-stages (Preparation & Execution)
# =============================================================================

def prepare_add_resources(
    workspace_config: WorkspaceConfig,
    package_name: str,
    import_paths: Sequence[Path],
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> AddResourcePlan:
    """
    Pre-flight preparation and planning sub-stage for resource imports (Read-Only).

    1. Validates that the package source directory exists.
    2. Triggers pre_source lifecycle hook.
    3. Resolves source render directory, target directory, render engines, and ignore rules.
    4. Compiles the global worklist of files to import.
    5. Asserts that no imported file conflicts with existing templates or blocking paths.
    6. Returns an AddResourcePlan.
    """
    src_pkg_dir = assert_package_source_exists(workspace_config, package_name)

    trigger_pre_source_hook(
        workspace_config, package_name, flags=flags
    )

    src_dir_to_render, target_base, render_engines = resolve_package_import_context(
        workspace_config, src_pkg_dir
    )
    ignore_handler = DriftIgnore.load_from_dir(src_pkg_dir, is_source=True)

    worklist = generate_import_worklist(target_base, import_paths, ignore_handler)

    assert_no_import_conflicts(
        drift_root=workspace_config.drift_root,
        render_engines=render_engines,
        src_dir_to_render=src_dir_to_render,
        worklist=worklist,
    )

    return plan_add_resources(
        package_name=package_name,
        src_dir_to_render=src_dir_to_render,
        target_base=target_base,
        worklist=worklist,
        dry_run=dry_run,
    )


def execute_add_resources(
    workspace_config: WorkspaceConfig,
    plan: AddResourcePlan,
) -> AddResourceResult:
    """
    Physical execution and state synchronization sub-stage for resource imports.

    If plan.dry_run is True, logs simulation details and performs zero filesystem mutations.
    Otherwise, executes planned actions using execute_delivery_actions.
    """
    if not plan.actions:
        logger.info(f"No resources to import into '{plan.package}'.")
        return AddResourceResult(
            command="add",
            status="SUCCESS",
            package=plan.package,
            imported_files=[],
            dry_run=plan.dry_run,
            plan=plan,
        )

    imported_files = [str(action.src_path) for action in plan.actions if action.src_path]

    if plan.dry_run:
        for action in plan.actions:
            rel_dest = to_relative_path(action.dst_path, workspace_config.drift_root) if action.dst_path else None
            logger.info(f"🔍 [DRY RUN] Would import '{action.src_path}' to '{rel_dest}'")
        return AddResourceResult(
            command="add",
            status="SUCCESS",
            package=plan.package,
            imported_files=imported_files,
            dry_run=True,
            plan=plan,
        )

    for action in plan.actions:
        rel_dest = to_relative_path(action.dst_path, workspace_config.drift_root) if action.dst_path else None
        logger.info(f"📥 Importing: {action.src_path}")
        logger.debug(f"   -> {rel_dest}")

    context = FileActionExecutionContext(
        sudo=False,
        resolve_symlinks=False,
    )
    execute_delivery_actions(context, plan.actions)

    logger.info(f"✨ Successfully imported {len(plan.actions)} file(s) into package '{plan.package}'.")
    return AddResourceResult(
        command="add",
        status="SUCCESS",
        package=plan.package,
        imported_files=imported_files,
        dry_run=False,
        plan=plan,
    )


# =============================================================================
# Layer 5: Public Composite Primitive Entry Point
# =============================================================================

def run_primitive_11_add_resources(
    workspace_config: WorkspaceConfig,
    package_name: str,
    import_paths: Sequence[Path],
    dry_run: bool = False,
    flags: Optional[HookExecFlags] = None,
) -> AddResourceResult:
    """
    Public composite primitive entry point for importing resources into a package.
    Decomposed into: prepare_add_resources >> execute_add_resources.
    """
    plan = prepare_add_resources(
        workspace_config=workspace_config,
        package_name=package_name,
        import_paths=import_paths,
        dry_run=dry_run,
        flags=flags,
    )
    return execute_add_resources(workspace_config, plan)
