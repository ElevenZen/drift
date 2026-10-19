# ⚙️ Configuration, Render Engines, Hooks & Dependencies Specification

This document specifies Drift's global workspace and package configuration schemas, `.local.toml` / `.envst.toml` layering, custom render engine pipelines, dynamic Python preprocessor hooks, subprocess lifecycle hooks, inter-package topological dependencies, `.drift_ignore` PCRE regex rules, and 3-state cross-platform path validation.

* **Core Architectural Overview**: [`docs/architecture.md`](../architecture.md)
* **Environment Variable Precedence & DAG Resolution**: [`docs/features/env_modeling.md`](env_modeling.md)
* **Render DAG & Merkle Compilation**: [`docs/features/render_dag.md`](render_dag.md)
* **Related Subpackage Maps**: [`src/drift/config/AGENT_MODULE.md`](../../src/drift/config/AGENT_MODULE.md), [`src/drift/hooks/AGENT_MODULE.md`](../../src/drift/hooks/AGENT_MODULE.md)

---

## 1. Global Workspace Configuration (`config/drift_workspace.toml`)

Rather than scanning the filesystem blindly, the Drift engine relies on a centralized workspace configuration file at `config/drift_workspace.toml` (which can itself be a template named `drift_workspace.envst.toml` and overlaid by `drift_workspace.local.toml` / `drift_workspace.local.envst.toml`).

It orchestrates three core responsibilities:
1. **Workspace Paths & Rendering Engines**: Defines directories (`source_directory`, `render_directory`, `install_directory`, `backup_directory`, `default_target_directory`, `default_install_method`) and template engines with their file suffixes and rendering subprocess commands.
2. **Global Execution Settings (`[settings]`)**: Configures workspace-level operational policies (`rollback = "auto" | "always" | "never"`, `ensure_hooks_executable_in_src`, `hook_inject_non_interactive_envs`, `hook_output_encoding`).
3. **Enabled Packages Registry (`[packages.enable]`)**: Declares which package subfolders under `src/` are globally active (`true` / `false`, plus optional `DEFAULT = true | false`).

```toml
# =====================================================================
# drift_workspace.toml Configuration
# =====================================================================

[workspace]
source_directory = "src"
render_directory = "render"
install_directory = "install"
backup_directory = "backup"
default_target_directory = "~"
default_install_method = "symlink"
# hook_file = "drift_workspace.py"

[settings]
ensure_hooks_executable_in_src = true
hook_inject_non_interactive_envs = true
hook_output_encoding = "utf-8"

# Workspace-level automatic rollback policy on fatal deploy/apply failure:
# - "auto" (default): Respects each package's `auto_rollback` setting in drift_package.toml
# - "always" / "yes": Forces automatic whole-batch rollback on any fatal package failure
# - "never" / "no": Disables automatic rollback across the workspace (unless overridden by CLI --rollback)
rollback = "auto"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
input_file = "mustache.envst.json"
suffix = "mustache"
render_command = "mustache %i %s"

[packages.enable]
DEFAULT = false
shell = true
nvim = true
qbittorrent = true
proxychains = false
```

### 1.1 Meta-Config Templating (`drift_workspace.envst.toml` & `drift_workspace.local.envst.toml`)
Workspace config files (`drift_workspace.toml` and `drift_workspace.local.toml`) can themselves be templates named `drift_workspace.envst.toml` or `drift_workspace.local.envst.toml`. Drift compiles them on-the-fly using `python_envsubst` populated with active system-level environment variables (`load_workspace_config_file_with_render` in [`src/drift/config/workspace_loader.py`](../../src/drift/config/workspace_loader.py)).

### 1.2 Native TOML Variable Stitching & Unidirectional 2-Stage Evaluation
Drift provides native, zero-dependency topological variable stitching across all TOML configuration files (`drift_workspace.toml`, `drift_workspace.local.toml`, `drift_package.toml`, `drift_package.local.toml`):
1. **Stage 1 (Environment Tables Resolution)**: Environment tables (`[env.default]`, `[env.override]`, `[env.fallback]`, `[env.secrets]`) evaluate first in topological order via Kahn's algorithm ([`topological_sort_env`](../../src/drift/utils/env_utils.py)). Cyclic or self-referencing variables raise [`ConfigError`](../../src/drift/core/exceptions.py).
2. **Stage 2 (Non-Env Section Interpolation)**: All other configuration fields (`[workspace]`, `[package]`, `[hooks]`, `[requirements]`) are recursively interpolated using the resolved environment (`resolved_env.impact.full_env()`). Prepending a backslash (`\$VAR` or `\${VAR}`) preserves literal strings.

### 1.3 Dynamic Workspace Python Hook (`config/drift_workspace.py`)
Executed dynamically during workspace loading ([`apply_workspace_hook`](../../src/drift/hooks/workspace_config_hook.py)) before package scanning and rendering begin:
```python
from typing import Dict, Any
from drift import WorkspaceHookContext

def configure_workspace(context: WorkspaceHookContext) -> Dict[str, Any]:
    if context.os == "Linux" and context.distro == "arch":
        context.config.setdefault("packages", {}).setdefault("enable", {})["hyprland"] = True
    return context.config
```
* **`WorkspaceHookContext` Properties**: `context.config`, `context.drift_root`, `context.env` (in-memory environment snapshot with **zero footprint on `os.environ`**), `context.facts` (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`), `context.discovered_packages`, and convenience accessors (`context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`).

---

## 2. Custom Render Engines & Template Input Dependencies

Defined in [`src/drift/config/render_engine_config.py`](../../src/drift/config/render_engine_config.py).

### 2.1 Render Engine Schema (`[render.<engine_name>]`)
1. **`input_file`**: Optional file path providing variables/values to the engine. Relative paths resolve against `config/` (workspace engines) or `src/<pkg>/` (package engines); paths escaping `base_dir` are rejected.
2. **`suffix`**: File extension token matched by the engine (e.g. `envst`, `mustache`). **Cannot contain dots (`.`)** and cannot use reserved Drift keywords (`drift_package`, `drift_hook`, `drift_ignore`, `drift_workspace`, `drift_hooks`, `drift`).
3. **`render_command`**: Shell command pattern supporting `%i` (resolved absolute path of compiled `input_file`) and `%s` (absolute path of source template). If `%i` is referenced (`requires_input_file = True`), `input_file` is mandatory.

### 2.2 Template Naming Convention (IDE & LSP Friendly)
* **Format**: `[filename].[engine_suffix].[target_extension]` (e.g. `dot-bashrc.envst.sh`, `settings.mustache.json`).
* Because the terminal extension is the target format (`.sh`, `.json`, `.nix`), editors and LSPs apply syntax highlighting and linting natively without custom filetype mappings.

### 2.3 Transitive Input Dependencies & Single-Dependency Constraint
* An engine's `input_file` can itself be a template matching another registered engine (e.g. `mustache` uses `input_file = "mustache.envst.json"`, which is compiled first by `envsubst` into `render/<pkg>/.drift/render/workspace/mustache.json`).
* **Single-Dependency Constraint**: Each input file matches at most one engine suffix at the outermost level (`file.<engine1>.<engine2>.ext` evaluates `<engine2>` first and yields `file.<engine1>.ext`).
* **Cyclic Detection**: During AST expansion ([`expand_node_dependencies`](../../src/drift/render/render_expansion.py)), circular engine chains (`A -> B -> A`) raise `ValueError`.
* **Render Collision Detection**: If two source files in a package compile or copy to the same destination path in `render/<pkg>/`, compilation halts with [`RenderCollisionError`](../../src/drift/core/exceptions.py).

### 2.4 Package-Level Render Engines & Scope Invariants
* **Overlay & Field-Level Inheritance**: At render time, `workspace_config.render_engine_configs.overlay(pkg_config.render_engine_configs)` merges package engines over workspace engines, inheriting unspecified fields (`input_file`, `suffix`, `render_command`).
* **Global Engines Only for Package Config**: Only workspace-level engines in `config/drift_workspace.toml` can compile `src/<pkg>/drift_package.<suffix>.toml` during Phase 1 bootstrap. Package-level `[render.<name>]` engines operate strictly in Phase 2 (hooks) and Phase 3 (payload dotfiles).

---

## 3. Package Configuration (`src/<pkg>/drift_package.toml`)

Every package requires `drift_package.toml` (or templated `drift_package.<suffix>.toml`) at the root of `src/<pkg>/`, optionally overlaid by git-ignored `drift_package.local.toml` (`package.local.toml`).

### 3.1 Unified Control-Plane Target (`render/<pkg>/.drift/drift_package.toml`)
During Phase 1 of rendering ([`load_package_config_from_source_dir`](../../src/drift/config/package_loader.py)), base and local override configs are compiled, merged, passed through `drift_package.py` (if present), interpolated across all 6 environment tiers, and written to `render/<pkg>/.drift/drift_package.toml` (and staged 1:1 to `install/<pkg>/.drift/drift_package.toml`). Downstream primitives (`stage`, `apply`, `uninstall`, `health`) read this static rendered TOML directly via `PackageConfig.from_render_dir` / `PackageConfig.from_install_dir`.

### 3.2 Dynamic Package Python Hook (`src/<pkg>/drift_package.py`)
Executed in-memory by [`apply_package_hook`](../../src/drift/hooks/package_config_hook.py) with **zero footprint on `os.environ`**:
```python
from __future__ import annotations
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from drift.hooks import PackageHookContext

def configure_package(context: PackageHookContext) -> Dict[str, Any]:
    if context.os == "darwin":
        context.config.setdefault("package", {})["target_directory"] = "~/Library/Application Support/MyApp"
    return context.config
```
* **`PackageHookContext` Properties**: `context.config`, `context.package_name`, `context.package_dir`, `context.drift_root`, `context.workspace_config`, `context.env`, `context.facts`, `context.package_facts` (`drift_package_name`, `drift_package_source_dir`, `drift_package_src_dir`, `drift_package_render_dir`, `drift_package_install_dir`), plus `.os`, `.arch`, `.distro`, `.hostname`, `.user`.

### 3.3 Full `drift_package.toml` Reference Schema
```toml
[package]
# hook_file = "drift_package.py"
enable_render = true
enable_install = true
install_method = "symlink"          # "symlink" or "copy"
target_directory = "~/.config/example"
# target_directory_windows = "%LOCALAPPDATA%/example"
# dependencies = ["base", { name = "git" }, { name = "nodejs", optional = true }]
# source_directory = "dotfiles"
sudo = false
auto_rollback = false
fully_controlled_dirs = ["sub_dir1", "sub_dir2"]

[requirements]
os = ["linux", "darwin"]
# arch = ["x86_64", "arm64"]
# distro = ["arch", "fedora"]
binaries = ["curl", "git"]
# env = ["WAYLAND_DISPLAY"]
# ip = ["192.168.1.*", "10.0.0.0/8"]

[hooks]
probe = "drift_hooks/probe.bash"
pre_source = "drift_hooks/pre-source.bash"
post_render = "drift_hooks/post-render.bash"
pre_install = "drift_hooks/pre-install.bash"
post_install = "drift_hooks/post-install.bash"
pre_update = "drift_hooks/pre-update.bash"
post_update = "drift_hooks/post-update.bash"
pre_uninstall = "drift_hooks/pre-uninstall.bash"
post_uninstall = "drift_hooks/post-uninstall.bash"
health = "drift_hooks/health.bash"
timeout = 120
failure_is_fatal = true             # bool or list of installation hooks: ["pre_install", "pre_update"]
```

### 3.4 Three Cascading Enablement Switches
1. **`drift_workspace.toml -> [packages.enable]`**: Controls global workspace discovery. Toggling a previously installed package to `false` causes global `drift deploy` (Stage 3 GC) to automatically uninstall it as an orphan.
2. **`drift_package.toml -> enable_render`**: Controls whether template engines run during Primitive 2 (`false` copies files 1:1 as static assets).
3. **`drift_package.toml -> enable_install`**: Controls whether compiled files in `render/` are staged to `install/` and deployed to the host (`false` isolates the package strictly inside the `render/` sandbox, even under `--force`).

### 3.5 Lifecycle Hooks & `drift_hooks/` Isolation
* **Strict `drift_hooks/` Placement**: All package-internal hook scripts and helper dependencies must reside inside `src/<pkg>/drift_hooks/` (or be external absolute binaries). Relative paths outside `drift_hooks/` raise `ConfigError`.
* **Control-Plane Isolation**: Hook scripts compile into `render/<pkg>/.drift/hooks/` and stage into `install/<pkg>/.drift/hooks/`, never deploying to the host target unless explicitly symlinked into the payload tree.
* **Execution Environment & Working Directory**: All lifecycle hooks run in user space without `sudo` (preserving all 6 environment tiers in `os.environ` via `with pkg_config.package_envs():`) and execute with `cwd = hook_path.parent`. The host target directory is exposed via `$drift_package_target_dir`.
* **Hook Fatality (`failure_is_fatal`)**: Defaults to `true`. Accepts a boolean (`true` / `false`) or a list of installation hook names (`["pre_install", "post_install", "pre_update", "post_update"]`). When non-fatal on `post_install` or `post_update`, the package still transitions to `"installed"` and commits its `install/` state before reporting the non-fatal hook error.
* **Event Ordering (`symlink` vs `copy`)**:
  * Under `install_method = "copy"`, host files remain at their prior version during `pre_update`, are updated during physical delivery, and have the new state during `post_update`.
  * Under `install_method = "symlink"`, staging (`render/` $\to$ `install/`) updates existing symlink targets before `pre_update` runs, while new/deleted symlinks are applied between `pre_update` and `post_update`.

---

## 4. Inter-Package Dependencies & Topological DAG Orchestration

Implemented in [`src/drift/config/package_config.py`](../../src/drift/config/package_config.py) (`PackageDependency`, `PackageDependencies`) and [`src/drift/primitives/package_assertions.py`](../../src/drift/primitives/package_assertions.py).

* **Required Dependencies (`optional = false`, Default)**: Must exist in the active package universe (installed in `state.toml` or targeted in the current batch with `enable_install = true`). Validated by [`assert_required_package_dependencies_exist`](../../src/drift/primitives/package_assertions.py) (bypassed via `--force` or `--no-deps`).
* **Optional Dependencies (`optional = true`)**: Ordering-only constraint; silently pruned if absent from the active universe.
* **Forward Order (Stage & Install)**: [`resolve_package_install_order`](../../src/drift/primitives/package_assertions.py) and [`resolve_target_package_order`](../../src/drift/primitives/package_assertions.py) sort prerequisites before dependents using Kahn's algorithm after verifying acyclicity via [`assert_no_cyclic_package_dependencies`](../../src/drift/primitives/package_assertions.py).
* **Reverse Order (Uninstall & Rollback)**: [`resolve_package_uninstall_order`](../../src/drift/primitives/package_assertions.py) sorts dependents before prerequisites. During uninstallation, [`assert_no_broken_dependencies_on_uninstall`](../../src/drift/primitives/package_assertions.py) prevents removing packages still required by remaining installed packages (unless `--force` or `--no-deps`).

---

## 5. Ignore Engine (`.drift_ignore`) & Prefix Conversion Rules

Implemented in [`src/drift/core/ignore.py`](../../src/drift/core/ignore.py) and [`src/drift/utils/path_utils.py`](../../src/drift/utils/path_utils.py).

1. **Single Root `.drift_ignore` File**: Allowed only at `src/<pkg>/.drift_ignore` (rendered/staged to `.drift/.drift_ignore`). Nested `.drift_ignore` or `.driftignore` files in subdirectories raise `ValueError`.
2. **PCRE Regular Expressions (Two-Group Stow Matching)**:
   * **Group 1 (Patterns containing `/`)**: Matched against the normalized relative path prefixed with `/` (e.g. `^/dot-config/nvim/init\.lua$`, `/cache/`).
   * **Group 2 (Patterns without `/`)**: Matched against the file or directory basename anywhere in the tree (e.g. `\.bak$`, `\.swp$`).
3. **Pre-Conversion Evaluation Timing**: Patterns match source filenames **before** `dot-` prefix translation or template suffix stripping (e.g. match `dot-bashrc.envst.sh`, not `.bashrc`).
4. **Hardcoded Control-Plane Shielding**: `.drift/` (`^/\.drift/`, `^/\.drift$`) is permanently excluded from host deployment.
5. **Mandatory `dot-` Prefix Rule**: Hidden target files in `src/<pkg>/` must use the `dot-` prefix (`dot-bashrc` $\leftrightarrow$ `.bashrc`, `dot-config/` $\leftrightarrow$ `.config/`). Raw dot-files in `src/<pkg>/` (except `.drift_ignore`) are skipped during rendering.

---

## 6. Cross-Platform 3-State Path Taxonomy & Boundary Validation

Implemented in [`src/drift/utils/path_utils.py`](../../src/drift/utils/path_utils.py). Across POSIX and Windows, every `pathlib.Path` falls into one of **3 mutually exclusive states**:

| State | Classification | Predicate | Examples | `base_dir / p` Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **State 1** | **Strictly Relative** | `not p.anchor` | `Path("src")`, `Path("a/b")` | Joins safely beneath `base_dir` |
| **State 2** | **Partially Anchored** | `bool(p.anchor) and not p.is_absolute()` | Win `Path("/foo")`, `Path("C:foo")` | **Hazardous**: strips `base_dir` path/drive |
| **State 3** | **Strictly Absolute** | `p.is_absolute()` | `Path("/etc")`, `Path("C:\\Users")` | Replaces `base_dir` completely |

* **Helper Predicates**: [`check_is_relative_path`](../../src/drift/utils/path_utils.py) validates State 1 (`not p.anchor`), and [`check_is_partially_anchored_path`](../../src/drift/utils/path_utils.py) detects State 2 (`bool(p.anchor) and not p.is_absolute()`).

* **Contract A — Strictly Relative Fields** (`source_directory`, `render_directory`, `install_directory`, `backup_directory`, `fully_controlled_dirs`, `encode_dot_prefix`, `decode_dot_prefix`): Enforce `not p.anchor`.
* **Contract B — Strictly Absolute Fields** (`default_target_directory`, `target_directory`, post-ingestion hook paths): Enforce `p.is_absolute()`.
* **Contract C — Hybrid Relative-or-Absolute Inputs** (`hook_file`, `[hooks].*`, `[render.<engine>].input_file`): Explicitly reject State 2 (`check_is_partially_anchored_path`) before resolving `p if p.anchor else (base_dir / p)`.
