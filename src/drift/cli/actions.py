"""Core action implementations for drift CLI backend triggers using pathlib."""

import sys
import logging
from pathlib import Path
from typing import Optional, List, Union

from ..constants import CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME, ExitCode
from ..exceptions import DriftError, ConfigError, DriftDetectedError, RenderError, CollisionError
from ..workspace_config import WorkspaceConfig, load_workspace_config
from ..workspace_init import init_drift_workspace
from ..git_utils import get_drift_root
from ..result_models import (
    SerializableModel,
    StatusResult,
    PackageStatusSummary,
    DeployResult,
    DeployFailure,
    NextActionType,
    DiffType,
    UninstallResult,
    GcResult,
    AdoptResult,
    PackageAdoptResult,
    NewPackageResult,
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

# Disable unused import warning for get_drift_root, as it may be used in CLI backends.
_ = get_drift_root


def load_workspace_config_default(drift_root: Path) -> WorkspaceConfig:
    return load_workspace_config(drift_root)


def execute_init(drift_root: Path, force: bool = False, no_git_root: bool = False, json_mode: bool = False) -> None:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    init_drift_workspace(drift_root, force=force, no_git_root=no_git_root)
    if json_mode:
        print(SerializableModel().to_json())


def execute_render(drift_root: Path, package_names: Optional[List[str]] = None, json_mode: bool = False, no_hooks: bool = False) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    from ..render_package import run_primitive_2_render_packages

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_2_render_packages(workspace_config, target_pkgs=package_names, no_hooks=no_hooks)
    if json_mode:
        print(res.to_json())


def execute_stage(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False, json_mode: bool = False) -> None:
    """Core function to execute staging from render to install, shared by both CLI backends."""
    from ..stage_repo import run_primitive_4_stage_render_to_install
    from ..result_models import StageResult

    workspace_config = load_workspace_config_default(drift_root)
    changes = run_primitive_4_stage_render_to_install(workspace_config, target_pkgs=package_names, force=force)
    if json_mode:
        pkg_names = [c.package_name for c in changes]
        print(StageResult(packages_changed=pkg_names).to_json())
        return

    if not changes:
        logger.info("No changes staged. All files are up-to-date.")
    else:
        for pkg_change in changes:
            logger.info(f"Package '{pkg_change.package_name}' staged changes:")
            for file in pkg_change.added_files:
                logger.info(f"  [+] {file.as_posix()}")
            for file in pkg_change.modified_files:
                logger.info(f"  [*] {file.as_posix()}")
            for file in pkg_change.deleted_files:
                logger.info(f"  [-] {file.as_posix()}")


def execute_apply(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False, json_mode: bool = False, no_hooks: bool = False) -> None:
    """Core function to execute state application (apply), shared by both CLI backends."""
    from ..install_repo import run_primitive_5_install_deployment

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_5_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=package_names,
        resolve_symlinks=True,
        force=force,
        package_changes=None,
        no_hooks=no_hooks
    )
    if json_mode:
        print(res.to_json())


def execute_render_commit(drift_root: Path, message: str, package_names: Optional[List[str]] = None, json_mode: bool = False) -> None:
    """Core function to execute committing render repository changes, shared by both CLI backends."""
    from ..render_package import run_primitive_3_commit_render_repo

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_3_commit_render_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_install_commit(drift_root: Path, message: str, package_names: Optional[List[str]] = None, json_mode: bool = False) -> None:
    """Core function to execute committing install repository changes, shared by both CLI backends."""
    from ..install_repo import run_primitive_6_commit_install_repo

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_6_commit_install_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_reverse_sync(drift_root: Path, package_names: Optional[List[str]] = None, json_mode: bool = False) -> None:
    """Core function to execute reverse sync (System -> install/), shared by both CLI backends."""
    from ..reverse_sync import run_primitive_1_reverse_sync

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_1_reverse_sync(workspace_config, package_names=package_names)
    if json_mode:
        print(res.to_json())


def execute_new_package(
    drift_root: Path,
    package_name: str,
    force: bool = False,
    target_directory: Optional[str] = None,
    install_method: Optional[str] = None,
    json_mode: bool = False
) -> None:
    """Core function to create a new package, shared by both CLI backends."""
    from ..new_package import run_primitive_10_create_new_package
    from ..constants import PACKAGE_CONFIG_FILE_NAME

    workspace_config = load_workspace_config_default(drift_root)
    pkg_dir = run_primitive_10_create_new_package(
        workspace_config,
        package_name,
        force=force,
        target_directory=target_directory,
        install_method=install_method
    )
    if json_mode:
        res = NewPackageResult(
            package=package_name,
            package_dir=str(pkg_dir),
            config_file=str(pkg_dir / PACKAGE_CONFIG_FILE_NAME),
            target_directory=target_directory or str(workspace_config.default_target_path),
            install_method=install_method or workspace_config.default_install_method
        )
        print(res.to_json())


def execute_uninstall(
    drift_root: Path,
    package_names: List[str],
    force: bool = False,
    dry_run: bool = False,
    detach: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to uninstall or detach packages, shared by both CLI backends."""
    from ..uninstall_repo import run_primitive_7_uninstall_packages

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_7_uninstall_packages(
        workspace_config,
        package_names=package_names,
        force=force,
        dry_run=dry_run,
        detach=detach,
        no_hooks=no_hooks
    )
    if json_mode:
        print(res.to_json())


def execute_status(drift_root: Path, package_names: Optional[List[str]] = None, json_mode: bool = False) -> None:
    """Core function to audit workspace status, shared by both CLI backends."""
    from ..workspace_status import run_primitive_status
    
    workspace_config = load_workspace_config_default(drift_root)
    status_result = run_primitive_status(workspace_config, target_pkgs=package_names)
    
    if json_mode:
        print(status_result.to_json())
    else:
        text = status_result.format_text()
        if text:
            print(text)

    if status_result.overall_status == "DRIFTED":
        sys.exit(ExitCode.DRIFT_DETECTED)


def execute_gc(drift_root: Path, dry_run: bool = False, json_mode: bool = False, no_hooks: bool = False) -> None:
    """Core function to garbage collect orphans and purge databases, shared by both CLI backends."""
    from ..workspace_gc import run_primitive_9_purge_workspace_garbage

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=dry_run, no_hooks=no_hooks)
    if json_mode:
        print(res.to_json())


def execute_adopt(
    drift_root: Path,
    package_names: List[str],
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to adopt system drifts back to source templates, shared by both CLI backends."""
    from ..adopt_repo import run_primitive_adopt_drifts

    workspace_config = load_workspace_config_default(drift_root)
    adopted_names = run_primitive_adopt_drifts(
        workspace_config=workspace_config,
        package_names=package_names,
        interactive=interactive,
        accept_conflicts=accept_conflicts,
        force=force,
        dry_run=dry_run,
        no_hooks=no_hooks
    )
    if json_mode:
        res = AdoptResult(
            command="adopt",
            status="SUCCESS",
            packages=[PackageAdoptResult(package=p, status="SUCCESS") for p in adopted_names]
        )
        print(res.to_json())


def execute_diff(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    diff_type: Union[DiffType, str] = DiffType.PENDING,
    side_by_side: bool = False,
    stat: bool = False,
    json_mode: bool = False
) -> None:
    """Core function to visualize changes, shared by both CLI backends."""
    diff_type_enum = diff_type if isinstance(diff_type, DiffType) else DiffType(diff_type)
    workspace_config = load_workspace_config_default(drift_root)
    # use the status primitive to get a diff result in JSON mode, otherwise use the diff primitive for text output
    if json_mode:
        from ..workspace_status import run_primitive_status
        status_res = run_primitive_status(workspace_config, target_pkgs=package_names)
        print(status_res.to_diff_result(diff_type=diff_type_enum).to_json())
        return

    from ..workspace_diff import run_primitive_diff
    run_primitive_diff(workspace_config, package_names=package_names, diff_type=diff_type_enum, side_by_side=side_by_side, stat=stat)


def execute_add(
    drift_root: Path,
    package_name: str,
    import_paths: List[str],
    dry_run: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to import resources into a package, shared by both CLI backends."""
    from ..add_resource import run_primitive_11_add_resources
    
    workspace_config = load_workspace_config_default(drift_root)
    paths = [Path(p) for p in import_paths]
    run_primitive_11_add_resources(workspace_config, package_name, paths, dry_run=dry_run, no_hooks=no_hooks)
    if json_mode:
        res = AddResourceResult(
            package=package_name,
            imported_files=[str(p) for p in paths],
            dry_run=dry_run
        )
        print(res.to_json())


def execute_rollback(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    force: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to rollback failed deployments, shared by both CLI backends."""
    from ..rollback_repo import run_primitive_8_rollback_recovery
    
    workspace_config = load_workspace_config_default(drift_root)
    restored = run_primitive_8_rollback_recovery(
        workspace_config=workspace_config,
        package_names=package_names,
        force=force,
        no_hooks=no_hooks
    )
    if json_mode:
        res = RollbackResult(
            target_packages=package_names or [],
            restored_packages=restored
        )
        print(res.to_json())


def execute_deploy(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    force: bool = False,
    json_mode: bool = False,
    no_hooks: bool = False
) -> None:
    """Core function to execute transactional deploy workflow, shared by both CLI backends."""
    from ..deploy_repo import run_primitive_deploy_pipeline

    workspace_config = load_workspace_config_default(drift_root)
    try:
        res = run_primitive_deploy_pipeline(
            workspace_config=workspace_config,
            packages_to_deploy=package_names,
            force=force,
            no_hooks=no_hooks
        )
        if json_mode:
            print(res.to_json())
    except Exception as e:
        if json_mode:
            err_str = str(e)
            is_drift = "System drift detected" in err_str
            requires_rollback = "Midway crash" in err_str
            if is_drift:
                next_action = NextActionType.ADOPT_OR_FORCE
                rec_cmd = "drift adopt"
            elif requires_rollback:
                next_action = NextActionType.ROLLBACK
                rec_cmd = f"drift rollback {' '.join(package_names or [])}"
            else:
                next_action = NextActionType.FIX_TEMPLATE
                rec_cmd = "drift deploy"

            fail = DeployFailure(
                step_index=0 if is_drift else 1,
                step_name="sentinel_drift_check" if is_drift else "pipeline_execution",
                package=package_names[0] if (package_names and len(package_names) == 1) else None,
                error_message=err_str,
                error_type=type(e).__name__,
                requires_rollback=requires_rollback,
                next_action_type=next_action,
                recommended_command=rec_cmd
            )
            res = DeployResult(
                status="ABORTED_DRIFT" if is_drift else "FAILED",
                is_global_deploy=(package_names is None),
                target_packages=package_names or [],
                failure=fail
            )
            print(res.to_json())
            if is_drift:
                sys.exit(ExitCode.DRIFT_DETECTED)
            elif isinstance(e, DriftError):
                sys.exit(e.exit_code)
            else:
                sys.exit(ExitCode.GENERAL_ERROR)
        raise


def execute_repair(drift_root: Path, dry_run: bool = False, json_mode: bool = False) -> None:
    """Core function to repair a damaged or partially-initialized drift workspace."""
    from ..workspace_repair import repair_drift_workspace
    from ..check_repo import check_existing_workspace_status

    report = check_existing_workspace_status(drift_root)
    actions: List[str] = []
    if not report.is_healthy():
        if not json_mode:
            logger.info(f"🔧 Repairing workspace at '{drift_root}'...")
        actions = repair_drift_workspace(drift_root, dry_run=dry_run)
        if not json_mode:
            if actions:
                for action in actions:
                    logger.info(f"  ✨ {action}")
                if not dry_run:
                    logger.info("✨ Workspace repair complete!")
                else:
                    logger.info("✨ [Dry-Run] Workspace repair simulation complete.")
            else:
                logger.info("No repair actions were required.")
    else:
        if not json_mode:
            logger.info(f"✨ Workspace at '{drift_root}' is already complete and healthy. No repairs needed.")

    if json_mode:
        checks_list = [
            RepairCheckDetail(name=c.name, status=c.status.value, details=c.details, fix_hint=c.fix_hint)
            for c in report.checks
        ]
        res = RepairResult(
            overall_health=report.overall_status.value,
            dry_run=dry_run,
            actions_performed=actions,
            checks=checks_list
        )
        print(res.to_json())


def execute_health(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    json_mode: bool = False,
    verbose: bool = False,
    timeout: Optional[int] = None
) -> None:
    """Core function to run package health check probes, shared by both CLI backends."""
    from ..package_health import run_primitive_health_checks

    workspace_config = load_workspace_config_default(drift_root)
    health_result = run_primitive_health_checks(
        workspace_config=workspace_config,
        package_names=package_names,
        custom_timeout=timeout
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
    from ..workspace_clone import run_primitive_clone

    res = run_primitive_clone(
        git_url=git_url,
        target_dir=target_dir,
        branch=branch,
        depth=depth,
        no_repair=no_repair
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
    json_mode: bool = False
) -> None:
    """Core function to trigger a single package lifecycle hook, shared by both CLI backends."""
    from ..package_hook import run_primitive_trigger_hook

    workspace_config = load_workspace_config_default(drift_root)
    res = run_primitive_trigger_hook(
        workspace_config=workspace_config,
        package_name=package_name,
        hook_name=hook_name
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
            installed_list.append({"shell": s, "path": str(dest_path)})

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


