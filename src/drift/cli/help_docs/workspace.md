# 📁 drift Workspace & Configuration Overrides

A **drift** workspace is a standard directory structure designed to cleanly, safely, and securely manage your dotfiles using a decoupled, two-stage rendering and staging database.

---

## 🏗️ 1. Workspace Directory Structure

A fully initialized workspace contains the following layout:

```
workspace/
├── .gitignore               # Excludes sandbox, database, and local secrets
├── config/
│   ├── drift.toml           # Shared, committed workspace configuration
│   ├── drift.local.toml     # (Gitignored) Machine-specific config overrides
│   ├── secrets.env          # (Gitignored) Private dotfiles secrets and tokens
│   ├── envsubst.bash        # envsubst static variables initialization script
│   └── ...                  # Other render engine input files
├── src/                     # Source templates directory (with 'dot-' prefixes)
├── render/                  # Sandbox rendering output path (untracked Git repo)
└── install/                 # Target deployment tracking database (untracked Git repo)
```

---

## ⚙️ 2. Dual-Layered Configuration Merging

Drift supports a clean, hierarchical merge model that allows you to standardize packages and settings across all machines, while overriding values locally on specific hosts without polluting version control.

### Layer 1: Primary Configuration (`config/drift.toml`)
Contains repository-wide, version-controlled settings such as render engine definitions, source directory mapping, and default installation registries.

### Layer 2: Machine Overrides (`config/drift.local.toml`)
A local-only, git-ignored override file. If present at startup, drift recursively deep-merges its content over `drift.toml`:
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
