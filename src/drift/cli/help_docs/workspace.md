# 📁 drift Workspace & Configuration Overrides

A **drift** workspace is a standard directory structure designed to cleanly, safely, and securely manage your dotfiles using a decoupled, two-stage rendering and staging database.

---

## 🏗️ 1. Workspace Directory Structure

A fully initialized workspace contains the following layout:

```
workspace/
├── .gitignore               # Excludes sandbox, database, and local secrets
├── config/
│   ├── drift_workspace.toml       # Shared, committed workspace configuration
│   ├── drift_workspace.local.toml # (Gitignored) Machine-specific config overrides
│   ├── drift_workspace.py         # (Optional) Dynamic Python workspace configuration hook
│   ├── secrets.env          # (Gitignored) Private dotfiles secrets and tokens
│   ├── envsubst.bash        # envsubst static variables initialization script
│   └── ...                  # Other render engine input files
├── src/                     # Source templates directory (with 'dot-' prefixes)
├── render/                  # Sandbox rendering output path (untracked Git repo)
└── install/                 # Target deployment tracking database (untracked Git repo)
```

---

## ⚙️ 2. Tri-Layered Configuration Merging & Python Hooks

Drift supports a clean, hierarchical merge model that allows you to standardize packages and settings across all machines, while overriding values locally on specific hosts without polluting version control.

### Layer 1: Primary Configuration (`config/drift_workspace.toml`)
Contains repository-wide, version-controlled settings such as render engine definitions, source directory mapping, and default installation registries.

### Layer 2: Machine Overrides (`config/drift_workspace.local.toml`)
A local-only, git-ignored override file. If present at startup, drift recursively deep-merges its content over `drift_workspace.toml`:
*   **Path Overrides**: Change the default deployment directory for a specific machine:
    ```toml
    [workspace]
    default_target_directory = "/Users/specific_username"
    ```
*   **Package Selection Overrides**: Enable/disable specific package directories for specific environments:
    ```toml
    [packages.enable]
    gui_apps = false  # Disabled on this headless server
    ```

### Layer 3: Dynamic Python Workspace Hook (`config/drift_workspace.py`)
For complete programmatic control across heterogeneous fleets, you can author a native Python hook (`config/drift_workspace.py` or configured via `[workspace] hook_file = "..."` relative to `config/`). The hook executes on-the-fly without external wrapper scripts, providing direct access to detected system facts, discovered packages, and configuration tables.

> [!TIP]
> **Best Practice — Remote Secrets & Configs Fetching**:
> `drift_workspace.py` is the **recommended place** to download workspace-wide configuration files or secret vaults from remote servers (such as 1Password CLI, HashiCorp Vault, Bitwarden, AWS Secrets Manager, or remote HTTP endpoints) and inject them dynamically into the workspace environment (`[env.secrets]` or `[env.default]`).

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

    # 1. Fetch remote secrets / global credentials and inject into [env.secrets] or [env.default]
    # token = subprocess.check_output(["op", "read", "op://vault/global/github_token"], text=True).strip()
    # env_secrets = cfg.setdefault("env", {}).setdefault("secrets", {})
    # env_secrets["GLOBAL_GITHUB_TOKEN"] = token
    env_default = cfg.setdefault("env", {}).setdefault("default", {})
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

#### Hook Context Attributes (`WorkspaceHookContext`):
*   **`context.config`**: The mutable configuration dictionary merged from `drift_workspace.toml` and `drift_workspace.local.toml`.
*   **`context.drift_root`**: Resolved `Path` to the active Drift workspace root.
*   **`context.env`**: In-memory dictionary snapshot of all environment variables, host facts, and secrets. The hook executes with zero mutation of ambient `os.environ`.
*   **`context.facts`**: Accessor dictionary for auto-detected host facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
*   **`context.discovered_packages`**: List of all package directory names found in `src/`.
*   **Helper properties**: `context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`.

---

## 🔒 3. Environment Secret Vault (`config/secrets.env`)

Public dotfiles repositories present a severe credential-leak hazard. To keep sensitive tokens, API keys, and private emails out of git, Drift isolates them inside a secure, git-ignored Dotenv vault.

### File Format (`config/secrets.env`)
You declare secrets inside `config/secrets.env` using standard shell variable syntax:
```env
# config/secrets.env (Added to .gitignore)
GITHUB_TOKEN="ghp_exampleToken12345"
WORK_EMAIL="jane.doe@company.com"
```

### Ingestion & Isolated Compilation Lifecycles
Secrets are handled with maximum security and performance during workspace and package operations:
1.  **Strict 6-Tier Variable Precedence**:
    *   **Tier 1 (CLI)**: Ambient Process Environment & CLI Variables (`INITIAL_ENV` / `os.environ`)
    *   **Tier 2 (Override)**: Package `[env.override]` > Workspace `[env.override]`
    *   **Tier 3 (Facts)**: Package Facts (`drift_package_*`) > System Facts (`drift_*` protected facts: `drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`, `drift_ip_addresses`)
    *   **Tier 4 (Secrets)**: Package `[env.secrets]` > Workspace `[env.secrets]` > `config/secrets.env`
    *   **Tier 5 (Default)**: Package `[env.default]` > Workspace `[env.default]`
    *   **Tier 6 (Fallback)**: Package `[env.fallback]` > Workspace `[env.fallback]`
2.  **Topological Self-Referencing in `[env.secrets]`**:
    *   Both workspace and package configurations support a dedicated `[env.secrets]` table (Tier 4).
    *   Variables in `[env.secrets]` can reference each other, lower-tier variables, system facts, and host environment variables.
    *   **Rendered Sandbox Metadata**: Fully stitched metadata (including resolved `[env.secrets]`) is stored in `render/<pkg>/.drift/drift_package.toml` and mirrored to `install/<pkg>/`. Because both directories are git-ignored by default, downstream lifecycle hooks (`post_install`, `health`) have seamless access to all environment tiers without re-parsing source files.
3.  **Single Ingestion & Explicit Workspace Cache**:
    *   `config/secrets.env` and workspace `[env.secrets]` are parsed during workspace loading and stored in memory on `WorkspaceConfig.env.secrets`.
    *   Downstream Python hooks access secrets via `context.env` in $O(1)$ memory without repeated disk I/O.
4.  **Transient Clean-Room Isolation (`package_envs`) & Log Masking**:
    *   During package rendering and lifecycle hook execution, Drift enters `pkg_config.package_envs()`.
    *   Secret values are automatically masked in debug logs as `KEY=****`.
    *   **Strict Restoration**: Upon exiting the scope, Drift completely restores the original host environment state, ensuring zero credential leakages to parent shells or unrelated processes.

---

## 🧩 4. Native Variable Stitching & Topological Resolution

Drift natively resolves inter-variable references (`$VAR`, `${VAR}`) across all workspace configuration files without spawning external subprocesses or template binaries:

### 🔄 Topological Self-Referencing in `[env.default]` and `[env.secrets]`
Variables declared in `[env.default]` and `[env.secrets]` can reference each other, host environment variables, and auto-detected system facts (`$drift_os`, `$drift_arch`, etc.):
```toml
[env.default]
SOCKS_PROXY_HOST = "127.0.0.1"
SOCKS_PROXY_PORT = "1080"
# Stitches variables together dynamically
DRIFT_SAMPLE_SOCKS_PROXY = "socks5h://${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}"
DRIFT_SAMPLE_ALL_PROXY = "${DRIFT_SAMPLE_SOCKS_PROXY}"
```
Drift automatically computes a Directed Acyclic Graph (DAG) using Kahn's topological sort algorithm, guaranteeing correct evaluation order and instantly detecting circular dependency loops (`A -> B -> A`).

### ➡️ Unidirectional Cross-Section Interpolation
*   **Evaluation Order**: The `[env.default]` and `[env.secrets]` tables are stitched and evaluated first.
*   **Field Interpolation**: Non-env workspace fields (`default_target_directory`, `source_directory`, `input_file`, etc.) can reference any resolved `[env.default]` or `[env.secrets]` variable (e.g. `default_target_directory = "${HOME}/.config"`).
*   **Unidirectional Boundary**: Variables defined in non-env sections cannot be referenced inside `[env.default]` or `[env.secrets]`.

### 📌 Values-Only Scope
Variable stitching and interpolation apply **strictly to configuration field values** (strings, arrays, and numbers). Variable syntax (`$VAR`, `${VAR}`) is **never evaluated inside TOML keys, table names, or section headers** (such as `[packages.enable]` or `[render.${NAME}]`). To dynamically generate keys or table structures, use the Python workspace hook (`config/drift_workspace.py`) or dynamic meta-templates (`.envst.toml`).

### 🛡️ Literal Escaping
To prevent interpolation and preserve literal text containing `$VAR` or `${VAR}`, prefix with a backslash:
```toml
[env.default]
SAMPLE_LITERAL = "\\${PRESERVE_ME}"
```
