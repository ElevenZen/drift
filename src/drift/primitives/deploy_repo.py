from dataclasses import dataclass
import shlex
import sys
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..utils.git_utils import get_git_status_porcelain, assert_repo_can_commit
from .reverse_sync import run_primitive_1_reverse_sync
from ..render.render_package import (
    run_primitive_2_render_packages,
    run_primitive_3_commit_render_repo,
    RenderOptions,
)
from .stage_repo import (
    prepare_stage_packages,
    execute_stage_packages,
    StageResult,
)
from .install_repo import (
    run_primitive_5_install,
    prepare_install,
    execute_install,
    InstallPlan,
    run_primitive_6_commit_install_repo,
    InstallOptions,
)
from .workspace_gc import run_primitive_9_purge_workspace_garbage
from ..hooks.lifecycle_hooks import HookExecFlags
from ..core.exceptions import HookExecutionError, DriftError, is_drift_error, is_logged, mark_logged
from ..core.constants import STATE_REGISTRY_FILE_NAME, InstallMethod
from ..core.state_registry import load_state_registry
from ..core.result_models import (
    NextActionType,
    CompletedStep,
    DeployFailure,
    DeployResult,
    PackageInstallResult,
    GcResult,
)

logger = logging.getLogger(__name__)


@dataclass
class DeployOptions:
    """Execution flags and behavioral options controlling deployment and dry-run planning pipelines.

    Attributes:
        force: If True, bypasses safeguard checks (e.g. host drift sentinel, midway failed states).
        reinstall: If True, forces re-execution of delivery actions regardless of delta checks.
        no_deps: If True, bypasses missing required package dependency assertions.
        no_hooks: If True, bypasses and disables all package lifecycle hooks.
        with_hooks: If True, executes pre-flight probes and hooks in isolated scratch sandboxes.
        no_cache: If True, bypasses render caches and Merkle lockfiles.
        dry_run: If True, runs planning and simulation pipelines without mutating host or state DB.
        show_all: If True, instructs formatters to display granular action breakdowns for all packages,
                  including unchanged packages (which are otherwise collapsed into a single summary line).
        verbose: If True, includes NO_CHANGE actions (e.g. SKIP_IDENTICAL, INFO_MESSAGE) in output.
    """
    force: bool = False
    reinstall: bool = False
    no_deps: bool = False
    no_hooks: bool = False
    with_hooks: bool = False
    no_cache: bool = False
    dry_run: bool = False
    show_all: bool = False
    verbose: bool = False


def check_and_prevent_system_drifts(
    workspace_config: WorkspaceConfig,
    target_pkgs: List[str],
    force: bool = False
) -> Tuple[List[str], List[str]]:
    """Stage 1: Safety Guard (Sentinel)
    Runs pre-flight state checks and silent reverse-sync to verify if any targeted package has drifted.
    If a midway state or drift is detected and force is False, aborts execution instantly with instructions.
    If drift is detected and force is True, captures and commits a drift snapshot into install/ repo
    to ensure live system modifications are preserved in history and overwritten backups are accurate.
    Returns (drifted_packages, drifted_files).
    """
    # 0. Check if any targeted package is in a midway transaction state from a previous crash/failure
    state_file = workspace_config.install_path / STATE_REGISTRY_FILE_NAME
    if state_file.exists() and not force:
        state_registry = load_state_registry(state_file)
        midway_pkgs = state_registry.get_rollback_eligible_packages(target_pkgs)

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
    
    sync_res = run_primitive_1_reverse_sync(
        workspace_config, package_names=target_pkgs, missing_ok=True
    )
    syncable_pkgs = [p.package for p in sync_res.packages] if sync_res.packages else []

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

    if drifted_packages:
        if not force:
            pkg_cmd_str = shlex.join(drifted_packages)
            err_msg = (
                f"❌ [DEPLOY ABORTED] System drift detected in packages: {', '.join(drifted_packages)}!\n"
                "Host configurations have drifted from the state database.\n\n"
                f"👉 Run 'drift diff -s {pkg_cmd_str}' to view the active system modifications.\n"
                f"👉 Run 'drift adopt {pkg_cmd_str}' to incorporate these modifications into your template.\n"
                f"👉 Run 'drift deploy {pkg_cmd_str} --force' to discard system drifts and overwrite."
            )
            raise RuntimeError(err_msg)

        logger.info(f"💾 Capturing host drift snapshot in install/ repository for: {', '.join(drifted_packages)}")
        run_primitive_6_commit_install_repo(
            workspace_config,
            commit_message=f"Drift Snapshot: Capture host modifications for {', '.join(drifted_packages)} before forced deployment",
            target_pkgs=drifted_packages
        )

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
    reinstall: bool = False,
    no_deps: bool = False,
) -> Tuple[List[PackageInstallResult], List[CompletedStep]]:
    """Stage 2: Sequential Compile & Apply with midway transaction error catching."""
    logger.info("🚀 [STAGE 2] Starting sequential compilation and apply pipeline...")
    completed_steps: List[CompletedStep] = []
    hook_flags = HookExecFlags.resolve(flags, settings=workspace_config.settings)
    
    # 1. Render raw templates to sandbox
    failed_step = "Step 1 (Template Rendering)"
    try:
        logger.info("   [1/5] Compiling source templates to sandbox render/ ...")
        render_res = run_primitive_2_render_packages(
            workspace_config,
            target_pkgs=target_pkgs,
            options=RenderOptions.resolve(hook_flags),
        )
        if render_res.status == "FAILED":
            raise RuntimeError(render_res.error_message or f"{failed_step} failed.")
        completed_steps.append(CompletedStep(1, "template_rendering"))
    except Exception as e:
        if is_drift_error(e) or is_logged(e):
            logger.error(f"❌ [CRITICAL] {failed_step} failed.")
        else:
            logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
            mark_logged(e)
        logger.info("👉 You can resolve the template issues and simply try 'drift deploy' again.")
        raise mark_logged(RuntimeError(f"{failed_step} failed.")) from e

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
        if is_drift_error(e) or is_logged(e):
            logger.error(f"❌ [CRITICAL] {failed_step} failed.")
        else:
            logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
            mark_logged(e)
        logger.info("👉 Please resolve any render sandbox repository Git issues and try 'drift deploy' again.")
        raise mark_logged(RuntimeError(f"{failed_step} failed.")) from e

    # 3a. Pre-flight Validation & Assertion for Sandbox Staging (Read-Only)
    failed_step = "Step 3 (Sandbox Staging Pre-flight)"
    try:
        stage_plan = prepare_stage_packages(
            workspace_config,
            target_pkgs=target_pkgs,
            force=force,
            no_deps=no_deps,
        )
    except Exception as e:
        if is_drift_error(e) or is_logged(e):
            logger.error(f"❌ [CRITICAL] {failed_step} failed.")
        else:
            logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
            mark_logged(e)
        causing_pkgs = getattr(e, "packages", None)
        if causing_pkgs:
            pkgs_str = ", ".join(f"'{p}'" for p in causing_pkgs)
            logger.info(f"👉 Problematic package(s): {pkgs_str}. Please resolve pre-flight issues and try 'drift deploy' again.")
        else:
            logger.info("👉 Please resolve any staging pre-flight issues and try 'drift deploy' again.")
        raise mark_logged(RuntimeError(f"{failed_step} failed.")) from e

    if not stage_plan.pkg_metadata:
        logger.info("✨ No active packages are enabled for staging/deployment. Skipping physical deployment.")
        return [], completed_steps

    # 3b. Stage render sandbox to install state base (State-Mutating)
    failed_step = "Step 3 (Sandbox Staging)"
    try:
        logger.info("   [3/5] Staging rendered changes from render/ to install/ state database ...")
        stage_result = execute_stage_packages(
            workspace_config,
            plan=stage_plan,
        )
        completed_steps.append(CompletedStep(3, "sandbox_staging"))
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise mark_logged(RuntimeError(f"Midway crash: {failed_step} failed.")) from e

    changed_pkgs = stage_result.packages_changed
    if reinstall:
        pkgs_to_install = target_pkgs
    elif changed_pkgs:
        pkgs_to_install = changed_pkgs
    else:
        logger.info("✨ No package changes detected during staging. Skipping physical deployment.")
        return [], completed_steps

    # NOTE [Stage Delta Filter & Reinstall Bridge]:
    # Stage 3 (stage_repo) tracks modifications across ALL files in render/<pkg>/ vs install/<pkg>/,
    # including non-deployable control plane files (.drift/hooks/*, .drift/drift_package.toml).
    # Consequently, changed_pkgs accurately reflects whether anything in the package was updated.
    # However, Primitive 5's internal change-detection (plan_package_install) only inspects physical
    # deployable files against host state. If a staging change only affected non-deployable files (e.g.
    # an updated lifecycle hook script or altered hook flags), Primitive 5 would see 0 host mutations
    # and skip the package unless reinstall=True.
    # Therefore, we pass reinstall=True to ensure that all packages filtered as changed by staging are
    # fully processed by Primitive 5 (ensuring hooks execute and state updates).
    install_options = InstallOptions(
        resolve_symlinks=True,
        force=force,
        reinstall=True,
        flags=hook_flags,
        no_deps=no_deps,
    )

    # 4a. Pre-flight Validation & Pre-Transaction Conflict Audit for Deployment (Read-Only)
    failed_step = "Step 4 (Deployment Pre-flight)"
    try:
        install_plan = prepare_install(
            workspace_config,
            target_pkgs=pkgs_to_install,
            options=install_options,
        )
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), pkgs_to_install)
        err = RuntimeError(f"Midway crash: {failed_step} failed.")
        raise mark_logged(err) from e

    # 4b. Physical Deployment of configurations to host system target paths (State-Mutating)
    failed_step = "Step 4 (Physical Deploy/Install)"
    pkgs_install_label = ", ".join(pkgs_to_install)
    try:
        logger.info(f"   [4/5] Deploying and copying/linking configurations to active host paths for: {pkgs_install_label} ...")
        install_res = execute_install(
            workspace_config,
            plan=install_plan,
        )
        completed_steps.append(CompletedStep(4, "physical_install"))
    except HookExecutionError as e:
        if e.requires_rollback:
            print_emergency_recovery_card(failed_step, str(e), target_pkgs)
            raise mark_logged(RuntimeError(f"Midway crash: {failed_step} failed.")) from e
        # No rollback needed, so commit the changes here.
        try:
            run_primitive_6_commit_install_repo(
                workspace_config,
                commit_message=f"Deploy Install: Automatically commit deployed changes for {pkgs_install_label}",
                target_pkgs=pkgs_to_install
            )
        except Exception as commit_err:
            logger.error(f"Failed to commit install/ repository changes following non-rollback hook failure: {commit_err}")
        logger.error(f"❌ [DEPLOY ABORTED] {failed_step} stopped due to hook failure in '{e.package}'.")
        raise mark_logged(RuntimeError(f"{failed_step} stopped due to hook failure in '{e.package}': {e.message}")) from e
    except Exception as e:
        print_emergency_recovery_card(failed_step, str(e), target_pkgs)
        raise mark_logged(RuntimeError(f"Midway crash: {failed_step} failed.")) from e

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
        if is_drift_error(e) or is_logged(e):
            logger.error(f"❌ [CRITICAL] {failed_step} failed.")
        else:
            logger.error(f"❌ [CRITICAL] {failed_step} failed. Error: {e}")
            mark_logged(e)
        msg = (
            f"The deployment succeeded on your host, but committing to the state database failed.\n"
            f"👉 Please resolve the Git state manually by running:\n"
            f"    drift install-commit -m \"Deploy Install: Automatically commit deployed changes for {pkgs_install_label}\""
        )
        print(msg, file=sys.stderr)
        raise mark_logged(RuntimeError(f"{failed_step} failed.")) from e

    return install_res.packages, completed_steps


def run_primitive_deploy_pipeline(
    workspace_config: WorkspaceConfig,
    packages_to_deploy: Sequence[str] = (),
    force: bool = False,
    flags: Optional[HookExecFlags] = None,
    reinstall: bool = False,
    no_deps: bool = False,
) -> DeployResult:
    """Main deployment pipeline controller running Sentinel Drift checking and sequential compile/apply.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_deploy: Specific package name(s) to deploy, or empty/omitted for all active packages.
        force: If True, bypasses the Sentinel Drift check (capturing and committing a drift snapshot
            in install/ before overwriting) and passes force to Primitive 4 (staging) and Primitive 5
            (install) to bypass midway failed state checks and uncommitted modification safeguards.
            Note: Does NOT bypass 'enable_install = false' package configurations.
        flags: Optional HookExecFlags controlling hook execution options.
        reinstall: If True, forces full reinstallation of all requested packages regardless of staging delta.
        no_deps: If True, bypasses missing required package dependency checks.

    Returns:
        DeployResult containing detailed status and deployed packages.
    """
    # 0. Pre-flight checks: Verify render/ and install/ repositories can commit successfully
    logger.info("🔍 Running pre-flight Git configuration checks...")
    assert_repo_can_commit(workspace_config.render_path)
    assert_repo_can_commit(workspace_config.install_path)

    # Discover target active packages from source directory
    target_pkgs = workspace_config.filter_source_packages_by_target(target_packages=packages_to_deploy or None)
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
        workspace_config,
        target_pkgs,
        force=force,
        flags=flags,
        reinstall=reinstall,
        no_deps=no_deps,
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
    reinstall: bool = False,
    no_deps: bool = False,
) -> DeployResult:
    """Executes the deployment pipeline, catching exceptions and returning a structured DeployResult.

    Args:
        workspace_config: The workspace configuration instance.
        packages_to_deploy: Specific package name(s) to deploy, or None for all active packages.
        force: If True, bypasses safeguards and proceeds with deployment.
        flags: Optional HookExecFlags controlling hook execution options.
        no_deps: If True, bypasses missing required package dependency checks.

    Returns:
        DeployResult containing detailed status, deployed packages, or failure details.
    """
    try:
        return run_primitive_deploy_pipeline(
            workspace_config=workspace_config,
            packages_to_deploy=packages_to_deploy,
            force=force,
            flags=flags,
            reinstall=reinstall,
            no_deps=no_deps,
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
            rollback_pkgs = packages_to_deploy
            rec_cmd = f"drift rollback {shlex.join(rollback_pkgs)}".strip()
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

