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
    assert_required_package_dependencies_exist,
    assert_no_cyclic_package_dependencies,
    assert_no_broken_dependencies_on_uninstall,
    resolve_package_install_order,
    resolve_package_uninstall_order,
    resolve_target_package_order,
)
from .stage_repo import (
    run_primitive_4_stage_render_to_install,
    PackageStagePlan,
    StageResult,
    plan_package_stage,
    execute_package_stage,
    assert_packages_stage_ready,
    prepare_stage_packages,
    execute_stage_packages,
    StagePlan,
)
from .install_repo import (
    run_primitive_5_install,
    prepare_install,
    execute_install,
    InstallPlan,
    run_primitive_6_commit_install_repo,
    InstallConfig,
    PackageInstallContext,
    install_one_package,
    execute_package_install,
    execute_package_install_impl,
    plan_package_install,
    execute_package_actions,
    assert_packages_install_ready,
)
from .uninstall_repo import (
    run_primitive_7_uninstall_packages,
    UninstallConfig,
    UninstallPlan,
    PackageUninstallContext,
    plan_package_uninstall,
    execute_package_uninstall,
    assert_packages_uninstall_ready,
    prepare_uninstall_packages,
    execute_uninstall_packages,
    uninstall_missing_package,
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
    prepare_add_resources,
    execute_add_resources,
    plan_add_resources,
    AddResourcePlan,
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
    "PackageStagePlan",
    "StageResult",
    "plan_package_stage",
    "execute_package_stage",
    "assert_packages_hooks_exist",
    "assert_install_pkg_dirs_clean",
    "assert_packages_not_in_midway_state",
    "assert_packages_install_dirs_exist",
    "assert_packages_target_dirs_valid",
    "assert_packages_target_dirs_writable",
    "assert_no_cross_package_conflicts",
    "assert_required_package_dependencies_exist",
    "assert_no_cyclic_package_dependencies",
    "assert_no_broken_dependencies_on_uninstall",
    "resolve_package_install_order",
    "resolve_package_uninstall_order",
    "resolve_target_package_order",
    "assert_packages_stage_ready",
    "assert_packages_install_ready",
    "prepare_install",
    "execute_install",
    "InstallPlan",
    "run_primitive_5_install",
    "run_primitive_6_commit_install_repo",
    "InstallConfig",
    "PackageInstallContext",
    "install_one_package",
    "execute_package_install",
    "execute_package_install_impl",
    "plan_package_install",
    "execute_package_actions",
    "run_primitive_7_uninstall_packages",
    "UninstallConfig",
    "UninstallPlan",
    "PackageUninstallContext",
    "plan_package_uninstall",
    "execute_package_uninstall",
    "assert_packages_uninstall_ready",
    "prepare_uninstall_packages",
    "execute_uninstall_packages",
    "uninstall_missing_package",
    "run_primitive_8_rollback_recovery",
    "run_primitive_9_purge_workspace_garbage",
    "run_primitive_10_create_new_package",
    "run_primitive_11_add_resources",
    "prepare_add_resources",
    "execute_add_resources",
    "plan_add_resources",
    "AddResourcePlan",
    "run_primitive_health_checks",
    "run_primitive_clone",
    "repair_drift_workspace",
    "build_repair_result",
    "run_primitive_15_workspace_diff",
    "PackageStatus",
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
