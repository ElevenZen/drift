# `src/drift/hooks/` — Layer 2 Hook Execution Engines Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 2 (Python Preprocessor Hooks & Subprocess Lifecycle Hook Engines)**
* **Core Responsibility**: Executes dynamic Python configuration preprocessor hooks (`config/drift_workspace.py` and `src/<pkg>/drift_package.py`) in a zero-`os.environ`-mutation clean room, and executes subprocess lifecycle hook scripts (`probe`, `pre_source`, `post_render`, `pre_install`, `post_install`, `pre_update`, `post_update`, `pre_uninstall`, `post_uninstall`, `health`) across `source`, `render`, and `install` tiers.
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../models/`](../models/) (in `trigger_hook.py` for `HookResult`), [`../config/`](../config/), and [`../render/`](../render/) (for compiling hook templates when triggered from `source`).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **In-Memory Python Preprocessor Hooks**: [`workspace_config_hook.py`](workspace_config_hook.py), [`package_config_hook.py`](package_config_hook.py)
2. **Subprocess Lifecycle Hook Runner**: [`lifecycle_hooks.py`](lifecycle_hooks.py)
3. **Standalone CLI Hook Trigger Primitive (`drift hook`)**: [`trigger_hook.py`](trigger_hook.py)

---

## 3. Module & Public API Catalog

### [`workspace_config_hook.py`](workspace_config_hook.py) — Dynamic Python Workspace Preprocessor (`config/drift_workspace.py`)
* `WorkspaceHookContext(config, drift_root, env, facts, discovered_packages=())`: Clean-room context passed to `configure_workspace(context) -> Dict[str, Any]` with convenience properties `.os`, `.arch`, `.distro`, `.hostname`, `.user`.
* `apply_workspace_hook(drift_root: Path, config_dict: Dict[str, Any], secrets_file_dict: Optional[Mapping[str, str]] = None) -> Dict[str, Any]`: Resolves `[workspace] hook_file` (or default `config/drift_workspace.py`), builds an in-memory `real_env` snapshot without touching `os.environ`, and returns the transformed workspace config dictionary.

### [`package_config_hook.py`](package_config_hook.py) — Dynamic Python Package Preprocessor (`src/<pkg>/drift_package.py`)
* `PackageHookContext(config, package_name, package_dir, drift_root=None, workspace_config=None, env=..., facts=..., package_facts=...)`: Clean-room context passed to `configure_package(context) -> Dict[str, Any]`.
* `resolve_package_hook_path(package_dir: Path, config_dict: Dict[str, Any], package_name: str = "") -> Optional[Path]`: Resolves `[package] hook_file` (or default `src/<pkg>/drift_package.py`) using 3-state hybrid path validation.
* `apply_package_hook(package_dir: Path, config_dict: Dict[str, Any], workspace_config: Optional[WorkspaceConfig] = None) -> Tuple[Dict[str, Any], Optional[Path]]`: Executes `configure_package(context)` in-memory and returns `(transformed_dict, resolved_hook_path)`.

### [`lifecycle_hooks.py`](lifecycle_hooks.py) — Subprocess Lifecycle Hook Engine
* `HookExecFlags(dry_run=False, no_hooks=False, force=False, raise_on_error=True, inject_non_interactive_envs=True, ensure_hooks_executable_in_src=True, output_encoding="utf-8")`:
  * `.resolve(flags: Optional[HookExecFlags], settings: Optional[SettingsConfig] = None, **overrides) -> HookExecFlags`: Merges explicit flags with workspace `[settings]` defaults.
* `HookFailureDetails`: Structured diagnostic formatter for hook timeouts, non-zero exit codes, and OS errors.
* `build_hook_execution_command(hook_path: Path) -> List[str]`: Dispatches between POSIX (`build_hook_execution_command_posix`) and Windows (`build_hook_execution_command_win32` supporting `.ps1`, `.bat`, `.cmd`, `.py`, `.sh`, `.bash`, `.exe`).
* `assert_valid_hook_file(hook_path: Optional[Path], package_name: str, hook_name: str) -> Path`: Verifies hook file exists, is not a directory, and is executable on POSIX (raising `HookMissingError`).
* `execute_hook_script(hook_path, package_name, hook_name, timeout=120, flags=None, is_fatal=True) -> bool`: Runs the hook subprocess with `cwd = hook_path.parent`, optionally injecting `NON_INTERACTIVE_HOOK_ENVS`, and raises `HookExecutionError(is_fatal=is_fatal)` on failure when `flags.raise_on_error` is `True`.
* `trigger_hook_with_render(...)`, `trigger_pre_source_hook(...)`, `trigger_probe_hook(...)`, `trigger_hook(...)`: High-level trigger entry points handling on-the-fly template compilation for `pre_source` / `probe` hooks and direct execution for rendered/installed hooks.

### [`trigger_hook.py`](trigger_hook.py) — Standalone Hook Execution (`drift hook`)
* `trigger_hook_from_source(workspace_config, package_name, hook_name, flags=None) -> HookResult`
* `trigger_hook_from_install(workspace_config, package_name, hook_name, stage=PackageStage.INSTALL, flags=None) -> HookResult` (supports both `PackageStage.INSTALL` and `PackageStage.RENDER`).
* `run_primitive_trigger_hook(workspace_config, package_name, hook_name, from_tier: PackageStage = PackageStage.INSTALL, flags=None) -> HookResult`: Dispatches `drift hook <pkg> <hook_name> [--from <install|render|source>]`.

---

## 4. Local Invariants & Modification Rules
1. **Clean-Room Python Preprocessor Invariant**: `apply_workspace_hook` and `apply_package_hook` must **never** mutate `os.environ`. All environment variables and facts are passed strictly via `context.env`, `context.facts`, and `context.package_facts`.
2. **User-Space Execution & Working Directory**: Subprocess lifecycle hooks always execute in user space (never prefixed with `sudo`) so all 6 tiers of `os.environ` are preserved, and always run with `cwd = hook_path.parent`.
3. **Hook Fatality Respect (`is_fatal`)**: `execute_hook_script` and `handle_hook_execution_failure` propagate `is_fatal = pkg_hooks.is_failure_fatal(hook_name)` onto `HookExecutionError.is_fatal` so `install_repo.py` can distinguish fatal aborts from non-fatal post-hook warnings.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/configuration_and_hooks.md`](../../../docs/features/configuration_and_hooks.md), [`docs/features/rollback_and_wal.md`](../../../docs/features/rollback_and_wal.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_hooks.py
  PYTHONPATH=src python3 -m unittest tests/test_python_hooks.py
  ```
