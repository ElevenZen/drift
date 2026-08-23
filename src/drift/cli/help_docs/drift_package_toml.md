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

# Execute deployment operations with root privileges (sudo) if required
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

# Lifecycle Hooks (Optional shell command execution)
# Timeout in seconds before hook processes are aborted (defaults to 120)
hook_timeout = 120

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

# Run immediately after sandbox rendering is complete (executed from render/ package root)
post_render = "scripts/generate_checksums.sh"
```
