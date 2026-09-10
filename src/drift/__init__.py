# engine/drift/__init__.py
from .toml_utils import parse_toml
from .workspace_config import (
    WorkspaceConfig,
    WorkspaceSectionConfig,
    SettingsConfig,
    load_workspace_config,
)
from .render_engine_config import (
    RenderEngineConfig,
    RenderEngineRegistry,
    RenderSourceMatch,
)
from .env_utils import (
    load_env_settings,
    unload_env_settings,
    env_scope,
    secrets_env_scope,
    parse_secrets_env,
    parse_env_file,
    parse_env_text,
)
from .process_utils import (
    run_command,
    format_output,
    strip_ansi,
    has_admin_privileges,
    check_sudo_privilege,
    run_sudo_command,
)
from .file_utils import (
    is_editor_or_os_temporary_file,
)
from .constants import (
    CONFIG_DIR_NAME,
    WORKSPACE_CONFIG_FILE_NAME,
    WORKSPACE_CONFIG_LOCAL_FILE_NAME,
    LEGACY_WORKSPACE_CONFIG_FILE_NAMES,
    SECRETS_ENV_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME,
    DRIFT_IGNORE_LEGACY_FILE_NAME,
    DRIFT_IGNORE_FILE_NAME_LIST,
    get_default_drift_workspace_toml_content,
    DEFAULT_DRIFT_WORKSPACE_LOCAL_TOML_CONTENT,
    get_default_secrets_env_content,
    get_default_envsubst_content,
    get_default_mustache_content,
    get_default_jinja2_content,
    FORBIDDEN_PACKAGE_NAMES,
    TEMPORARY_FILE_PATTERNS,
    DEFAULT_DIFF_EXCLUDE_PATTERNS,
    DEFAULT_INTERNAL_GITIGNORE_CONTENT,
    get_default_internal_gitignore_content,
    INITIAL_ENV,
    SYSTEM_FACT_KEYS,
    inject_system_facts,
    update_initial_env,
    set_initial_env,
)
from .host_facts import (
    SystemFacts,
    get_system_facts,
    get_host_ip_addresses,
    get_host_os,
    get_host_arch,
    get_host_distro,
    get_host_hostname,
    get_host_user,
)
from .package_config import (
    PackageConfig,
    PackageHooks,
    PackageRequirements,
    resolve_and_interpolate_package_config,
    load_package_config_rendered,
    load_package_config_from_source_dir,
    load_package_config_from_render_dir,
)
from .lifecycle_hooks import (
    HookExecFlags,
    execute_hook_script,
    trigger_package_hook_with_render,
    trigger_pre_source_hook,
    trigger_probe_hook,
    trigger_package_hook,
)
from .render_core import (
    render_template,
    render_template_to_file,
)
from .render_package import (
    render_package,
    run_primitive_3_commit_render_repo,
)
from .reverse_sync import (
    run_primitive_1_reverse_sync,
)
from .ignore import (
    DriftIgnore,
    IgnoreHandler,
)
from .workspace_init import (
    init_drift_workspace,
)
from .git_utils import (
    git_init_repo,
    append_to_gitignore,
)
from .workspace_repair import (
    repair_drift_workspace,
)
from .check_repo import (
    check_existing_workspace_status,
    ComponentStatus,
    WorkspaceHealthReport,
)
from .stage_repo import (
    run_primitive_4_stage_render_to_install,
    PackageStageChanges,
)
from .state_registry import (
    StateRegistry,
    load_state_registry,
    save_state_registry,
)
from .install_repo import (
    run_primitive_5_install_deployment,
    run_primitive_6_commit_install_repo,
)
from .deploy_repo import (
    run_primitive_deploy_pipeline,
    run_primitive_deploy_pipeline_with_error_handling,
)
from .render_input import (
    resolve_dependencies,
    check_cyclic_dependencies,
    render_input_templates,
)
from .result_models import (
    NextActionType,
    FileOperations,
    PackageReverseSyncResult,
    ReverseSyncResult,
    PackageRenderResult,
    RenderResult,
    StageResult,
    PackageInstallResult,
    InstallDeploymentResult,
    RestoredBackup,
    PackageUninstallResult,
    UninstallResult,
    PackageAdoptResult,
    AdoptResult,
    GcResult,
    NewPackageResult,
    AddResourceResult,
    DeployFailure,
    DeployResult,
    RollbackResult,
    StatusResult,
    PackageStatusSummary,
    DiffType,
    DiffResult,
    RepairResult,
)
from .workspace_status import (
    PackageStatus,
    WorkspaceStatusResult,
    run_primitive_status,
)
from .workspace_diff import (
    run_primitive_diff,
)
from .editor_utils import (
    get_configured_editor,
    launch_single_file_editor,
    launch_side_by_side_editor,
    launch_vim_diff,
    launch_vscode_diff,
    launch_emacs_diff,
)
from .exceptions import (
    DriftError,
    ConfigError,
    DriftDetectedError,
    RenderError,
    CollisionError,
    HookExecutionError,
)

