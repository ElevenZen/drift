# 📝 drift_workspace.toml Complete Global Configuration Reference

## 📖 Overview
In Drift, the entire workspace is orchestrated by the global `config/drift_workspace.toml` configuration file (with optional local overrides via `config/drift_workspace.local.toml` and programmatic extensions via `config/drift_workspace.py`).

This document provides a comprehensive reference for all global workspace configuration tables and settings, including:
1. **Directory Topology & Defaults (`[workspace]`)**: Relative locations of source templates (`src/`), compilation sandbox (`render/`), deployment database (`install/`), backup archive (`backup/`), global default destination path (`default_target_directory`), default install method (`default_install_method = "stow" | "copy"`), and custom Python workspace hooks (`hook_file`).
2. **Unified 6-Tier Environment Variables (`[env]`)**: Symmetrical workspace variables across 4 sub-tables (`[env.override]`, `[env.secrets]`, `[env.default]`, `[env.fallback]`) evaluated via Kahn's topological sort algorithm with cyclic dependency detection, host fact injection, secret vault interpolation, and cross-section referencing.
3. **Template Rendering Engine DAGs (`[render.<name>]`)**: Multi-level template compilation engines (e.g. `envsubst`, `mustache`, `jinja2`, `var`) with dependency resolution and `.drift/render/` sandboxing.
4. **Behavioral Settings (`[settings]`)**: Global workspace runtime flags including automatic non-interactive environment injection (`PAGER=cat`, `CI=true`) during lifecycle hook runs, and optional Git identity configuration (`git_user_name`, `git_user_email`) for internal auto-commits in `render/` and `install/` repositories.
5. **Active Packages Registry (`[packages.enable]`)**: Declarative enablement and disablement of package folders, supporting explicit keys and fallback `DEFAULT = true | false`.
6. **Dynamic Python Workspace Hooks (`drift_workspace.py`)**: Programmatic preprocessor executed before variable stitching—the best place to dynamically download global configuration or secrets from remote servers and inject them into `[env.default]` / `[env.secrets]`.

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
# Supports home expansion (~) and ${VAR} interpolation from [env.default] / [env.secrets].
default_target_directory = "~"

# Global default installation method if unspecified in drift_package.toml
# Options: "stow" (symlinks) or "copy" (physical copies)
default_install_method = "stow"

# Optional dynamic Python workspace configuration hook file (relative to the 'config/' directory).
# Defaults to "drift_workspace.py" if present.
# hook_file = "drift_workspace.py"


# ---------------------------------------------------------------------
# Workspace Environment Variables, Native Variable Stitching & 6-Tier Precedence
# ---------------------------------------------------------------------
# Drift configurations natively support topological variable self-referencing and stitching ($VAR, ${VAR})
# directly within .toml files. External render engines (e.g. drift_workspace.local.envst.toml) can also
# be used if desired, but native self-referencing is the built-in, zero-dependency default.
#
# Symmetrical Sub-Tables & 6-Tier Precedence Model:
# Both workspace and package configs share 4 symmetrical sub-tables under [env]:
# 1. [env.override]: Tier 2 - Highest-priority configuration variables (CLI at Tier 1 wins).
# 2. [env.secrets]:  Tier 4 - Declarative secret credentials and sensitive tokens.
# 3. [env.default]:  Tier 5 - Standard baseline defaults (recommended default location).
# 4. [env.fallback]: Tier 6 - Low-priority fallbacks applied only when unset across all other tiers.
# (Package > Workspace within each macro tier; System/Package facts reside in Tier 3; CLI at Tier 1).
#
# Variable Stitching & Referencing Rules:
# 1. Topological Stitching in [env.*]: Variables can reference each other (e.g. DRIFT_SAMPLE_SOCKS_PROXY = "...${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}").
#    Drift automatically evaluates dependencies using Kahn's topological sort algorithm with cycle detection.
# 2. External References: You can reference host environment variables (${HOME}, ${USER}), secret vault entries,
#    and auto-populated system facts (${drift_os}, ${drift_arch}, ${drift_distro}, ${drift_hostname}, ${drift_user}).
# 3. Unidirectional Evaluation Flow: ONLY variables defined in [env.*] (and inherited process environment/facts)
#    can be referenced across other drift_workspace.toml sections. Variables outside environment tables cannot be referenced inside environment tables.
# 4. Values-Only Scope: Variable stitching and interpolation occurs STRICTLY within configuration field values
#    (strings, arrays). Variable references are NEVER evaluated in TOML keys, table names, or section headers.
# 5. Escaping: Use a leading backslash (\${VAR} or \$VAR) to prevent interpolation and preserve literal text.

# [env.override]
# DRIFT_SAMPLE_OVERRIDE_FLAG = "workspace_level_override"

# [env.secrets]
# WORKSPACE_SECRET_KEY = "vault-injected-or-declared-secret"

[env.default]
SOCKS_PROXY_HOST = "127.0.0.1"
SOCKS_PROXY_PORT = "1080"
DRIFT_SAMPLE_SOCKS_PROXY = "socks5h://${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}"
DRIFT_SAMPLE_ALL_PROXY = "${DRIFT_SAMPLE_SOCKS_PROXY}"
DRIFT_SAMPLE_ENV_THEME = "nord-dark"
DRIFT_SAMPLE_ENV_EDITOR = "vim"

# [env.fallback]
# DRIFT_SAMPLE_FALLBACK_VAR = "workspace_level_fallback"


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
# Automatically inject non-interactive environment variables (PAGER=cat, CI=true, etc.)
# during lifecycle hook executions. Defaults to true.
# hook_inject_non_interactive_envs = true

# Optional Git identity for render/ and install/ auto-commits.
# Useful on machines without a global Git user or to use a separate identity.
# Applied to render/.git/config and install/.git/config during 'drift repair'.
# git_user_name = "Drift Bot"
# git_user_email = "drift@localhost"


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
> `drift_workspace.py` is the **recommended, canonical place** to fetch global configuration files or secret vaults from remote servers (such as 1Password CLI, HashiCorp Vault, Bitwarden, AWS Secrets Manager, or remote HTTP endpoints) and inject them dynamically into the workspace environment (`[env.default]` or `[env.secrets]`). Because this hook runs as a preprocessor before variable stitching, any values injected into `context.config["env"]["default"]` participate seamlessly in topological DAG resolution and cross-section template interpolation!

### Automatic Discovery or Custom Path
* **Default Path**: Place a `drift_workspace.py` file directly in the `config/` directory (`config/drift_workspace.py`). Drift automatically scaffolds this when running `drift init`.
* **Custom Path**: Explicitly configure `[workspace] hook_file = "my_workspace_hook.py"` (resolved relative to `config/`).

### Execution Model & Pipeline Order
1. **Multi-File Discovery & Merging**: Discovers candidate workspace configuration files (`config/drift_workspace.toml`, `config/drift_workspace.local.toml`, or custom layers) and `.envst.toml` templates, merging them sequentially into a raw configuration dictionary.
2. **Dynamic Python Workspace Hook (Preprocessor)**: Executes `configure_workspace(context)` BEFORE variable stitching with **zero footprint on `os.environ`**. The hook receives the raw merged dictionary and has full in-memory access to resolved host facts (`context.facts`), system facts (`context.os`, `context.arch`, `context.distro`, etc.), active environment snapshot (`context.env`), and discovered package folder names (`context.discovered_packages`). The hook can inject `[env.default]` / `[env.secrets]`, dynamically toggle `[packages.enable]`, or customize default paths.
3. **Variable Stitching & Topological Resolution (Compiler)**: Resolves the `[env.default]` and `[env.secrets]` tables (including any injected by the hook) according to Kahn's topological sort algorithm and variable self-referencing.
4. **Cross-Section Interpolation**: Interpolates `${VAR}` expressions across non-env sections (`default_target_directory`, render engine fields, etc.).
5. **Schema Validation & Model Construction**: Instantiates the strongly-typed `WorkspaceConfig` object.

### `WorkspaceHookContext` Reference
The `context` object passed into `configure_workspace(context)` is an instance of `WorkspaceHookContext` and provides:
* `context.config`: The workspace's raw configuration dictionary (from `drift_workspace.toml` and `.local.toml`).
* `context.drift_root`: Absolute `Path` to the active Drift workspace root directory.
* `context.discovered_packages`: List of all package directory names found under `src/` (`List[str]`).
* `context.env`: Full in-memory environment snapshot including workspace secrets and host facts (`Dict[str, str]`). The hook executes without mutating ambient `os.environ`.
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

    # 1. Fetch remote secrets / global credentials and inject into workspace [env.secrets] or [env.default]
    # token = subprocess.check_output(["op", "read", "op://vault/global/github_token"], text=True).strip()
    env_default = cfg.setdefault("env", {}).setdefault("default", {})
    # env_default["GITHUB_TOKEN"] = token
    if context.os == "darwin":
        env_default["HOMEBREW_PREFIX"] = "/opt/homebrew"

    # 2. Dynamically compute enabled package roster based on host facts
    enable = cfg.setdefault("packages", {}).setdefault("enable", {})
    enable["shell"] = True
    enable["nvim"] = True
    enable["cuda_toolkit"] = (context.os == "linux" and "gpu" in context.hostname)
    enable["desktop_hyprland"] = (context.os == "linux" and "laptop" in context.hostname)
    enable["macos_settings"] = (context.os == "darwin")

    return cfg
```


## ⚙️ Tri-Layered Configuration Merging Hierarchy

Drift evaluates workspace configuration through a strict 3-tier merge pipeline before any workspace operations begin:

### 1. Layer 1: Version-Controlled Baseline (`config/drift_workspace.toml`)
* Committed to Git; provides team-wide or cross-machine baseline defaults.
* Defines global directories, shared render engines, default package toggles, and base environment defaults.

### 2. Layer 2: Machine-Specific Overrides (`config/drift_workspace.local.toml`)
* Uncommitted and automatically git-ignored.
* Recursively deep-merged over `drift_workspace.toml` at startup.
* Ideal for machine-specific path adjustments, localized render settings, or workstation package toggles:
  ```toml
  [workspace]
  default_target_directory = "/custom/host/path"

  [packages.enable]
  heavy_gpu_tools = false
  ```

### 3. Layer 3: Dynamic Python Preprocessor (`config/drift_workspace.py`)
* Executes dynamically *before* variable stitching and cross-section interpolation.
* Ingests the merged dictionary from Layers 1 & 2.
* Can inspect detected system facts (`context.os`, `context.arch`, `context.hostname`) and dynamically compute configuration dictionaries.


## 🔒 Environment Secret Vault (`config/secrets.env`) & 6-Tier Precedence

To prevent credential leakage in dotfiles repositories, Drift strictly separates private credentials from committed configuration files.

### 1. Dotenv Vault Syntax (`config/secrets.env`)
Private tokens and credentials live in `config/secrets.env` (automatically included in `.gitignore`):
```env
# config/secrets.env
GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxx"
NPM_AUTH_TOKEN="npm_secret_token_val"
AWS_PROFILE="production"
```

### 2. Six-Tier Variable Precedence Model
During variable stitching, template rendering, and lifecycle hook execution, variables resolve according to a strict 6-tier precedence order (highest priority wins):

| Tier | Level | Scope & Description |
| :---: | :--- | :--- |
| **Tier 1** | **CLI & Ambient** | Process environment variables and CLI overrides (`INITIAL_ENV` / `os.environ`). |
| **Tier 2** | **Override** | Forced override variables: Package `[env.override]` > Workspace `[env.override]`. |
| **Tier 3** | **Facts** | Authoritative system/package facts: Package facts (`drift_package_*`) > System facts (`drift_*`). |
| **Tier 4** | **Secrets** | Declarative credentials: Package `[env.secrets]` > Workspace `[env.secrets]` > `config/secrets.env`. |
| **Tier 5** | **Default** | Standard baseline defaults: Package `[env.default]` > Workspace `[env.default]`. |
| **Tier 6** | **Fallback** | Soft fallback defaults: Package `[env.fallback]` > Workspace `[env.fallback]`. |

### 3. Clean-Room Transient Execution (`package_envs`)
* When Drift renders package templates or executes lifecycle hooks, it enters an isolated execution context (`package_envs()`).
* Injected secrets and facts are populated in memory.
* Secret values are automatically masked in debug logs as `KEY=****`.
* Upon exiting the scope, Drift completely restores the original host process environment, preventing ambient leakage.


## 🧩 Native Variable Stitching & Kahn's Topological Sort

Drift TOML configurations natively support topological variable self-referencing (`$VAR`, `${VAR}`) across all configuration files without spawning external shell processes:

### 1. Topological Self-Referencing in `[env.*]`
Variables declared in `[env.default]`, `[env.secrets]`, `[env.override]`, and `[env.fallback]` can reference each other, host environment variables, and auto-detected system facts (`$drift_os`, `$drift_arch`, etc.):
```toml
[env.default]
PROXY_HOST = "127.0.0.1"
PROXY_PORT = "8080"
HTTP_PROXY = "http://${PROXY_HOST}:${PROXY_PORT}"
ALL_PROXY = "${HTTP_PROXY}"
```
Drift automatically computes a Directed Acyclic Graph (DAG) using **Kahn's topological sort algorithm**, guaranteeing correct resolution order and raising a `ConfigError` if a cyclic loop is detected (`A -> B -> A`).

### 2. Unidirectional Cross-Section Interpolation
* The `[env]` tables are stitched and resolved first.
* Non-env configuration fields (such as `default_target_directory`, `source_directory`, `render_directory`, and engine `input_file`) can reference any resolved environment variable (e.g. `default_target_directory = "${HOME}/.config"`).
* **Unidirectional Boundary**: Variables defined outside `[env]` cannot be referenced inside `[env]`.

### 3. Values-Only Scope & Escaping
* Variable interpolation applies **strictly to configuration field values** (strings, arrays, and numbers).
* Variable syntax is **never evaluated inside TOML keys, table names, or section headers** (such as `[render.${NAME}]` or `[packages.enable]`).
* To output literal `${VAR}` or `$VAR` without interpolation, escape with a backslash: `\${VAR}` or `\$VAR`.


## 🎨 Template Rendering Engines & Dependency Graph Chain

The `[render.<name>]` section defines how templates in `src/` are compiled into static configuration files in `render/`.

### 1. Engine Configuration Parameters
* **`suffix`**: The template file suffix that triggers this engine (e.g. `suffix = "envst"` matches `*.envst` files).
* **`input_file`**: An optional data or environment file (relative to `config/`) passed into the compiler.
* **`render_command`**: The shell command to execute, where `%i` is replaced by the resolved input file path and `%s` is replaced by the source template path. Use `"internal"` for built-in engines like `[render.var]`.

### 2. Multi-Engine DAG Dependency Chaining
If an engine's `input_file` is itself a template produced by another render engine (for example, `mustache.envst.json` which has the `.envst` suffix of the `envsubst` engine):
1. Drift detects the dependency relationship between engines.
2. Drift compiles the input template first using the prerequisite engine.
3. The compiled artifact in `render/.drift/render/` is then supplied as the input file `%i` to the downstream engine.

### 3. Two-Phase Sandbox Compilation
* **Phase 1 (Workspace Scope)**: Global workspace engines compile workspace input templates into `render/.drift/render/` and compile package configuration templates (e.g. `drift_package.envst.toml` $\rightarrow$ `render/<pkg>/.drift/drift_package.toml`).
* **Phase 2 (Package Scope)**: Package-level engines (defined in `drift_package.toml`) are overlaid onto workspace engines, and package payload files are compiled into `render/<pkg>/`.


## 📦 Package Enablement & Fleet Targeting Rules ([packages.enable])

The `[packages.enable]` table declaratively dictates which packages in `src/` are activated for deployment on the active host machine.

### 1. Explicit Package Toggles
Specify the directory name under `src/` as the key and a boolean as the value:
```toml
[packages.enable]
nvim = true
zsh = true
docker = false
```

### 2. Wildcard `DEFAULT` Policy
The special `DEFAULT` key establishes the fallback behavior for any package folder found in `src/` that is not explicitly enumerated in `[packages.enable]`:
* **Opt-Out Model (`DEFAULT = true`)**: All packages in `src/` are active by default unless explicitly set to `false`.
* **Opt-In Model (`DEFAULT = false`)**: Packages are disabled by default; only packages explicitly set to `true` are deployed.

### 3. Precedence & Fleet Deployment
1. `DEFAULT` baseline provides the workspace policy.
2. Explicit keys in `config/drift_workspace.toml` set team-wide overrides.
3. Machine-specific keys in `config/drift_workspace.local.toml` toggle packages per host.
4. Dynamic logic in `config/drift_workspace.py` computes runtime enablement based on OS, architecture, or hostname.
