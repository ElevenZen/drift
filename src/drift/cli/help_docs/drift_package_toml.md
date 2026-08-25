# 📝 drift_package.toml Complete Configuration Reference

Below is a complete, fully documented template for `drift_package.toml` (or `drift_package.local.toml`):

```toml
[package]
# Unique identifier name for the package
name = "my_package"

# How files are deployed to the host.
# Options: "stow" (symlinks, GNU Stow logic) or "copy" (physical copies)
# Falls back to "default_install_method" in drift.toml if unspecified.
install_method = "stow"

# The target folder path on the host where files should be mapped.
# Supports home expansion (~).
# Falls back to "default_target_directory" in drift.toml if unspecified.
target_directory = "~/.config/my_app"

# Advanced Flags

# Execute physical file deployments and installation/update lifecycle hooks with root privileges (sudo).
# Note: Source/compilation hooks (pre_source, post_render) always run in user space without sudo.
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

[hooks]
# Lifecycle Hooks (Optional shell command execution)
# Timeout in seconds before hook processes are aborted (defaults to 120)
timeout = 120

# Run before reading/writing source package files (e.g. generating dynamic files based on system status before render, adopt, or add)
# Executed from src/ package root (runs in user space, never with sudo)
pre_source = "scripts/generate_dynamic_templates.sh"

# Run before a first-time installation (executed from install/ package root; runs with sudo if sudo = true)
pre_install = "scripts/bootstrap.sh"

# Run after a first-time installation (executed from host target directory; runs with sudo if sudo = true)
post_install = "echo 'Completed installation!'"

# Run before updating an already installed package (executed from install/ package root; runs with sudo if sudo = true)
pre_update = "scripts/backup_settings.sh"

# Run after updating an already installed package (executed from host target directory; runs with sudo if sudo = true)
post_update = "scripts/reload_service.sh"

# Run immediately after sandbox rendering is complete (executed from render/ package root; runs in user space, never with sudo)
post_render = "scripts/generate_checksums.sh"
```

## 🌐 Default Package Environment Variables & Precedence

When executing lifecycle hooks (such as `pre_source`, `post_render`, `pre_install`, `post_update`) and when rendering package template files (e.g. `.envst` templates via `envsubst`), Drift automatically injects the following package-specific environment variables:

*   **`$drift_package_name`**: Name / directory name of the package.
*   **`$drift_package_target_dir`**: Resolved absolute destination target directory path on the host system.
*   **`$drift_package_source_dir`**: Absolute path to the package's source directory (`<drift_root>/src/<pkg>`).
*   **`$drift_package_render_dir`**: Absolute path to the package's compiled sandbox directory (`<drift_root>/render/<pkg>`).
*   **`$drift_package_install_dir`**: Absolute path to the package's state database directory (`<drift_root>/install/<pkg>`).
*   **`$drift_install_method`**: Resolved deployment method (`stow` or `copy`).

### ⚡ Environment Variable Precedence:
Package environment variables take the **highest precedence** in Drift and strictly **override all other environment variables**, including:
1. Host system / CLI environment variables (`os.environ`).
2. Global workspace environment variables in `drift.toml` (`[env]` table).
3. Secret variables loaded from `config/secrets.env`.

*(Note: These variables are loaded into the environment after the package configuration is parsed, so they are available inside templates and hook scripts, but cannot be used inside `drift_package.toml` itself.)*

