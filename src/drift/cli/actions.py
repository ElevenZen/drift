"""Core action implementations for drift CLI backend triggers using pathlib.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

This module is the shared action layer invoked by both the Typer CLI backend
(src/drift/cli/typer_app.py) and the plain CLI backend. Each execute_* function
is a thin orchestration wrapper that:
  1. Optionally validates the workspace via assert_workspace_healthy().
  2. Loads WorkspaceConfig via load_workspace_config_default().
  3. Delegates to the corresponding primitive entry point (P1–P16).
  4. Handles JSON output and exit codes.

-------------------------------------------------------------------------------
Actions Classified by Workspace Dependency
-------------------------------------------------------------------------------

Requires a healthy workspace (calls assert_workspace_healthy):
  execute_render          -> P2  run_primitive_2_render_packages
  execute_render_commit   -> P3  run_primitive_3_commit_render_repo
  execute_stage           -> P4  run_primitive_4_stage_render_to_install
  execute_apply           -> P5  run_primitive_5_install
  execute_install_commit  -> P6  run_primitive_6_commit_install_repo
  execute_uninstall       -> P7  run_primitive_7_uninstall_packages
  execute_rollback        -> P8  run_primitive_8_rollback_recovery
  execute_gc              -> P9  run_primitive_9_purge_workspace_garbage
  execute_new_package     -> P10 run_primitive_10_create_new_package
  execute_add             -> P11 run_primitive_11_add_resources
  execute_packages_enable ->     run_primitive_packages_enable (drift enable / disable)
  execute_deploy          ->     run_primitive_deploy_pipeline
  execute_adopt           ->     run_primitive_adopt_drifts
  execute_diff            -> P15 run_primitive_15_workspace_diff
  execute_status          -> P16 run_primitive_status
  execute_reverse_sync    -> P1  run_primitive_1_reverse_sync

Does NOT require a healthy workspace (bypasses assert_workspace_healthy):
  execute_init            ->     init_drift_workspace       (creates the workspace)
  execute_repair          ->     repair_drift_workspace     (fixes a broken workspace)
  execute_clone           ->     run_primitive_clone        (no local workspace yet)
  execute_health          ->     run_primitive_health_checks (reads install/ directly)
  execute_hook            ->     run_primitive_trigger_hook  (operates on install/)
  execute_help            ->     print_help_document        (static docs, no workspace)
  execute_complete        ->     generate_completion_script (static output, no workspace)
  execute_install_stub    ->     didactic 404 guidance stub (no workspace)

-------------------------------------------------------------------------------
Workspace Guard Helpers
-------------------------------------------------------------------------------
  check_sudo_and_root(drift_root)    -- Blocks sudo/root misuse on user workspaces.
  assert_workspace_healthy(drift_root, command_name)
                                     -- Raises ConfigError on is_uninitialized() (NOT_FOUND) or BROKEN.
  load_workspace_config_default(drift_root) -> WorkspaceConfig
===============================================================================
"""

import sys
import os
import getpass
import logging
from pathlib import Path
from typing import Optional, List, Union, Any, Sequence

from ..core.constants import CONFIG_DIR_NAME, WORKSPACE_CONFIG_FILE_NAME, ExitCode, InstallMethod, PackageStage, RollbackPolicy
from ..core.exceptions import DriftError, ConfigError, DriftDetectedError, RenderError, InstallCollisionError
from ..utils.host_facts import init_system_facts
from ..config.workspace_config import WorkspaceConfig
from ..primitives.workspace_init import init_drift_workspace
from ..models import (
    SerializableModel,
    StatusResult,
    PackageStatus,
    DeployResult,
    DeployFailure,
    NextActionType,
    WorkspaceDiffType,
    UninstallResult,
    GcResult,
    AdoptResult,
    PackageAdoptResult,
    NewPackageResult,
    PackageEnableResult,
    AddResourceResult,
    RollbackResult,
    RepairResult,
    RepairCheckDetail,
    RenderResult,
    ReverseSyncResult,
    DiffResult,
    PackageDiffDetail,
    FileDiffDetail,
    PackageHealthStatus,
    PackageHealthResult,
    HealthResult,
    CloneResult,
    HookResult,
)

logger = logging.getLogger(__name__)


def is_directory_owned_by_root(path: Path) -> bool:
    """Returns True if path (or its nearest existing ancestor) is owned by root (UID 0)."""
    try:
        cur = path.resolve()
        while not cur.exists() and cur.parent != cur:
            cur = cur.parent
        if cur.exists():
            return cur.stat().st_uid == 0
    except (OSError, AttributeError):
        pass
    return False


def check_sudo_and_root(drift_root: Path) -> None:
    """Ensures that the user does not run Drift under sudo or root privileges on a user-owned workspace.

    Allowed scenarios:
    1. The user is actual root (logged in as root, os.getuid() == 0 without sudo).
    2. The drift workspace (drift_root) is owned by root (UID 0), meaning root file creation
       will not corrupt regular user workspace file ownership.
    """
    if sys.platform == "win32":
        # On Windows, elevated Administrator shell is allowed and sudo/root checks do not apply.
        return

    # 1. Check if running under sudo (SUDO_USER or SUDO_UID environment variables exist)
    is_sudo = "SUDO_USER" in os.environ or "SUDO_UID" in os.environ

    # 2. Check if running with root privilege (UID 0)
    has_root_privilege = False
    try:
        has_root_privilege = (os.getuid() == 0)
    except AttributeError:
        # Non-POSIX platforms
        pass

    if not is_sudo and not has_root_privilege:
        # Regular non-root user running without sudo
        return

    # Check Scenario 1: Actual root user without sudo
    if has_root_privilege and not is_sudo:
        try:
            current_user = getpass.getuser()
        except Exception:
            current_user = os.environ.get("USER", "root")
        if current_user == "root":
            return

    # Check Scenario 2: drift_root is owned by root (UID 0)
    if is_directory_owned_by_root(drift_root):
        return

    # Neither scenario is satisfied: running under sudo / root on a user-owned workspace.
    root_display = drift_root.as_posix()
    print(
        f"❌ [ERROR] Running Drift under 'sudo' on a user-owned workspace is prohibited.\n\n"
        f"Why this is prevented:\n"
        f"  1. Target Path Mismatch: Running under sudo changes $HOME and '~' to expand to '/root' instead of\n"
        f"     your user home directory, causing dotfiles to be deployed to the wrong target location.\n"
        f"  2. File Permission Pollution: Running under sudo creates internal repository and staging files\n"
        f"     (in render/, install/, backup/, and state.toml) with root ownership (root:root), breaking\n"
        f"     file permissions when you later run Drift as a regular user.\n\n"
        f"How to resolve:\n"
        f"  1. [Recommended] Run Drift as your normal user without 'sudo'. If a specific package requires root\n"
        f"     privileges to deploy files, set 'sudo = true' in that package's 'drift_package.toml'. Drift will\n"
        f"     elevate only the file deployment step via sudo while keeping workspace databases user-owned.\n"
        f"  2. If managing system-wide dotfiles for the root user, move the Drift workspace into root's home\n"
        f"     directory (e.g. /root/drift) and run Drift as the actual 'root' user.\n"
        f"  3. If you explicitly want to run this workspace under sudo, change the workspace ownership to root:\n"
        f"     sudo chown -R root:root {root_display}",
        file=sys.stderr,
    )
    sys.exit(1)


def prepare_cli_environment(drift_root: Path) -> None:
    """Prepares CLI execution context: verifies sudo/root safety and initializes system facts."""
    check_sudo_and_root(drift_root)
    init_system_facts()


def load_workspace_config_default(drift_root: Path) -> WorkspaceConfig:
    return WorkspaceConfig.from_workspace_dir(drift_root)


def assert_workspace_healthy(
    drift_root: Path,
    command_name: str = "diff",
) -> None:
    """Ensures workspace structure is present and up to date before executing commands.

    Raises ConfigError with a user-friendly hint if:
    - The workspace is uninitialized (no drift artifacts detected at drift_root).
    - The workspace is BROKEN (outdated or missing components).
    """
    from ..primitives.workspace_check import check_existing_workspace_status, ComponentStatus

    report = check_existing_workspace_status(drift_root, quiet=True)
    if report.is_uninitialized():
        raise ConfigError(
            f"No Drift workspace found at '{drift_root}'.\n\n"
            f"To create a new workspace here, run:  drift init\n"
            f"To clone an existing workspace, run:  drift clone <url>"
        )
    if report.is_broken():
        broken_checks = [c for c in report.checks if c.status != ComponentStatus.GOOD]
        details = "\n".join(f"  - {c.name}: {c.details}" for c in broken_checks)
        raise ConfigError(
            f"Cannot run 'drift {command_name}' because the workspace structure is outdated or broken:\n"
            f"{details}\n\n"
            f"Please run 'drift repair' to resolve workspace issues and migrate legacy metadata."
        )


def execute_init(drift_root: Path, force: bool = False, json_mode: bool = False) -> WorkspaceConfig:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    prepare_cli_environment(drift_root)
    ws_cfg = init_drift_workspace(drift_root, force=force)
    if json_mode:
        print(SerializableModel().to_json())
    return ws_cfg


def execute_render(
    drift_root: Path,
    package_names: Sequence[str] = (),
    json_mode: bool = False,
    no_hooks: bool = False,
    no_cache: bool = False,
    dry_run: bool = False,
    with_hooks: bool = False,
    verbose: bool = False,
) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    from ..primitives.render_repo import run_primitive_2_render_packages, RenderOptions
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="render")
    workspace_config = load_workspace_config_default(drift_root)
    effective_no_hooks = no_hooks if not dry_run else (not with_hooks)
    flags = HookExecFlags(no_hooks=effective_no_hooks, streaming=not json_mode)
    options = RenderOptions(no_cache=no_cache, dry_run=dry_run, flags=flags)
    res = run_primitive_2_render_packages(
        workspace_config, target_pkgs=package_names, options=options
    )
    if json_mode:
        print(res.to_json())
    elif dry_run:
        text = res.format_text(drift_root=drift_root, verbose=verbose)
        if text:
            print(text)
    if res.status != "SUCCESS":
        raise RuntimeError(res.error_message or "Template rendering failed.")


def execute_stage(
    drift_root: Path,
    package_names: Sequence[str] = (),
    force: bool = False,
    json_mode: bool = False,
    no_deps: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Core function to execute staging from render to install, shared by both CLI backends."""
    from ..primitives.stage_repo import run_primitive_4_stage_render_to_install, StageOptions

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="stage")
    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_4_stage_render_to_install(
        workspace_config,
        target_pkgs=package_names,
        options=StageOptions(
            force=force,
            no_deps=no_deps,
            dry_run=dry_run,
        ),
    )
    if json_mode:
        print(res.to_json())
        return

    if dry_run:
        text = res.format_text(verbose=verbose)
        if text:
            print(text)
        return

    if not res:
        logger.info("No changes staged. All files are up-to-date.")
    else:
        for plan in res.plans:
            logger.info(f"✨ Package '{plan.package}' staged successfully.")


def execute_apply(
    drift_root: Path,
    package_names: Sequence[str] = (),
    force: bool = False,
    reinstall: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False,
    no_deps: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    rollback: Optional[RollbackPolicy] = None,
) -> None:
    """Core function to execute state application (apply), shared by both CLI backends."""
    from ..primitives.install_repo import run_primitive_5_install, InstallOptions
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="apply")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    res = run_primitive_5_install(
        workspace_config=workspace_config,
        target_pkgs=package_names,
        options=InstallOptions(
            resolve_symlinks=True,
            force=force,
            reinstall=reinstall,
            flags=flags,
            no_deps=no_deps,
            dry_run=dry_run,
            rollback=rollback,
        ),
    )
    if json_mode:
        print(res.to_json())
    elif dry_run:
        text = res.format_text(verbose=verbose)
        if text:
            print(text)
    if res.status != "SUCCESS":
        raise RuntimeError(res.error_message or "Deployment installation failed.")


def execute_render_commit(drift_root: Path, message: str, package_names: Sequence[str] = (), json_mode: bool = False) -> None:
    """Core function to execute committing render repository changes, shared by both CLI backends."""
    from ..primitives.render_repo import run_primitive_3_commit_render_repo

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="render-commit")
    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_3_commit_render_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_install_commit(drift_root: Path, message: str, package_names: Sequence[str] = (), json_mode: bool = False) -> None:
    """Core function to execute committing install repository changes, shared by both CLI backends."""
    from ..primitives.install_repo import run_primitive_6_commit_install_repo

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="install-commit")
    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_6_commit_install_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_reverse_sync(drift_root: Path, package_names: Sequence[str] = (), json_mode: bool = False) -> None:
    """Core function to execute reverse sync (System -> install/), shared by both CLI backends."""
    from ..primitives.reverse_sync import run_primitive_1_reverse_sync

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="reverse-sync")
    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_1_reverse_sync(workspace_config, package_names=package_names)
    if json_mode:
        print(res.to_json())
    if res.status != "SUCCESS":
        raise RuntimeError(res.error_message or "Reverse sync failed.")


def execute_new_package(
    drift_root: Path,
    package_name: str,
    force: bool = False,
    target_directory: Optional[Path] = None,
    install_method: Optional[InstallMethod] = None,
    json_mode: bool = False
) -> None:
    """Core function to create a new package, shared by both CLI backends."""
    from ..primitives.new_package import run_primitive_10_create_new_package

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="new")
    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_10_create_new_package(
        workspace_config,
        package_name,
        force=force,
        target_directory=target_directory,
        install_method=install_method
    )
    if json_mode:
        print(res.to_json())


_DIFF_LINE_STYLES = (
    (("+++", "---"), "bold"),
    (("+",), "green"),
    (("-",), "red"),
    (("@@",), "cyan"),
)


def _style_unified_diff_line(line: str, escape: Any) -> str:
    """Wraps one unified-diff line in Rich markup based on its prefix (headers first)."""
    style = next((s for prefixes, s in _DIFF_LINE_STYLES if line.startswith(prefixes)), None)
    escaped = escape(line)
    if style is None:
        return escaped
    return f"[{style}]{escaped}[/{style}]"


def print_unified_diff(diff: str, use_rich: bool = True) -> None:
    """Prints a unified diff, colored via Rich when available (plain text otherwise).

    Rich strips styles automatically when stdout is not a terminal.
    """
    if use_rich:
        try:
            from rich.console import Console
            from rich.markup import escape
        except ImportError:
            pass
        else:
            console = Console(highlight=False, soft_wrap=True)
            for line in diff.splitlines():
                console.print(_style_unified_diff_line(line, escape))
            return
    print(diff)


def execute_packages_enable(
    drift_root: Path,
    packages: Sequence[str],
    enabled: bool,
    local: bool = False,
    dry_run: bool = False,
    json_mode: bool = False,
    use_rich: bool = True,
) -> PackageEnableResult:
    """Core function to enable/disable packages in drift_workspace.toml, shared by both CLI backends."""
    from ..primitives.workspace_enable import PackageEnableOptions, run_primitive_packages_enable

    options = PackageEnableOptions(enabled=enabled, local=local, dry_run=dry_run)
    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name=options.command_name)
    # quiet=True: the "no packages enabled" warning is noise when the user is enabling one.
    workspace_config = WorkspaceConfig.from_workspace_dir(drift_root, quiet=True)
    res = run_primitive_packages_enable(workspace_config, packages, options)
    if json_mode:
        print(res.to_json())
    elif res.dry_run and res.diff:
        print_unified_diff(res.diff, use_rich=use_rich)
    else:
        text = res.format_text()
        if text:
            print(text)
    return res


def execute_uninstall(
    drift_root: Path,
    package_names: Sequence[str] = (),
    force: bool = False,
    adhoc: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False,
    no_deps: bool = False,
    verbose: bool = False,
) -> None:
    """Core function to uninstall or detach packages, shared by both CLI backends."""
    from ..primitives.uninstall_repo import run_primitive_7_uninstall_packages, UninstallOptions
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="uninstall")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    options = UninstallOptions(
        force=force,
        adhoc=adhoc,
        dry_run=dry_run,
        detach=detach,
        flags=flags,
        no_deps=no_deps,
    )
    res = run_primitive_7_uninstall_packages(
        workspace_config,
        package_names=package_names,
        options=options,
    )
    if json_mode:
        print(res.to_json())
    elif dry_run:
        text = res.format_text(verbose=verbose)
        if text:
            print(text)
    if res.status != "SUCCESS":
        raise RuntimeError(res.error_message or "Package uninstallation failed.")


def execute_status(
    drift_root: Path,
    package_names: Sequence[str] = (),
    list_only: bool = False,
    json_mode: bool = False
) -> None:
    """Core function to audit workspace status, shared by both CLI backends."""
    from ..primitives.workspace_status import run_primitive_status
    
    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="status")
    workspace_config = load_workspace_config_default(drift_root)
    status_result = run_primitive_status(
        workspace_config, target_pkgs=package_names, list_only=list_only
    )
    
    if json_mode:
        print(status_result.to_json())
    else:
        text = status_result.format_text()
        if text:
            print(text)

    if not list_only and status_result.overall_status == "DRIFTED":
        sys.exit(ExitCode.DRIFT_DETECTED)


def execute_gc(drift_root: Path, dry_run: bool = False, json_mode: bool = False, no_hooks: bool = False) -> None:
    """Core function to garbage collect orphans and purge databases, shared by both CLI backends."""
    from ..primitives.workspace_gc import run_primitive_9_purge_workspace_garbage
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="gc")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    res = run_primitive_9_purge_workspace_garbage(
        workspace_config, dry_run=dry_run, flags=flags
    )
    if json_mode:
        print(res.to_json())
    if res.status != "SUCCESS":
        raise RuntimeError(res.error_message or "Garbage collection failed.")


def execute_adopt(
    drift_root: Path,
    package_names: Sequence[str] = (),
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False,
    no_render: bool = False,
    with_render: bool = False,
    no_reverse_sync: bool = False,
    verbose: bool = False,
) -> None:
    """Core function to adopt system drifts back to source templates, shared by both CLI backends."""
    from ..models import AdoptStatus
    from ..primitives.adopt_repo import run_primitive_adopt_drifts, AdoptOptions
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="adopt")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    options = AdoptOptions(
        interactive=interactive,
        accept_conflicts=accept_conflicts,
        force=force,
        dry_run=dry_run,
        no_render=no_render,
        with_render=with_render,
        no_reverse_sync=no_reverse_sync,
        flags=flags,
    )
    res = run_primitive_adopt_drifts(
        workspace_config=workspace_config,
        package_names=package_names,
        options=options,
    )
    if json_mode:
        print(res.to_json())
    elif dry_run:
        text = res.format_text(verbose=verbose)
        if text:
            print(text)
    if res.status == AdoptStatus.FAILED:
        raise RuntimeError(res.error_message or "Drift adoption failed.")


def execute_diff(
    drift_root: Path,
    package_names: Sequence[str] = (),
    diff_type: WorkspaceDiffType = WorkspaceDiffType.PENDING,
    side_by_side: bool = False,
    stat: bool = False,
    json_mode: bool = False
) -> None:
    """Core function to visualize changes, shared by both CLI backends."""
    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="diff")
    workspace_config = load_workspace_config_default(drift_root)
    from ..primitives.workspace_diff import run_primitive_15_workspace_diff
    diff_result = run_primitive_15_workspace_diff(
        workspace_config,
        package_names=package_names,
        diff_type=diff_type,
        side_by_side=side_by_side,
        stat=stat,
        quiet=json_mode,
    )
    if json_mode:
        print(diff_result.to_json())


def execute_add(
    drift_root: Path,
    package_name: str,
    import_paths: Sequence[Path],
    dry_run: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False,
    verbose: bool = False,
) -> None:
    """Core function to import resources into a package, shared by both CLI backends."""
    from ..primitives.add_resource import run_primitive_11_add_resources
    from ..hooks.lifecycle_hooks import HookExecFlags
    
    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="add")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    res = run_primitive_11_add_resources(
        workspace_config, package_name, list(import_paths), dry_run=dry_run, flags=flags
    )
    if json_mode:
        print(res.to_json())
    elif dry_run:
        text = res.format_text(verbose=verbose)
        if text:
            print(text)


def execute_rollback(
    drift_root: Path,
    package_names: Sequence[str] = (),
    force: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to rollback failed deployments, shared by both CLI backends."""
    from ..primitives.rollback_repo import run_primitive_8_rollback_recovery
    from ..hooks.lifecycle_hooks import HookExecFlags
    
    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="rollback")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode)
    res = run_primitive_8_rollback_recovery(
        workspace_config=workspace_config,
        package_names=package_names,
        force=force,
        flags=flags,
    )
    if json_mode:
        print(res.to_json())


def build_deploy_failure_error(res: Any) -> DriftError:
    """Converts a failed DeployResult into a DriftError carrying the original exception's exit code."""
    failure = res.failure
    err = DriftError(failure.error_message if failure else "Deployment failed.", packages=res.target_packages)
    err.exit_code = failure.exit_code if failure else ExitCode.GENERAL_ERROR
    return err


def execute_deploy(
    drift_root: Path,
    package_names: Sequence[str] = (),
    force: bool = False,
    adhoc: bool = False,
    gc: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False,
    reinstall: bool = False,
    no_deps: bool = False,
    no_cache: bool = False,
    rollback: Optional[RollbackPolicy] = None,
) -> None:
    """Core function to execute transactional deploy workflow, shared by both CLI backends."""
    from ..primitives.deploy_repo import run_primitive_deploy_pipeline, DeployOptions
    from ..hooks.lifecycle_hooks import HookExecFlags

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name="deploy")
    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=no_hooks, streaming=not json_mode, no_cache=no_cache)
    options = DeployOptions(
        force=force,
        adhoc=adhoc,
        gc=gc,
        reinstall=reinstall,
        no_deps=no_deps,
        no_hooks=no_hooks,
        no_cache=no_cache,
        rollback=rollback,
        flags=flags,
    )
    res = run_primitive_deploy_pipeline(
        workspace_config=workspace_config,
        packages_to_deploy=package_names,
        options=options,
    )
    if json_mode:
        print(res.to_json())
        if res.status != "SUCCESS":
            sys.exit(res.failure.exit_code if res.failure else ExitCode.GENERAL_ERROR)
    elif res.status != "SUCCESS":
        raise build_deploy_failure_error(res)


def execute_plan(
    drift_root: Path,
    packages: Sequence[str] = (),
    options: Optional[Any] = None,
    json_mode: bool = False,
    verbose: bool = False,
    command_name: str = "plan",
) -> None:
    """Core function to execute deployment planning and preview workflow, shared by both CLI backends."""
    from ..primitives.plan_repo import preview_deploy
    from ..primitives.deploy_repo import DeployOptions

    prepare_cli_environment(drift_root)
    assert_workspace_healthy(drift_root, command_name=command_name)
    workspace_config = load_workspace_config_default(drift_root)

    opts = options or DeployOptions(verbose=verbose)

    preview = preview_deploy(
        workspace_config=workspace_config,
        target_pkgs=packages,
        options=opts,
        command_name=command_name,
    )

    if json_mode:
        print(preview.to_json())
    else:
        text = preview.format_text(verbose=opts.verbose, show_all=opts.show_all)
        if text:
            print(text)

    if preview.global_errors or preview.packages_with_errors:
        sys.exit(ExitCode.GENERAL_ERROR)

    if preview.status == "DRIFT_DETECTED":
        sys.exit(ExitCode.DRIFT_DETECTED)


def execute_repair(drift_root: Path, dry_run: bool = False, json_mode: bool = False) -> None:
    """Core function to repair a damaged or partially-initialized drift workspace."""
    prepare_cli_environment(drift_root)
    from ..primitives.workspace_repair import repair_drift_workspace, build_repair_result
    from ..primitives.workspace_check import check_existing_workspace_status

    report = check_existing_workspace_status(drift_root, quiet=True)

    if report.is_healthy():
        if json_mode:
            print(build_repair_result(report, actions=[], dry_run=dry_run).to_json())
        else:
            logger.info(f"✨ Workspace at '{drift_root}' is already complete and healthy. No repairs needed.")
        return

    if not json_mode:
        logger.info(f"🔧 Repairing workspace at '{drift_root}'...")
    actions = repair_drift_workspace(drift_root, dry_run=dry_run)
    if json_mode:
        print(build_repair_result(report, actions=actions, dry_run=dry_run).to_json())
    else:
        if actions:
            for action in actions:
                logger.info(f"  ✨ {action}")
            if not dry_run:
                logger.info("✨ Workspace repair complete!")
            else:
                logger.info("✨ [Dry-Run] Workspace repair simulation complete.")
        else:
            logger.info("No repair actions were required.")


def execute_health(
    drift_root: Path,
    package_names: Sequence[str] = (),
    json_mode: bool = False,
    verbose: bool = False,
    timeout: Optional[int] = None,
    from_stage: PackageStage = PackageStage.INSTALL,
) -> None:
    """Core function to run package health check probes, shared by both CLI backends."""
    prepare_cli_environment(drift_root)
    from ..primitives.package_health import run_primitive_health_checks

    workspace_config = load_workspace_config_default(drift_root)
    health_result = run_primitive_health_checks(
        workspace_config=workspace_config,
        package_names=package_names,
        timeout_override=timeout,
        from_stage=from_stage
    )

    if json_mode:
        print(health_result.to_json())
    else:
        text = health_result.format_text(verbose=verbose)
        if text:
            print(text)

    if health_result.status != "SUCCESS":
        sys.exit(ExitCode.HEALTH_CHECK_FAILED)


def execute_clone(
    git_url: str,
    target_dir: Optional[Path] = None,
    branch: Optional[str] = None,
    depth: Optional[int] = None,
    no_repair: bool = False,
    json_mode: bool = False
) -> None:
    """Core function to clone a git repository and bootstrap/repair the drift workspace."""
    from ..primitives.workspace_clone import run_primitive_clone

    prepare_cli_environment(target_dir if target_dir is not None else Path.cwd().resolve())

    res = run_primitive_clone(
        git_url=git_url,
        target_dir=target_dir,
        branch=branch,
        depth=depth,
        no_repair=no_repair,
        streaming=not json_mode,
    )

    if json_mode:
        print(res.to_json())
    else:
        text = res.format_text()
        if text:
            print(text)

    if res.status != "SUCCESS":
        sys.exit(1)


def execute_help(topic: Optional[str] = None) -> None:
    """Core function to display help documentation pages with paging fallback support."""
    from .help_docs import print_help_document
    print_help_document(topic)


def execute_hook(
    drift_root: Path,
    package_name: str,
    hook_name: str,
    json_mode: bool = False,
    from_stage: Optional[PackageStage] = None,
) -> None:
    """Core function to trigger a single package lifecycle hook, shared by both CLI backends."""
    prepare_cli_environment(drift_root)
    from ..hooks.trigger_hook import run_primitive_trigger_hook
    from ..hooks.lifecycle_hooks import HookExecFlags

    workspace_config = load_workspace_config_default(drift_root)
    flags = HookExecFlags(no_hooks=False, streaming=not json_mode)
    res = run_primitive_trigger_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name=hook_name,
        from_stage=from_stage,
        flags=flags,
    )

    if json_mode:
        print(res.to_json())
    else:
        text = res.format_text()
        if text:
            print(text)

    if res.status == "SKIPPED":
        sys.exit(ExitCode.HOOK_SKIPPED)
    elif res.status != "SUCCESS":
        sys.exit(ExitCode.GENERAL_ERROR)


def get_default_completion_path(shell: str) -> Path:
    """Returns the standard user-level completion file path for the given shell."""
    home = Path.home()
    if shell == "bash":
        return home / ".local" / "share" / "bash-completion" / "completions" / "drift"
    elif shell == "zsh":
        return home / ".local" / "share" / "zsh" / "site-functions" / "_drift"
    elif shell == "fish":
        return home / ".config" / "fish" / "completions" / "drift.fish"
    elif shell in ("nu", "nushell"):
        return home / ".config" / "nushell" / "completions" / "drift.nu"
    else:
        raise ValueError(f"Unknown shell '{shell}'")


def execute_complete(
    shell: Optional[str] = None,
    install: bool = False,
    json_mode: bool = False
) -> None:
    """Core function to generate or install interactive shell tab-completion scripts."""
    import os
    import json
    import shutil
    from .completion import generate_completion_script, SHELLS

    valid_shells = {c.value for c in SHELLS}

    if install and shell is None:
        # Install completion scripts for all supported shells (bash, zsh, fish)
        target_shells = [c.value for c in SHELLS]
    else:
        target_shell = shell
        if not target_shell:
            shell_env = os.environ.get("SHELL", "")
            if shell_env:
                shell_name = Path(shell_env).name.lower()
                if shell_name in valid_shells:
                    target_shell = shell_name
                else:
                    target_shell = "bash"
            else:
                target_shell = "bash"
        target_shells = [target_shell]

    if install:
        installed_list = []
        for s in target_shells:
            script_content = generate_completion_script(s)
            dest_path = get_default_completion_path(s)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(script_content, encoding="utf-8")
            installed_list.append({"shell": s, "path": dest_path.as_posix()})

        if json_mode:
            data = {
                "command": "complete",
                "status": "SUCCESS",
                "installed": installed_list
            }
            print(json.dumps(data, indent=2))
        else:
            for item in installed_list:
                print(f"✨ Installed {item['shell']} completion script to:")
                print(f"   {item['path']}")
            if any(item["shell"] == "zsh" for item in installed_list):
                print()
                print("💡 [NOTE for Zsh]: Ensure your ~/.zshrc contains the following before compinit:")
                print("   fpath=(~/.local/share/zsh/site-functions $fpath)")
                print("   autoload -Uz compinit && compinit")
    else:
        target_shell = target_shells[0]
        script = generate_completion_script(target_shell)
        if json_mode:
            data = {
                "command": "complete",
                "status": "SUCCESS",
                "shell": target_shell,
                "script": script
            }
            print(json.dumps(data, indent=2))
        else:
            print(script)


def execute_install_stub(packages: Sequence[str] = (), json_mode: bool = False) -> None:
    """Provides didactic guidance explaining that 'drift install' does not exist and suggests deploy vs. apply."""
    import json
    if json_mode:
        data = {
            "status": "ERROR",
            "error": "'drift install' is not a Drift command.",
            "hint": "Use 'drift deploy' for end-to-end deployment from source, or 'drift apply' to deploy already-staged files from install/.",
            "alternatives": {
                "deploy": "Compiles templates (render), stages deltas (stage), and deploys configurations to your host system.",
                "apply": "Deploys files that are already staged in the install/ state database to your host system without re-rendering.",
            },
        }
        print(json.dumps(data, indent=2), file=sys.stderr)
        sys.exit(1)

    message = (
        "\n"
        "ℹ️  'drift install' is not a Drift command.\n\n"
        "Depending on what you want to achieve, you probably want one of the following:\n\n"
        "  • drift deploy [pkgs]\n"
        "    End-to-end deployment: Compiles templates (render), stages deltas (stage),\n"
        "    and applies configurations to your host system. (This is usually what you want).\n\n"
        "  • drift apply [pkgs]\n"
        "    Low-level primitive: Deploys files that are already staged in the install/\n"
        "    state database to your host system without re-rendering.\n"
    )
    sys.stderr.write(message)
    sys.exit(1)


