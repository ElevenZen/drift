# 📝 drift_package.toml Complete Configuration Reference

Below is a complete, fully documented template for `drift_package.toml` (or `drift_package.local.toml`):

```toml
[package]
# How files are deployed to the host.
# Options: "stow" (symlinks, GNU Stow logic) or "copy" (physical copies)
# Falls back to "default_install_method" in drift_workspace.toml if unspecified.
install_method = "stow"

# The target folder path on the host where files should be mapped.
# Supports home expansion (~).
# Falls back to "default_target_directory" in drift_workspace.toml if unspecified.
target_directory = "~/.config/my_app"

# Optional Windows-specific target folder path.
# Used instead of target_directory when running on Windows (win32).
# Supports %USERPROFILE%, %APPDATA%, %LOCALAPPDATA%, ~, etc.
# Aliases accepted: target_directory_windows, target_directory_win32, target_directory_winos, target_directory_win.
# target_directory_windows = "%LOCALAPPDATA%/my_app"

# Optional subfolder within src/<pkg>/ to render (defaults to ".").
# If specified, only files in this subfolder are compiled and deployed to the host.
# source_directory = "dotfiles"

# Advanced Flags

# Optional dynamic Python package hook file path (relative to src/<pkg>/, defaults to "drift_package.py" if present)
# hook_file = "drift_package.py"

# Execute physical file deployments (copy, stow, deletions, permissions) with root privileges (sudo).
# Note: All lifecycle hooks always execute in user space without sudo to preserve all injected environment variables.
sudo = false

# Enable or disable template rendering for this package
enable_render = true

# Enable or disable installation/deployment for this package
enable_install = true

# Directories where Drift has total control. Drift will automatically
# synchronize and prune deleted files inside these subdirectories (FCDs).
fully_controlled_dirs = [
    "themes",
    "plugins"
]

# Host Requirements & Prerequisites (Declarative pre-flight checks; package is skipped if unmet)
[package.requirements]
# os = ["linux"]                     # Allowed OS: "linux", "darwin", "windows", "freebsd"
# arch = ["x86_64", "aarch64"]        # Allowed Arch: "x86_64", "arm64", "aarch64", "x86"
# distro = ["arch", "ubuntu"]         # Allowed Linux Distro IDs from /etc/os-release
# binaries = ["sway", "waybar"]       # Executables required in host $PATH
# env = ["WAYLAND_DISPLAY"]           # Required environment variables when starting drift
# ip = ["192.168.1.0/24"]             # Allowed LAN IPs (exact IP, CIDR subnet e.g. 10.0.0.0/8, or wildcard e.g. 192.168.1.*)

# ---------------------------------------------------------------------
# Package Environment Variables, Native Variable Stitching & 7-Tier Precedence
# ---------------------------------------------------------------------
# Drift TOML configurations natively support topological variable self-referencing and stitching ($VAR, ${VAR}).
# External render engines (e.g. drift_package.envst.toml) can also be used if dynamic generation
# is needed, but native self-referencing is the built-in, zero-dependency default for .toml files.
#
# Variable Stitching & Referencing Rules:
# 1. [env.fallback] (Tier 7): Baseline defaults applied ONLY when unset across upper tiers.
#    Evaluated first against base environment. CANNOT reference [env.override].
# 2. [env.override] (Tier 2): High-priority overrides (overwrites facts/workspace env; CLI wins).
#    Evaluated second. CAN reference [env.fallback], package facts (${drift_package_name},
#    ${drift_package_source_dir}), system facts (${drift_os}, ${drift_arch}), and workspace [env].
# 3. Non-Env Sections: Fields across [package], [hooks], etc. can reference any resolved [env]
#    variables (e.g. target_directory = "${HOME}/.config/${drift_package_name}").
#    Variables defined outside [env] cannot be referenced inside [env].
# 4. Values-Only Scope: Variable stitching and interpolation occurs STRICTLY within configuration field values
#    (strings, arrays). Variable references are NEVER evaluated in TOML keys, table names, or section headers.
# 5. Escaping: Use a leading backslash (\${VAR} or \$VAR) to keep literal strings without interpolation.

[env.fallback]
# Baseline default values (applied only when unset across all other scopes)
# APP_PORT = "8080"
# APP_HOST = "localhost"
# APP_DATA_DIR = "${HOME}/.local/share/${drift_package_name}"

[env.override]
# Highest-priority package variables (overrides workspace configs, secrets, and system facts)
# APP_URL = "http://${APP_HOST}:${APP_PORT}"
# APP_SRC = "${drift_package_source_dir}"


[hooks]
# Lifecycle Hooks (Optional shell command execution)
# Timeout in seconds before hook processes are aborted (defaults to 120)
# Note: All hooks run in user space with full environment variable inheritance (all 7 tiers).
# If a hook command requires root privileges, use 'sudo' explicitly inside the hook script.
timeout = 120

# Run pre-flight dynamic requirement probe (Exit 0 = Met, Exit != 0 = Unmet -> package gracefully skipped)
# Executed from src/ package root in user space
# probe = "scripts/check_wayland.sh"

# Run before reading/writing source package files (e.g. generating dynamic files based on system status before render, adopt, or add)
# Executed from src/ package root
pre_source = "scripts/generate_dynamic_templates.sh"

# Run before a first-time installation (executed from install/ package root)
pre_install = "scripts/bootstrap.sh"

# Run after a first-time installation (executed from host target directory)
post_install = "echo 'Completed installation!'"

# Run before updating an already installed package (executed from install/ package root)
pre_update = "scripts/backup_settings.sh"

# Run after updating an already installed package (executed from host target directory)
post_update = "scripts/reload_service.sh"

# Run before uninstalling a package (executed from install/ package root)
pre_uninstall = "scripts/cleanup_pre.sh"

# Run after uninstalling a package (executed from host target directory)
post_uninstall = "scripts/cleanup_post.sh"

# Run immediately after sandbox rendering is complete (executed from render/ package root)
post_render = "scripts/generate_checksums.sh"

# Run runtime health check probes on installed package (executed from host target directory)
health = "scripts/health_check.sh"

# Control whether installation hook failure triggers an emergency rollback state (default: true).
# Set to false if hook failure does not corrupt system files and only requires stopping with a report.
# Can also be set to a list of installation hook names: e.g. rollback_on_failure = ["pre_install", "post_install"]
# (Allowed hook names: pre_install, post_install, pre_update, post_update)
rollback_on_failure = true


# Optional Windows-specific hook overrides.
# When running on Windows (win32), hook paths defined here automatically
# override the default [hooks] entries.
# Aliases accepted: [hooks.windows], [hooks.win32], [hooks.winos], [hooks.win].
# On Windows, '.exe' binary files are executed directly as native executables,
# and following file types are automatically executed via their respective interpreters:
#   • .ps1  -> powershell.exe -NoProfile -ExecutionPolicy Bypass -File <script>
#   • .bat / .cmd -> cmd.exe /c <script>
#   • .py   -> python <script>
#   • .sh / .bash -> bash.exe <script> (if available in PATH)
[hooks.windows]
pre_install = "scripts/bootstrap.exe"
post_install = "scripts/setup.ps1"
post_update = "scripts/reload_service.bat"
health = "scripts/health_check.ps1"


# ---------------------------------------------------------------------
# Package-Level Render Engines & Field-Level Inheritance
# ---------------------------------------------------------------------
# Packages can define custom render engines or override workspace engines.
# When overriding an existing engine from drift_workspace.toml, unspecified fields (suffix, render_command)
# are inherited from workspace configuration, while input_file is overridden.
# Relative input_file paths are resolved relative to this package's directory (src/<pkg>/).
# Intermediate input file template outputs are rendered into render/<pkg>/.drift/ sandbox.

# Define a brand new package-scoped render engine:
[render.custom]
input_file = "config_data.json"
suffix = "custom"
render_command = "bash -c 'cat %i %s'"

# Or override only the input_file of a workspace-level engine (inherits suffix and render_command):
# [render.envsubst]
# input_file = "pkg_env.sh"
```

## 🪝 Lifecycle Hooks Execution Matrix

All lifecycle hooks execute **in user space without `sudo`**, preserving all 7 tiers of environment variables (`$drift_package_*`, `$drift_*`, `[env.override]`, `[env.fallback]`, secrets). If elevated root privileges are required for a specific command (e.g., restarting a service), write `sudo` explicitly within the hook script.

| Hook Name | Lifecycle Trigger Stage | Working Directory (`cwd`) |
| :--- | :--- | :--- |
| `probe` | Requirement validation (`deploy`, `render`, `status`) | `src/<pkg>` |
| `pre_source` | Before reading templates (`render`, `adopt`, `add`, `deploy`) | `src/<pkg>` |
| `post_render` | After sandbox compilation (`render`, `deploy`) | `render/<pkg>` |
| `pre_install` | Before first-time deployment (`apply`, `deploy`, `rollback`) | `install/<pkg>` |
| `post_install` | After first-time deployment (`apply`, `deploy`, `rollback`) | `target_directory` |
| `pre_update` | Before updating an installed package (`apply`, `deploy`, `rollback`) | `install/<pkg>` |
| `post_update` | After updating an installed package (`apply`, `deploy`, `rollback`) | `target_directory` |
| `pre_uninstall` | Before unlinking/deleting files (`uninstall`, `gc`, `deploy`) | `target_directory` |
| `post_uninstall` | After unlinking/deleting files (`uninstall`, `gc`, `deploy`) | `install/<pkg>` |
| `health` | During `drift health` probe execution | `target_directory` |

> [!NOTE]
> Pass `--no-hooks` (or `--no-hook`) on relevant CLI commands (`render`, `apply`, `deploy`, `adopt`, `add`, `uninstall`, `rollback`, `gc`) to bypass hook execution entirely.

> [!TIP]
> **Triggering Hooks Directly**: You can trigger any individual lifecycle hook script in isolation using the low-level command:
> ```bash
> drift hook <package> <hook-name> [--json]
> ```
> This executes the hook with its standard working directory, stage directory context, and complete environment variable injections.

## 🌐 Package Environment Variables & Preemption Order
 
When executing lifecycle hooks (such as `pre_source`, `post_render`, `pre_install`, `post_update`, `pre_uninstall`, `post_uninstall`, `health`) and when rendering package template files (e.g. `.envst` templates via `envsubst`), Drift automatically injects authoritative host facts and package-specific environment variables:
 
### 🖥️ Auto-Populated System Facts:
*   **`$drift_os`**: Target OS family (`linux`, `darwin`, `windows`, `freebsd`).
*   **`$drift_arch`**: Target CPU architecture (`x86_64`, `arm64`, `aarch64`, `x86`).
*   **`$drift_distro`**: Linux distribution ID or OS identifier (`ubuntu`, `arch`, `debian`, `fedora`, `macos`, `windows`).
*   **`$drift_hostname`**: Host network hostname.
*   **`$drift_user`**: Current user login name.

### 📦 Package-Specific Facts:
*   **`$drift_package_name`**: Name / directory name of the package.
*   **`$drift_package_target_dir`**: Resolved absolute destination target directory path on the host system.
*   **`$drift_package_source_dir`**: Absolute path to the package's source directory (`<drift_root>/src/<pkg>`).
*   **`$drift_package_render_dir`**: Absolute path to the package's compiled sandbox directory (`<drift_root>/render/<pkg>`).
*   **`$drift_package_install_dir`**: Absolute path to the package's state database directory (`<drift_root>/install/<pkg>`).
*   **`$drift_package_install_method`**: Resolved deployment method (`stow` or `copy`).
 
### ⚡ Seven-Tier Variable Preemption Order:
When rendering package templates and running hook scripts, variables resolve in the following strict order (highest priority wins):
1. **Host Shell / CLI Variables**: Explicit user environment variables from invocation.
2. **`[env.override]` in Package Config**: Package-enforced overrides (`src/<pkg>/drift_package.toml`).
3. **`drift_package_*` Package Facts**: Authoritative package paths, target directory, install method.
4. **`drift_*` System Facts**: Authoritative host OS, architecture, distro, hostname, user.
5. **`secrets` in Workspace**: Loaded from `config/secrets.env` / Secret Provider.
6. **`[env]` in Workspace Config**: Shared defaults from `config/drift_workspace.toml` / `drift_workspace.local.toml`.
7. **`[env.fallback]` in Package Config**: Package defaults used only when unset by upper tiers.
 
 
## 🐍 Dynamic Python Package Hooks (`drift_package.py`)

For programmatic, procedural package configuration that exceeds static TOML or variable stitching capabilities, Drift supports **dynamic Python package hooks**.

### Automatic Discovery or Custom Path
* **Default Path**: Place a `drift_package.py` file directly in your package's source directory (`src/<pkg>/drift_package.py`). Drift automatically discovers and executes it.
* **Custom Path**: Explicitly configure `[package] hook_file = "my_hook.py"` (resolved relative to `src/<pkg>/`).

### Execution Model & Pipeline Order
1. **Multi-File Discovery & Merging**: Discovers candidate configuration files (`drift_package.toml`, `drift_package.local.toml`, or custom layers) and `.envst.toml` templates via `load_package_config_dict`, merging them sequentially.
2. **Dynamic Python Package Hook (Preprocessor)**: Executes `configure_package(context)` BEFORE variable stitching. The hook receives the raw merged dictionary and has full access to resolved host facts (`context.facts`), system facts (`context.os`, `context.arch`, `context.distro`, etc.), and active environment (`context.env`). The hook can inject `[env.override]`, customize `target_directory`, or set `enable_install = False`.
3. **Variable Stitching & Topological Resolution (Compiler)**: Resolves `[env.override]` and `[env.fallback]` tables (including any injected by the hook) according to Drift's 7-tier precedence model and Kahn's topological sort algorithm.
4. **Cross-Section Interpolation**: Interpolates `${VAR}` expressions across non-env sections (`target_directory`, `requirements`, etc.).
5. **Render Staging**: Writes the fully resolved, stitched configuration to `render/<pkg>/drift_package.toml`. Downstream install stages (`apply`, `deploy`) consume the rendered static TOML, ensuring single compilation and high performance.
6. **Schema Validation & Model Construction**: Instantiates the strongly-typed `PackageConfig` object.

### `PackageHookContext` Reference
The `context` object passed into `configure_package(context)` provides:
* `context.config`: The package's raw configuration dictionary (from `drift_package.toml` and `.local.toml`).
* `context.package_name`: Active package name (`str`).
* `context.package_dir`: Absolute path to the package's source directory (`Path`).
* `context.drift_root`: Absolute path to the drift root directory (`Optional[Path]`).
* `context.workspace_config`: The parent `WorkspaceConfig` object (`Optional[WorkspaceConfig]`).
* `context.env`: Full host environment snapshot (`Dict[str, str]`).
* `context.facts`: Detected system facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
* `context.package_facts`: Detected package facts (`drift_package_name`, `drift_package_source_dir`, `drift_package_render_dir`, `drift_package_install_dir`).
* Helper properties: `context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`.

### Example `drift_package.py`
```python
# src/my_app/drift_package.py

def configure_package(context):
    """Dynamically transform package configuration based on host facts and workspace settings."""
    cfg = context.config
    pkg = cfg.setdefault("package", {})

    # Strictly disable package installation on incompatible hosts
    if context.os not in ("linux", "darwin"):
        pkg["enable_install"] = False
        return cfg

    # Dynamically select install method or target directory based on OS
    if context.os == "darwin":
        pkg["target_directory"] = "~/Library/Application Support/my_app"
    elif context.os == "linux" and context.distro == "arch":
        pkg["install_method"] = "stow"

    # Dynamically set host requirements
    reqs = pkg.setdefault("requirements", {})
    if context.arch == "x86_64":
        reqs["binaries"] = ["my_app_x86"]

    # Inject package environment variables
    env_override = cfg.setdefault("env", {}).setdefault("override", {})
    env_override["APP_RUN_MODE"] = "optimized" if "prod" in context.hostname else "debug"

    return cfg
```


## 🎨 Package-Level Render Engines & 2-Stage Compilation

Packages can define custom render engines or override global workspace render engines via `[render.<name>]` in `drift_package.toml`.

### 1. Field-Level Inheritance
When a package defines a `[render.<name>]` table for an engine already defined in `drift_workspace.toml`, Drift applies **field-level inheritance**:
* **`input_file`**: Overridden by the package's local input file (relative to `src/<pkg>/`).
* **`suffix`**: Inherited from the workspace engine if omitted in the package config.
* **`render_command`**: Inherited from the workspace engine if omitted in the package config.

### 2. Multi-Stage Compilation Chain & Sandbox Isolation
Drift evaluates compilation pipelines across two distinct, isolated stages:
1. **Stage 1 (Workspace Scope)**: Global workspace engines render workspace input templates into `render/.drift/` and compile templated package configuration files (e.g. `src/<pkg>/drift_package.envst.toml` $\rightarrow$ `render/<pkg>/drift_package.toml`).
2. **Stage 2 (Package Scope)**: Package configurations are loaded, package engines are overlaid onto workspace engines, and any package-level input templates are rendered into the package's internal sandbox (`render/<pkg>/.drift/`). Package files are then compiled using the effective engine registry.

### 3. Reverse Sync, Add, & Adopt Integration
All downstream primitives (`drift adopt`, `drift add`, `drift reverse-sync`) automatically respect package-level render engine definitions and suffix mappings when reconciling file modifications, renames, and imports.



