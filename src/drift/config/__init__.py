"""Drift configuration models, specifications, and parsers."""

from ..utils.env_utils import (
    EnvConfig,
    EnvImpact,
    EnvResolve,
    parse_env_dict,
    resolve_env_configs,
)
from .workspace_config import (
    WorkspaceConfig,
    WorkspaceSectionConfig,
    SettingsConfig,
)
from .secrets_config import (
    SecretsConfig,
    fetch_workspace_secrets,
    parse_decrypt_command,
)
from .workspace_loader import (
    load_workspace_config,
    locate_workspace_config_file,
    parse_workspace_config_content,
    render_workspace_config,
    load_workspace_config_file_with_render,
    load_workspace_config_files_layered,
    resolve_and_interpolate_workspace_config,
    assert_no_legacy_workspace_config,
)
from .workspace_enable_edit import (
    WorkspaceEnableTarget,
    WorkspaceEnableEdit,
    resolve_workspace_enable_target,
    read_config_text,
    build_expected_enable_dict,
    assert_workspace_enable_edit_verified,
    assert_packages_enable_header_editable,
    check_shadowed_packages,
    render_workspace_enable_diff,
    plan_workspace_enable_edit,
)
from .package_config import (
    PackageConfig,
    PackageSectionConfig,
    PackageDependency,
    PackageDependencies,
)
from .package_requirements import (
    PackageRequirements,
    match_ip_address,
    match_ip_addresses,
)
from .package_hooks import (
    PackageHooks,
    normalize_hook_value,
)
from .package_loader import (
    load_package_config_rendered,
    load_package_config_from_source_dir,
    load_package_config_from_render_dir,
    load_package_config_for_install,
    resolve_and_interpolate_package_config,
)
from .render_engine_config import (
    RenderEngineConfig,
    RenderEngineRegistry,
    RenderSourceMatch,
    parse_render_command,
    parse_render_input_file,
)

__all__ = [
    "EnvConfig",
    "EnvImpact",
    "EnvResolve",
    "parse_env_dict",
    "resolve_env_configs",
    "WorkspaceConfig",
    "WorkspaceSectionConfig",
    "SecretsConfig",
    "SettingsConfig",
    "fetch_workspace_secrets",
    "parse_decrypt_command",
    "assert_no_legacy_workspace_config",
    "resolve_and_interpolate_workspace_config",
    "load_workspace_config",
    "locate_workspace_config_file",
    "parse_workspace_config_content",
    "render_workspace_config",
    "load_workspace_config_file_with_render",
    "load_workspace_config_files_layered",
    "WorkspaceEnableTarget",
    "WorkspaceEnableEdit",
    "resolve_workspace_enable_target",
    "read_config_text",
    "build_expected_enable_dict",
    "assert_workspace_enable_edit_verified",
    "assert_packages_enable_header_editable",
    "check_shadowed_packages",
    "render_workspace_enable_diff",
    "plan_workspace_enable_edit",
    "PackageConfig",
    "PackageSectionConfig",
    "PackageDependency",
    "PackageDependencies",
    "PackageHooks",
    "normalize_hook_value",
    "PackageRequirements",
    "match_ip_address",
    "match_ip_addresses",
    "resolve_and_interpolate_package_config",
    "load_package_config_rendered",
    "load_package_config_from_source_dir",
    "load_package_config_from_render_dir",
    "load_package_config_for_install",
    "RenderEngineConfig",
    "RenderEngineRegistry",
    "RenderSourceMatch",
    "parse_render_command",
    "parse_render_input_file",
]
