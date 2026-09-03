# Git-Backed Decoupled Two-Stage Dotfiles Management Design

## 1. Introduction & Background

Dotfiles management systems constantly balance three competing goals:
1. **Precision (Declarative accuracy)**: Knowing exactly what configuration is generated and where it is deployed.
2. **Reproducibility**: Being able to recreate the entire system environment on a fresh machine from source templates.
3. **Adaptability (Bidirectional synchronization)**: Recognizing that local machines, desktop environments, and GUI programs (such as qBittorrent, VSCode, or Neovim plugins) frequently modify configuration files at runtime.

### The Problem with Traditional Approaches

*   **GNU Stow (Direct Symlinking)**: Creates direct symlinks from `$HOME` (Applied State) to a physical directory `install/` (Intermediary State).
    *   If an application *overwrites* a file by deleting the symlink and writing a regular file, the link is broken. The repository loses track of local changes.
    *   If an application *writes directly* into the symlink, it modifies the file inside `install/`. Since `install/` is usually generated or ignored, the next run of a template-rendering script will silently overwrite these changes, causing a **Lost Update**.
*   **Chezmoi (Monolithic State)**: Uses a central repository and renders files directly into `$HOME`. It lacks the modular "package-based" categorization of GNU Stow, making it difficult to enable/disable specific modules per host easily, and it handles GUI-driven bidirectional configuration drifts poorly without heavy manual intervention.

### Competitive Edge & Market Comparison

Comparing **drift** to popular dotfiles managers listed on `dotfiles.github.io/utilities`:

| Feature | **drift** (Python) | **Chezmoi** (Go) | **Dotbot** (Python) | **GNU Stow** (Perl) | **VCSH** (Shell) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Engine** | **Dual Local Git Repos** | Single Repo + BoltDB | None (YAML links) | Symlink Farm | Bare Git on `$HOME` |
| **Pipeline Stages** | **2-Stage (Render $\rightarrow$ DB $\rightarrow$ System)** | 1-Stage (Compile $\rightarrow$ System) | 1-Stage (Link) | 1-Stage (Link) | 1-Stage (Direct Git) |
| **Active Drift Audit**| **Yes (Automatic Reverse Sync)** | Yes (Manual `re-add/merge`)| No | No | Rely on Git status |
| **Dry-Run Fidelity** | **Absolute (Diff Δ comparison)** | Dry-run on templates | No | Stow `-n` simulation | No |
| **Mid-Fail Rollback** | **Yes (Dedicated Function)** | Manual cleanup | No | Stow `-D` unlink | No |
| **Machine Output** | **Yes (`--json` across all commands)** | Partial JSON | No | No | No |

#### Key Differentiators:
*   **Over Chezmoi**: Chezmoi hides state in an opaque BoltDB binary database. Under drift, your `install/` state database is a **pure Git repository**. You can walk into `install/`, run `git log`, `git checkout`, or hook up git GUI clients (like Lazygit or GitKraken) to review your deployment history.
*   **Over Dotbot / Stow**: They are strictly one-way bootstrap scripts. They do not comprehend drift, leaving you susceptible to silently lost runtime configurations.

### The Solution: Decoupled Two-Stage Git-Backed Architecture

This design introduces a **sandbox rendering folder (`render/`)** and a **deployment folder (`install/`)**, both managed as **local-only, untracked Git repositories**. 

By separating rendering from deployment and turning both folders into Git databases, we gain absolute, safe visibility over configurations without automated, dangerous, or unintended merges on the live system. It natively supports both **`stow` (symlink-based)** and **`copy` (copy-based)** deployment methods, with built-in privileges management (`sudo`), granular package-level overrides, and strongly-typed machine-readable outputs (`--json`).

---

## 2. High-Level Architecture & Interaction Flows

The architecture separates configurations into four distinct physical and logical tiers:

```
                  [ 1. DECLARATIVE SOURCE ]
                    src/ (Templates & Scripts)
                               │
                               ▼ (Stage 2: sandbox render)
                  [ 2. SANDBOX RENDER ZONE ]
                    render/ (Git repo tracking template-only history)
                               │
                      Diff A   ▼ (Stage 2: incremental staging)
                      (Dry)   [ 3. LOCAL STATE DATABASE ]
                    install/ (Git repo tracking live configuration state)
                               ▲
                      Diff B   │ (Stage 1: System -> install/ reverse-sync)
                     (Live)    ▼ (Symlinks or Physical Copy)
                  [ 4. SYSTEM ACTIVE CONFIGS ]
                     ~/* or /etc/* (Active system configuration files)
```

### Tier Descriptions & Directory Structure

1.  **Declarative Source (`src/`, `config/`)**:
    *   Contains template configurations, raw config files, and global shell variables.
    *   An explicit list of enabled packages for the active machine is defined in **`config/drift.toml`**.
    *   Committed directly to the main git repository.
2.  **Sandbox Render Zone (`render/`)**:
    *   A clean directory initialized as a local Git repository.
    *   Overwriting or clearing this folder has **zero side effects** on the active system.
    *   Ignored by the main repository.
3.  **Local State Database (`install/`)**:
    *   A local-only Git repository initialized inside the `install/` directory.
    *   Ignored by the main repository.
    *   Tracks the exact "applied and committed" state of configurations.
4.  **System Active State (`$HOME`, `/etc`, etc.)**:
    *   The active system directories where software loads configurations.
    *   Files here are either symlinks pointing to `install/<package>/...` (`stow` method) or physical file copies (`copy` method).

---

### User Interaction Methods & Typical Workflows

This architecture organizes daily developer workflows into robust patterns, supporting both **bulk operations (all packages)** and **targeted single-package operations** via the `drift` command line tool:

#### Workflow 1: Developing Declarative Changes (The Template Loop)
You decide to modify your global shell variables or edit a Neovim template.
1.  **Edit Source**: You modify `src/nvim/dot-config/nvim/init.lua` or edit `config/envsubst.bash`.
2.  **Verify Evolution (`drift diff --template nvim` / `-t`)**:
    *   Renders your edits into the `render/` sandbox.
    *   It prints **Diff A**, showing you exactly how your templates evolved.
3.  **Dry-Run check (`drift diff nvim`)**:
    *   Shows **Diff Δ (Pending Delta)**, displaying exactly what changes will be applied to the system.
4.  **Deploy (`drift deploy nvim`)**:
    *   You run the deployment sequence. Since your live system hasn't drifted, Stage 1 completes with a "Clean Slate" status, and Stage 2 runs to instantly apply your new templates to the active environment.

#### Workflow 2: Auditing GUI & Runtime System Drifts (The Drift Audit)
A program (like qBittorrent or a terminal theme tool) has rewritten its configuration file in the background, or you modified a file in your home directory directly to test a setting.
1.  **Audit Drift (`drift diff --system qbittorrent` / `-s`)**:
    *   Pulls active system drift back into `install/` and shows you **Diff B**, displaying exactly what changes were introduced.
2.  **Reconciliation**:
    *   If you want to **Adopt** these changes: You run `drift adopt qbittorrent` to incorporate modifications from `install/` back to your declarative templates under `src/`.
    *   If you want to **Dismiss / Overwrite** these changes: You run `drift deploy qbittorrent --force`. Stage 1 will detect the drift but `--force` bypasses the sentinel and overwrites the system with the declarative state.

#### Workflow 3: Full Recovery (The Rollback Loop)
A deployment failed midway due to a permission error, or manual system edits corrupted a config directory.
1.  **Rollback (`drift rollback nvim`)**:
    *   Reverts the `install/` database to the last successfully committed deployment commit, then triggers a **Full Package Redeploy**, restoring configurations to a known-clean state.

#### Workflow 4: Uninstallation & Detachment (The Uninstall Loop)
You no longer want a package active on this machine.
1.  **Standard Uninstall (`drift uninstall proxychains`)**:
    *   Safely removes all symlinks or copied files from the live system.
    *   Restores any original files backed up under `backup/` to their original paths.
    *   Cleans package records from `install/state.toml` and commits to `install/`.
2.  **Detach / Eject (`drift uninstall proxychains --detach`)**:
    *   Converts active symlinks to permanent physical files on host.
    *   Preserves configurations without restoring backups, safely decoupling from Drift.

---

## 3. The Core Primitives

All high-level workflows in drift are composed of fourteen atomic, sequential primitives:

```
                          [ Execution: drift deploy ]
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │ Stage 1: Primitive 1 (Reverse)│
                       └───────────────┬───────────────┘
                                       ▼
                       ┌───────────────────────────────┐
                       │    Is git -C install clean?   │
                       └───────────────┬───────────────┘
                                       │
                      ┌────────────────┴────────────────┐
                      ▼ (No: Drift detected)            ▼ (Yes: Clean Slate)
               [ ABORT DEPLOY ]                  ┌───────────────────────────────┐
            - Show Diff B                        │ Stage 2 Sequential Flow:      │
            - Guide adopt / force                │ 2. Render Packages            │
                                                 │ 3. Render Repo Commit         │
                                                 │ 4. Stage Render to Install    │
                                                 │ 5. Install Repo Deployment    │
                                                 │ 6. Install Repo Commit        │
                                                 └───────────────┬───────────────┘
                                                                 ▼
                                                 ┌───────────────────────────────┐
                                                 │ Stage 3: Post-Deployment GC   │
                                                 │ 9. Workspace GC (Bulk Only)   │
                                                 └───────────────┬───────────────┘
                                                                 ▼
                                                         [ Deploy Success ]
```

### Primitive 1: Reverse Sync (System $\rightarrow$ `install/` [Low-level: `drift reverse-sync`])
Unconditionally pulls the current host configuration state into the `install/` state Git repository using targeted, $O(N_{\text{pkg}})$ comparisons rather than scanning the entire target host directory (`$HOME`):

1.  **Tracked Package Files Check (`sync_tracked_files`)**:
    *   Runs `compare_folders(src_dir=install/<package>, dst_dir=target_directory, src_only=True, translate_mode="forward")` to probe only the package's tracked files on the host system.
    *   **Deleted on System**: If a tracked file or directory exists in `install/` but is missing on the host system, it is symmetrically removed from the local state repository (`install/`).
    *   **Modified on System**: If a file on the host system contains modifications, it is reverse-copied back to `install/`.
    *   **Type Changes**: If a file changed into a directory on host (or vice-versa), its contents are synced into `install/`.

2.  **Scoped Fully-Controlled Directories (`sync_fully_controlled_dirs`)**:
    *   For directories configured under `fully_controlled_dirs` (FCD), comparisons are scoped strictly to those specific subdirectories (e.g. `~/.config/nvim`), reverse-syncing any wild/untracked files or deletions without traversing the rest of the host filesystem.

### Primitive 2: Render (`src/` $\rightarrow$ `render/` [Low-level: `drift render`])
Processes files in `src/` (expanding templates via `envsubst`/`mustache` or custom configured engines) and places the results in `render/`. No live system files are altered. Triggers `pre_source` before reading source templates and `post_render` hook upon completion.

### Primitive 3: Render Repo Commit [Low-level: `drift render-commit`]
Automatically commits any updates inside the `render/` sandbox Git repository.

### Primitive 4: Stage Render to Install [Low-level: `drift stage`]
Reconciles the sandbox `render/` folder into the `install/` database:
*   **Mechanism**: Computes exactly which files and packages require redeployment. Moves deleted files in `install/` to `backup/<package>/deleted_files/`, copies added/modified files into `install/`, and generates a `PackageStageChanges` object.
*   **Stage Isolation**: Does **not** touch active system target files. All physical system file operations are deferred to Primitive 5.
*   **State Machine**: Sets the package state to **`"staging"`** (transient guard) at the start, and transitions to **`"staged"`** (stable mid-state) upon successful completion. This indicates the database is ready but the system is not yet updated.

### Primitive 5: Install Repo Deployment [Low-level: `drift apply`]
Applies changes to the physical active system:
*   **Collision Guard**: Backs up colliding physical files to `backup/<package>/overwritten/`.
*   **Hooks**: Triggers `pre_install` / `pre_update` before deployment, and `post_install` / `post_update` after successful deployment.
*   **State Machine**: Sets the package state to **`"deploying"`** (transient guard) at the start, and transitions to **`"installed"`** (final state) upon successful completion.
*   **Stow Mode**: Executes individual manual symlinks (Incremental) or runs GNU Stow (Full Deploy).
*   **Copy Mode**: Copies files to `target_directory` (prefixed with `sudo` if configured).

### Primitive 6: Install Repo Commit [Low-level: `drift install-commit`]
Locks the deployed configurations and `state.toml` into the local state database with an automated commit.

### Primitive 7: Uninstall Repo Package [High-level: `drift uninstall`]
Removes or detaches a package from the system:
1.  **Standard Uninstall Mode (Default)**:
    *   **De-stow or Delete**: Unlinks symlinks or deletes physical files.
    *   **Rollback Collision Guard**: Restores original host files backed up in `backup/<package>/overwritten/`.
    *   **Update Registry**: Removes the package from the state database (`install/state.toml`) and commits uninstallation.
2.  **Detach/Eject Mode (`--detach`)**:
    *   **Keep Configuration**: Stops managing this package via Drift, but preserves the current configuration files active on the system (e.g. freezing them as permanent configurations).
    *   **Symlink to Copy Conversion**: If the package was installed using `stow` (symlinking), the engine recursively iterates through the deployed files, removes the symlink, and copies the physical file counterpart from `install/<pkg>/` to the active host target path.
    *   **Backups Kept Intact**: Leaves the user's historical original backups inside `backup/<pkg>/overwritten/` completely untouched (does not restore them).
    *   **Clean Database Decouple**: Unregisters the package from `state.toml` and deletes the local `install/<pkg>` directory, fully decoupling the repository from the active host system without deleting configurations.

### Primitive 8: Rollback Recovery [High-level: `drift rollback`]
Restores the system configuration and the local state database to the last known-clean, committed state after a midway failure. Resets `install/` to HEAD, purges untracked files via `git clean -fd`, and executes a Full Package Redeploy with `force=True`.

### Primitive 9: Workspace Garbage Collection [High-level: `drift gc`]
Identifies and cleans up workspace anomalies, orphaned packages, and zombie database directories:
1.  **Orphan Package Uninstallation**: Automates uninstallation for packages that are registered as `"installed"` in `state.toml` but are no longer enabled/active in `drift.toml`.
2.  **Zombie Folder Purge**: Scans `render/` and `install/` base directories, identifying and purging any subdirectories that do not contain a valid package configuration file (like `drift_package.toml`), preventing database pollution.
3.  **Auto-Commit Database changes**: Auto-stages and commits zombie removal operations inside `render/` and `install/` databases.

### Primitive 10: Package Creation [High-level: `drift new`]
Scaffolds a new declarative package inside the `src/` directory:
1.  Creates the `src/<package_name>` directory.
2.  Generates a default `drift_package.toml` with standard safe defaults (e.g. `install_method = "stow"`).
3.  Features built-in probing guards to prevent accidental overwriting of existing package configurations unless `--force` is used.

### Primitive 11: Resource Import [High-level: `drift add`]
Imports an existing, active host system configuration file directly into the declarative source repository:
1.  Resolves symlinks to capture actual physical file contents for reproducibility.
2.  Translates standard hidden dotfile names (e.g. `.bashrc`) to repository-safe dot-prefixes (e.g. `dot-bashrc`).
3.  Performs a global conflict check before copying; if an import would overwrite an existing source template in `src/<package>`, it halts and reports a conflict error to protect declarative templates.

### Primitive 12: Package Runtime Health Checks [High-level: `drift health`]
Executes live runtime health check probe scripts declared in `drift_package.toml` (`[hooks] health = ...`):
1.  Executes the declared health probe script with the working directory (`cwd`) set to the package's deployed host `target_directory`.
2.  Captures execution exit codes, standard output, standard error, and timing metrics under strict timeout constraints.
3.  Aggregates health diagnostics across all installed packages with rich status displays and machine-readable `--json` summaries.

### Primitive 13: Repository Cloning & Legacy Migration [High-level: `drift clone`]
Clones a remote Git repository and automatically bootstraps the workspace:
1.  **Case A (Drift Workspace)**: Clones the repository and immediately triggers non-destructive self-healing (`repair_drift_workspace`) to reconstruct local databases (`render/.git`, `install/.git`, `state.toml`), `.gitignore` rules, and local config templates (`config/drift.local.toml`, `config/secrets.env`).
2.  **Case B (Plain / Legacy Dotfiles)**: Migrates plain dotfiles into `src/<pkg_name>/`, initializes full Drift workspace infrastructure, generates `drift_package.toml` and `.drift_ignore`, and enables the package in `config/drift.toml`.

### Primitive 14: Workspace Diagnostics & Self-Healing [High-level: `drift repair`]
Audits and self-heals workspace structure, repositories, configuration templates, and secrets:
1.  Reconstructs missing Git state databases (`render/.git`, `install/.git`) and `install/state.toml`.
2.  Rebuilds `.gitignore` and `install/.stow-local-ignore` isolation rules.
3.  Generates default templates for `config/drift.local.toml` and `config/secrets.env` if missing.

---

## 4. User-Facing Operations (CLI Overview)

The `drift` Python command provides a unified interface for all primitives and high-level workflows with `--json` machine-readable output support.

### High-Level Commands (Ordered by Lifecycle)
*   **`drift clone <repository> [destination] [-b/--branch <branch>] [--depth <N>] [--no-repair] [--json]`**: Clones a Git repo and auto-bootstraps/repairs the Drift workspace (Primitive 13).
*   **`drift init [-f/--force] [--no-git-root] [--json]`**: Initializes a new drift workspace.
*   **`drift new <package> [-f/--force] [-t/--target <dir>] [-m/--method <stow|copy>] [--json]`**: Scaffolds a new dotfiles package (Primitive 10).
*   **`drift add <package> <paths...> [--dry-run] [--no-hooks] [--json]`**: Imports active system files into a declarative package (Primitive 11).
*   **`drift adopt [packages...] [-i/--interactive] [--accept-conflicts] [-f/--force] [--dry-run] [--no-hooks] [--json]`**: Reconciles system drift into templates.
*   **`drift deploy [packages...] [-f/--force] [--no-hooks] [--json]`**: Atomic Two-Stage deployment with Sentinel drift safety guards.
*   **`drift health [packages...] [-t/--timeout <secs>] [-v/--verbose] [--json]`**: Runs runtime health check probes on installed packages (Primitive 12).
*   **`drift uninstall <packages...> [-f/--force] [--detach] [--dry-run] [--no-hooks] [--json]`**: Safely cleans or detaches a package from the system (Primitive 7).
*   **`drift rollback [packages...] [-f/--force] [--no-hooks] [--json]`**: Emergency recovery after midway failure (Primitive 8).
*   **`drift status [packages...] [--json]`**: Audits and aggregates the alignment of templates, system drift, and pending deployments.
*   **`drift diff [packages...] [-t/--template] [-s/--system] [--stat] [-y/--side-by-side] [--json]`**: Visualizes changes between layers (Diff A, Diff B, or Diff Δ).
*   **`drift gc [--dry-run] [--no-hooks] [--json]`**: Cleans orphan packages and purges zombie database directories (Primitive 9).
*   **`drift repair [--dry-run] [--json]`**: Audits and self-heals workspace structure, repositories, config templates, and secrets (Primitive 14).
*   **`drift complete [<shell>] [--install] [--json]`**: Generates or installs native interactive shell tab-completion scripts (bash, zsh, fish, nu).
*   **`drift help [topic]`**: Interactive mini user manual with pager fallback support (topics: `package`, `src`, `render`, `install`, `fcd`, `ignore`, `drift_package.toml`, `drift.toml`, `workspace`, `health`, `clone`, `faq`).

### Low-Level Control Commands (Ordered by Pipeline Lifecycle)
These commands are for advanced users or CI/CD pipelines to trigger specific primitives:
*   **`drift reverse-sync [packages...] [--json]`**: Trigger Primitive 1 (System $\rightarrow$ install/).
*   **`drift render [packages...] [--no-hooks] [--json]`**: Trigger Primitive 2 (Render).
*   **`drift render-commit [packages...] -m <msg> [--json]`**: Trigger Primitive 3 (Commit Render).
*   **`drift stage [packages...] [--force] [--json]`**: Trigger Primitive 4 (Staging).
*   **`drift apply [packages...] [--force] [--no-hooks] [--json]`**: Trigger Primitive 5 (Physical Deployment).
*   **`drift install-commit [packages...] -m <msg> [--json]`**: Trigger Primitive 6 (Commit install/).
*   **`drift hook <package> <hook_name> [--json]`**: Directly executes a specific lifecycle hook script for a single package.

---

## 5. Workspace & Package Configuration

This section provides the essential syntax and specifications for global and package-level configurations.

### A. Global Workspace Configuration: `config/drift.toml` Specification
Rather than scanning the filesystem blindly, the drift engine relies on a centralized workspace configuration file located at `config/drift.toml` (which can itself be a template named `drift.envst.toml`). This file orchestrates two main responsibilities:
1. **Workspace Paths & Rendering Engines**: Defines directories (`source_directory`, `render_directory`, `install_directory`, `backup_directory`, `default_target_directory`, `default_install_method`) and template engines with their file suffixes and rendering subprocess commands (e.g. `envsubst`, `mustache`).
2. **Enabled Packages Registry**: Declares exactly which package subfolders under `src/` are globally active via the `[packages.enable]` section.

*   **Active Package Determination**:
    During global operations (like a bulk `drift status` or `drift deploy`), the engine checks the package registry table:
    - If a package is listed as `true`, it is processed.
    - If listed as `false`, it is completely ignored.
    - An optional `DEFAULT = true | false` key specifies whether packages not explicitly listed are enabled or disabled by default. If `DEFAULT` is omitted or `false`, unlisted folders are ignored.

```toml
# =====================================================================
# drift.toml Configuration
# =====================================================================

[workspace]
# Source directory for packages, default value is "src"
source_directory = "src"

# Sandbox rendering output path, default value is "render"
render_directory = "render"

# Deployment database tracking folder, default value is "install"
install_directory = "install"

# Backup archive folder for collisions & deletions, default value is "backup"
backup_directory = "backup"

# Global default target directory, default value is user home.
# Supports home expansion (~ at the beginning).
default_target_directory = "~"

# Default deployment method: "stow" (symlink) or "copy" (physical)
default_install_method = "stow"

[render.envsubst]
# Shell script providing env variables for envsubst
# If it's a relative path, it's always relative to the 'config' folder under working directory.
# The file is located at "config/envsubst.bash" .
input_file = "envsubst.bash"

# Files with name "file.envst.suffix" or "file.envst" will be rendered using envsubst.
suffix = "envst"

# The output of render_command will be written as render result.
# %i means engine input, %s means source template.
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
# Json file as the input to mustache template render engine.
# This filename ends with "envst.json", so it needs to be rendered with envsubst first to get the actual json file.
input_file = "mustache.envst.json"

# Files with name "file.mustache.suffix" or "file.mustache" will be rendered using mustache.
suffix = "mustache"
render_command = "mustache %i %s"

# ---------------------------------------------------------------------
# Enabled Packages Registry
# ---------------------------------------------------------------------
# Key: package folder name under src/
# Value: True/False to enable or disable the package globally
# Entry "DEFAULT = true | false" will set the default value for unlisted packages.
# "DEFAULT = false" is the default setting.
[packages.enable]
DEFAULT = false
shell = true
nvim = true
qbittorrent = true
proxychains = false
```

#### Meta-Config Templating: `drift.envst.toml` & `drift.local.envst.toml`
To allow complete bootstrapping of workspaces under different environment parameters, the workspace config files (`drift.toml` and `drift.local.toml`) can themselves be templates named `drift.envst.toml` or `drift.local.envst.toml`. Drift automatically compiles them on-the-fly using `envsubst` populated with active system-level environment variables.

For example, a user or provisioning script can compute machine capabilities and export an environment variable containing the desired package roster:
```bash
export DRIFT_PACKAGES="shell = true
nvim = true
cuda_toolkit = true
desktop_hyprland = false
"
```
And author `config/drift.local.envst.toml`:
```toml
[packages.enable]
DEFAULT = false
${DRIFT_PACKAGES}
```
When Drift loads the workspace configuration, `render_envst_load_toml` automatically evaluates `${DRIFT_PACKAGES}` into valid TOML key-value pairs.

#### Private Dotenv Vault: `config/secrets.env`
To isolate secret tokens, private API keys, and work-specific emails from public dotfiles repositories, Drift provides a secure, local-only, git-ignored Dotenv vault located at `config/secrets.env`.

1. **Strict 7-Tier Variable Precedence**:
   During template parsing and hook execution, variables are resolved in a strict order of precedence (highest precedence overrides lower layers):
   - **Tier 1 - Host Shell / CLI Environment**: Active environment variables provided at invocation context (`os.environ`).
   - **Tier 2 - Package `[env.override]`**: Package-enforced configuration overrides defined in `src/<pkg>/drift_package.toml`.
   - **Tier 3 - Package Facts (`drift_package_*`)**: Dynamic attributes (`drift_package_name`, `drift_package_target_dir`, `drift_package_install_method`, etc.).
   - **Tier 4 - System Facts (`drift_*`)**: Auto-populated host facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
   - **Tier 5 - Secret Vault (`config/secrets.env`)**: Local, private settings and sensitive overrides loaded dynamically during rendering.
   - **Tier 6 - Global Workspace Environment (`[env]` table in `drift.toml`)**: Shared, non-sensitive environment defaults.
   - **Tier 7 - Package `[env.fallback]`**: Default fallback values defined in `src/<pkg>/drift_package.toml` used only when unset by upper tiers.

2. **Transient, Clean-Room Isolation**:
   To prevent credentials from leaking to other processes, secrets are loaded with transient isolation:
   - At the very beginning of **Render Package Primitive 2** (before template input compiling and package rendering begin), the engine parses `config/secrets.env` (stripping comments and quotes) and loads them into `os.environ`.
   - It records the original state of all loaded keys.
   - Once all rendering operations are finished, a secure `finally` block runs, unloading the secrets and completely restoring the parent shell's original environment variables. This guarantees zero credential contamination.

### B. Custom Render Engines & Template Input Dependencies
Rather than utilizing closed/hardcoded compilation scripts, the drift workspace supports registering flexible, custom-defined template render engines.

#### 1. Custom Render Engine Schema
Under the `[render.<engine_name>]` tables in `drift.toml`, developers can define arbitrary engines. Each engine declaration supports three main properties:
1.  **`input_file`**: The file path providing active variables or values to the engine (e.g. a shell environment script or JSON dataset). If relative, the path is always resolved against the `config/` base folder.
2.  **`suffix`**: The file extension pattern matched by the engine (e.g., matching `.envst` or `.mustache`).
3.  **`render_command`**: The exact shell execution pattern used to compile files. It supports two special interpolation placeholders:
    *   `%i`: Substituted with the resolved, absolute path of the engine's `input_file` (or its rendered counterpart).
    *   `%s`: Substituted with the absolute path of the source template file inside `src/`.

#### 2. Template Input Dependencies
Render engines often require dynamic input parameters (such as `mustache` needing a static JSON configuration constructed from variable environment templates). To support this cleanly, the drift engine natively implements **Template Input Dependencies**:
*   An engine's `input_file` can itself be a template matching another registered render engine.
*   **The Transitive Resolution Chain**: 
    If the system detects that an engine's `input_file` matches another engine's template suffix, it automatically compiles the input file first. This resolution is fully transitive/recursive: a multi-level dependency chain (e.g., Engine A -> Engine B -> Engine C -> Engine D) is allowed and gets compiled in topological order from leaf to root.
    *   *Example*: The `mustache` engine registers `input_file = "mustache.envst.json"`. Since `.envst.json` matches the `envsubst` suffix (`envst`), the compiler first renders `config/mustache.envst.json` via the `envsubst` engine.
    *   The compiled static output is saved inside the sandbox under `render/config/mustache.json`.
    *   The `mustache` engine is then invoked, substituting `%i` with the absolute path of this rendered file (`render/config/mustache.json`).

#### 3. Single-Dependency Constraint per Engine
While multi-level transitive chains are fully supported, each engine's input file can match at most one other engine's suffix pattern. Thus, every engine is limited to a single direct dependency (a 1-to-1 matching relationship per level), forming a dependency tree/forest (without cycles) rather than a complex multi-parent DAG. Double extensions or nested suffixes are strictly evaluated at the outermost matching level:
*   An input named `file.<engine1>.<engine2>.suffix` is evaluated as a template for `engine2` only. The `<engine1>` portion of the name remains treated as passive text, and `file.<engine1>.suffix` is forwarded as the final compiled input file to the parent engine.

#### 4. Directed Acyclic Graph (DAG) Cyclic Detection
Because inputs can depend on the outputs of other engines, compilation order must follow a strictly sequential pipeline.
*   Before any rendering begins, the compiler builds a dependency graph of all registered render engines and executes a **Cycle Detection** algorithm.
*   If any circular dependency is detected (e.g., Engine A's input depends on Engine B's output, and Engine B's input depends on Engine A's output), compilation is instantly aborted with a `CyclicDependencyError` to prevent infinite rendering loops.

#### 5. Graceful Disabling & Deferred Execution Check
If a registered engine's `input_file` is not specified, is empty, or is missing on disk (whether as a static path or a templated dependency), the compilation engine handles it gracefully:
*   **Initialization Warning**: During the workspace bootstrapping phase (`render_input_templates`), instead of raising a fatal crash, the engine logs a clear, descriptive warning and sets the engine's resolved input file to `Path("")` (an empty path). This allows other independent render processes to initialize and compile normally.
*   **Deferred Runtime Check**: The safety safeguard is deferred to actual template rendering. If any template file in the repository relies on a gracefully disabled engine, the core rendering pipeline (`resolve_render_template_args`) checks for the empty `Path("")` input path. If found, it halts compilation immediately with a descriptive `ValueError` (e.g., `Render engine '<name>' is disabled or has an invalid/empty input file`), ensuring that no silent partial configurations are deployed.

### C. Package Configuration: `drift_package.toml` Specification
A package configuration file — named `drift_package.toml` — is **strictly required** for every active package and **must be located in the root of the package directory** (e.g. `src/<package_name>/drift_package.toml`). If a package configuration is missing, the engine throws a `FileNotFoundError` and halts to prevent unsafe actions or system corruption.

#### Layered Overrides and Unified Rendering Code Flow
To handle machine-specific overrides and secrets at the package level, Drift implements a layered override merge system and a unified rendered target name pattern:
1. **Hierarchical Merging (`package.local.toml` / `drift_package.local.toml`)**:
   - The primary package configuration (`drift_package.toml`) is committed to the version-controlled repository.
   - Users can create a local-only machine override file (`package.local.toml` or `drift_package.local.toml`) which is gitignored (using `*.local.toml` patterns).
   - During rendering, the engine locates and reads the base configuration, locates and reads the local override configuration (if present), and recursively merges their dictionary trees.
2. **On-the-Fly Template Rendering**:
   - For both the base and local configurations, if they are templates (e.g. `package.envst.toml`), they are rendered on-the-fly to temporary files before being parsed to dictionary structures.
3. **Unified Render Target Name (`drift_package.toml`)**:
   - Regardless of whether the original source files are named `drift_package.toml`, or their template/local override counterparts, the final merged TOML dictionary is **always serialized and rendered as `drift_package.toml`** inside the sandbox directory at `render/<package_name>/drift_package.toml`.
   - All subsequent package inspections, change visualizations, and staging processes read from this standardized `render/<package_name>/drift_package.toml` file, ensuring perfect downstream modularity and zero ambiguity.
4. **Exclusion Guard**: The final rendered `drift_package.toml` is strictly marked as a metadata file. It is **never copied** or symlinked onto the active target system, but stays as an index inside `install/<package_name>/drift_package.toml`.

#### Default Config Template:
```toml
# =====================================================================
# drift_package.toml Template & Specification
# Place this file in: src/<package_name>/drift_package.toml
# =====================================================================

[package]
# ---------------------------------------------------------------------
# Feature Flags (Default: true)
# ---------------------------------------------------------------------
# If false, the package templates inside src/ will not be processed during rendering, let alone installation.
enable_render = true

# If false, the package will be excluded from Stage 1 synchronization and Stage 2 deployment
enable_install = true

# ---------------------------------------------------------------------
# Installation Options
# ---------------------------------------------------------------------
# Deployment method. Options: 
#   - "stow" : Creates symbolic links from target_directory to install/ folder. (Standard for user dotfiles)
#   - "copy" : Physically copies files from install/ to target_directory. (Standard for system/etc configs)
# Falls back to "default_install_method" in drift.toml if unspecified.
install_method = "stow"

# The physical path where this package should be deployed on Unix/Linux/macOS hosts.
# Supports home expansion (~ at the beginning).
# Falls back to "default_target_directory" in drift.toml if unspecified.
target_directory = "~/.config/example"

# Optional Windows-specific target folder path.
# Used instead of target_directory when running on Windows (win32).
# Supports %USERPROFILE%, %APPDATA%, %LOCALAPPDATA%, ~, etc.
# Aliases accepted: target_directory_windows, target_directory_win32, target_directory_winos, target_directory_win.
# target_directory_windows = "%LOCALAPPDATA%/example"

# Optional subfolder within src/<package_name>/ to render and deploy (defaults to ".").
# If specified, only files in this subfolder are compiled and deployed to the host.
# source_directory = "dotfiles"

# If true, all physical file creation, copying, deletion, and symlinking operations
# for this package will be executed utilizing "sudo" elevation.
# Note: All lifecycle hooks always execute in user space without sudo to preserve injected environment variables.
sudo = false

# ---------------------------------------------------------------------
# Fully-Controlled Directory (FCD) Audit Options & Ignore Mechanics
# ---------------------------------------------------------------------
# List of subdirectories (expressed as relative paths under target_directory)
# which are fully owned by this dotfiles repository.
# Stage 1 recursively scans these folders on the host system. Any untracked
# files found here are reverse-synchronized back to install/.
#
# FCD Ignore & Discard Reconciliation Mechanics:
# When untracked files are found in FCDs, they are reverse-synced to install/.
# Developers can reconcile them using `drift adopt`:
# - Adopt: Copy the file from install/ to src/ (translating dot-prefixes).
# - Ignore: Symmetrically deletes the file from install/, and appends the
#   relative path pattern to the package's `.drift_ignore` file. This prevents
#   future reverse-sync passes from sweeping this file back to install/, leaving
#   it safely untouched on the host system.
# - Discard/Delete: Symmetrically deletes the file from the install/ database. 
#   In the subsequent deploy pass, since the file is missing from src/ (not rendered), 
#   it is treated as an orphan and is automatically deleted from the host system.
fully_controlled_dirs = [
    "sub_dir1",
    "sub_dir2"
]

[hooks]
# ---------------------------------------------------------------------
# Lifecycle Hooks
# ---------------------------------------------------------------------
# Executable scripts located inside the package directory.
# All lifecycle hooks always run in user space without sudo, preserving all 7 tiers of environment variables.
# If a hook requires elevated privileges for a specific operation, use 'sudo' explicitly inside the hook script.

# Run before reading/writing source package files (e.g. generating dynamic templates before render, adopt, or add, CWD: src/pkg).
pre_source = "pre-source.bash"

# Run after templates are rendered into sandbox (CWD: render/pkg).
post_render = "post-render.bash"

# Run before first-time installation (CWD: install/pkg).
pre_install = "pre-install.bash"

# Run after successful first-time installation (CWD: target_directory).
post_install = "post-install.bash"

# Run before any update/deployment (CWD: install/pkg).
pre_update = "pre-update.bash"

# Run after any successful update/deployment (CWD: target_directory).
post_update = "post-update.bash"

# Run before package uninstallation (CWD: install/pkg).
pre_uninstall = "pre-uninstall.bash"

# Run after package uninstallation (CWD: target_directory).
post_uninstall = "post-uninstall.bash"

# Run runtime health check probe on installed package (CWD: target_directory).
health = "health.bash"

# Timeout in seconds for lifecycle hook script executions (Default: 120)
timeout = 120

# Optional Windows-specific hook overrides (aliases: [hooks.windows], [hooks.win32], [hooks.winos], [hooks.win]).
# [hooks.windows]
# pre_install = "scripts/bootstrap.exe"
# post_install = "scripts/setup.ps1"
# post_update = "scripts/reload_service.bat"
# health = "scripts/health_check.ps1"
```

#### Lifecycle Hooks Execution Matrix
| Hook Name | Lifecycle Trigger Stage | Working Directory (`cwd`) | Privilege Model |
| :--- | :--- | :--- | :--- |
| `probe` | Requirement validation (deploy, render, status) | `src/<pkg>` | Always user space (Preserves envs) |
| `pre_source` | Before reading templates (render, adopt, add) | `src/<pkg>` | Always user space (Preserves envs) |
| `post_render` | After sandbox compilation | `render/<pkg>` | Always user space (Preserves envs) |
| `pre_install` | Before first-time deployment | `install/<pkg>` | Always user space (Preserves envs) |
| `post_install` | After first-time deployment | `target_directory` | Always user space (Preserves envs) |
| `pre_update` | Before incremental/full update deploy | `install/<pkg>` | Always user space (Preserves envs) |
| `post_update` | After incremental/full update deploy | `target_directory` | Always user space (Preserves envs) |
| `pre_uninstall` | Before unlinking/deleting files | `install/<pkg>` | Always user space (Preserves envs) |
| `post_uninstall`| After unlinking/deleting files | `target_directory` | Always user space (Preserves envs) |
| `health` | During `drift health` probe execution | `target_directory` | Always user space (Preserves envs) |

#### Event Ordering & Install Method Semantics (`stow` vs. `copy`)
Because Drift separates template staging (Primitive 4: `render/` $\rightarrow$ `install/`) from host delivery (Primitive 5: `install/` $\rightarrow$ host), the timing of file content updates relative to lifecycle hooks depends on the package's `install_method`:

*   **`install_method = "copy"` (Strict Event Ordering)**:
    *   Host target files remain strictly in their previous state during the Staging phase (Primitive 4).
    *   `pre_update` executes while host files are strictly at their prior version.
    *   Physical files are then copied, updated, or removed on the host during Primitive 5.
    *   `post_update` executes after host files have received the new state.
    *   👉 **Recommendation**: If your package configuration is watched by active system services or daemons (e.g. `systemd` user units with inotify watchers) that must be cleanly stopped in `pre_update` before configuration files change, use **`install_method = "copy"`**.

*   **`install_method = "stow"` (Symlink Pointers)**:
    *   Because active host paths are symbolic links pointing into `install/<pkg>/`, modifying file contents in `install/` during Staging (Primitive 4) makes content modifications immediately visible on the host **before** `pre_update` runs in Primitive 5.
    *   Structural changes (creating symlinks for new files or pruning deleted symlinks) are applied during Primitive 5 after `pre_update`.
    *   👉 **Recommendation**: Ideal for standard user dotfiles (e.g. `.zshrc`, `.tmux.conf`, Neovim configs) where instant reflection and symlink transparency are preferred.

#### Default Package Environment Variables & Precedence
After parsing a package's configuration, the drift engine dynamically loads package-specific environment variables into `os.environ` via `PackageConfig.load_package_envs(workspace_config)` (with `overwrite=True`):
*   **`drift_package_name`**: Name / directory name of the package.
*   **`drift_package_target_dir`**: Resolved absolute destination target directory path on the host system.
*   **`drift_package_source_dir`**: Absolute path to the package's source directory in the workspace (`<drift_root>/src/<pkg>`).
*   **`drift_package_render_dir`**: Absolute path to the package's compiled sandbox directory (`<drift_root>/render/<pkg>`).
*   **`drift_package_install_dir`**: Absolute path to the package's state database directory (`<drift_root>/install/<pkg>`).
*   **`drift_package_install_method`**: Resolved deployment method (`stow` or `copy`).

> [!IMPORTANT]
> **Environment Variable Precedence & Overrides**:
> Variables within package operations follow the strict 7-tier precedence hierarchy:
> 1. Host shell / CLI environment variables (`os.environ`).
> 2. Package `[env.override]` overrides.
> 3. Package facts (`drift_package_*`).
> 4. Host facts (`drift_*`).
> 5. Secret variables loaded from `config/secrets.env`.
> 6. Global workspace environment variables in `config/drift.toml` (`[env]` table).
> 7. Package `[env.fallback]` defaults.
>
> This guarantees that templates and hook scripts always receive the exact, authoritative package attributes regardless of any external or global environment definitions.

These variables are active during:
1.  **Lifecycle Hook Script Executions** (`pre_source`, `post_render`, `pre_install`, `post_install`, `pre_update`, `post_update`).
2.  **Template Compilations** (accessible as `${drift_package_name}`, `${drift_package_target_dir}`, `${drift_package_source_dir}`, etc. in `.envst` / `envsubst` templates).
3.  **Physical Deployment Operations**.

Upon completion of the package's render or deployment phase, these variables are restored and unloaded via `PackageConfig.unload_package_envs()`, guaranteeing clean-room environment isolation between packages.

---

## 6. Execution Safeguards, Policies & Customization

This section defines the core architectural policies, safeguards, and customization guidelines required to maintain technical integrity.

### A. Ignored Files and Name Conversion Rules
Both `stow` and `copy` deployment strategies must natively respect ignore files and name transformation specifications:
1.  **Ignore Filter (`.drift_ignore`) Syntax & Rules**:
    *   **Single File Restriction & Stow Compatibility**:
        *   Exactly **one** `.drift_ignore` file is allowed at the root of each package directory. Nested subdirectory ignore files are strictly prohibited and will trigger execution aborts.
        *   **Default Stow Ignore List**: If no `.drift_ignore` is provided for a package, Drift automatically applies GNU Stow's default ignore list (`RCS`, `\.+,v`, `CVS`, `\.\#.+=`, `\.cvsignore`, `\.svn`, `_darcs`, `\.hg`, `\.git`, `\.gitignore`, `.+~`, `\#.*\#`, `^/README.*`, `^/LICENSE.*`, `^/COPYING.*`) for complete compatibility.
    *   **Syntax & Engine**:
        *   The `.drift_ignore` matches the exact syntax and matching rules used by GNU Stow's `.stow-local-ignore`.
        *   **No Globbing**: The ignore engine **does NOT use globbing**. Instead, it compiles and evaluates patterns as **PCRE Regular Expressions** (compiled in Python's `re` engine).
        *   **Comments and Blank Lines**: Lines starting with `#` are treated as comments and stripped (unless escaped as `\#`), and empty lines are bypassed.
    *   **Matching Algorithm**:
        *   *With Slashes*: If a pattern contains a forward slash `/`, it is evaluated against the complete relative path of the file prefixed with a forward slash (e.g. `/dot-config/coc-settings.json`).
        *   *Without Slashes*: If a pattern does not contain a slash, it is matched directly against the file's `basename` (e.g., `\.bak$`).
    *   **Match Timing Guard**: The ignore engine matches file patterns against the native repository filenames **before** any prefix conversion or suffix extraction takes place.
        *   *Important*: To ignore a file named `dot-bashrc`, your `.drift_ignore` file must list `dot-bashrc`, not `.bashrc`. Listing `.bashrc` will fail to match on disk, and the file will still be processed.
    *   **Implicit Exclusions**: Package configurations (`drift_package.toml`, `.drift_ignore`, `.stow-local-ignore`, `drift_package.local.toml`) are automatically excluded by the compilation engine without requiring manual entries.
    *   **Automated `.stow-local-ignore` Generation**: During staging and deployment, Drift exports all active `DriftIgnore` patterns plus `MANAGED_CONFIG_FILES` into `install/<package>/.stow-local-ignore`. This ensures GNU Stow respects both custom and default ignore rules without polluting host target directories.
    *   An extra `.stow-local-ignore` is dynamically generated at the root of the `install/` directory to prevent GNU Stow from parsing the internal database file `state.toml` as an active package.

    #### PCRE `.drift_ignore` File Example:
    ```ini
    # Ignore any files ending in '.bak' anywhere in the package
    \.bak$

    # Ignore any files starting with a tilde (such as temp files)
    ^~

    # Ignore a specific directory named 'build' anywhere in the package path
    /build/

    # Ignore a specific path relative to the package root
    ^/dot-config/coc-settings\.json$

    # Ignore a specific directory under a subfolder, recursively
    ^/dot-config/nvim/tmp/
    ```

2.  **Prefix Conversion (`dot-` to `.`)**:
    *   To allow developers to easily manage hidden folders in standard git environments, folders and files starting with the prefix `dot-` inside `install/` must be translated to a dot `.` prefix at deployment target paths.
    *   **Mandatory 'dot-' Prefix Rule in Source Templates**: All files and directories inside source packages under `src/` that should be rendered and installed as hidden files/directories (starting with `.`) **must** be named with a `dot-` prefix (e.g., `dot-bashrc`, `dot-config/`). Raw hidden files starting with a dot `.` (such as `.bashrc` or `.env`) are **strictly prohibited** in source packages. The rendering process (`render_package`) will skip any `.*` files (except the special `.drift_ignore` file) in the rendering process and print an information log about them.
    *   *Example*: `install/shell/dot-bashrc` translates to `~/.bashrc`.
    *   *Example*: `install/nvim/dot-config/nvim/` translates to `~/.config/nvim/`.
    *   This translation is enforced symmetrically across both `stow` and `copy` installation methods.

### B. Naming Convention for Templates (IDE & LSP Friendly)
To guarantee full IDE and Language Server Protocol (LSP) features (e.g., syntax highlighting, linting, autocomplete) for template files within editors (such as VSCode, Neovim, or Emacs), the system enforces a strict suffix naming convention:
*   **Format**: `[filename].[engine_prefix].[target_extension]`
*   **Suffix No-Dot Restriction**: The suffix defined for any render engine (such as `envst` or `mustache`) **cannot contain any dots ('.')**. This is validated during configuration loading, and any engine suffix containing dots will cause validation to fail.
*   **Officially Supported Engines** (Custom engines can be defined in `drift.toml`):
    1.  *Envsubst*: Uses suffix **`.envst.[ext]`** (e.g., `dot-bashrc.envst.sh`, `all_proxy.envst.conf`).
    2.  *Mustache*: Uses suffix **`.mustache.[ext]`** (e.g., `home.mustache.nix`, `settings.mustache.json`).
*   **Why this is superior**: Because the terminal extension is the actual target format (like `.sh`, `.nix`, `.json`), text editors instantly apply the correct syntax highlighting, formatters, and LSP environments without requiring custom regex filetype mappings.

### C. Incremental vs. Full Deployment Strategies
To minimize system disruption and application reloads, deployment is executed under two distinct strategies:
1.  **Incremental Deployment (Surgical File-by-File)**:
    *   When executing a deployment sequence, Primitive 4 outputs a granular `PackageStageChanges` object of added, modified, or deleted files.
    *   In both `stow` and `copy` modes, the deployment engine **avoids invoking external command-line tools** (like the `stow` binary or heavy shell directories copiers).
    *   Instead, it surgically iterates through the computed file list, creating individual symlinks (or copying individual files) manually. This maintains a minimal interruption footprint and prevents bulk reload signals to running processes.
    *   **Infinite Loop Protection**: In `stow` mode, before creating any symlink `~/<path>`, the incremental deployer traverses up the directory path from the parent. If it discovers any parent directory (e.g. `~/.config/nvim/`) is **already a symlink** pointing into `install/`, it **must immediately and safely skip** creating individual symlinks inside that directory. This prevents creating self-referencing circular symlink loops inside the local database.
2.  **Full Deployment (Heavy-Duty Fallback)**:
    *   When triggering a standalone deployment, running `drift rollback`, or performing a first-time setup, the system defaults to a robust **Full Package Redeploy**.
    *   It utilizes high-level automated commands:
        *   *Stow Packages*: Invokes GNU Stow with flags **always set to**: `stow --no-folding --dotfiles -t <target_directory> <package>`, prefixed with `sudo` if configured.  
            If stow >= 2.4.1 (which fixes `--dotfiles` problems with stow ignore) is not found, the external `stow` command will be replaced by incremental deployment for all files in the install folder.
        *   *Copy Packages*: Invokes copying commands (like `rsync -av` or `cp -r` prefixed with `sudo` if configured. Use `rsync` first, if not available, fallback to `cp`) **without using `--delete`** (avoiding deleting unrelated files inside target directories). Any wild-file pruning is strictly scoped and handled during Primitive 1.  

The program ensures compatibility between these two modes. In either mode, the program verifies the package config has `enable_install=true` and loads the install method, install location, and sudo flag from it.

### D. Physical Conflict Prevention (Collision Guard)
To protect pre-existing manual files from being silently overridden or destroyed during deployment, the Collision Guard strictly enforces four levels of audits using the centralized `compare_folders` tool. 

#### 1. Target Parent Symlink Safety Abort (Pre-Check)
Prior to running any folder comparisons, the system traverses up the target directory path. If it discovers that any parent directory of `target_dir` (at or above the target base) is a symlink pointing inside the workspace root (`drift_root`), it **aborts immediately** with a `RuntimeError`. Automatic cleanup/resolution of parent-level symlinks is highly risky and must be resolved manually by the user to avoid data loss.

#### 2. Unified Recursive Audit Flow
If the parent symlink safety check passes, the system invokes `compare_folders` with parameters `src_only=True` and `translate_mode="forward"` to check how files in the repository's `install/` base compare to the active target system paths (handling path conversions like `dot-` to `.` automatically). 

Files are categorized and safely routed to prevent overwriting or data loss:
1.  **Safety Aborts (`CollisionError` / Exit Code `5`)**:
    *   *Path Collision Guard Abort*: When an untracked, external file or broken symlink exists at the target host destination and deployment is run without `--force`, the collision guard halts execution to prevent unintentional overwrites.
    *   *State Ownership Collision*: If the target destination is already claimed and owned by a different Drift package in `state.toml`, deployment aborts immediately.
2.  **Backup & Collision Routing (Non-Abort Operations)**:
    *   *Internal Symlinks (`diff.internal_symlinks`)*: Any path discovered inside the target directory that is a symlink pointing inside the workspace root (`drift_root`) represents severe repo pollution. The system backs up this symlink to `backup/<package>/overwritten/<path>`, removes the link, and recreates physical parent directories.
    *   *Type Mismatches (`diff.deleted`)*: If the type of a target path on the host differs from the repository (e.g. a physical file exists where the repo expects a folder), the system backs up the host item to `backup/<package>/overwritten/<path>` and clears the path.
    *   *Modified paths (`diff.modified`)*:
        *   *Stow Link Exemption*: If the target is already a symlink pointing to OUR package in `install/`, it is a valid pre-existing link and skipped.
        *   *Copy Mode Exemption*: If deployed via `copy` and already registered in `state.toml`, target files are overwritten with updated content.
        *   *Overwritten Backup*: Otherwise, the conflicting file/folder on the host is backed up to `backup/<package>/overwritten/<path>` and removed.
    *   *Matches (`diff.matches` under Stow mode)*: If a file matches content exactly but exists on the host as a physical regular file rather than a symlink, the physical file is backed up to `backup/<package>/overwritten/<path>` and replaced by the Stow symlink.

> [!IMPORTANT]
> **Transient `backup/` Directory Policy & User Responsibility**:
> Drift creates timestamped and package-scoped subdirectories under `backup/<package>/overwritten/` and `backup/<package>/deleted_files/` to protect pre-existing host files from silent loss.
> *   **Not Versioned or Tracked by Drift**: The `backup/` folder is intentionally unmanaged and untracked by Git in Drift.
> *   **User Responsibility**: It is solely the user's responsibility to periodically review `backup/`, archive important displaced configurations, or commit them into personal backup storage before cleaning.

### E. Execution Safeguards and Package Exclusion
To enable granular control over modular configurations, the deployment pipeline respects three cascading enablement switches across different execution phases:

#### 1. Global Activation Switch: `drift.toml [packages.enable]`
*   **Location**: Global workspace config (`config/drift.toml`).
*   **Affected Phase**: **Global Workspace Discovery**.
*   **How it works**: This table controls whether a package is active on this machine.
    *   If a package is set to `false` (or is unlisted while `DEFAULT = false` is active), the orchestrator completely ignores its directory.
    *   The package is skipped during *render*, *stage*, and *deploy* tasks if the package name is not explicitly mentioned in commands.
    *   **Self-Cleaning**: If a package was previously installed but is now toggled to `false` in this table, running a global `drift deploy` will automatically detect the orphan status and invoke **Primitive 9 (Workspace Garbage Collection)** to cleanly remove it from the system.

#### 2. Sandbox Compilation Switch: `drift_package.toml -> enable_render`
*   **Location**: Package configuration file (`src/<pkg>/drift_package.toml`).
*   **Affected Phase**: **Primitive 2: Render Packages** (Sandbox Rendering).
*   **How it works**: Controls whether templates inside `src/` are compiled via template engines when copying into `render/`.
    *   Defaults to `true`. If explicitly set to `false`, the rendering engine bypasses template compilation and copies all files directly into the `render/` sandbox as static assets.
    *   This allows static packages to proceed through staging (`install/`) and host deployment without executing template engines.

#### 3. State Promotion Switch: `drift_package.toml -> enable_install`
*   **Location**: Package configuration file (`src/<pkg>/drift_package.toml`).
*   **Affected Phase**: **Primitive 4: Stage Render to Install** (Staging Promotion).
*   **How it works**: Controls whether compiled files in the `render/` sandbox are promoted to the staging state database `install/` for eventual deployment to the system.
    *   Defaults to `true`. If set to `false`, the sync engine **completely skips copying its files from `render/` to `install/`**.
    *   This isolates the package's output files inside the local `render/` sandbox database, preventing them from registering in the state database or deploying onto the host target. This is ideal for testing rendering outputs in sandboxes before enabling active system installation.

### F. Orphan Package Garbage Collection & Uninstall Protection
To maintain parity between declarations and system states, the deployer enforces two robust policies:
1.  **Orphan Package Garbage Collection (Self-Cleaning)**:
    *   When executing a **Bulk All-Packages Deployment** (`drift deploy` with no targeted package), the system compares the state database `install/state.toml` with the active packages list in `config/drift.toml` (and respects `enable_install = false` in `drift_package.toml`).
    *   If a package is registered as `"installed"` in `install/state.toml`, but is **no longer active/enabled** in configuration declarations, the post-deployment GC step **automatically executes Primitive 7 (Uninstall) on this orphan package** during Stage 3.
    *   This ensures decommissioned packages are automatically and cleanly purged from the host system.
2.  **Uninstall Protection Safeguard**:
    *   If a user tries to manually uninstall a package (e.g. `drift uninstall proxychains`), but that package is **still active/enabled** inside `config/drift.toml` (and has `enable_install != false`), this represents a direct contradiction because the package would simply be re-installed on the next bulk deploy.
    *   In this case, the uninstaller will **halt and print an error**, instructing the user to first disable the package in declarations, **unless a `--force` flag is supplied**.

### G. Architectural Policy on Host Deletions & System Drift Adoption
If a configuration file, folder, or symlink is manually deleted, modified, or added by the user on the active system host target, the deployment pipeline executes the following reconciliation flow:

1.  **Reverse Sync Detection**:
    *   Stage 1's **Reverse Sync** detects that the file has been deleted, modified, or added on the target system and symmetrically syncs/mirrors the change inside the `install/` base folder (handling reverse prefix translations like `.` back to `dot-` automatically).
    *   This generates an uncommitted state inside the `install/` state database (e.g., `git -C install status` will list files as deleted, modified, or newly created). This is intentional; it reflects the real-world live configuration change and represents active host drift (Diff B).

2.  **Reconciling Deletions (Adopt vs. Discard/Restore)**:
    *   **Adopt (Persist the Deletion)**:
        *   If the user wants to permanently keep this deletion, they can delete the corresponding source template from the `src/` directory (or use `drift adopt <pkg>`).
        *   They can then commit the deletion inside the `install/` git repository, aligning the declarative source with the local state.
    *   **Discard (Restore the File / Acknowledge System Drift)**:
        *   If the user wants to reject the deletion and restore the file to the active host target, they must **commit the deletion inside the `install/` repository without changing anything in `src/`**.
        *   This commit serves as a formal **acknowledgement on system drift**, returning the `install/` repository status to clean/committed.
        *   Because the template file still exists in `src/`, running `drift deploy` (Stage 2) compiles the template into `render/`, sees that the compiled file is missing in the newly clean `install/` base (since we committed its deletion!), treats it as a brand-new **file addition**, stages it to `install/`, and deploys it back onto the system, perfectly restoring the missing resource!

3.  **Adopting Host-Side Modifications & New Additions**:
    When manual modifications or new file additions (within Fully-Controlled Directories or from type promotions) are reverse-synced back into `install/`:
    *   **To Adopt a File Modification**:
        1.  *Automated Adoption*: Run `drift adopt <package>` to programmatically apply unified patches onto template files in `src/` or launch interactive conflict resolution.
        2.  *Declarative Backport*: Preserves changes permanently in the source repository for future builds.
    *   **To Adopt a New File Addition**:
        1.  *Commit the Drift*: Register and commit the new resource inside the local `install/` state database.
        2.  *Declarative Alignment*: Copy the newly added file from `install/<package>/<path>` back into the matching folder under `src/<package>/<path>` (converting dot-prefixes to native names, and setting up template suffixes or config mappings if desired).

### H. State Registry Database (`install/state.toml`)
To safely determine whether a package should execute its `pre/post_install` or `pre/post_update` lifecycle hook, the system maintains a persistent, local-only state registry file at `install/state.toml`.
*   This registry tracks package lifecycle states:
    ```toml
    # install/state.toml
    [packages.nvim]
    state = "installed"
    last_deployed = "2026-08-16T21:10:50.123456"
    install_method = "stow"
    deployed_files = ["dot-config/nvim/init.lua", "dot-config/nvim/coc-settings.json"]

    [packages.qbittorrent]
    state = "staged"
    install_method = "copy"
    deployed_files = ["config.ini"]

    [packages.wezterm]
    state = "deploying"
    install_method = "stow"
    deployed_files = []
    ```
*   **Lifecycle States**:
    - **`"installed"`**: (Stable) The package is fully applied to the host system.
    - **`"staged"`**: (Stable) The package has been successfully staged from `render/` to `install/`, but not yet applied to the system.
    - **`"staging"`**: (Transient) The package is currently undergoing database synchronization (Primitive 4).
    - **`"deploying"`**: (Transient) The package is currently being physically applied to the system (Primitive 5).
*   **Safety Abort Logic**:
    When a package enters Primitive 4 or 5, the system checks its current state.
    - **Mid-Operation Safety Interlocks**: If the state is **`"staging"`** or **`"deploying"`**, and the `force` flag is not passed, the operation **aborts immediately**. This indicates a previous execution failed midway, leaving the database or system in an inconsistent state. The user is instructed to run `drift rollback` to restore integrity.
    - **Nesting and Scope Safety Checks**: 
      The target directory written in the configuration (`target_directory` or `default_target_directory`) cannot be inside or equal to the `drift` workspace root (`drift_root`). If the absolute target directory is inside or equal to the absolute workspace root, the operation **aborts immediately** with a `ValueError`. This protects the workspace from accidentally being polluted or recursively linked.
    - A package in **`"staged"`** state is allowed to proceed to deployment or be re-staged.
*   **Hook Classification**:
    When a package is about to be deployed:
    1.  The system reads `install/state.toml`.
    2.  If the package is **not listed** in the registry (or `last_deployed` is `None`), it is classified as a **First-Time Installation** (triggers `pre/post_install`).
    3.  If the package is **listed** with a recorded deployment timestamp, it is classified as an **Update/Redeploy** (triggers `pre/post_update`).
*   **Desired-State Manifest Tracking**:
    To ensure self-healing and robust deletion behavior during standalone executions, retries, or rollbacks without relying on event-driven stages (Primitive 4), the registry tracks the precise relative paths of all successfully deployed files under the `deployed_files` array.
    Upon each full redeployment, the engine compares the current desired files inside `install/<package>/` with the historical `deployed_files` manifest. Any orphaned files found in `deployed_files` but no longer present in `install/` are dynamically treated as delete instructions. They are safely backed up to `backup/<package>/deleted_files/` and surgically pruned from the active host system, ensuring zero file-leaks.

### I. Fully-Controlled Directories (FCD) Audit Mechanics & Ignore Reconciliation
A package can declare a list of subdirectories under `target_directory` as **Fully-Controlled Directories (FCD)** using the `fully_controlled_dirs` configuration array. These directories are designated as fully owned and managed by the workspace dotfiles package.

#### 1. FCD Reverse-Sync Sweep
During **Primitive 1: Reverse Sync** (Stage 1 deployment check), the engine recursively traverses the host's FCD subdirectories on disk.
*   Any wild, untracked, or newly created file found inside these directories on the host is automatically reverse-synchronized and copied back into the `install/` state database folder.
*   This places the workspace in an uncommitted state, signaling an active host configuration drift that must be reconciled.

#### 2. Bidirectional Reconciliation Flow (`drift adopt`)
Developers reconcile discovered untracked FCD additions using `drift adopt`, which executes the following underlying Git and ignore engine mechanics depending on the selection:
*   **Scoped Git Cleanliness Safeguard Check**: Prior to modifying any file inside `src/`, `drift adopt` verifies that the specific target package's source directory (`src/<package>/`) is completely Git clean. This ensures uncommitted draft changes in other active packages do not block the adoption workflow.

*   **Adopt (Keep in Repository)**:
    1.  *Source Copy*: The file is copied from `install/<package>/` to `src/<package>/` (reverting prefix translations like `.` back to `dot-` and preparing template configurations if desired).
    2.  *Commit*: The change is committed, merging the new file permanently into the declarative source repository.
*   **Ignore (Keep on System, Stop Tracking)**:
    1.  *State Unlink*: The untracked file is deleted from the `install/<package>/` state database, restoring state cleanliness.
    2.  *Ignore Registration*: The file's relative path pattern is appended to the package's `.drift_ignore` PCRE ignore configuration.
    3.  *The Result*: During all future `reverse-sync` sweeps, the ignore engine sees that the physical host file matches `.drift_ignore` and skips syncing it, allowing the untracked file to reside on the active host system without registering as database drift.
*   **Discard/Delete (Remove from System)**:
    1.  *State Indexing (`git add`)*: Instead of immediately unlinking, Drift stages and tracks the untracked file inside the local `install/` Git repository by executing `git -C install add <file_path>`.
    2.  *Staging Delta Promotion*: On the subsequent `drift deploy` (Stage 2) run, because the file is tracked in `install/` but is completely absent from the newly compiled `render/` sandbox output, the staging promotion compiler (`drift stage` / Primitive 4) automatically flags it as an **orphaned deletion** (tracked in state, but missing from compiled declarations).
    3.  *The Result*: A delete instruction is generated for this file, and during the physical deployment phase (`drift apply` / Primitive 5), it is symmetrically and cleanly deleted from the active host system, restoring pristine configuration baseline alignment!

---

## 7. Detailed Implementation Specifications

### Overview of Control Flow & Orchestration

The active configuration engine and orchestrator follow a strict sequence designed for predictability, transaction-like integrity, and extensive error recovery.

#### 1. Discovery and Registry Check
Deployment can be triggered in **Bulk Mode** (evaluating all declared active packages) or **Targeted Mode** (focusing on a specific package).
*   **Discovery**: The orchestrator checks workspace declarations in `config/drift.toml` to identify enabled packages, then verifies that `enable_install` is `true` in each package's `drift_package.toml`.
*   **Mid-Operation Registry Interlock**: The state database at `install/state.toml` is queried. If any package is currently in a `"staging"` or `"deploying"` state, execution is aborted unless the `--force` flag is supplied, preventing corruption from a previous midway failure.

#### 2. Stage 1: Alignment Safeguard (System -> Install)
*   The system executes **Primitive 1: Reverse Sync** on all target packages to capture and reconcile any manual, local modifications made directly on the active host system.
*   **The Reverse-Sync Reconciliation Flow**:
    - **Targeted Tracked Comparison**: Probes only package files in `install/<package>` on host (`src_only=True, translate_mode="forward"`), avoiding full `$HOME` directory traversals.
    - **System Deletions**: Tracked files manually deleted on the system are symmetrically removed from the `install/` state database folder.
    - **System Modifications & Type Changes**: Files edited on the system (or transformed into directories) are reverse-copied back to `install/` with dot-prefix translation (`.bashrc` $\rightarrow$ `dot-bashrc`).
    - **Scoped FCD Sync**: Any wild/untracked files inside configured **Fully-Controlled Directories (FCD)** are discovered and synced back via scoped subtree comparisons.
*   **Uncommitted State Check**: After performing reverse-sync on all active packages, the deployer checks if `git -C install status` is dirty. If uncommitted changes are detected (representing active host drift, i.e., Diff B), the deployer **halts immediately**. This acts as a security sentinel, forcing the developer to explicitly review the drift (via `drift diff --system`) and either **Adopt** (via `drift adopt`) or **Dismiss** (by deploying with `--force`) the system changes before template rendering can continue.

#### 3. Stage 2: Sandboxing & Reconciliation (Render -> Stage)
*   **Sandbox Render (Primitive 2)**:
    - **Global Pre-render Resolving**: Before compiling packages, all configured global template variables or input database configs (e.g. `mustache.envst.json` -> `mustache.json` using `envsubst`) are rendered.
    - **Rendering Execution Flow (`render_package.py`)**:
        1. *Sandbox Cleansing*: Clears any pre-existing package folder in `render/` using `shutil.rmtree` to maintain clean state while preserving the underlying `render/.git` repo.
        2. *Metadata Compiling*: Loads and compiles package config from the source folder (supporting on-the-fly parsing of `package.envst.toml` templates). If `enable_render` is false, rendering is skipped.
        3. *Pre-Source Lifecycle Hook*: Triggers the `pre_source` hook script (if defined) running with working directory set to `src/<package>` to dynamically generate/update source templates or dynamic system files prior to compilation.
        4. *Misspelled Ignore Warning*: Checks for a misspelled `.driftignore` and if found (without `.drift_ignore`), logs a warning and automatically copies it under correct name `.drift_ignore`.
        5. *Surgical File Walk*: Traverses the source package directory. Subdirectory ignore files (`.drift_ignore` or `.driftignore`) are blocked with errors. Static files are physically copied. Template files matching any active engine configuration suffix are surgically compiled (engine suffix is stripped from the rendered file name).
        6. *Post-Render Lifecycle Hook*: Triggers the `post_render` hook script (if defined) running with its working directory set to `render/<package>`.
    - **Sandbox Render Commit (Primitive 3)**: Automatically commits the sandbox changes inside the local `render/` repository to maintain a full history of declarative rendering.

*   **Staging Database (Primitive 4 - `stage_repo.py`)**:
    - **Installation Exclusions**: Skips any packages that declared `enable_install` as `false`.
    - **Staging Conflict Safeguard**: If any targeted package in the state database `install/` contains uncommitted local modifications, staging aborts immediately (unless `--force` is used).
    - **Staging Transaction Interlock**: Sets the package state to transient `"staging"` inside `state.toml` before any changes are written. If a package is found in `"staging"` or `"deploying"` state from a previous crash, staging is aborted.
    - **Reconciliation & Synchronization Pipeline**:
        1. *Deployable Changes Calculation*: Runs `compare_folders` with the package's `DriftIgnore` handler to calculate granular deployable changes (`PackageStageChanges`: `added_files`, `modified_files`, `deleted_files`) for the function return value and downstream physical deployment.
        2. *Physical Full-State Synchronization*: Runs `compare_folders` **without** ignore filtering (`ignore_handler=None`) to synchronize **all** physical files from `render/<package>` into `install/<package>` (deleting removed files, copying additions and modifications). This ensures that lifecycle hook scripts (e.g. `pre_install.sh`) and helper assets reside in `install/<package>` where they can be executed by Drift during installation.
        3. *Stow Ignore Generation*: Copies `.drift_ignore` and `drift_package.toml` to `install/<package>`. It automatically generates `.stow-local-ignore` inside `install/<package>`, appending exclusions for the primary `.drift_ignore` and package config file so GNU Stow never symlinks ignored files, hooks, or configurations to the active host.
    - **Staged Transaction Complete**: Updates the state registry database to stable `"staged"` and returns the list of `PackageStageChanges` containing only deployable file changes.

#### 4. Stage 2: Physical Deployment Sequence (Primitive 5)
For each redeployable package:
*   **Target Directory Check**: The engine verifies that the package's target directory is absolute and is not nested inside or equal to the workspace root.
*   **State Transition to `"deploying"`**: The state registry database `state.toml` is written to mark the package's state as `"deploying"`.
*   **Collision Guard (Incremental/Full)**:
    - *Symlinked Parent Pre-Check*: Traverses up the target path. If any parent directory is a symlink pointing into the workspace root, aborts immediately.
    - *Unified Audit*: Uses `compare_folders` to compare files in the `install/` base directory with the active target. Collisions are safely backed up to `backup/<package>/overwritten/` and removed from the active system to clear the path.
*   **Lifecycle Pre-Hook**: The package's `pre_install` (first-time install) or `pre_update` (subsequent update) executable script is triggered, running with its working directory set to `install/<package>`.
*   **File Delivery Phase**:
    - *Full Deployment Delivery*: If `package_changes` is `None` (representing a clean redeploy, rollback, or initial deploy):
        1.  *Orphan File Pruning*: Compares the current package files with the historical `deployed_files` manifest. Any orphaned paths are backed up and deleted from the target system.
        2.  *High-Level Delivery*: Invokes copying commands (using `rsync` if available, otherwise `cp -R`) or links packages via GNU Stow (stow version must be >= 2.4.1; falls back to manual file-by-file linking on older versions).
    - *Incremental Deployment Delivery*: If `package_changes` is provided (surgical deploy):
        1.  Deletes files listed in `package_changes.deleted_files`.
        2.  Deploys individual files manually using precise symlink creation or copy operations.
*   **Lifecycle Post-Hook**: Triggers `post_install` or `post_update` executable scripts, running with its working directory set to the package's target directory.
*   **State Registry Lock**: The state database is updated: the package's state is set to `"installed"`, a deployment timestamp is written, and the list of successfully deployed paths is saved to the `deployed_files` manifest inside `state.toml`.

#### 5. Stage 2: Final State Commit (Primitive 6)
*   The updated configurations and `state.toml` file are staged and committed into the local-only `install/` Git repository, locking the environment into a clean, reproducible state.

#### 6. Stage 3: Post-Deployment Workspace Garbage Collection (Primitive 9 - Bulk Mode Only)
*   **Workspace GC**: If a bulk deployment (all active packages) succeeds, the engine automatically triggers **Primitive 9: Workspace Garbage Collection**.
*   This uninstalls orphan packages (previously installed but now disabled in workspace config) and purges "zombie" directory folders in `render/` and `install/` which lack valid package configuration files, auto-committing the database cleanup to lock in a clean workspace environment.

---

## 8. Philosophical & Operational Benefits

By implementing this architecture, the user reaps distinct Unix-style benefits:
1.  **Strict Demarcation of Merges**: Automation is restricted to simple *reverse-syncing state* and *unilateral overwrite deployment*. The human developer remains the sole merge authority. If active system changes (Diff B) are dirty, the user is presented with standard Git outputs and handles the backport to templates manually (or via `drift adopt`).
2.  **No Hand-Crafted State Engine**: By designating `install/` and `render/` as local Git repositories, we avoid writing custom rollback, commit-tracking, and differential history features. Git manages the hard stuff (index, diffs, conflicts).
3.  **Complete, High-Fidelity Backups**: Deleted configurations and overwritten links are never silently purged; they are meticulously structured and swept into `backup/<package>/` with clean console reporting.
4.  **Extensible Lifecycles**: Adding complex validation or post-deployment logic is as simple as dropping a standard executable bash script in `src/<package>/pre-update.bash` or `post-install.bash`.
5.  **Structured Observability**: Built-in `--json` support across all primitives and CLI entry points allows agentic pairing, automation tools, and CI/CD pipelines to interact with strongly-typed result models.
