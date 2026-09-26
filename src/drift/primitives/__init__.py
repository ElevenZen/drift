"""Drift operational state-transition primitives and maintenance workflows."""

from .reverse_sync import (
    run_primitive_1_reverse_sync,
)
from .package_assertions import (
    assert_packages_hooks_exist,
    assert_install_pkg_dirs_clean,
    assert_packages_not_in_midway_state,
    assert_packages_install_dirs_exist,
    assert_packages_target_dirs_valid,
    assert_packages_target_dirs_writable,
    assert_no_cross_package_conflicts,
)
from .stage_repo import (
    run_primitive_4_stage_render_to_install,
    PackageStageChanges,
    apply_package_stage_changes,
    assert_packages_stage_ready,
    prepare_stage_packages,
    execute_stage_packages,
    StagePlan,
)
from .install_repo import (
    run_primitive_5_install_deployment,
    prepare_install_deployment,
    execute_install_deployment,
    DeployPlan,
    run_primitive_6_commit_install_repo,
    DeployOptions,
    PackageInstallContext,
    deploy_one_package,
    deploy_one_package_impl,
    assert_packages_deployment_ready,
)
from .uninstall_repo import (
    run_primitive_7_uninstall_packages,
)
from .rollback_repo import (
    run_primitive_8_rollback_recovery,
)
from .workspace_gc import (
    run_primitive_9_purge_workspace_garbage,
)
from .new_package import (
    run_primitive_10_create_new_package,
)
from .add_resource import (
    run_primitive_11_add_resources,
)
from .package_health import (
    run_primitive_health_checks,
)
from .workspace_clone import (
    run_primitive_clone,
)
from .workspace_repair import (
    repair_drift_workspace,
    build_repair_result,
)
from .workspace_diff import (
    run_primitive_15_workspace_diff,
)
from .workspace_status import (
    PackageStatus,
    WorkspaceStatusResult,
    run_primitive_status,
)
from .deploy_repo import (
    run_primitive_deploy_pipeline,
    run_primitive_deploy_pipeline_with_error_handling,
)
from .adopt_repo import (
    run_primitive_adopt_drifts,
    adopt_one_package_drifts,
)
from .workspace_check import (
    check_existing_workspace_status,
    check_drift_workspace,
    ComponentStatus,
    WorkspaceHealthReport,
)
from .workspace_init import (
    init_drift_workspace,
)

__all__ = [
    "run_primitive_1_reverse_sync",
    "run_primitive_4_stage_render_to_install",
    "prepare_stage_packages",
    "execute_stage_packages",
    "StagePlan",
    "PackageStageChanges",
    "apply_package_stage_changes",
    "assert_packages_hooks_exist",
    "assert_install_pkg_dirs_clean",
    "assert_packages_not_in_midway_state",
    "assert_packages_install_dirs_exist",
    "assert_packages_target_dirs_valid",
    "assert_packages_target_dirs_writable",
    "assert_no_cross_package_conflicts",
    "assert_packages_stage_ready",
    "assert_packages_deployment_ready",
    "prepare_install_deployment",
    "execute_install_deployment",
    "DeployPlan",
    "run_primitive_5_install_deployment",
    "run_primitive_6_commit_install_repo",
    "DeployOptions",
    "PackageInstallContext",
    "deploy_one_package",
    "deploy_one_package_impl",
    "run_primitive_7_uninstall_packages",
    "run_primitive_8_rollback_recovery",
    "run_primitive_9_purge_workspace_garbage",
    "run_primitive_10_create_new_package",
    "run_primitive_11_add_resources",
    "run_primitive_health_checks",
    "run_primitive_clone",
    "repair_drift_workspace",
    "build_repair_result",
    "run_primitive_15_workspace_diff",
    "PackageStatus",
    "WorkspaceStatusResult",
    "run_primitive_status",
    "run_primitive_deploy_pipeline",
    "run_primitive_deploy_pipeline_with_error_handling",
    "run_primitive_adopt_drifts",
    "adopt_one_package_drifts",
    "check_existing_workspace_status",
    "check_drift_workspace",
    "ComponentStatus",
    "WorkspaceHealthReport",
    "init_drift_workspace",
]
