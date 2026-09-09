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
For complete programmatic control across heterogeneous fleets, you can author a native Python hook (`config/drift_workspace.py` or configured via `[workspace] hook_file = "..."`). The hook executes on-the-fly without external wrapper scripts, providing direct access to detected system facts, discovered packages, and configuration tables:

```python
# config/drift_workspace.py
def configure_workspace(context):
    """Dynamically configure workspace packages and environment on the fly."""
    cfg = context.config
    facts = context.facts
    os_name = facts.get("drift_os", "")
    hostname = facts.get("drift_hostname", "")

    # 1. Dynamically compute enabled package roster
    enable = cfg.setdefault("packages", {}).setdefault("enable", {})
    enable["shell"] = True
    enable["nvim"] = True
    enable["cuda_toolkit"] = (os_name == "linux" and "gpu" in hostname)
    enable["desktop_hyprland"] = (os_name == "linux" and "laptop" in hostname)
    enable["macos_settings"] = (os_name == "darwin")

    # 2. Dynamically inject workspace-level environment variables
    env = cfg.setdefault("env", {})
    if os_name == "darwin":
        env["HOMEBREW_PREFIX"] = "/opt/homebrew"

    return cfg
```

#### Hook Context Attributes (`WorkspaceHookContext`):
*   **`context.config`**: The mutable configuration dictionary merged from `drift_workspace.toml` and `drift_workspace.local.toml`.
*   **`context.drift_root`**: Resolved `Path` to the active Drift workspace root.
*   **`context.facts`**: Accessor dictionary for auto-detected host facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
*   **`context.env`**: Dictionary of all environment variables and host facts.
*   **`context.discovered_packages`**: List of all package directory names found in `src/`.

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

### isolated Compilation Lifecycles
Secrets are handled with maximum security during dotfiles compilation:
1.  **Strict Variable Precedence**:
    *   System Host Environment
    *   Secret Vault (`config/secrets.env`)
    *   Global Workspace Environment (`[env]` table inside TOML)
2.  **Transient Isolation**:
    *   At the start of **Render Package Primitive 2** (before templates rendering begins), Drift parses `secrets.env` and temporarily injects keys into `os.environ`.
    *   It backs up any pre-existing environment variables.
    *   During rendering, template engines (like `envsubst`) compile templates substituting these private variables in the sandboxed `render/` directory.
    *   **Strict Restoration**: Before primitive 2 exits, Drift cleanses `os.environ` and completely restores the original host environment state, ensuring zero leakages to parent shell or child processes.

---

## 🧩 4. Native Variable Stitching & Topological Resolution

Drift natively resolves inter-variable references (`$VAR`, `${VAR}`) across all workspace configuration files without spawning external subprocesses or template binaries:

### 🔄 Topological Self-Referencing in `[env]`
Variables declared in `[env]` can reference each other, host environment variables, and auto-detected system facts (`$drift_os`, `$drift_arch`, etc.):
```toml
[env]
SOCKS_PROXY_HOST = "127.0.0.1"
SOCKS_PROXY_PORT = "1080"
# Stitches variables together dynamically
DRIFT_SAMPLE_SOCKS_PROXY = "socks5h://${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}"
DRIFT_SAMPLE_ALL_PROXY = "${DRIFT_SAMPLE_SOCKS_PROXY}"
```
Drift automatically computes a Directed Acyclic Graph (DAG) using Kahn's topological sort algorithm, guaranteeing correct evaluation order and instantly detecting circular dependency loops (`A -> B -> A`).

### ➡️ Unidirectional Cross-Section Interpolation
*   **Evaluation Order**: The `[env]` table is stitched and evaluated first.
*   **Field Interpolation**: Non-env workspace fields (`default_target_directory`, `source_directory`, `input_file`, etc.) can reference any resolved `[env]` variable (e.g. `default_target_directory = "${HOME}/.config"`).
*   **Unidirectional Boundary**: Variables defined in non-env sections cannot be referenced inside `[env]`.

### 📌 Values-Only Scope
Variable stitching and interpolation apply **strictly to configuration field values** (strings, arrays, and numbers). Variable syntax (`$VAR`, `${VAR}`) is **never evaluated inside TOML keys, table names, or section headers** (such as `[packages.enable]` or `[render.${NAME}]`). To dynamically generate keys or table structures, use the Python workspace hook (`config/drift_workspace.py`) or dynamic meta-templates (`.envst.toml`).

### 🛡️ Literal Escaping
To prevent interpolation and preserve literal text containing `$VAR` or `${VAR}`, prefix with a backslash:
```toml
[env]
SAMPLE_LITERAL = "\\${PRESERVE_ME}"
```
