from __future__ import annotations

import shlex
import sys
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Sequence

from .workspace_config import WorkspaceConfig
from .git_utils import get_git_status_porcelain, check_repo_can_commit
from .reverse_sync import run_primitive_1_reverse_sync
from .render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
from .stage_repo import run_primitive_4_stage_render_to_install, PackageStageChanges
from .install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
from .workspace_gc import run_primitive_9_purge_workspace_garbage
from .lifecycle_hooks import HookExecFlags
from .exceptions import HookExecutionError
from .constants import STATE_REGISTRY_FILE_NAME
from .state_registry import load_state_registry
from .result_models import (
    NextActionType,
    CompletedStep,
    DeployFailure,
    DeployResult,
    PackageInstallResult,
    GcResult,
)

logger = logging.getLogger(__name__)


def check_and_prevent_system_drifts(
    workspace_config: WorkspaceConfig,
    target_pkgs: List[str],
    force: bool = False
) -> Tuple[List[str], List[str]]:
    """Stage 1: Safety Guard (Sentinel)
    Runs pre-flight state checks and silent reverse-sync to verify if any targeted package has drifted.
    If a midway state or drift is detected and force is False, aborts execution instantly with instructions.
    Returns (drifted_packages, drifted_files).
    """
    # 0. Check if any targeted package is in a midway transaction state from a previous crash/failure
    state_file = workspace_config.install_path / STATE_REGISTRY_FILE_NAME
    if state_file.exists() and not force:
        state_registry = load_state_registry(state_file)
        midway_pkgs = state_registry.get_midway_packages(target_pkgs)

        if midway_pkgs:
            pkg_names = [p[0] for p in midway_pkgs]
            pkg_cmd_str = shlex.join(pkg_names)
            details = ", ".join(f"'{p}' ({st})" for p, st in midway_pkgs)
            err_msg = (
                f"❌ [DEPLOY ABORTED] Package(s) in midway transaction state: {details}!\n"
                "A previous operation failed midway, leaving uncommitted state in the install repository.\n\n"
                f"👉 Run 'drift rollback {pkg_cmd_str}' to safely restore your system to the last known good configuration.\n"
                f"👉 Run 'drift deploy {pkg_cmd_str} --force' to bypass this safeguard and proceed anyway."
            )
            raise RuntimeError(err_msg)

    logger.info("🔍 [STAGE 1] Triggering silent reverse synchronization audit...")
    
    # We only reverse-sync packages that actually exist in install/, as first-time packages
    # cannot have recorded drifts yet.
    syncable_pkgs = [
        pkg for pkg in target_pkgs
        if (workspace_config.install_path / pkg).is_dir()
    ]
    
    if syncable_pkgs:
        run_primitive_1_reverse_sync(workspace_config, package_names=syncable_pkgs)

    drifted_packages = []
    drifted_files = []
    for pkg in syncable_pkgs:
        pkg_rel_path = f"{pkg}/"
        git_status = get_git_status_porcelain(workspace_config.install_path, pkg_rel_path)
        if git_status:
            drifted_packages.append(pkg)
            logger.warning(f"🛡️  System drift detected for package '{pkg}'!")
            for line in git_status:
                logger.debug(f"   Drift: {line}")
                drifted_files.append(line)

    if drifted_packages and not force:
        pkg_cmd_str = shlex.join(drifted_packages)
        err_msg = (
            f"❌ [DEPLOY ABORTED] System drift detected in packages: {', '.join(drifted_packages)}!\n"
            "Host configurations have drifted from the state database.\n\n"
            f"👉 Run 'drift diff -s {pkg_cmd_str}' to view the active system modifications.\n"
            f"👉 Run 'drift adopt {pkg_cmd_str}' to incorporate these modifications into your template.\n"
            f"👉 Run 'drift deploy {pkg_cmd_str} --force' to discard system drifts and overwrite."
        )
        raise RuntimeError(err_msg)

    return drifted_packages, drifted_files


def print_emergency_recovery_card(failed_step: str, error_msg: str, package_names: List[str]) -> None:
    """Prints a highly visible emergency recovery instruction block to stderr."""
    pkgs_str = shlex.join(package_names) if package_names else "<packages>"
    card = f"""
\033[1;31m💥 [CRITICAL FAILURE] deployment failed during {failed_step}!\033[0m
   \033[1;31mError:\033[0m {error_msg}

================================================================================
                           \033[1;33mEMERGENCY RECOVERY REQUIRED\033[0m                          
================================================================================
The deployment has failed midway, leaving your host system in an inconsistent 
and half-written state.

👉 Step 1 (Recover State): Run \033[1;32m'drift rollback {pkgs_str}'\033[0m to restore
   the state database, delete any half-written files, and execute a fallback
   deployment to your last successfully committed configurations.

👉 Step 2 (Fix & Retry): Inspect the error details above, resolve the underlying
   issue in your source template or hook, then run 'drift deploy'.

💡 \033[1;36mINFO:\033[0m Only run rollback when recovering from a failed deployment or
   explicitly restoring previous state. Rollback bypasses system drift checking
   and will discard uncommitted local system adjustments.
================================================================================
"""
    print(card, file=sys.stderr)


def execute_sequential_compile_and_apply(
    workspace_config: WorkspaceConfig,
    target_pkgs: List[str],
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
    redeploy: bool = False,
) -> Tuple[List[PackageInstallResult], List[CompletedStep]]:
    """Stage 2: Sequential Compile & Apply with midway transaction error catching."""
    logger.info("🚀 [STAGE 2] Starting sequential compilation and apply pipeline...")
    completed_steps: List[CompletedStep] = []
    hook_flags = HookExecFlags.resolve(flags)
    
    # 1. Render raw templates to sandbox
    failed_step = "Step 1 (Template Rendering)"
    try:
        logger.info("   [1/5] Compiling source templates to sandbox render/ ...")
        render_res = run_primitive_2_render_packages(
            workspace_config, target_pkgs=target_pkgs, flags=hook_flags
        )
        if render_res.status == "FAILED":
            raise RuntimeError(render_res.error_message or f"{failed_step} failed.")
        completed_steps.append(CompletedStep(1, "template_rendering"))
    except Exception as e:
        logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
        logger.info("👉 You can resolve the template issues and simply try 'drift deploy' again.")
        raise RuntimeError(f"{failed_step} failed.") from e

    # 2. Commit sandbox changes
    failed_step = "Step 2 (Sandbox History Committing)"
    pkgs_label = ", ".join(target_pkgs)
    try:
        logger.info("   [2/5] Committing sandbox changes in render/ repository ...")
        run_primitive_3_commit_render_repo(
            workspace_config,
            commit_message=f"Deploy Render: Automatically compile templates for {pkgs_label}",
            target_pkgs=target_pkgs
        )
        completed_steps.append(CompletedStep(2, "render_commit"))
    except Exception as e:
        logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
        logger.info("👉 Please resolve any render sandbox repository Git issues and try 'drift deploy' again.")
        raise RuntimeError(f"{failed_step} failed.") from e

    # 3. Stage render sandbox to install state base
    failed_step = "Step 3 (Sandbox Staging)"
    try:
        logger.info("   [3/5] Staging rendered changes from render/ to install/ state database ...")
        package_changes = run_primitive_4_stage_render_to_install(
            workspace_config,
            target_pkgs=target_pkgs,
            force=force
        )
        completed_steps.append(CompletedStep(3, "sandbox_staging"))
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise RuntimeError(f"Midway crash: {failed_step} failed.") from e

    changed_pkgs = [pkg for pkg, change in package_changes.items() if change.has_changes]
    if redeploy:
        pkgs_to_install = target_pkgs
    elif changed_pkgs:
        pkgs_to_install = changed_pkgs
    else:
        logger.info("✨ No package changes detected during staging. Skipping physical deployment.")
        return [], completed_steps

    # 4. Physical Deployment of configurations to host system target paths
    failed_step = "Step 4 (Physical Deploy/Install)"
    pkgs_install_label = ", ".join(pkgs_to_install)
    try:
        logger.info(f"   [4/5] Deploying and copying/linking configurations to active host paths for: {pkgs_install_label} ...")
        install_res = run_primitive_5_install_deployment(
            workspace_config,
            packages_to_redeploy=pkgs_to_install,
            resolve_symlinks=True,
            force=force,
            package_changes=package_changes,
            flags=hook_flags,
            redeploy=redeploy,
        )
        completed_steps.append(CompletedStep(4, "physical_install"))
    except HookExecutionError as e:
        if not e.requires_rollback:
            try:
                run_primitive_6_commit_install_repo(
                    workspace_config,
                    commit_message=f"Deploy Install: Automatically commit deployed changes for {pkgs_install_label}",
                    target_pkgs=pkgs_to_install
                )
            except Exception as commit_err:
                logger.error(f"Failed to commit install/ repository changes following non-rollback hook failure: {commit_err}")
            logger.error(f"❌ [DEPLOY ABORTED] {failed_step} stopped due to hook failure: {e.message}")
            raise RuntimeError(f"{failed_step} stopped due to hook failure in '{e.package}': {e.message}") from e
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise RuntimeError(f"Midway crash: {failed_step} failed.") from e
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise RuntimeError(f"Midway crash: {failed_step} failed.") from e

    # 5. Commit state database configurations
    failed_step = "Step 5 (State Database Committing)"
    try:
        logger.info("   [5/5] Committing deployment changes in install/ repository ...")
        run_primitive_6_commit_install_repo(
            workspace_config,
            commit_message=f"Deploy Install: Automatically commit deployed changes for {pkgs_install_label}",
            target_pkgs=pkgs_to_install
        )
        completed_steps.append(CompletedStep(5, "install_commit"))
    except Exception as e:
        logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
        msg = (
            f"The deployment succeeded on your host, but committing to the state database failed.\n"
            f"👉 Please resolve the Git state manually by running:\n"
            f"    drift install-commit -m \"Deploy Install: Automatically commit deployed changes for {pkgs_install_label}\""
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(f"{failed_step} failed.") from e

    return install_res.packages, completed_steps


def run_primitive_deploy_pipeline(
    workspace_config: WorkspaceConfig,
    packages_to_deploy: Sequence[str] = (),
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
    redeploy: bool = False,
) -> DeployResult:
    """Main deployment pipeline controller running Sentinel Drift checking and sequential compile/apply.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_deploy: Specific package name(s) to deploy, or empty/omitted for all active packages.
        force: If True, bypasses the Sentinel Drift check (allowing deployment even if uncommitted
            drifts exist in install/) and passes force to Primitive 4 (staging) and Primitive 5
            (install deployment) to bypass midway failed state checks and uncommitted modification safeguards.
            Note: Does NOT bypass 'enable_install = false' package configurations.
        flags: Optional HookExecFlags controlling hook execution options.
        redeploy: If True, forces full redeployment of all requested packages regardless of staging delta.

    Returns:
        DeployResult containing detailed status and deployed packages.
    """
    # 0. Pre-flight checks: Verify render/ and install/ repositories can commit successfully
    logger.info("🔍 Running pre-flight Git configuration checks...")
    check_repo_can_commit(workspace_config.render_path)
    check_repo_can_commit(workspace_config.install_path)

    # Discover target active packages from source directory
    target_pkgs = workspace_config.get_source_packages(target_pkgs=packages_to_deploy)
    if not target_pkgs:
        logger.info("No active packages selected or enabled for deployment. Skipping.")
        return DeployResult(
            command="deploy",
            status="SUCCESS",
            is_global_deploy=(not packages_to_deploy),
            target_packages=[],
            deployed_packages=[]
        )

    # Stage 1: Sentinel Drift Auditing
    check_and_prevent_system_drifts(workspace_config, target_pkgs, force=force)

    # Stage 2: Deploy Pipeline Execution
    deployed_packages, completed_steps = execute_sequential_compile_and_apply(
        workspace_config, target_pkgs, force=force, flags=flags, redeploy=redeploy
    )

    # Stage 3: Call garbage collection on global deploy
    gc_res: Optional[GcResult] = None
    if not packages_to_deploy:
        logger.info("🧹 Performing global deployment garbage collection...")
        gc_res = run_primitive_9_purge_workspace_garbage(
            workspace_config, dry_run=False, flags=flags
        )

    if deployed_packages:
        logger.info(f"✨ Successfully completed deployment for package(s): {', '.join(p.package for p in deployed_packages)}")
    else:
        logger.info("✨ Deployment completed: all packages are already up-to-date (no changes detected).")

    return DeployResult(
        command="deploy",
        status="SUCCESS",
        is_global_deploy=(not packages_to_deploy),
        target_packages=target_pkgs,
        deployed_packages=deployed_packages,
        gc=gc_res,
        completed_steps=completed_steps
    )


def run_primitive_deploy_pipeline_with_error_handling(
    workspace_config: WorkspaceConfig,
    packages_to_deploy: Sequence[str] = (),
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
    redeploy: bool = False,
) -> DeployResult:
    """Executes the deployment pipeline, catching exceptions and returning a structured DeployResult.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_deploy: Specific package name(s) to deploy, or None for all active packages.
        force: If True, bypasses safeguards and proceeds with deployment.
        flags: Optional HookExecFlags controlling hook execution options.

    Returns:
        DeployResult containing detailed status, deployed packages, or failure details.
    """
    try:
        return run_primitive_deploy_pipeline(
            workspace_config=workspace_config,
            packages_to_deploy=packages_to_deploy,
            force=force,
            flags=flags,
            redeploy=redeploy,
        )
    except Exception as e:
        err_str = str(e)
        is_drift = "System drift detected" in err_str
        is_midway = "midway transaction state" in err_str or "Safety Abort: Package" in err_str
        requires_rollback = "Midway crash" in err_str or is_midway
        if is_drift:
            next_action = NextActionType.ADOPT_OR_FORCE
            rec_cmd = "drift adopt"
        elif requires_rollback:
            next_action = NextActionType.ROLLBACK
            rec_cmd = f"drift rollback {shlex.join(packages_to_deploy)}".strip()
        else:
            next_action = NextActionType.FIX_TEMPLATE
            rec_cmd = "drift deploy"

        fail = DeployFailure(
            step_index=0 if (is_drift or is_midway) else 1,
            step_name="sentinel_drift_check" if (is_drift or is_midway) else "pipeline_execution",
            package=packages_to_deploy[0] if (packages_to_deploy and len(packages_to_deploy) == 1) else None,
            error_message=err_str,
            error_type=type(e).__name__,
            requires_rollback=requires_rollback,
            next_action_type=next_action,
            recommended_command=rec_cmd
        )
        return DeployResult(
            command="deploy",
            status="ABORTED_DRIFT" if is_drift else "FAILED",
            is_global_deploy=(packages_to_deploy is None),
            target_packages=list(packages_to_deploy),
            failure=fail
        )

