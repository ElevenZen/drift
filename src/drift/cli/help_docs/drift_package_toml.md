# 📝 drift_package.toml Complete Configuration Reference

Below is a complete, fully documented template for `drift_package.toml` (or `drift_package.local.toml`):

```toml
[package]
# How files are deployed to the host.
# Options: "stow" (symlinks, GNU Stow logic) or "copy" (physical copies)
# Falls back to "default_install_method" in drift.toml if unspecified.
install_method = "stow"

# The target folder path on the host where files should be mapped.
# Supports home expansion (~).
# Falls back to "default_target_directory" in drift.toml if unspecified.
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

# Package Environment Variables

# Package-level overrides: takes precedence over workspace [env], secrets.env, and system facts (CLI environment still wins).
[env.override]
# APP_THEME = "dark"
# LOG_LEVEL = "debug"

# Package-level defaults: safely populates variables if they haven't been defined by the host, workspace, or secrets.
[env.fallback]
# APP_PORT = "8080"
# APP_HOST = "localhost"

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
```

## 🪝 Lifecycle Hooks Execution Matrix

All lifecycle hooks execute **in user space without `sudo`**, preserving all 7 tiers of environment variables (`$drift_package_*`, `$drift_*`, `[env.override]`, `[env.fallback]`, secrets). If elevated root privileges are required for a specific command (e.g., restarting a service), write `sudo` explicitly within the hook script.

| Hook Name | Lifecycle Trigger Stage | Working Directory (`cwd`) | Privilege Model |
| :--- | :--- | :--- | :--- |
| `probe` | Requirement validation (`deploy`, `render`, `status`) | `src/<pkg>` | User space (Preserves all envs) |
| `pre_source` | Before reading templates (`render`, `adopt`, `add`, `deploy`) | `src/<pkg>` | User space (Preserves all envs) |
| `post_render` | After sandbox compilation (`render`, `deploy`) | `render/<pkg>` | User space (Preserves all envs) |
| `pre_install` | Before first-time deployment (`apply`, `deploy`, `rollback`) | `install/<pkg>` | User space (Preserves all envs) |
| `post_install` | After first-time deployment (`apply`, `deploy`, `rollback`) | `target_directory` | User space (Preserves all envs) |
| `pre_update` | Before updating an installed package (`apply`, `deploy`, `rollback`) | `install/<pkg>` | User space (Preserves all envs) |
| `post_update` | After updating an installed package (`apply`, `deploy`, `rollback`) | `target_directory` | User space (Preserves all envs) |
| `pre_uninstall` | Before unlinking/deleting files (`uninstall`, `gc`, `deploy`) | `target_directory` | User space (Preserves all envs) |
| `post_uninstall` | After unlinking/deleting files (`uninstall`, `gc`, `deploy`) | `install/<pkg>` | User space (Preserves all envs) |
| `health` | During `drift health` probe execution | `target_directory` | User space (Preserves all envs) |

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
6. **`[env]` in Workspace Config**: Shared defaults from `config/drift.toml` / `drift.local.toml`.
7. **`[env.fallback]` in Package Config**: Package defaults used only when unset by upper tiers.

