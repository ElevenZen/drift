# "drift": Decoupled Two-Stage Git-Backed Dotfiles Manager
## Product Design & Interactive CLI Specification

---

## 1. Introduction & Product Vision

**"drift"** is a modern, developer-centric dotfiles manager written in Python. It is designed to solve the age-old conflict of dotfiles management: **how to maintain a pristine, declarative template-based configuration source while gracefully embracing, auditing, and synchronizing runtime system/GUI changes (drifts).**

Unlike traditional one-stage dotfiles managers (like GNU Stow, Chezmoi, or Dotbot) which either blindly overwrite local changes or hide system state in obscure binary databases, **drift** relies on a **Decoupled Two-Stage Git-Backed local database architecture**. By turning both the sandbox rendering zone (`render/`) and the applied configuration state (`install/`) into fully operational, local-only Git repositories, drift grants developers absolute visibility, safety, and bidirectional synchronization.

### The Core Philosophies of drift:
1. **Embrace the Drift**: Systems change. GUI tools write runtime adjustments, themes update, and hot-edits happen. Instead of fighting them, drift treats system drift as uncommitted Git edits in the local state database.
2. **Double Git-Database Integrity**: By leveraging standard Git indices inside `render/` and `install/`, drift delegates version control, differential tracking, and rollbacks to Git itself, keeping the CLI engine incredibly lightweight, auditable, and transparent.
3. **Sandbox Isolation**: Template compiling and rendering never touch active host paths directly. Edits are isolated inside a clean `render/` repository before staging.
4. **Structured Machine-Readable Output**: All commands support `--json` outputting deterministic, strongly-typed result dataclasses, enabling seamless automation, CI/CD verification, and agentic orchestration.

---

## 2. Competitive Edge & Market Positioning

Comparing **drift** to popular dotfiles managers listed on `dotfiles.github.io/utilities`:

| Feature | **drift** (Python) | **Chezmoi** (Go) | **Dotbot** (Python) | **GNU Stow** (Perl) | **VCSH** (Shell) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Engine** | **Dual Local Git Repos** | Single Repo + BoltDB | None (YAML links) | Symlink Farm | Bare Git on `$HOME` |
| **Pipeline Stages** | **2-Stage (Render $\rightarrow$ DB $\rightarrow$ System)** | 1-Stage (Compile $\rightarrow$ System) | 1-Stage (Link) | 1-Stage (Link) | 1-Stage (Direct Git) |
| **Active Drift Audit**| **Yes (Automatic Reverse Sync)** | Yes (Manual `re-add/merge`)| No | No | Rely on Git status |
| **Dry-Run Fidelity** | **Absolute (Diff Δ comparison)** | Dry-run on templates | No | Stow `-n` simulation | No |
| **Mid-Fail Rollback** | **Yes (Dedicated Function)** | Manual cleanup | No | Stow `-D` unlink | No |
| **Machine Output** | **Yes (`--json` across all commands)** | Partial JSON | No | No | No |

### Why drift beats the alternatives:
* **Over Chezmoi**: Chezmoi hides state in an opaque BoltDB binary database. Under drift, your `install/` state database is a **pure Git repository**. You can walk into `install/`, run `git log`, `git checkout`, or hook up git GUI clients (like Lazygit or GitKraken) to review your deployment history.
* **Over Dotbot/Stow**: They are strictly one-way bootstrap scripts. They do not comprehend drift, leaving you susceptible to silently lost runtime configurations.

---

## 3. High-Level Architectural Pipeline

```
                     [ 1. DECLARATIVE SOURCE ]
                     src/ (Templates & drift.toml)
                                 │
                                 ▼ (drift render / Stage 2 sandbox compiler)
                    [ 2. SANDBOX RENDER ZONE ]
                      render/ (Git repo tracking template compile history)
                                 │
                        Diff A   ▼ (Stage 2 incremental sync)
                        (Dry)   [ 3. LOCAL STATE DATABASE ]
                      install/ (Git repo tracking live configuration state)
                                 ▲
                        Diff B   │ (Stage 1 System -> install/ reverse-sync)
                        (Live)    ▼ (Symlinks or Physical Copy)
                     [ 4. SYSTEM ACTIVE CONFIGS ]
                        ~/* or /etc/* (Active system configuration files)
```

---

## 4. User-Facing CLI Command Design

drift exposes a rich, intuitive, and colored CLI interface powered by Python's `Typer` / `rich` with fallback support for standard library `argparse`.

```bash
drift [--global-flags] [command] [package] [--command-flags]
```

### Global Flags:
*   `-C, --directory <DIR>`: Run as if drift was started in `<DIR>` instead of the current working directory.
*   `--no-git-root`: Bypass searching for the parent repository git root; treat the current or `-C` directory as the literal workspace root.
*   `-v, --verbose`: Enable verbose (DEBUG) logging output.
*   `--json`: Output results in structured machine-readable JSON format using typed result models.

---

### A. Initialization: `drift init [--force] [--secrets/--no-secrets] [--json]`
Initializes the active repository as a drift workspace.  
Only works if the directory is empty or tracked by Git.  
*   **Command Signature**: `drift init [--force / -f] [--secrets / --no-secrets] [--no-git-root] [--json]`
*   **Actions**:
    1.  If the directory is empty and not tracked by Git, initializes an empty Git repository.
    2.  Verifies the main repository is tracked by Git (unless `--no-git-root` is active).
    3.  Creates `.gitignore` entries to isolate `render/`, `install/`, and `config/secrets.env` files.
    4.  Initializes `render/` and `install/` as independent, untracked local Git repositories.
    5.  Creates default directory structure (`src/`, `config/drift.toml`, `config/envsubst.bash`, `config/mustache.envst.json`, `install/state.toml`).
    6.  Generates a scaffold `config/secrets.env` file (enabled by default; disable with `--no-secrets`).
*   **Terminal Output**:
    ```bash
    ✨ Initialized drift workspace!
    📁 Created render/ sandbox Git database.
    📁 Created install/ local state Git database.
    📝 Generated drift.toml template.
    📝 Generated config/envsubst.bash and config/mustache.envst.json.
    🔒 Initialized config/secrets.env template.
    ```

---

### B. Package Creation: `drift new <package> [--force] [--target <dir>] [--method <stow|copy>] [--json]`
#### **Common Usage**
Create a new package directory with the default `drift_package.toml` configuration file:
```bash
$ drift new nvim --target ~/.config/nvim --method copy
✨ Package 'nvim' created successfully!
📝 Generated drift_package.toml at src/nvim/drift_package.toml.
```

#### **Details & Deep Probing Logic**
*   **Command Signature**: `drift new <package> [--force / -f] [--target / -t <target_directory>] [--method / -m <install_method>] [--json]`
*   **Optional Arguments & Flags**:
    - `--force / -f`: Forcefully overwrites any existing config file inside the package.
    - `--target / -t <target_directory>`: Explicitly configures the deployment target directory field inside the generated `drift_package.toml`. Defaults to `default_target_directory` in `drift.toml`.
    - `--method / -m <install_method>`: Explicitly configures the deployment installation method (`stow` or `copy`) field inside the generated `drift_package.toml`. Defaults to `default_install_method` in `drift.toml`.
    - `--json`: Outputs a `NewPackageResult` object in JSON format.
*   **Probing Guard**:
    - The CLI first checks if *any* configuration file already exists inside the package directory `src/<package>/` (including `drift_package.toml` or engine templates like `drift_package.envst.toml`).
    - If a configuration file exists without `--force`, the command halts and prevents accidental overwrite.

#### **Machine-Specific Override Layering (`*.local.toml`)**:
To support machine-specific configuration overrides (e.g. unique target home directories per machine), Drift supports a layered TOML system:
- **Global Layer**: `config/drift.local.toml` automatically overrides properties in global `config/drift.toml`.
- **Package Layer**: `src/<pkg>/package.local.toml` (or `drift_package.local.toml`) automatically overrides properties in `src/<pkg>/drift_package.toml`.
- **Git Isolation**: Files matching `*.local.toml` are gitignored by default. During rendering, Drift merges local TOML overrides on top of base configurations.

---

### C. Resource Import: `drift add <package> <paths...> [--dry-run] [--no-hooks] [--json]`
#### **Common Usage**
Import physical active system configuration files or directories (like `~/.config/nvim/init.lua`) into your declarative source folder:
```bash
$ drift add nvim ~/.config/nvim/init.lua
🚀 Imported ~/.config/nvim/init.lua into nvim package!
📁 Copied contents to src/nvim/dot-config/nvim/init.lua (Translated dot-prefix).
```

#### **Details & Logic**
*   **Command Signature**: `drift add <package> <paths...> [--dry-run] [--no-hooks / --no-hook] [--json]`
*   **Dry-Run Mode**: Passing `--dry-run` performs all symlink resolutions, prefix translations, and path calculations, previewing changes without touching disk.
*   **Bypass Lifecycle Hooks**: Passing `--no-hooks` (or `--no-hook`) bypasses execution of the `pre_source` hook.
*   **Dot-Prefix Translation (Symmetric Symmetry)**:
    - Files and directories starting with `.` are automatically translated to `dot-` prefixes (e.g., `.config/nvim/init.lua` $\rightarrow$ `dot-config/nvim/init.lua`).
*   **Symlink Resolution Policy**:
    - If an input path is a symlink pointing outside the state database, Drift resolves the target and copies the physical contents for reproducibility across machines.

---

### D. Status Inspection: `drift status [packages...] [--json]`
Analyzes and aggregates current system alignment across three vectors:
*   **Template Status [A]**: Did template files under `src/` evolve compared to `render/`?
*   **System Drift Status [B]**: Has the host system drifted from `install/` due to runtime edits?
*   **Pending Delta [Δ]**: Are there differences waiting to be deployed from `render/` to `install/`?

#### Terminal Visual Representation:
```bash
$ drift status

Package: nvim
  [A] Template: MODIFIED
      M nvim/init.lua
  [B] System:   CLEAN
  [Δ] Pending:  STAGED
      (+1, ~1, -0 files)

Package: qbittorrent
  [A] Template: CLEAN
  [B] System:   DRIFTED
      M qbittorrent/qBittorrent.conf
  [Δ] Pending:  CLEAN
```

When run with `--json`, returns a structured `StatusResult` with `overall_status` (`CLEAN`, `DRIFTED`, `PENDING`, `MODIFIED`) and per-package summaries.

---

### E. Change Visualization: `drift diff [packages...] [options] [--json]`
Provides deep comparisons between configuration layers:

*   **`drift diff [packages...]` (Default: Pending Delta / Diff Δ)**:
    Compares what is waiting in `render/` against what is recorded in `install/`.
*   **`drift diff [packages...] --template` (or `-t`, Diff A)**:
    Visualizes Template Evolution (changes between `src/` and `render/`).
*   **`drift diff [packages...] --system` (or `-s`, Diff B)**:
    Visualizes Active System Drift (what host tools/GUIs have changed).
*   **`drift diff [packages...] --stat`**:
    Shows a concise diffstat summary of file change counts.
*   **`drift diff [packages...] --side-by-side` (or `-y`)**:
    Displays side-by-side vertical terminal diff comparisons.
*   **`--json`**:
    Returns a typed `DiffResult` containing per-package added, modified, and deleted files.

---

### F. Safe Deployment: `drift deploy [packages...] [--force] [--no-hooks] [--json]`
Deploys configurations using an atomic two-stage compilation and application engine.

#### Stage 0: Pre-flight Checks
1.  **Git Committability Check**: Verifies that Git `user.name` and `user.email` are valid on both `render/` and `install/` repositories.
2.  **Discovered Packages**: Determines active target packages.

#### Stage 1: Safety Guard (Sentinel)
1.  Triggers a silent **Reverse Sync** (Primitive 1) pulling current host configuration state into `install/`.
2.  Inspects the `install/` Git tree for changes:
    *   *If changes exist (Drift detected)*: **Aborts immediately** (exit code `3`). Displays active system drift and guides the developer:
        ```bash
        ❌ [DEPLOY ABORTED] System drift detected in package 'qbittorrent'!
        Host configurations have drifted from the state database.
        
        👉 Run 'drift diff -s qbittorrent' to view active system modifications.
        👉 Run 'drift adopt qbittorrent' to incorporate these modifications into your template.
        ```
    *   In `--json` mode, returns `DeployResult(status="ABORTED_DRIFT")` with `DeployFailure(next_action_type="adopt_or_force", requires_rollback=false)`.

#### Stage 2: Sequential Compile & Apply
If no drift is detected (or `--force` is supplied):
1.  **Render**: Sandbox-compiles `src/` templates into `render/` (`Primitive 2`).
2.  **Commit Render**: Automatically commits compiled sandbox history (`Primitive 3`).
3.  **Stage Render to Install**: Staged delta-sync from `render/` to `install/` (`Primitive 4`).
4.  **Install Deployment**: Delivers files via atomic stow/copy with collision checking (`Primitive 5`).
5.  **Commit Install**: Scope-commits deployed configurations in `install/` (`Primitive 6`).

*Pass `--no-hooks` (or `--no-hook`) to completely bypass executing all lifecycle hooks across rendering, deployment, and post-deploy garbage collection.*

#### Stage 3: Post-deploy Garbage Collection
*   *Global Deploy*: When deploying without specific package names, Drift automatically runs `drift gc` (`Primitive 9`) to clean orphan packages and purge zombie directories.

#### 🚨 Midway Fail-Fast Guard & Recovery:
If physical installation fails midway, Drift halts execution immediately and prints an emergency recovery card:
```bash
💥 [CRITICAL FAILURE] deployment failed during Step 4 (Physical Deploy/Install)!
================================================================================
                            EMERGENCY RECOVERY REQUIRED                          
================================================================================
The deployment has failed midway, leaving your host system in an inconsistent 
and half-written state.

👉 Please fix the error above and run: 'drift rollback nvim'
================================================================================
```
In `--json` mode, returns `DeployFailure(next_action_type="rollback", requires_rollback=true)`.

---

### G. Recovery: `drift rollback [packages...] [--force] [--no-hooks] [--json]`
*   **Mechanism (Primitive 8)**:
    1.  Resets `install/` database for target packages to the last clean HEAD commit.
    2.  Resets `install/state.toml` back to HEAD.
    3.  Performs a **Full Redeploy** (`force=True`) to restore physical target system files.
    4.  Restores state registry entries back to `"installed"`.
*   **Operational Protection**: If no package is in an inconsistent state (`staging` or `deploying`), `drift rollback` aborts to prevent unintended file overwrites. Pass `--force` to bypass this check.
*   **Bypass Lifecycle Hooks**: Pass `--no-hooks` (or `--no-hook`) to skip lifecycle hooks during rollback redeployment.

---

### H. Synchronization & Bidirectional Drift Adoption: `drift adopt [packages...] [--interactive] [--accept-conflicts] [--force] [--dry-run] [--no-hooks] [--json]`
Incorporate runtime system/GUI changes back into your declarative templates under `src/`.

#### Command Options:
*   `packages...`: Optional package name(s) to adopt. If omitted, all drifted packages are adopted.
*   `--interactive / -i`: Interactively prompt for each modified, added, or deleted file.
*   `--accept-conflicts`: Apply conflicting patches, writing merge conflict markers directly into templates.
*   `--force / -f`: Force adoption even if the package source directory has uncommitted modifications.
*   `--dry-run`: Simulate adoption without writing changes to disk.
*   `--no-hooks / --no-hook`: Bypass execution of `pre_source` lifecycle hooks during adoption.
*   `--json`: Return structured `AdoptResult`.

#### Interactive Options:
*   **File Additions (FCD)**: [1] Adopt into source, [2] Ignore (append to `.drift_ignore`), [3] Discard, [4] Skip.
*   **File Deletions**: [1] Adopt deletion, [2] Discard deletion / Restore, [3] Skip.
*   **File Modifications**:
    - **Symmetric Patch Application**: Extracts unified patch from `install/` and programmatically applies it to template files in `src/`.
    - **Conflict Fallback**: [1] Over-render & Freeze, [2] Open Merge Conflict Editor (`$EDITOR`), [3] Open Side-by-Side Reference, [4] Discard / Restore, [5] Skip.

---

### I. Uninstallation & Detachment: `drift uninstall <packages...> [--force] [--detach] [--dry-run] [--no-hooks] [--json]`
*   **Command Signature**: `drift uninstall <packages...> [--force / -f] [--detach] [--dry-run] [--no-hooks / --no-hook] [--json]`
*   **Standard Mode (Default)**:
    1.  Removes active symlinks (stow) or deletes deployed files (copy).
    2.  Restores collision backups from `backup/<package>/overwritten/`.
    3.  Deletes `install/<package>/` and commits the uninstallation to the state database.
*   **Detach / Eject Mode (`--detach`)**:
    1.  Decouples the package from Drift while leaving physical configuration files intact on host.
    2.  Converts symlinks to physical files in place.
    3.  Leaves backups untouched in `backup/`.
    4.  Deletes `install/<package>/` and commits with a `Detach:` prefix.
*   **Bypass Lifecycle Hooks**: Pass `--no-hooks` (or `--no-hook`) to skip execution of `pre_uninstall` and `post_uninstall` hooks.

---

### J. Garbage Collection: `drift gc [--dry-run] [--no-hooks] [--json]`
Identifies and purges orphaned and untracked database entities across the workspace.
*   **Command Signature**: `drift gc [--dry-run] [--no-hooks / --no-hook] [--json]`
*   **Actions (Primitive 9)**:
    1.  **Orphan Packages**: Uninstalls packages present in `install/` state database but disabled in `drift.toml` (skipping uninstall hooks if `--no-hooks` is provided).
    2.  **Zombie Folders**: Identifies and removes package subdirectories in `render/` and `install/` that lack valid configuration files.
    3.  **Database Commits**: Automatically commits purged zombie cleanups in both repositories.

---

### K. Health Audit & Workspace Repair: `drift repair [--dry-run] [--json]`
Audits and repairs damaged, missing, or partially-initialized workspace components.
*   **Command Signature**: `drift repair [--dry-run] [--json]`
*   **Checks and Self-Healing Actions**:
    1.  **Directory Structure**: Recreates missing `src/`, `render/`, `install/`, `backup/`, and `config/` directories.
    2.  **Git Database Repositories**: Re-initializes missing or broken `.git` repositories in `render/` and `install/`.
    3.  **Git Configuration**: Verifies and repairs `user.name` and `user.email` in sub-repositories.
    4.  **Configuration Templates**: Regenerates missing `drift.toml`, `envsubst.bash`, and `mustache.envst.json`.
    5.  **State Registry**: Recreates missing or malformed `install/state.toml`.
    6.  **Secrets Scaffold**: Initializes missing `config/secrets.env` template.
    7.  **Gitignore Rules**: Verifies and restores required `.gitignore` entries.

---

### L. Workspace Cloning & Bootstrapping: `drift clone <repository> [destination] [--branch] [--depth] [--json]`
Clones a remote or local Git repository and immediately reconstructs and heals the workspace for deployment in a single operation.
*   **Command Signature**: `drift clone <repository> [destination] [--branch / -b <branch>] [--depth <depth>] [--no-repair] [--json]`
*   **Workflow**:
    1.  **Repository Clone**: Clones the remote repository using `git clone` with optional branch and shallow `--depth` settings.
    2.  **Autonomous Bootstrap Healing**:
        - **Existing Drift Workspace**: Reconstructs untracked runtime state databases (`render/` and `install/` Git repos, `install/state.toml`, `.gitignore`, `config/drift.local.toml`, `config/secrets.env`).
        - **Plain / Legacy Dotfiles Repository**: Automatically migrates root dotfiles into a package folder (`src/<pkg>/`), initializes `drift_package.toml` with `stow` install method, generates `.drift_ignore`, and enables the package in `config/drift.toml`.
    3.  **Actionable Post-Clone Guidance**: Outputs next steps for configuring machine-specific overrides and deploying.

---

### M. Package Runtime Health Probing: `drift health [packages...] [--timeout] [--json]`
Executes runtime health check probes on installed packages to verify if deployed services, daemons, terminal environments, and host tools are operating properly on the host machine.
*   **Command Signature**: `drift health [packages...] [--timeout <seconds>] [--json]`
*   **Configuration (`drift_package.toml`)**:
    ```toml
    [package]
    install_method = "stow"
    target_directory = "~/.config/tmux"

    [hooks]
    health = "scripts/health_check.sh"
    timeout = 15
    ```
*   **Execution Invariants**:
    1.  **Probe Source**: Script is executed from the installed package directory in `install/<pkg>/`.
    2.  **Working Directory (CWD)**: Runs with the package's **host target directory** as the active working directory (`cwd = target_directory`).
    3.  **Environment Injection**: Standard Drift variables (`$drift_package_name`, `$drift_package_target_dir`, `$drift_install_method`, etc.) are automatically injected.
    4.  **Sudo Privileges**: Executes with `sudo` elevation if `sudo = true` is configured for the package.
    5.  **Exit Code Evaluation**: `0` = Healthy, non-zero = Unhealthy.

---

### N. Mini User Manual: `drift help [topic]`
Provides built-in documentation with automatic terminal pager fallback.
*   **Syntax**: `drift help [topic]`
*   **Available Topics**:
    - *(no topic)*: Architectural loop diagram, command map, and data-flow model.
    - `overall`: High-level system overview and architectural diagrams.
    - `package`: Package structure, directory layouts, and configuration options.
    - `src`: Declarative source rules and `dot-` prefix translation.
    - `render`: Sandbox compilation, isolated rendering, and DAG pipelines.
    - `install`: State database mechanics, delta calculations, and collision guards.
    - `fcd`: Fully-Controlled Directories, wild file tracking, and promotion.
    - `ignore`: PCRE patterns in `.drift_ignore` and ignore precedence.
    - `drift_package.toml`: Full reference for package configuration.
    - `drift.toml`: Full reference for workspace settings.
    - `workspace`: Multi-environment workflows and local overrides.
    - `health`: Package runtime health check probes and configurations.
    - `clone`: Workspace cloning, bootstrap healing, and legacy migration.
    - `faq`: Frequently asked questions and troubleshooting guides.

---

### O. Low-Level Control Commands
For advanced continuous integration, scripting, and automation:

1.  **`drift render [packages...] [--no-hooks] [--json]`**: Compiles source templates to sandbox `render/` (`Primitive 2`).
2.  **`drift render-commit [packages...] -m "message" [--json]`**: Stages and commits compiled sandbox changes (`Primitive 3`).
3.  **`drift reverse-sync [packages...] [--json]`**: Pulls live host configuration changes into `install/` (`Primitive 1`).
4.  **`drift stage [packages...] [--force] [--json]`**: Computes delta and stages sandbox to `install/` (`Primitive 4`).
5.  **`drift apply [packages...] [--force] [--resolve-symlinks/--no-resolve-symlinks] [--no-hooks] [--json]`**: Deploys `install/` state to host paths (`Primitive 5`).
6.  **`drift install-commit [packages...] -m "message" [--json]`**: Commits deployed configurations inside `install/` (`Primitive 6`).
7.  **`drift hook <package> <hook-name> [--json]`**: Directly executes a specific lifecycle hook script for a single package.
    *   `pre_source`: Loaded from package source (`src/<package>`) and executed with `cwd = src/<package>`.
    *   `post_render`: Loaded from compiled sandbox (`render/<package>`) and executed with `cwd = render/<package>`.
    *   `pre_install`, `pre_update`, `pre_uninstall`: Loaded from state database (`install/<package>`) and executed with `cwd = install/<package>`.
    *   `post_install`, `post_update`, `post_uninstall`, `health`: Loaded from state database (`install/<package>`) and executed with `cwd = target_directory`.
    *   Automatically injects package environment variables (`$drift_package_name`, `$drift_package_target_dir`, etc.) and honors `sudo = true` for eligible hooks.

---

## 5. Global Configuration: `drift.toml`

```toml
# =====================================================================
# drift.toml Configuration
# =====================================================================

[workspace]
# Source directory for packages (default: "src")
source_directory = "src"

# Sandbox rendering output path (default: "render")
render_directory = "render"

# Deployment database tracking folder (default: "install")
install_directory = "install"

# Backup archive folder for collisions & deletions (default: "backup")
backup_directory = "backup"

# Global default target directory (supports home expansion ~)
default_target_directory = "~"

# Default deployment method: "stow" (symlink) or "copy" (physical)
default_install_method = "stow"

# ---------------------------------------------------------------------
# Static Global Environment Variables
# ---------------------------------------------------------------------
[env]
DRIFT_THEME = "catppuccin-mocha"
DRIFT_FONT = "JetBrainsMono Nerd Font"

# ---------------------------------------------------------------------
# Template Rendering Engines (DAG Pipeline)
# ---------------------------------------------------------------------
[render.var]
suffix = "var"
render_command = "internal"

[render.envsubst]
input_file = "envsubst.bash"
suffix = "envst"
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
input_file = "mustache.envst.json"
suffix = "mustache"
render_command = "mustache %i %s"

# ---------------------------------------------------------------------
# Enabled Packages Registry
# ---------------------------------------------------------------------
# Key: package folder name under src/
# Value: true/false to enable or disable the package globally
# DEFAULT = true | false sets the default value for unlisted packages.
[packages.enable]
DEFAULT = false
shell = true
nvim = true
qbittorrent = true
proxychains = false
```

---

## 6. Architecture & Implementation Blueprint

### A. Python Implementation Architecture
1.  **CLI Framework**: `typer` and `rich` for high-signal formatted output, with zero-dependency `argparse` fallback.
2.  **Git Engine**: Direct execution of native `git` commands via Python standard library `subprocess` (`src/drift/git_utils.py`), eliminating external wrapper overhead.
3.  **Configuration Parsing**: Python 3.11+ built-in `tomllib` with lightweight fallback for earlier versions.
4.  **Template Engine**: Modular DAG-based compiler supporting arbitrary external or shell-based engines configured via `drift.toml`.
5.  **Serialization Models**: Native Python dataclasses inheriting from `SerializableModel` for pure standard-library `--json` output without third-party dependencies.

### B. Standardized Process Exit Codes

| Exit Code | Semantic Meaning | Description |
| :---: | :--- | :--- |
| **`0`** | **`SUCCESS` / `HEALTHY`** | The command completed cleanly; status is clean, diffs match, or all health probes passed. |
| **`1`** | **`GENERAL_ERROR` / `UNHEALTHY`** | Runtime failure, subprocess crash, unresolved file collision, or unhealthy probe. |
| **`2`** | **`CONFIG_ERROR`** | Missing or malformed `drift.toml`, `drift_package.toml`, or invalid configuration types. |
| **`3`** | **`DRIFT_DETECTED`** | Sentinel safety guard tripped: uncommitted runtime system changes detected on host. |
| **`4`** | **`RENDER_ERROR`** | Template compilation failure or missing required environment variable. |
| **`5`** | **`COLLISION_ERROR`** | Target file collision detected during install or apply. |
| **`6`** | **`HEALTH_CHECK_FAILED`** | Package health check probe failed or returned non-zero exit status. |
| **`7`** | **`HOOK_SKIPPED`** | Direct hook trigger bypassed or skipped because the hook is not configured or disabled. |

### C. High-Signal Console Aesthetics (Rich Styling)
*   `✨` **Gold/Yellow**: Primary action success / Initiation.
*   `🔍` **Cyan**: Analysis, search, and status checks.
*   `🚀` **Green**: Deployments, additions, and successful updates.
*   `❌` **Bold Red**: Sentinel-blocked operations, aborts, and configuration errors.
*   `💥` **Inverted Bold Red**: Critical execution midway crashes.
*   `⚠️` **Orange/Yellow**: Collision guard warnings, safety backup prompts.
*   `🩺` **Cyan/Teal**: Health audit and runtime probing checks.
