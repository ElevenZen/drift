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

## 3. The 11 Core Primitives

All high-level workflows in drift are composed of eleven atomic, sequential primitives:

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
Unconditionally pulls the current host configuration state into the `install/` state Git repository using the unified `compare_folders` tool in `"reverse"` translation mode (comparing the system target `target_directory` as source against the repository `install/<package>` folder as destination).

The comparison identifies three categories of change:
1.  **Deleted on System (`diff.deleted`)**:
    *   If a tracked file or directory exists in the repo but is missing from the active host system, it is symmetrically removed from the local state repository (`install/`).
2.  **Modified on System (`diff.modified`)**:
    *   If a file on the host system contains modifications or differs from its repository counterpart, it is reverse-copied back to the local state repository (`install/`).
3.  **Added on System (`diff.added`)**:
    *   New/untracked files or directories on the host system are only synced back to the local state repository (`install/`) under two conditions:
        - **Fully-Controlled Directory (FCD) Check:** The new file lies within one of the package's configured `fully_controlled_dirs`.
        - **Tracked Path Type Promotion/Change:** The new file resides at a path that was previously tracked but changed its type (e.g. parent path is reported in `diff.deleted` indicating a type promotion from file to directory or vice versa).

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

---

## 4. User-Facing Operations (CLI Overview)

The `drift` Python command provides a unified interface for all primitives and high-level workflows with `--json` machine-readable output support.

### High-Level Commands (Implemented)
*   **`drift init [--force] [--secrets/--no-secrets] [--json]`**: Initializes a new drift workspace.
*   **`drift new <package> [--force] [--target <dir>] [--method <stow|copy>] [--json]`**: Scaffolds a new dotfiles package (Primitive 10).
*   **`drift add <package> <paths...> [--dry-run] [--json]`**: Imports active system files into a declarative package (Primitive 11).
*   **`drift status [packages...] [--json]`**: Audits and aggregates the alignment of templates, system drift, and pending deployments.
*   **`drift diff [packages...] [--template] [--system] [--stat] [--side-by-side] [--json]`**: Visualizes changes between layers (Diff A, Diff B, or Diff Δ).
*   **`drift deploy [packages...] [--force] [--json]`**: Atomic Two-Stage deployment with Sentinel drift safety guards.
*   **`drift rollback [packages...] [--force] [--json]`**: Emergency recovery after midway failure (Primitive 8).
*   **`drift adopt [packages...] [--interactive] [--accept-conflicts] [--force] [--dry-run] [--json]`**: Reconciles system drift into templates.
*   **`drift uninstall <packages...> [--force] [--detach] [--dry-run] [--json]`**: Safely cleans or detaches a package from the system (Primitive 7).
*   **`drift gc [--dry-run] [--json]`**: Cleans orphan packages and purges zombie database directories (Primitive 9).
*   **`drift repair [--dry-run] [--json]`**: Audits and self-heals workspace structure, repositories, config templates, and secrets.
*   **`drift help [topic]`**: Interactive mini user manual with pager fallback support.

### Low-Level Control Commands
These commands are for advanced users or CI/CD pipelines to trigger specific primitives:
*   **`drift render [packages...] [--json]`**: Trigger Primitive 2 (Render).
*   **`drift render-commit [packages...] -m <msg> [--json]`**: Trigger Primitive 3 (Commit Render).
*   **`drift reverse-sync [packages...] [--json]`**: Trigger Primitive 1 (System $\rightarrow$ install/).
*   **`drift stage [packages...] [--force] [--json]`**: Trigger Primitive 4 (Staging).
*   **`drift apply [packages...] [--force] [--resolve-symlinks/--no-resolve-symlinks] [--json]`**: Trigger Primitive 5 (Physical Deployment).
*   **`drift install-commit [packages...] -m <msg> [--json]`**: Trigger Primitive 6 (Commit install/).

---

## 5. Workspace & Package Configuration

This section provides the essential syntax and specifications for global and package-level configurations.

### A. Global Workspace Configuration: `config/drift.toml` Specification
Rather than scanning the filesystem blindly, the drift engine relies on a centralized workspace configuration file located at `config/drift.toml` (which can itself be a template named `drift.envst.toml`). This file orchestrates two main responsibilities:
1. **Workspace Paths & Rendering Engines**: Defines directories (`source_directory`, `render_directory`, `install_directory`, `backup_directory`, `default_target_directory`, `default_install_method`) and template engines with their file suffixes and rendering subprocess commands (e.g. `envsubst`, `mustache`).
2. **Enabled Packages Registry**: Declares exactly which package subfolders under `src/` are globally active via the `[packages]` (or `[packages.enable]`) section.

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
# Both flat [packages] and nested [packages.enable] table formats are supported.
# Key: package folder name under src/
# Value: True/False to enable or disable the package globally
# Entry "DEFAULT = true | false" will set the default value for unlisted packages.
# "DEFAULT = false" is the default setting.
[packages]
DEFAULT = false
shell = true
nvim = true
qbittorrent = true
proxychains = false
```

#### Meta-Config Templating: `drift.envst.toml`
To allow complete bootstrapping of workspaces under different environment parameters, the main config file itself can be a template. By renaming `config/drift.toml` to `config/drift.envst.toml`, the system will compile it on-the-fly using `envsubst` populated with active system-level environment variables.
*   The generated output is safely rendered, loaded, and printed in the logs:
    ```
    # pseudocode when config/drift.toml does not exist and config/drift.envst.toml can be found.
    rendered_content = render_envsubst_string(template_content)
    logger.debug("Workspace config is rendered from template: config/drift.envst.toml")
    ```

#### Private Dotenv Vault: `config/secrets.env`
To isolate secret tokens, private API keys, and work-specific emails from public dotfiles repositories, Drift provides a secure, local-only, git-ignored Dotenv vault located at `config/secrets.env`.

1. **Strict Variable Precedence**:
   During template parsing and compiling, variables are resolved in a strict order of precedence (highest precedence overrides lower layers):
   - **Package Environment (`drift_package_name`, `drift_package_target_dir`, `drift_install_method`, etc.)**: Dynamic package attributes loaded via `PackageConfig.package_envs()`. **Package environment variables have the absolute highest precedence and override all other layers.**
   - **Secret Vault (`config/secrets.env`)**: Local, private settings and sensitive overrides loaded dynamically during rendering.
   - **Global Workspace Environment (`[env]` table in `drift.toml`)**: Shared, non-sensitive environment defaults.
   - **System Host / CLI Environment**: Base host-level environment variables (e.g. active shell exports in `os.environ`).

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
# Unique name identifying the package, default value is the package folder name.
# This is mainly used for logging. The package folder name will be used when locating files.
name = "example_package"

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
install_method = "stow"

# The physical path where this package should be deployed.
# Supports home expansion (~ at the beginning).
target_directory = "~/.config/example"

# If true, all physical file creation, copying, deletion, and symlinking operations
# for this package, as well as installation/update lifecycle hooks (pre/post_install, pre/post_update),
# will be executed utilizing "sudo" elevation.
# Note: Source/compilation hooks (pre_source, post_render) always run in user space without sudo.
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
# Run before reading/writing source package files (e.g. generating dynamic templates before render, adopt, or add, CWD: src/pkg). Always runs in user space without sudo.
pre_source = "pre-source.bash"

# Run after templates are rendered into sandbox (CWD: render/pkg). Always runs in user space without sudo.
post_render = "post-render.bash"

# Run before first-time installation (CWD: install/pkg). Runs with sudo if sudo = true.
pre_install = "pre-install.bash"

# Run after successful first-time installation (CWD: target_directory). Runs with sudo if sudo = true.
post_install = "post-install.bash"

# Run before any update/deployment (CWD: install/pkg). Runs with sudo if sudo = true.
pre_update = "pre-update.bash"

# Run after any successful update/deployment (CWD: target_directory). Runs with sudo if sudo = true.
post_update = "post-update.bash"

# Timeout in seconds for lifecycle hook script executions (Default: 120)
timeout = 120
```

#### Default Package Environment Variables & Precedence
After parsing a package's configuration, the drift engine dynamically loads package-specific environment variables into `os.environ` via `PackageConfig.load_package_envs(workspace_config)` (with `overwrite=True`):
*   **`drift_package_name`**: Name / directory name of the package.
*   **`drift_package_target_dir`**: Resolved absolute destination target directory path on the host system.
*   **`drift_package_source_dir`**: Absolute path to the package's source directory in the workspace (`<drift_root>/src/<pkg>`).
*   **`drift_package_render_dir`**: Absolute path to the package's compiled sandbox directory (`<drift_root>/render/<pkg>`).
*   **`drift_package_install_dir`**: Absolute path to the package's state database directory (`<drift_root>/install/<pkg>`).
*   **`drift_install_method`**: Resolved deployment method (`stow` or `copy`).

> [!IMPORTANT]
> **Environment Variable Precedence & Overrides**:
> Package environment variables have the **highest precedence** in Drift. When loaded, they strictly **override all other environment variables**, including:
> 1. Host shell / CLI environment variables (`os.environ`).
> 2. Global workspace environment variables defined in `config/drift.toml` (`[env]` table).
> 3. Secret variables loaded from `config/secrets.env`.
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
    *   **Single File Restriction**: Exactly **one** `.drift_ignore` (or `drift_ignore`) file is allowed at the root of each package directory. Nested subdirectory ignore files are strictly prohibited and will trigger execution aborts.
    *   **Syntax & Engine**:
        *   The `.drift_ignore` matches the exact syntax and matching rules used by GNU Stow's `.stow-local-ignore`.
        *   **No Globbing**: The ignore engine **does NOT use globbing**. Instead, it compiles and evaluates patterns as **PCRE Regular Expressions** (compiled in Python's `re` engine).
        *   **Comments and Blank Lines**: Lines starting with `#` are treated as comments and stripped (unless escaped as `\#`), and empty lines are bypassed.
    *   **Matching Algorithm**:
        *   *With Slashes*: If a pattern contains a forward slash `/`, it is evaluated against the complete relative path of the file prefixed with a forward slash (e.g. `/dot-config/coc-settings.json`).
        *   *Without Slashes*: If a pattern does not contain a slash, it is matched directly against the file's `basename` (e.g., `\.bak$`).
    *   **Match Timing Guard**: The ignore engine matches file patterns against the native repository filenames **before** any prefix conversion or suffix extraction takes place.
        *   *Important*: To ignore a file named `dot-bashrc`, your `.drift_ignore` file must list `dot-bashrc`, not `.bashrc`. Listing `.bashrc` will fail to match on disk, and the file will still be processed.
    *   **Implicit Exclusions**: Package configurations (`drift_package.toml`) are automatically excluded by the compilation engine without requiring manual entries.
    *   **Lifecycle Hook Script Coexistence**: Lifecycle hook scripts (such as `pre_install.sh`, `post_update.sh`) and helper build scripts can be listed in `.drift_ignore`. Drift stages these scripts into `install/<package>/` so they can be executed by Drift during lifecycle stages, while ensuring they are never deployed or symlinked onto the active host target filesystem.
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
1.  **Internal Symlinks (`diff.internal_symlinks`)**:
    *   Any path discovered inside the target directory that is a symlink pointing inside the workspace root (`drift_root`) represents severe repo pollution.
    *   The system backs up this symlink to `backup/<package>/overwritten/<path>`, removes the link, and (if the repository expects a physical directory at that path) recreates it as a physical directory to avoid infinite directory-reference loops.
2.  **Deleted files & Type Mismatches (`diff.deleted`)**:
    *   *Ignored Files*: If a file exists on the system at a path matched by `.drift_ignore` patterns, it acts as an active delete instruction. It is backed up to `backup/<package>/deleted_files/<path>` and removed from the active system.
    *   *Type Mismatches*: If the type of a target path on the host system differs from the repository (e.g., a physical regular file exists where the repo expects a folder), the system backs up the host item to `backup/<package>/overwritten/<path>` and removes it to clear the way.
3.  **Modified paths (`diff.modified`)**:
    *   *Stow Link Exemption*: If the package is deployed via `stow` and the target is already a symlink pointing to OUR package inside the `install/` base directory, it is a valid pre-existing link and is skipped.
    *   *Copy Mode Exemption*: If the package is deployed via `copy` and it is **not** a first-time installation (i.e. already registered as `"installed"` or `"staged"` in `state.toml`), collision backups are skipped; the target file is simply overwritten with updated contents.
    *   *Overwritten Backup*: Otherwise, the conflicting file/folder on the host is backed up to `backup/<package>/overwritten/<path>` and removed.
4.  **Matches (`diff.matches` under Stow mode)**:
    *   If a file matches the repo's content exactly but exists on the host as a **regular physical file** rather than a symlink, it is still treated as a collision because Stow requires symlinks. The system backs up the physical file to `backup/<package>/overwritten/<path>` and deletes it so the symlink can be created safely.

### E. Execution Safeguards and Package Exclusion
To enable granular control over modular configurations, the deployment pipeline respects three cascading enablement switches across different execution phases:

#### 1. Global Activation Switch: `drift.toml [packages]` (or `[packages.enable]`)
*   **Location**: Global workspace config (`config/drift.toml`).
*   **Affected Phase**: **Global Workspace Discovery**.
*   **How it works**: This table controls whether a package is active on this machine.
    *   If a package is set to `false` (or is unlisted while `DEFAULT = false` is active), the orchestrator completely ignores its directory.
    *   The package is skipped during *render*, *stage*, and *deploy* tasks if the package name is not explicitly mentioned in commands.
    *   **Self-Cleaning**: If a package was previously installed but is now toggled to `false` in this table, running a global `drift deploy` will automatically detect the orphan status and invoke **Primitive 9 (Workspace Garbage Collection)** to cleanly remove it from the system.

#### 2. Sandbox Compilation Switch: `drift_package.toml -> enable_render`
*   **Location**: Package configuration file (`src/<pkg>/drift_package.toml`).
*   **Affected Phase**: **Primitive 2: Render Packages** (Sandbox Rendering).
*   **How it works**: Controls whether templates inside `src/` are compiled and output into the `render/` sandbox directory.
    *   Defaults to `true`. If explicitly set to `false`, the rendering engine skips compiling the package directory entirely.
    *   This is useful for local static packages where no template processing is required and the developer wants to bypass rendering and installing completely.

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
    - **Folder Comparison**: The orchestrator triggers `compare_folders(translate_mode="reverse")` with the system target directory as the source and `install/<package>` as the destination.
    - **System Deletions (`diff.deleted`)**: Any files manually deleted on the system that are currently tracked by the repository are symmetrically pruned/removed from the `install/` state database folder.
    - **System Modifications (`diff.modified`)**: Any files manually edited on the system are reverse-copied back to their corresponding repository paths in `install/` (applying reverse dot-prefix translation so system `.bashrc` translates back to repo `dot-bashrc`).
    - **System Additions & Type Changes (`diff.added`)**: New files on the system are processed and synced back only if they reside inside configured **Fully-Controlled Directories (FCD)**, or if they represent a **Type Change / Type Promotion** (meaning the file path or one of its parent paths changed type, which is detected by checking if it appears in `diff.deleted`).
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
    - *Unified Audit*: Uses `compare_folders` to compare files in the `install/` base directory with the active target. Collisions are safely backed up to `backup/<package>/overwritten/` (or `deleted_files/` if matched by `.drift_ignore` patterns) and removed from the active system to clear the path.
*   **Lifecycle Pre-Hook**: The package's `pre_install` (first-time install) or `pre_update` (subsequent update) executable script is triggered, running with its working directory set to `install/<package>` (with `sudo` elevation if `sudo = true`).
*   **File Delivery Phase**:
    - *Full Deployment Delivery*: If `package_changes` is `None` (representing a clean redeploy, rollback, or initial deploy):
        1.  *Orphan File Pruning*: Compares the current package files with the historical `deployed_files` manifest. Any orphaned paths are backed up and deleted from the target system.
        2.  *High-Level Delivery*: Invokes copying commands (using `rsync` if available, otherwise `cp -R`) or links packages via GNU Stow (stow version must be >= 2.4.1; falls back to manual file-by-file linking on older versions).
    - *Incremental Deployment Delivery*: If `package_changes` is provided (surgical deploy):
        1.  Deletes files listed in `package_changes.deleted_files`.
        2.  Deploys individual files manually using precise symlink creation or copy operations.
*   **Lifecycle Post-Hook**: Triggers `post_install` or `post_update` executable scripts, running with its working directory set to the package's target directory (with `sudo` elevation if `sudo = true`).
*   **State Registry Lock**: The state database is updated: the package's state is set to `"installed"`, a deployment timestamp is written, and the list of successfully deployed paths is saved to the `deployed_files` manifest inside `state.toml`.

#### 5. Stage 2: Final State Commit (Primitive 6)
*   The updated configurations and `state.toml` file are staged and committed into the local-only `install/` Git repository, locking the environment into a clean, reproducible state.

#### 6. Stage 3: Post-Deployment Workspace Garbage Collection (Primitive 9 - Bulk Mode Only)
*   **Workspace GC**: If a bulk deployment (all active packages) succeeds, the engine automatically triggers **Primitive 9: Workspace Garbage Collection**.
*   This uninstalls orphan packages (previously installed but now disabled in workspace config) and purges "zombie" directory folders in `render/` and `install/` which lack valid package configuration files, auto-committing the database cleanup to lock in a clean workspace environment.

---

### Detailed Workflow Control Flow & Pseudocode  

#### 1. Command Orchestration & Deployment Control Flow

```python
def run_primitive_deploy_pipeline(workspace_config: WorkspaceConfig, packages_to_deploy: Optional[List[str]] = None, force: bool = False) -> DeployResult:
    # 0. Pre-flight checks: Verify git committability
    check_repo_can_commit(workspace_config.render_path)
    check_repo_can_commit(workspace_config.install_path)

    # Discover active packages
    target_pkgs = workspace_config.get_discovered_packages(
        custom_dir=workspace_config.source_path,
        target_pkgs=packages_to_deploy
    )
    if not target_pkgs:
        return DeployResult(
            command="deploy",
            status="SUCCESS",
            is_global_deploy=(packages_to_deploy is None),
            target_packages=[],
            deployed_packages=[]
        )

    # --- Stage 1: Sentinel Drift Auditing ---
    drifted_packages, drifted_files = check_and_prevent_system_drifts(workspace_config, target_pkgs, force=force)

    # --- Stage 2: Sequential Compile & Apply ---
    deployed_packages, completed_steps = execute_sequential_compile_and_apply(workspace_config, target_pkgs, force=force)

    # --- Stage 3: Post-Deployment Workspace Garbage Collection (Bulk mode only) ---
    gc_res = None
    if not packages_to_deploy:
        gc_res = run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=False)

    return DeployResult(
        command="deploy",
        status="SUCCESS",
        is_global_deploy=(packages_to_deploy is None),
        target_packages=target_pkgs,
        deployed_packages=deployed_packages,
        gc=gc_res,
        completed_steps=completed_steps
    )


def check_and_prevent_system_drifts(workspace_config: WorkspaceConfig, target_pkgs: List[str], force: bool = False) -> Tuple[List[str], List[str]]:
    # Run silent reverse-sync on existing packages
    syncable_pkgs = [pkg for pkg in target_pkgs if (workspace_config.install_path / pkg).is_dir()]
    if syncable_pkgs:
        run_primitive_1_reverse_sync(workspace_config, package_names=syncable_pkgs)

    drifted_packages = []
    drifted_files = []
    for pkg in syncable_pkgs:
        git_status = get_git_status_porcelain(workspace_config.install_path, f"{pkg}/")
        if git_status:
            drifted_packages.append(pkg)
            drifted_files.extend(git_status)

    if drifted_packages and not force:
        first_pkg = drifted_packages[0]
        raise RuntimeError(
            f"[DEPLOY ABORTED] System drift detected in package '{first_pkg}'!\n"
            f"Run 'drift diff -s {first_pkg}' to view modifications or 'drift adopt {first_pkg}' to incorporate them."
        )

    return drifted_packages, drifted_files


def execute_sequential_compile_and_apply(workspace_config: WorkspaceConfig, target_pkgs: List[str], force: bool = False) -> Tuple[List[PackageInstallResult], List[CompletedStep]]:
    completed_steps = []

    # Step 1: Render raw templates into sandbox render/
    run_primitive_2_render_packages(workspace_config, target_pkgs=target_pkgs)
    completed_steps.append(CompletedStep(1, "template_rendering"))

    # Step 2: Commit sandbox render repository
    pkgs_label = ", ".join(target_pkgs)
    run_primitive_3_commit_render_repo(workspace_config, f"Deploy Render: Automatically compile templates for {pkgs_label}", target_pkgs=target_pkgs)
    completed_steps.append(CompletedStep(2, "render_commit"))

    # Step 3: Stage render sandbox into install state database
    package_changes = run_primitive_4_stage_render_to_install(workspace_config, target_pkgs=target_pkgs, force=force)
    completed_steps.append(CompletedStep(3, "sandbox_staging"))

    # Step 4: Physical deployment delivery
    install_res = run_primitive_5_install_deployment(
        workspace_config,
        packages_to_redeploy=target_pkgs,
        resolve_symlinks=True,
        force=force,
        package_changes=package_changes
    )
    completed_steps.append(CompletedStep(4, "physical_install"))

    # Step 5: Commit install repository state
    run_primitive_6_commit_install_repo(workspace_config, f"Deploy Install: Automatically commit deployed changes for {pkgs_label}", target_pkgs=target_pkgs)
    completed_steps.append(CompletedStep(5, "install_commit"))

    return install_res.packages, completed_steps
```

---

#### 2. Primitive 1: Reverse Sync (Host $\rightarrow$ `install/`)

```python
def run_primitive_1_reverse_sync(workspace_config: WorkspaceConfig, package_names: Optional[List[str]] = None) -> ReverseSyncResult:
    install_base = workspace_config.install_path
    if not install_base.exists():
        return ReverseSyncResult(status="FAILED", error_message="Install state database directory does not exist.")

    discovered_packages = workspace_config.get_discovered_packages(custom_dir=install_base, target_pkgs=package_names)
    results = []

    for pkg in discovered_packages:
        metadata = load_config_for_install(install_base, pkg)
        if not metadata.enable_install:
            results.append(PackageReverseSyncResult(package=pkg, target_directory=str(metadata.get_target_directory(workspace_config)), status="SKIPPED"))
            continue

        target_dir_path = metadata.get_target_directory(workspace_config)
        if not target_dir_path.exists():
            results.append(PackageReverseSyncResult(package=pkg, target_directory=str(target_dir_path), status="SKIPPED"))
            continue

        install_pkg_dir = install_base / pkg
        ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

        # Unified Reverse Folder Comparison
        diff = compare_folders(
            src_dir=target_dir_path,
            dst_dir=install_pkg_dir,
            ignore_handler=ignore_handler,
            resolve_symlinks=True,
            translate_mode="reverse"
        )

        drifted_files = []
        synced_files = []

        # 1. Handle system deletions
        for rel in diff.deleted:
            repo_rel = translate_dot_prefixes_reverse(rel)
            repo_file = install_pkg_dir / repo_rel
            if repo_file.exists():
                remove_file_or_dir(repo_file)
                drifted_files.append(str(rel))
                synced_files.append(str(repo_rel))

        # 2. Handle system modifications
        for rel in diff.modified:
            drifted_str, synced_str = sync_file_to_install(rel, target_dir_path, install_pkg_dir, ignore_handler)
            drifted_files.append(drifted_str)
            synced_files.append(synced_str)

        # 3. Handle wild files (FCD) or promoted tracked files
        added_to_sync = filter_added_files_to_sync(diff.added, diff.deleted, metadata.fully_controlled_dirs)
        for rel in added_to_sync:
            drifted_str, synced_str = sync_file_to_install(rel, target_dir_path, install_pkg_dir, ignore_handler)
            drifted_files.append(drifted_str)
            synced_files.append(synced_str)

        results.append(PackageReverseSyncResult(
            package=pkg,
            target_directory=str(target_dir_path),
            drifted_files=drifted_files,
            synced_files=synced_files,
            status="SUCCESS"
        ))

    return ReverseSyncResult(status="SUCCESS", packages=results)
```

---

#### 3. Primitives 4, 5, 6, 7 & 8: Reconcile, Deploy, Commit, Uninstall & Rollback

```python
def run_primitive_4_stage_render_to_install(
    workspace_config: WorkspaceConfig,
    target_pkgs: Optional[List[str]] = None,
    force: bool = False
) -> List[PackageStageChanges]:
    """Reconciles sandbox render/ into install/ state database (Primitive 4). Does NOT touch host files."""
    install_base = workspace_config.install_path
    render_base = workspace_config.render_path
    state_file = install_base / "state.toml"
    state_registry = load_state_registry(state_file)

    discovered_packages = workspace_config.get_discovered_packages(custom_dir=render_base, target_pkgs=target_pkgs)
    stage_changes = []

    for pkg in discovered_packages:
        pkg_metadata = load_config_for_install(render_base, pkg)
        if not (force or pkg_metadata.enable_install):
            continue

        current_state = state_registry.get_package_state(pkg)
        if not force and current_state in ("staging", "deploying"):
            raise RuntimeError(f"Safety Abort: Package '{pkg}' is currently in '{current_state}' state. Run 'drift rollback {pkg}'.")

        state_registry.set_package_state(pkg, "staging", install_method=pkg_metadata.get_install_method(workspace_config))
        save_state_registry(state_file, state_registry)

        render_pkg_dir = render_base / pkg
        install_pkg_dir = install_base / pkg
        backup_pkg_dir = workspace_config.backup_path / pkg / "deleted_files"

        ignore_handler = DriftIgnore.load_from_dir(render_pkg_dir)
        diff = compare_folders(render_pkg_dir, install_pkg_dir, ignore_handler=ignore_handler, resolve_symlinks=True)

        # 1. Process Deletions: Move deleted files from install/ to backup/
        deleted_list = []
        for rel in diff.deleted:
            if rel.name in MANAGED_CONFIG_FILES:
                continue
            src_file = install_pkg_dir / rel
            dst_file = backup_pkg_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_file, dst_file)
            deleted_list.append(rel)

        # 2. Process Additions and Modifications: Copy from render/ to install/
        added_list = []
        modified_list = []
        for rel in diff.added:
            dest = install_pkg_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(render_pkg_dir / rel, dest)
            added_list.append(rel)

        for rel in diff.modified:
            dest = install_pkg_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(render_pkg_dir / rel, dest)
            modified_list.append(rel)

        changes = PackageStageChanges(
            package_name=pkg,
            added_files=added_list,
            modified_files=modified_list,
            deleted_files=deleted_list
        )
        stage_changes.append(changes)

        state_registry.set_package_state(pkg, "staged", install_method=pkg_metadata.get_install_method(workspace_config))
        save_state_registry(state_file, state_registry)

    return stage_changes


def deploy_package_impl(
    workspace_config: WorkspaceConfig,
    pkg: str,
    state_registry: StateRegistry,
    state_file: Path,
    resolve_symlinks: bool,
    force: bool,
    package_changes: Optional[PackageStageChanges] = None
) -> PackageInstallResult:
    """Core function to physically deploy a single package configuration."""
    install_base = workspace_config.install_path
    metadata = load_config_for_install(install_base, pkg)

    if not (force or metadata.enable_install):
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(metadata.get_target_directory(workspace_config)),
            status="SKIPPED",
            error="enable_install is False"
        )

    target_dir = metadata.get_target_directory(workspace_config)
    abs_target = target_dir.absolute()
    abs_drift_root = workspace_config.drift_root.absolute()
    if abs_target == abs_drift_root or is_relative_to(abs_target, abs_drift_root):
        raise ValueError(f"Safety Abort: The target directory written in config '{target_dir}' cannot be inside or equal to drift workspace root.")

    ensure_directory_writable(target_dir, metadata.sudo)

    current_state = state_registry.get_package_state(pkg)
    if not force and current_state in ("staging", "deploying"):
        raise RuntimeError(f"Safety Abort: Package '{pkg}' is currently in '{current_state}' state. Run 'drift rollback {pkg}'.")

    pkg_state = state_registry.packages.get(pkg)
    is_first_time = (pkg_state is None or pkg_state.last_deployed is None)

    state_registry.set_package_state(pkg, "deploying", install_method=metadata.get_install_method(workspace_config))
    save_state_registry(state_file, state_registry)

    install_pkg_dir = install_base / pkg
    if not install_pkg_dir.is_dir():
        return PackageInstallResult(
            package=pkg,
            install_method=metadata.get_install_method(workspace_config),
            target_directory=str(target_dir),
            status="SKIPPED",
            error=f"Package installation directory '{install_pkg_dir}' does not exist."
        )

    ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

    # 1. Physical Collision Guard Pre-Deployment Audit
    run_collision_guard(
        workspace_config=workspace_config,
        pkg=pkg,
        install_pkg_dir=install_pkg_dir,
        metadata=metadata,
        ignore_handler=ignore_handler,
        target_dir=target_dir,
        is_first_time=is_first_time,
        resolve_symlinks=resolve_symlinks,
        install_base=install_base
    )

    full_redeploy = (package_changes is None)
    current_files = ignore_handler.filter_deployable_files(install_pkg_dir)

    if full_redeploy:
        reconcile_orphaned_files(
            pkg=pkg,
            target_dir=target_dir,
            current_files=current_files,
            state_registry=state_registry,
            workspace_config=workspace_config,
            metadata=metadata,
            resolve_symlinks=resolve_symlinks
        )

    # 2. Trigger Pre-deployment Lifecycle Hook (CWD: install_pkg_dir)
    if is_first_time:
        metadata.hooks.trigger_pre_install(install_pkg_dir, install_pkg_dir)
    else:
        metadata.hooks.trigger_pre_update(install_pkg_dir, install_pkg_dir)

    # 3. Physical File Delivery
    stow_version = get_stow_version() if metadata.get_install_method(workspace_config) == "stow" else None
    stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False

    if full_redeploy:
        run_full_file_delivery(
            workspace_config=workspace_config,
            pkg=pkg,
            install_base=install_base,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata,
            deployable_files=current_files,
            stow_sufficient=stow_sufficient
        )
    else:
        run_incremental_file_delivery(
            workspace_config=workspace_config,
            package_changes=package_changes,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata
        )

    # 4. Trigger Post-deployment Lifecycle Hook (CWD: target_dir)
    if is_first_time:
        metadata.hooks.trigger_post_install(install_pkg_dir, target_dir)
    else:
        metadata.hooks.trigger_post_update(install_pkg_dir, target_dir)

    # 5. Lock Final State and Manifest
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.get_install_method(workspace_config))

    if full_redeploy:
        state_registry.set_package_deployed_files(pkg, current_files)
    else:
        new_deployed = set(state_registry.get_package_deployed_files(pkg))
        if package_changes:
            for rel in package_changes.deleted_files:
                new_deployed.discard(rel)
            for rel in package_changes.added_files:
                new_deployed.add(rel)
        state_registry.set_package_deployed_files(pkg, sorted(list(new_deployed)))

    save_state_registry(state_file, state_registry)

    ops = FileOperations()
    if package_changes is not None:
        ops.added = [str(p) for p in package_changes.added_files]
        ops.modified = [str(p) for p in package_changes.modified_files]
        ops.deleted = [str(p) for p in package_changes.deleted_files]
    else:
        ops.added = [str(p) for p in current_files]

    return PackageInstallResult(
        package=pkg,
        install_method=metadata.get_install_method(workspace_config),
        target_directory=str(target_dir),
        operations=ops,
        is_first_time=is_first_time,
        status="SUCCESS"
    )


def run_primitive_7_uninstall_packages(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    force: bool = False,
    detach: bool = False,
    dry_run: bool = False
) -> UninstallResult:
    """Removes or detaches packages from the system (Primitive 7)."""
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    registry = load_state_registry(state_file)

    safe_map, rejected_pkgs = filter_uninstallable_packages(workspace_config, registry, package_names, force=force)
    if rejected_pkgs and not force:
        raise RuntimeError(f"Safeguard abort: Package(s) {', '.join(rejected_pkgs)} are active.")

    package_results = []
    successfully_uninstalled = []

    for pkg, pkg_state in safe_map.items():
        target_dir, sudo = get_uninstall_metadata(workspace_config, pkg)
        if uninstall_single_package(workspace_config, pkg, pkg_state, dry_run=dry_run, detach=detach):
            if not dry_run:
                registry.remove_package(pkg)
                successfully_uninstalled.append(pkg)
            package_results.append(PackageUninstallResult(
                package=pkg,
                install_method=pkg_state.install_method or "stow",
                target_directory=str(target_dir),
                detach_mode=detach,
                removed_files=list(pkg_state.deployed_files) if not detach else [],
                converted_symlinks=list(pkg_state.deployed_files) if detach else [],
                status="SUCCESS"
            ))

    if dry_run:
        return UninstallResult(status="SUCCESS", detach_mode=detach, packages=package_results)

    save_state_registry(state_file, registry)

    if successfully_uninstalled:
        action_name = "Detach" if detach else "Uninstall"
        commit_msg = f"{action_name}: Removed package(s) {', '.join(successfully_uninstalled)}"
        run_primitive_6_commit_install_repo(workspace_config, commit_msg, successfully_uninstalled)

    return UninstallResult(status="SUCCESS", detach_mode=detach, packages=package_results)


def run_primitive_8_rollback_recovery(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    force: bool = False
) -> RollbackResult:
    """Restores system and state database to the last clean committed deployment state (Primitive 8)."""
    install_base = workspace_config.install_path
    state_file = install_base / "state.toml"
    registry = load_state_registry(state_file)

    if not force:
        has_conflict = any(s.state in ("staging", "deploying") for s in registry.packages.values())
        if not has_conflict:
            raise RuntimeError("Safeguard abort: No package is in a conflict state ('staging' or 'deploying'). Use --force to proceed.")

    # 1. Reset state.toml from HEAD
    run_command(["git", "checkout", "HEAD", "--", "state.toml"], cwd=install_base)

    # 2. Reset package directories from HEAD and clean untracked files
    target_pkgs = package_names if package_names is not None else list(registry.packages.keys())
    for pkg in target_pkgs:
        if (install_base / pkg).exists():
            run_command(["git", "checkout", "HEAD", "--", pkg], cwd=install_base)
            run_command(["git", "clean", "-fd", pkg], cwd=install_base)

    # 3. Trigger full redeploy fallback
    run_primitive_5_install_deployment(
        workspace_config,
        packages_to_redeploy=package_names,
        resolve_symlinks=True,
        force=True
    )

    # 4. Restore state registry package status to "installed"
    restored_registry = load_state_registry(state_file)
    for pkg in target_pkgs:
        if pkg in restored_registry.packages:
            restored_registry.set_package_state(pkg, "installed")
    save_state_registry(state_file, restored_registry)

    return RollbackResult(command="rollback", status="SUCCESS")
```

---

## 8. Philosophical & Operational Benefits

By implementing this architecture, the user reaps distinct Unix-style benefits:
1.  **Strict Demarcation of Merges**: Automation is restricted to simple *reverse-syncing state* and *unilateral overwrite deployment*. The human developer remains the sole merge authority. If active system changes (Diff B) are dirty, the user is presented with standard Git outputs and handles the backport to templates manually (or via `drift adopt`).
2.  **No Hand-Crafted State Engine**: By designating `install/` and `render/` as local Git repositories, we avoid writing custom rollback, commit-tracking, and differential history features. Git manages the hard stuff (index, diffs, conflicts).
3.  **Complete, High-Fidelity Backups**: Deleted configurations and overwritten links are never silently purged; they are meticulously structured and swept into `backup/<package>/` with clean console reporting.
4.  **Extensible Lifecycles**: Adding complex validation or post-deployment logic is as simple as dropping a standard executable bash script in `src/<package>/pre-update.bash` or `post-install.bash`.
5.  **Structured Observability**: Built-in `--json` support across all primitives and CLI entry points allows agentic pairing, automation tools, and CI/CD pipelines to interact with strongly-typed result models.
