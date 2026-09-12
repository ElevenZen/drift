# 📝 drift_workspace.toml Complete Global Configuration Reference

Below is a complete, fully documented template for the global `config/drift_workspace.toml` file:

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
# Supports home expansion (~) and ${VAR} interpolation from [env].
default_target_directory = "~"

# Global default installation method if unspecified in drift_package.toml
# Options: "stow" (symlinks) or "copy" (physical copies)
default_install_method = "stow"

# Optional dynamic Python workspace configuration hook file (relative to the 'config/' directory).
# Defaults to "drift_workspace.py".
# hook_file = "drift_workspace.py"


# ---------------------------------------------------------------------
# Dynamic Python Workspace Hook (config/drift_workspace.py)
# ---------------------------------------------------------------------
# For programmatic configuration across heterogeneous machines without external scripts,
# author a Python hook in `config/drift_workspace.py` (or specify via `[workspace] hook_file` above, relative to `config/`).
#
# Configuration Pipeline Execution Order:
# 1. Multi-File Discovery & Merging: Loads base and override TOML files (or .envst.toml templates).
# 2. Dynamic Python Hook (Preprocessor): Executes configure_workspace(context) BEFORE variable stitching.
#    The hook receives raw config dict and can inject/modify [env], [packages.enable], etc., with full access to
#    resolved host facts (context.facts, context.os, context.arch, etc.) and active environment (context.env).
# 3. Variable Stitching & Topological Resolution: Resolves inter-variable references in [env] (including any
#    injected by the hook) and interpolates ${VAR} across all non-env sections.
# 4. Schema Validation: Constructs validated WorkspaceConfig instance.
#
# def configure_workspace(context):
#     cfg = context.config
#     facts = context.facts
#     if facts.get("drift_os") == "darwin":
#         cfg.setdefault("packages", {}).setdefault("enable", {})["macos_tools"] = True
#     return cfg


# ---------------------------------------------------------------------
# Workspace Environment Variables, Native Variable Stitching & Topological Resolution
# ---------------------------------------------------------------------
# Drift configurations natively support topological variable self-referencing and stitching ($VAR, ${VAR})
# directly within .toml files. External render engines (e.g. drift_workspace.local.envst.toml) can also
# be used if desired, but native self-referencing is the built-in, zero-dependency default.
#
# Variable Stitching & Referencing Rules:
# 1. Topological Stitching in [env]: Variables can reference each other (e.g. DRIFT_SAMPLE_SOCKS_PROXY = "...${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}").
#    Drift automatically evaluates dependencies using Kahn's topological sort algorithm with cycle detection.
# 2. External References: You can reference host environment variables (${HOME}, ${USER}), secret vault entries,
#    and auto-populated system facts (${drift_os}, ${drift_arch}, ${drift_distro}, ${drift_hostname}, ${drift_user}).
# 3. Unidirectional Evaluation Flow: ONLY variables defined in [env] (and inherited process environment/facts)
#    can be referenced across other drift_workspace.toml sections. Variables outside [env] cannot be referenced inside [env].
# 4. Values-Only Scope: Variable stitching and interpolation occurs STRICTLY within configuration field values
#    (strings, arrays). Variable references are NEVER evaluated in TOML keys, table names, or section headers.
# 5. Escaping: Use a leading backslash (\${VAR} or \$VAR) to prevent interpolation and preserve literal text.
[env]
SOCKS_PROXY_HOST = "127.0.0.1"
SOCKS_PROXY_PORT = "1080"
DRIFT_SAMPLE_SOCKS_PROXY = "socks5h://${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}"
DRIFT_SAMPLE_ALL_PROXY = "${DRIFT_SAMPLE_SOCKS_PROXY}"
DRIFT_SAMPLE_ENV_THEME = "nord-dark"
DRIFT_SAMPLE_ENV_EDITOR = "vim"


# ---------------------------------------------------------------------
# Render Engines Configurations
# ---------------------------------------------------------------------
# Note: 'input_file' fields in render engines are specified as names BEFORE rendering
# (e.g. "mustache.envst.json" rather than "mustache.json"). Because input files frequently contain
# render engine names/suffixes, specifying the pre-rendered source name allows Drift to detect
# dependencies unambiguously and compile inputs in topological DAG order.

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
