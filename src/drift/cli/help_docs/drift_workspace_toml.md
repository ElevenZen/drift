# 📝 drift_workspace.toml Complete Global Configuration Reference

## 📖 Overview
In Drift, the entire workspace is orchestrated by the global `config/drift_workspace.toml` configuration file (with optional local overrides via `config/drift_workspace.local.toml` and programmatic extensions via `config/drift_workspace.py`).

This document provides a comprehensive reference for all global workspace configuration tables and settings, including:
1. **Directory Topology & Defaults (`[workspace]`)**: Relative locations of source templates (`src/`), compilation sandbox (`render/`), deployment database (`install/`), backup archive (`backup/`), global default destination path (`default_target_directory`), default install method (`default_install_method = "stow" | "copy"`), and custom Python workspace hooks (`hook_file`).
2. **Topological Environment Variables (`[env]`)**: Global variables evaluated via Kahn's topological sort algorithm with cyclic dependency detection, host fact injection, secret vault interpolation, and cross-section referencing.
3. **Template Rendering Engine DAGs (`[render.<name>]`)**: Multi-level template compilation engines (e.g. `envsubst`, `mustache`, `jinja2`, `var`) with dependency resolution and `.drift/render/` sandboxing.
4. **Behavioral Settings (`[settings]`)**: Global workspace runtime flags including WAN IP probing and automatic non-interactive environment injection (`PAGER=cat`, `CI=true`) during lifecycle hook runs.
5. **Active Packages Registry (`[packages.enable]`)**: Declarative enablement and disablement of package folders, supporting explicit keys and fallback `DEFAULT = true | false`.
6. **Dynamic Python Workspace Hooks (`drift_workspace.py`)**: Programmatic preprocessor executed before variable stitching—the best place to dynamically download global configuration or secrets from remote servers and inject them into `[env]`.

---

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
# Defaults to "drift_workspace.py" if present.
# hook_file = "drift_workspace.py"


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

# Automatically inject non-interactive environment variables (PAGER=cat, CI=true, etc.)
# during lifecycle hook executions. Defaults to true.
# hook_inject_non_interactive_envs = true


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


## 🐍 Dynamic Python Workspace Hooks (`drift_workspace.py`)

For programmatic workspace configuration and fleet management across heterogeneous machines, Drift supports **dynamic Python workspace hooks**.

> [!TIP]
> **Best Practice — Remote Secrets & Configs Fetching**:
> `drift_workspace.py` is the **recommended, canonical place** to fetch global configuration files or secret vaults from remote servers (such as 1Password CLI, HashiCorp Vault, Bitwarden, AWS Secrets Manager, or remote HTTP endpoints) and inject them dynamically into the workspace environment (`[env]`). Because this hook runs as a preprocessor before variable stitching, any values injected into `context.config["env"]` participate seamlessly in topological DAG resolution and cross-section template interpolation!

### Automatic Discovery or Custom Path
* **Default Path**: Place a `drift_workspace.py` file directly in the `config/` directory (`config/drift_workspace.py`). Drift automatically scaffolds this when running `drift init`.
* **Custom Path**: Explicitly configure `[workspace] hook_file = "my_workspace_hook.py"` (resolved relative to `config/`).

### Execution Model & Pipeline Order
1. **Multi-File Discovery & Merging**: Discovers candidate workspace configuration files (`config/drift_workspace.toml`, `config/drift_workspace.local.toml`, or custom layers) and `.envst.toml` templates, merging them sequentially into a raw configuration dictionary.
2. **Dynamic Python Workspace Hook (Preprocessor)**: Executes `configure_workspace(context)` BEFORE variable stitching. The hook receives the raw merged dictionary and has full access to resolved host facts (`context.facts`), system facts (`context.os`, `context.arch`, `context.distro`, etc.), active environment (`context.env`), and discovered package folder names (`context.discovered_packages`). The hook can inject `[env]`, dynamically toggle `[packages.enable]`, or customize default paths.
3. **Variable Stitching & Topological Resolution (Compiler)**: Resolves the `[env]` table (including any injected by the hook) according to Kahn's topological sort algorithm and variable self-referencing.
4. **Cross-Section Interpolation**: Interpolates `${VAR}` expressions across non-env sections (`default_target_directory`, render engine fields, etc.).
5. **Schema Validation & Model Construction**: Instantiates the strongly-typed `WorkspaceConfig` object.

### `WorkspaceHookContext` Reference
The `context` object passed into `configure_workspace(context)` is an instance of `WorkspaceHookContext` and provides:
* `context.config`: The workspace's raw configuration dictionary (from `drift_workspace.toml` and `.local.toml`).
* `context.drift_root`: Absolute `Path` to the active Drift workspace root directory.
* `context.discovered_packages`: List of all package directory names found under `src/` (`List[str]`).
* `context.env`: Full host environment snapshot (`Dict[str, str]`).
* `context.facts`: Detected system facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
* Helper properties: `context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`.

### Example `drift_workspace.py`
```python
# config/drift_workspace.py
from __future__ import annotations
import subprocess
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from drift.hooks import WorkspaceHookContext


def configure_workspace(context: WorkspaceHookContext) -> Dict[str, Any]:
    """Dynamically configure workspace packages, remote secrets, and environment on the fly."""
    cfg = context.config

    # 1. Fetch remote secrets / global credentials and inject into workspace [env]
    # token = subprocess.check_output(["op", "read", "op://vault/global/github_token"], text=True).strip()
    env = cfg.setdefault("env", {})
    # env["GITHUB_TOKEN"] = token
    if context.os == "darwin":
        env["HOMEBREW_PREFIX"] = "/opt/homebrew"

    # 2. Dynamically compute enabled package roster based on host facts
    enable = cfg.setdefault("packages", {}).setdefault("enable", {})
    enable["shell"] = True
    enable["nvim"] = True
    enable["cuda_toolkit"] = (context.os == "linux" and "gpu" in context.hostname)
    enable["desktop_hyprland"] = (context.os == "linux" and "laptop" in context.hostname)
    enable["macos_settings"] = (context.os == "darwin")

    return cfg
```
