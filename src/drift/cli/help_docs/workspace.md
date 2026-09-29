# 📁 drift Workspace & Configuration Overrides

A **Drift Workspace** is the root control plane and central repository designed to cleanly, 
safely, and securely orchestrate your dotfiles across diverse host environments.

Rather than managing dotfiles as isolated, machine-specific scripts, a Drift workspace provides 
a unified, declarative topology. It combines a decoupled two-stage rendering and state database 
(`render/` and `install/`), multi-engine template compilation, a hierarchical 6-tier environment 
model, and git-ignored secret vaults—allowing you to maintain one single dotfiles repository that 
powers everything from cloud Linux servers and macOS workstations to Windows development boxes.

---

## 🏗️ Workspace Directory Layout & Anatomy

A fully initialized Drift workspace contains the following directory layout:

```
workspace/
├── .gitignore                      # Excludes sandbox, database, and local secrets
├── config/
│   ├── drift_workspace.toml        # Shared, version-controlled workspace configuration
│   ├── drift_workspace.local.toml  # Optional machine-specific overrides (gitignored)
│   ├── drift_workspace.py          # Optional dynamic Python workspace configuration hook
│   ├── secrets.env                 # Private dotfiles secrets and credentials (gitignored)
│   ├── envsubst.bash               # envsubst static variables initialization script
│   └── mustache.envst.json         # Mustache engine input data template
├── src/                            # Declarative source packages (e.g. src/nvim/, src/zsh/)
├── render/                         # Sandbox template compilation zone (isolated Git repo)
├── install/                        # Local state tracking database (isolated Git repo)
└── backup/                         # Automatic rollback and deletion backups
```

* **`config/`**: Global control plane housing workspace configurations, dynamic hooks, engine input files, and private secret vaults.
* **`src/`**: Declarative source packages. Each subdirectory in `src/<pkg>/` is an independent package with its own dotfiles, ignore rules, and settings.
* **`render/`**: Sandbox directory where templates compile under active environment variables. Completely decoupled from host destination paths.
* **`install/`**: Local state tracking database recording deployed file states, checksums, and package manifests.
* **`backup/`**: Safety net directory where Drift archives existing host files before overwriting or pruning them during deployments.
* **`.gitignore`**: Protects ephemeral state, sandbox builds, and private credentials (`render/`, `install/`, `backup/`, `*.local.toml`, `secrets.env`).

---

## 🧭 The Workspace Mental Framework: 10 Core Capabilities

Before exploring granular TOML settings, understanding workspace capabilities provides the architectural mental model:

### 1. ⚙️ Tri-Layered Configuration Merging & Python Hooks
Drift evaluates workspace configuration through a 3-layer merge hierarchy:
*   **Layer 1 (Shared Baseline)**: `config/drift_workspace.toml` provides version-controlled global defaults.
*   **Layer 2 (Machine Overrides)**: `config/drift_workspace.local.toml` applies git-ignored, host-specific overrides (e.g. custom paths, package filters).
*   **Layer 3 (Dynamic Python Hook)**: `config/drift_workspace.py` provides programmatic preprocessor execution with access to host facts and discovered packages.

### 2. 🐍 Dynamic Python Workspace Hook (`config/drift_workspace.py`)
For programmatic fleet management and conditional orchestration:
*   Executes `configure_workspace(context)` dynamically before variable stitching with **zero footprint on `os.environ`**.
*   The recommended, canonical location to securely fetch global configurations or tokens from secret managers (1Password, Vault, Bitwarden) and inject them into `[env.secrets]` or `[env.default]`.
*   Can dynamically toggle `[packages.enable]` based on detected CPU architecture, OS, or hostname.

### 3. 🔒 Environment Secret Vault (`config/secrets.env`) & 6-Tier Precedence
Keep sensitive tokens, passwords, and private emails strictly out of git:
*   **Dotenv Secret Vault**: `config/secrets.env` stores uncommitted key-value pairs (e.g. `GITHUB_TOKEN="ghp_xxx"`).
*   **6-Tier Hierarchy**: Resolves CLI overrides (Tier 1) > Overrides (Tier 2) > System/Package Facts (Tier 3) > Secrets (Tier 4) > Defaults (Tier 5) > Fallbacks (Tier 6).
*   **Transient Clean-Room Isolation (`package_envs`)**: Secrets are loaded in memory, masked in debug logs (`KEY=****`), and completely cleaned up after execution without leaking into parent shells.

### 4. 🧩 Native Variable Stitching & Topological Resolution (`[env]`)
Resolve derived environment variables natively within TOML without external tools:
*   **In-TOML Stitching**: Define derived values like `PROXY_URL = "http://${HOST}:${PORT}"` in `[env.default]`, `[env.secrets]`, or `[env.override]`.
*   **Kahn's DAG Algorithm**: Automatically evaluates inter-variable dependencies and detects cyclic dependency loops.
*   **Unidirectional Cross-Section Referencing**: Resolved variables can be referenced in non-env sections (e.g. `default_target_directory = "${HOME}/.config"`).

### 5. 📦 Workspace-Wide Package Enablement & Fleet Targeting (`[packages.enable]`)
Declaratively control which packages deploy on the active machine:
*   **Explicit Toggles**: Enable or disable specific packages (e.g. `nvim = true`, `cuda = false`).
*   **Default Policy**: Set `DEFAULT = true` (opt-out model) or `DEFAULT = false` (opt-in whitelist).
*   **Local Machine Customization**: Override activation flags in `drift_workspace.local.toml` without touching shared git history.

### 6. 🎨 Multi-Level Template Render Engines (`[render.<name>]`)
Compile dotfile templates using extensible, declarative engines:
*   **Engine DAG Dependencies**: Declare input files (`input_file`) and shell render commands (`render_command`). If an input file is itself a template (e.g. `mustache.envst.json`), Drift compiles it first via topological dependency sorting.
*   **Built-In Engines**: Native zero-dependency variable substitution (`var`), `envsubst`, and external CLI tools (`mustache`, `jinja2`).
*   **Phase 1 Sandboxing**: Compiles workspace-level meta-templates and package configuration templates into `render/.drift/render/`.

### 7. 🗄️ Decoupled 2-Stage Staging & State Tracking Database (`render/` and `install/`)
Eliminate side-effects and deployment surprises:
*   **Stage 1 (Render)**: Source templates compile into `render/<pkg>/` without touching host target files.
*   **Stage 2 (State DB)**: Rendered files stage into `install/<pkg>/` with 1:1 structural fidelity and delta computation.
*   **Inspectable Diff**: Run `drift diff` or `drift status` to inspect exactly what changed before touching a single host file.

### 8. 🛡️ Behavioral Runtime Settings & Non-Interactive Automation (`[settings]`)
Tune workspace-wide execution behavior:
*   **Non-Interactive Execution**: Automatically sets `PAGER=cat`, `CI=true`, and non-interactive flags during lifecycle hooks.
*   **CI/CD Friendly**: Enables automated scripting, testing pipelines, and container dotfile provisioning without interactive prompts blocking execution.

### 9. 💾 Automatic Backup & Rollback Safety (`backup/`)
Protect against data loss during file deployments:
*   When deploying with physical copy mode or unlinking conflicting files, Drift creates atomic backups inside `backup/`.
*   Automatic rollback restores previous file states if a deployment pass or lifecycle hook fails mid-flight.

### 10. 🧹 Garbage Collection & Orphan Detection (`drift gc`)
Maintain a clean, pristine environment over time:
*   Audits the `install/` state database against active packages in `src/`.
*   Cleans up ghost package records and uninstalls packages removed from version control.
*   Prunes orphaned render artifacts and unreferenced temporary files.

---

## 🔄 How the Workspace Orchestrates the Drift Pipeline

The workspace coordinates the four core Drift primitives across all packages:

```
[Host Edits]
    │
    ▼ (Primitive 1: Reverse-Sync / drift adopt)
[Declarative Source: src/]
    │
    ▼ (Primitive 2: Render via [render.<name>] & [env])
[Sandbox Render Zone: render/]
    │
    ▼ (Primitive 4: Stage with dependency DAG ordering)
[Local State Database: install/]
    │
    ▼ (Primitive 5: Apply via relative symlinks or physical copy)
[Active Host System: target directories]
```

1.  **Reverse-Sync (Primitive 1)**: Inspects live host modifications and imports untracked or edited files back into `src/` (`drift adopt`).
2.  **Render (Primitive 2)**: Stitches 6-tier variables and compiles source templates into `render/`.
3.  **Stage (Primitive 4)**: Calculates deltas, orders packages by dependency DAG, and commits changes into `install/`.
4.  **Apply (Primitive 5)**: Projects relative symlinks (`symlink`) or writes physical copies (`copy`) to target host directories with collision checks.

---

## 📚 Where to Go Next

*   **Complete TOML Configuration Reference**: Run `drift help drift_workspace.toml` (or `drift help workspace_config`) for detailed syntax rules, table structures, and complete configuration options.
*   **Package Architecture & Anatomy**: Run `drift help package` to explore package directory layout, 10 core capabilities, and mental framework.
*   **Package TOML Reference**: Run `drift help drift_package.toml` (or `drift help package_config`) for package-level settings, lifecycle hooks, and requirements.
*   **Health Checks & Hooks**: Run `drift help health` to learn about runtime verification probes.
