# 📝 drift.toml Complete Global Configuration Reference

Below is a complete, fully documented template for the global `config/drift.toml` file:

```toml
[workspace]
# Source directory for declarative packages (relative to workspace root)
source_directory = "src"

# Sandbox compilation directory path
render_directory = "render"

# State tracking database directory path
install_directory = "install"

# Backup directory path for overwritten or deleted files
backup_directory = "backup"

# Global default target directory for packages if unspecified in drift_package.toml
# Supports home expansion (~).
default_target_directory = "~"

# Global default installation method if unspecified in drift_package.toml
# Options: "stow" (symlinks) or "copy" (physical copies)
default_install_method = "stow"


# ---------------------------------------------------------------------
# Workspace Environment Variables Propagation
# ---------------------------------------------------------------------
# Variables defined under the [env] table are automatically populated into
# os.environ. They provide global defaults for template rendering engines.
# (Note: Package-specific envs like $drift_package_target_dir and secrets in
# config/secrets.env take precedence over workspace [env] definitions.)
[env]
DRIFT_SAMPLE_ENV_THEME = "nord-dark"
DRIFT_SAMPLE_ENV_EDITOR = "vim"


# ---------------------------------------------------------------------
# Render Engines Configurations
# ---------------------------------------------------------------------

# Built-in zero-dependency variable substitution engine (Windows + POSIX)
# Strictly validates that all referenced variables ($VAR, ${VAR}) exist.
[render.var]
suffix = "var"
render_command = "internal"


[render.envsubst]
# Input environment file relative to workspace 'config' folder
input_file = "envsubst.bash"

# File suffix to trigger envsubst template compilation
suffix = "envst"

# Render execution shell command.
# %i represents the resolved input_file path, %s represents the template path.
render_command = "bash -c 'source %i && envsubst < %s'"


[render.mustache]
# Input variables file. Since this ends with '.envst.json', envsubst will compile
# it first before mustache evaluates it (DAG dependency mapping).
input_file = "mustache.envst.json"

# File suffix to trigger mustache template compilation
suffix = "mustache"

# Render execution shell command
render_command = "mustache %i %s"


# ---------------------------------------------------------------------
# Workspace Behavioral Settings
# ---------------------------------------------------------------------
[settings]
# Probe outbound WAN / Internet routing IP address when collecting system facts.
# Defaults to false (only local system tables and interfaces are inspected, no network traffic).
# probe_wan_ip = false


# ---------------------------------------------------------------------
# Active Packages Registry
# ---------------------------------------------------------------------
# Dictionary registering active/enabled packages.
# Key: package folder name under src/
# Value: true (active) / false (disabled)
[packages.enable]
# The special 'DEFAULT' key sets the default activation state for unlisted packages
DEFAULT = false

shell = true
nvim = true
qbittorrent = true
```
