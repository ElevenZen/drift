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
# Render Engines Configurations
# ---------------------------------------------------------------------

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
