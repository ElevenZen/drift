from __future__ import annotations

import sys
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from .workspace_config import WorkspaceConfig
from .git_utils import get_git_status_porcelain, check_repo_can_commit
from .reverse_sync import run_primitive_1_reverse_sync
from .render_package import run_primitive_2_render_packages, run_primitive_3_commit_render_repo
from .stage_repo import run_primitive_4_stage_render_to_install, PackageStageChanges
from .install_repo import run_primitive_5_install_deployment, run_primitive_6_commit_install_repo
from .workspace_gc import run_primitive_9_purge_workspace_garbage
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
    Runs a silent reverse-sync and checks if any targeted package has drifted.
    If drift is detected and force is False, aborts execution instantly with instructions.
    Returns (drifted_packages, drifted_files).
    """
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
        first_pkg = drifted_packages[0]
        err_msg = (
            f"❌ [DEPLOY ABORTED] System drift detected in package '{first_pkg}'!\n"
            "Host configurations have drifted from the state database.\n\n"
            f"👉 Run 'drift diff -s {first_pkg}' to view the active system modifications.\n"
            f"👉 Run 'drift adopt {first_pkg}' to incorporate these modifications into your template.\n"
            f"👉 Run 'drift deploy {first_pkg} --force' to discard system drifts and overwrite."
        )
        raise RuntimeError(err_msg)

    return drifted_packages, drifted_files


def print_emergency_recovery_card(failed_step: str, error_msg: str, package_names: List[str]) -> None:
    """Prints a highly visible emergency recovery instruction block to stderr."""
    pkgs_str = " ".join(package_names) if package_names else "<packages>"
    card = f"""
\033[1;31m💥 [CRITICAL FAILURE] deployment failed during {failed_step}!\033[0m
   \033[1;31mError:\033[0m {error_msg}

================================================================================
                           \033[1;33mEMERGENCY RECOVERY REQUIRED\033[0m                          
================================================================================
The deployment has failed midway, leaving your host system in an inconsistent 
and half-written state.

👉 Please fix the error above and run: \033[1;32m'drift rollback {pkgs_str}'\033[0m

This command will restore the state database, delete any half-written files, 
and execute a full deployment fallback to restore your system to the last
successfully committed configurations.

⚠️  \033[1;33mWARNING:\033[0m Do not run rollback under normal circumstances. It bypasses
   system drift checking and will discard uncommitted local system adjustments.
================================================================================
"""
    print(card, file=sys.stderr)


def execute_sequential_compile_and_apply(
    workspace_config: WorkspaceConfig,
    target_pkgs: List[str],
    force: bool = False,
    no_hooks: bool = False
) -> Tuple[List[PackageInstallResult], List[CompletedStep]]:
    """Stage 2: Sequential Compile & Apply with midway transaction error catching."""
    logger.info("🚀 [STAGE 2] Starting sequential compilation and apply pipeline...")
    completed_steps: List[CompletedStep] = []
    
    # 1. Render raw templates to sandbox
    failed_step = "Step 1 (Template Rendering)"
    try:
        logger.info("   [1/5] Compiling source templates to sandbox render/ ...")
        render_res = run_primitive_2_render_packages(workspace_config, target_pkgs=target_pkgs, no_hooks=no_hooks)
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

    # 4. Physical Deployment of configurations to host system target paths
    failed_step = "Step 4 (Physical Deploy/Install)"
    try:
        logger.info("   [4/5] Deploying and copying/linking configurations to active host paths ...")
        install_res = run_primitive_5_install_deployment(
            workspace_config,
            packages_to_redeploy=target_pkgs,
            resolve_symlinks=True,
            force=force,
            package_changes=package_changes,
            no_hooks=no_hooks
        )
        completed_steps.append(CompletedStep(4, "physical_install"))
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise RuntimeError(f"Midway crash: {failed_step} failed.") from e

    # 5. Commit state database configurations
    failed_step = "Step 5 (State Database Committing)"
    try:
        logger.info("   [5/5] Committing deployment changes in install/ repository ...")
        run_primitive_6_commit_install_repo(
            workspace_config,
            commit_message=f"Deploy Install: Automatically commit deployed changes for {pkgs_label}",
            target_pkgs=target_pkgs
        )
        completed_steps.append(CompletedStep(5, "install_commit"))
    except Exception as e:
        logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
        msg = (
            f"The deployment succeeded on your host, but committing to the state database failed.\n"
            f"👉 Please resolve the Git state manually by running:\n"
            f"    drift install-commit -m \"Deploy Install: Automatically commit deployed changes for {pkgs_label}\""
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(f"{failed_step} failed.") from e

    return install_res.packages, completed_steps


def run_primitive_deploy_pipeline(
    workspace_config: WorkspaceConfig,
    packages_to_deploy: Optional[List[str]] = None,
    force: bool = False,
    no_hooks: bool = False
) -> DeployResult:
    """Main deployment pipeline controller running Sentinel Drift checking and sequential compile/apply."""
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
            is_global_deploy=(packages_to_deploy is None),
            target_packages=[],
            deployed_packages=[]
        )

    # Stage 1: Sentinel Drift Auditing
    check_and_prevent_system_drifts(workspace_config, target_pkgs, force=force)

    # Stage 2: Deploy Pipeline Execution
    deployed_packages, completed_steps = execute_sequential_compile_and_apply(
        workspace_config, target_pkgs, force=force, no_hooks=no_hooks
    )

    # Stage 3: Call garbage collection on global deploy
    gc_res: Optional[GcResult] = None
    if not packages_to_deploy:
        logger.info("🧹 Performing global deployment garbage collection...")
        gc_res = run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=False, no_hooks=no_hooks)

    logger.info(f"✨ Successfully completed deployment for package(s): {', '.join(target_pkgs)}")

    return DeployResult(
        command="deploy",
        status="SUCCESS",
        is_global_deploy=(packages_to_deploy is None),
        target_packages=target_pkgs,
        deployed_packages=deployed_packages,
        gc=gc_res,
        completed_steps=completed_steps
    )
