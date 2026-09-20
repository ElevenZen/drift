"""Drift lifecycle hooks and extensibility systems."""

from .lifecycle_hooks import (
    HookExecFlags,
    execute_hook_script,
    trigger_package_hook_with_render,
    trigger_pre_source_hook,
    trigger_probe_hook,
    trigger_package_hook,
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
    "execute_hook_script",
    "trigger_package_hook_with_render",
    "trigger_pre_source_hook",
    "trigger_probe_hook",
    "trigger_package_hook",
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
