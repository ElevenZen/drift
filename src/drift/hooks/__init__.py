"""Drift lifecycle hooks and extensibility systems."""

from .lifecycle_hooks import (
    HookExecFlags,
    assert_valid_hook_file,
    execute_hook_script,
    resolve_hook_source_path,
    resolve_hook_exec_path,
    trigger_hook_with_render,
    trigger_pre_source_hook,
    trigger_probe_hook,
    trigger_hook,
)
from .workspace_hook import (
    WorkspaceHookContext,
    load_workspace_hook_module,
    execute_workspace_hook,
    apply_workspace_hook,
)
from .package_hook import (
    PackageHookContext,
    resolve_package_hook_path,
    load_package_hook_module,
    execute_package_hook,
    apply_package_hook,
)
from .trigger_hook import (
    run_primitive_trigger_hook,
)

__all__ = [
    "HookExecFlags",
    "assert_valid_hook_file",
    "execute_hook_script",
    "resolve_hook_source_path",
    "resolve_hook_exec_path",
    "trigger_hook_with_render",
    "trigger_pre_source_hook",
    "trigger_probe_hook",
    "trigger_hook",
    "WorkspaceHookContext",
    "load_workspace_hook_module",
    "execute_workspace_hook",
    "apply_workspace_hook",
    "PackageHookContext",
    "resolve_package_hook_path",
    "load_package_hook_module",
    "execute_package_hook",
    "apply_package_hook",
    "run_primitive_trigger_hook",
]
