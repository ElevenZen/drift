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

By separating rendering from deployment and turning both folders into Git databases, we gain absolute, safe visibility over configurations without automated, dangerous, or unintended merges on the live system. It natively supports both **`stow` (symlink-based)** and **`copy` (copy-based)** deployment methods, with built-in privileges management (`sudo`) and granular package-level overrides.

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
2.  **Verify Evolution (`drift diff --template nvim`)**:
    *   Renders your edits into the `render/` sandbox.
    *   It prints **Diff A**, showing you exactly how your templates evolved.
3.  **Dry-Run check (`drift diff nvim`)**:
    *   You see exactly what changes will be applied to the system.
4.  **Deploy (`drift deploy nvim`)**:
    *   You run the deployment sequence. Since your live system hasn't drifted, Stage 1 completes with a "Clean Slate" status, and Stage 2 runs to instantly apply your new templates to the active environment.

#### Workflow 2: Auditing GUI & Runtime System Drifts (The Drift Audit)
A program (like qBittorrent or a terminal theme tool) has rewritten its configuration file in the background, or you modified a file in your home directory directly to test a setting.
1.  **Audit Drift (`drift diff --system qbittorrent`)**:
    *   Pull active system drift back into `install/` and shows you exactly what changes were introduced.
2.  **Manual Reconciliation**:
    *   If you want to **Adopt** these changes: You incorporate modifications from `install/` back to your declarative templates under `src/`.
    *   If you want to **Dismiss** these changes: You simply run `drift deploy`. Stage 1 will detect the drift and abort. You can then force-deploy (`--force`) to overwrite the drift.

#### Workflow 3: Full Recovery (The Rollback Loop)
A deployment failed midway due to a permission error, or manual system edits corrupted a config directory.
1.  **Rollback (`drift rollback nvim`)**:
    *   Reverts the `install/` database to the last successfully committed deployment commit, then triggers a **Full Package Redeploy**, restoring configurations to a known-clean state.

#### Workflow 4: Uninstallation (The Uninstall Loop)
You no longer want a package active on this machine.
1.  **Uninstall (`drift uninstall proxychains`)**:
    *   Safely removes all symlinks or copied files from the live system.
    *   Restores any original files backed up under `backup/` to their original paths.
    *   Updates the `install/state.toml` to clean the package state.

---

## 3. The 9 Core Primitives

All high-level workflows in drift are composed of these nine atomic, sequential primitives:

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
           - Require manual commit              │ 2. Render                     │
                                                │ 3. Render Repo Commit         │
                                                │ 4. Stage Render to Install    │
                                                │ 5. Install Repo Deployment    │
                                                │ 6. Install Repo Commit        │
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
Processes files in `src/` (expanding templates via `envsubst`/`mustache`) and places the results in `render/`. No live system files are altered. Triggers the `post_render` hook upon completion.

### Primitive 3: Render Repo Commit [Low-level: `drift render-commit`]
Automatically commits any updates inside the `render/` sandbox Git repository.

### Primitive 4: Stage Render to Install [Low-level: `drift stage`]
Reconciles the sandbox `render/` folder into the `install/` database. 
*   **Mechanism**: Computes exactly which files and packages require redeployment.
*   **State Machine**: Sets the package state to **`"staging"`** (transient guard) at the start, and transitions to **`"staged"`** (stable mid-state) upon successful completion. This indicates the database is ready but the system is not yet updated.

### Primitive 5: Install Repo Deployment [Low-level: `drift apply`]
Applies changes to the physical active system.
*   **Collision Guard**: Backs up colliding physical files to `backup/`.
*   **Hooks**: Triggers `pre_install` / `pre_update` before deployment, and `post_install` / `post_update` after successful deployment.
*   **State Machine**: Sets the package state to **`"deploying"`** (transient guard) at the start, and transitions to **`"installed"`** (final state) upon successful completion.
*   **Stow Mode**: Executes individual manual symlinks (Incremental) or runs GNU Stow (Full Deploy).
*   **Copy Mode**: Copies files to `target_directory` (prefixed with `sudo` if configured).

### Primitive 6: Install Repo Commit [Low-level: `drift install-commit`]
Locks the deployed configurations into the local state database with an automated commit.

### Primitive 7: Uninstall Repo Package [High-level: `drift uninstall`]
Removes a package from the system:
1.  **De-stow or Delete**: Unlinks symlinks or deletes physical files.
2.  **Rollback Collision Guard**: Restores original host files backed up in `backup/`.
3.  **Update Registry**: Removes the package from the state database.

### Primitive 8: Rollback Recovery [High-level: `drift rollback`]
Restores the system configuration and the local state database to the last known-clean, committed state after a midway failure.

### Primitive 9: Workspace Garbage Collection [Low-level: `drift gc`]
Identifies and cleans up workspace anomalies, orphaned packages, and zombie database directories:
1.  **Orphan Package Uninstallation**: Automates uninstallation for packages that are registered as `"installed"` in `state.toml` but are no longer enabled/active in `drift.toml`.
2.  **Zombie Folder Purge**: Scans `render/` and `install/` base directories, identifying and purging any subdirectories that do not contain a valid package configuration file (like `drift_package.toml` or `package.toml`), which prevents database pollution from historical directories.
3.  **Auto-Commit Database changes**: Auto-stages and commits zombie removal operations inside `render/` and `install/` databases.

---

## 4. User-Facing Operations (CLI Overview)

The `drift` Python command provides a unified interface for all primitives and high-level workflows.

### High-Level Commands (Planned)
*   **`drift status [packages...]`**: Audits and aggregates the alignment of templates, system drift, and pending deployments.
*   **`drift diff [packages...]`**: Visualizes changes between layers (Diff A, B, or C).
*   **`drift deploy [packages...]`**: Atomic Two-Stage deployment with safety guards.
*   **`drift rollback [packages...]`**: Emergency recovery after midway failure.
*   **`drift uninstall <package>`**: Safely cleans a package from the system.

### Low-Level Control Commands
These commands are for advanced users or CI/CD pipelines to trigger specific primitives:
*   **`drift init`**: Initialize a new drift workspace.
*   **`drift render [packages...]`**: Trigger Primitive 2 (Render).
*   **`drift render-commit [packages...] -m <msg>`**: Trigger Primitive 3 (Commit Render).
*   **`drift reverse-sync [packages...]`**: Trigger Primitive 1 (System $\rightarrow$ install/).
*   **`drift stage [packages...]`**: Trigger Primitive 4 (Staging).
*   **`drift apply [packages...]`**: Trigger Primitive 5 (Physical Deployment).
*   **`drift install-commit [packages...] -m <msg>`**: Trigger Primitive 6 (Commit install/).

---

## 5. Recovery & Edge Case Safeguards

This section defines the core architectural policies required to maintain technical integrity under edge cases and partial execution failures.

### A. State Registry Database (`install/state.toml`)
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
      The target directory written in the configuration (`target_directory` or `default_target_path`) cannot be inside or equal to the `drift` workspace root (`drift_root`). If the absolute target directory is inside or equal to the absolute workspace root, the operation **aborts immediately** with a `ValueError`. This protects the workspace from accidentally being polluted or recursively linked.
    - A package in **`"staged"`** state is allowed to proceed to deployment or be re-staged.
*   **Hook Classification**:
    When a package is about to be deployed:
    1.  The system reads `install/state.toml`.
    2.  If the package is **not listed** in the registry, it is classified as a **First-Time Installation** (triggers `pre/post_install`).
    3.  If the package is **listed** (even as `"staged"` or `"installed"`), it is classified as an **Update/Redeploy** (triggers `pre/post_update`).
*   **Desired-State Manifest Tracking**:
    To ensure self-healing and robust deletion behavior during standalone executions, retries, or rollbacks without relying on event-driven stages (Primitive 4), the registry tracks the precise relative paths of all successfully deployed files under the `deployed_files` array.
    Upon each full redeployment, the engine compares the current desired files inside `install/<package>/` with the historical `deployed_files` manifest. Any orphaned files found in `deployed_files` but no longer present in `install/` are dynamically treated as delete instructions. They are safely backed up to `backup/<package>/deleted_files/` and surgically pruned from the active host system, ensuring zero file-leaks.

### B. Physical Conflict Prevention (Collision Guard)
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

### C. Incremental vs. Full Deployment Strategies
To minimize system disruption and application reloads, deployment is executed under two distinct strategies:
1.  **Incremental Deployment (Surgical File-by-File)**:
    *   When executing a contiguous `make deploy` sequence, Primitive 4 outputs a granular list of added, modified, or deleted files.
    *   In both `stow` and `copy` modes, the deployment engine **avoids invoking external command-line tools** (like the `stow` binary or heavy shell directories copiers).
    *   Instead, it surgically iterates through the computed file list, creating individual symlinks (or copying individual files) manually. This maintains a minimal interruption footprint and prevents bulk reload signals to running processes.
    *   **Infinite Loop Protection**: In `stow` mode, before creating any symlink `~/<path>`, the incremental deployer traverses up the directory path from the parent. If it discovers any parent directory (e.g. `~/.config/nvim/`) is **already a symlink** pointing into `install/`, it **must immediately and safely skip** creating individual symlinks inside that directory. This prevents creating self-referencing circular symlink loops inside the local database.
2.  **Full Deployment (Heavy-Duty Fallback)**:
    *   When triggering a standalone deployment, running `make rollback`, or performing a first-time setup, the system defaults to a robust **Full Package Redeploy**.
    *   It utilizes high-level automated commands:
        *   *Stow Packages*: Invokes GNU Stow with flags **always set to**: `stow --no-folding --dotfiles -t <target_directory> <package>`, prefixed with `sudo` if configured.  
        TODO: If stow >= 2.4.1 (which fix --dotfiles problems with stow ignore) is not found, we should symlink file by file in the package folder, and print a warning at the end.
        *   *Copy Packages*: Invokes copying commands (like `rsync -av` or `cp -r` prefixed with `sudo` if configured. Use `rsync` first, if not available, fallback to `cp`) **without using `--delete`** (avoiding deleting unrelated files inside target directories). Any wild-file pruning is strictly scoped and handled during Primitive 1.  

The program needs to ensure the compatibility between these two ways. In either way, the program needs to check the package config has 'enable_install=true' and load the install method, install location, sudo flag from it.

### D. Ignored Files and Name Conversion Rules
Both `stow` and `copy` deployment strategies must natively respect ignore files and name transformation specifications:
1.  **Ignore Filter (`.drift_ignore`)**:
    *   The system parses `.drift_ignore` at the root of each package directory.
    *   Any file matching the ignore patterns (such as `package.toml` or helper files) is completely skipped during render and deployment.
    *   An extra `.stow-local-ignore` must be generated at the root of the `install/` directory to prevent GNU Stow from parsing the internal database file `state.toml` as a package.
2.  **Prefix Conversion (`dot-` to `.`)**:
    *   To allow developers to easily manage hidden folders in standard git environments, folders and files starting with the prefix `dot-` inside `install/` must be translated to a dot `.` prefix at deployment target paths.
    *   *Example*: `install/shell/dot-bashrc` translates to `~/.bashrc`.
    *   *Example*: `install/nvim/dot-config/nvim/` translates to `~/.config/nvim/`.
    *   This translation is enforced symmetrically across both `stow` and `copy` installation methods.

### E. Active Package Discovery (`config/drift.toml`)
Rather than scanning the entire `src/` directory, the system reads an explicit active packages registry from `config/drift.toml`. Only packages listed as enabled in this file are evaluated.
*   *Example*:
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

    [render.envsubst]
    # Shell script providing env variables for envsubst
    # If it's a relative path, it's always relative to the 'config' folder under of working directory.
    # The file is located at "config/envsubst.bash" .
    input_file = "envsubst.bash"

    # Files with name "file.envst.suffix" or "file.envst" will be rendered using envsubst.
    suffix = "envst"

    # The output of render_command will be written as render result.
    # %i means engine input, %s means source template.
    render_command = "bash -c 'source %i && envsubst < %s'"

    [render.mustache]
    # Json file as the input to mustache template render engine.
    # This filename ends with "envst.json", so it need to be rendered with envsubst first to get the actual json file.
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
    shell = true
    nvim = true
    qbittorrent = true
    proxychains = false
    ```

Input files of every used render engine should be stated clearly in this configuration. Corresponding render engine will be disabled if its input file is not stated in `drift.toml` file or cannot be read. If any file in `src/` relies on that render engine, a render failure will occur.  
Render engine input file can be a template of another render engine, only one level of rendering is allowed. The name "file.<engine1>.suffix" is allowed, this input template file will be rendered using engine 1. The name "file.<engine1>.<engine2>.suffix" for engine 3 won't work, only engine 2 rendering will be applied, and "file.<engine1>.suffix" will be used as final input file to engine 3.  
Rendered outputs will be stored at `render/config/file.suffix` .  
So, what can be done if we need a multi-level rendering? A clean dependency hierarchy should be maintained, input files of powerful render engines can be rendered by simple ones.  
User MUST NOT create cyclic dependency in render engine inputs. Dependency error will be thrown before anything being rendered.

This `drift.toml` file can also be a template. You can rename `drift.toml` to `drift.envst.toml` to enable this meta rendering. But only one level of envsubst will be used. The input of envsubst is the env variables when invoking this application. The rendered result will be stored in a temporary file with path printed out.

```
# pseudocode when config/drift.toml does not exist and config/drift.envst.toml can be found.
tempfile=$(mktemp)
envsubst < config/drift.envst.toml > $tempfile
echo "Workspace config is loaded from: $tempfile"
```

### F. Execution Safeguards and Package Exclusion
*   **The `enable_install = false` Block**:
    *   If a package's `package.toml` declares `enable_install = false`, the sync engine **completely skips copying its files from `render/` to `install/`**.
    *   This ensures the package remains purely in the sandbox and has no presence inside the local state database, preventing any accidental system deployments.

### G. Orphan Package Garbage Collection & Uninstall Protection
To maintain parity between declarations and system states, the deployer enforces two robust policies:
1.  **Orphan Package Garbage Collection (Self-Cleaning)**:
    *   When executing a **Bulk All-Packages Deployment** (`make deploy` with no targeted package), the system compares the state database `install/state.toml` with the active packages list in `config/drift.toml` (and respects `enable_install = false` in `package.toml`).
    *   If a package is registered as `"installed"` in `install/state.toml`, but is **no longer active/enabled** in configuration declarations, the deployment script **automatically executes Primitive 7 (Uninstall) on this orphan package** before initiating Stage 2 deployment.
    *   This ensures decommissioned packages are automatically and cleanly purged from the host system.
2.  **Uninstall Protection Safeguard**:
    *   If a user tries to manually uninstall a package (e.g. `make uninstall package=proxychains`), but that package is **still active/enabled** inside `config/drift.toml` (and has `enable_install != false`), this represents a direct contradiction because the package would simply be re-installed on the next bulk deploy.
    *   In this case, the uninstaller will **halt and print an error**, instructing the user to first disable the package in declarations, **unless a `--force` flag is supplied**.

### H. Architectural Policy on Host Deletions
If a configuration file or symlink is manually deleted by the user on the active system target:
*   Stage 1's `Reverse Sync` detects the deletion and symmetrically removes the file counterpart inside `install/`.
*   This generates an uncommitted deletion state (`git -C install status` will show the file as deleted). This is **intentional behavior**: the system's current live state (deletion) is faithfully tracked by Diff B.
*   **Reconciling Deletion (Adopt vs. Dismiss)**:
    *   *Adopt*: To persist the deletion declaratively, the user must update their `src/` templates to remove the file, commit the deletion inside `install/`, and backport changes.
    *   *Dismiss (Recovery)*: To discard the deletion and restore the file, the user simply runs `make deploy` (Stage 2 will render the sandbox copy, see it as missing in `install/` and recreate the file and link via normal `Add` logic, completing a clean `Delete -> Add` Git timeline).

### I. Naming Convention for Templates (IDE & LSP Friendly)
To guarantee full IDE and Language Server Protocol (LSP) features (e.g., syntax highlighting, linting, autocomplete) for template files within editors (such as VSCode, Neovim, or Emacs), the system enforces a strict suffix naming convention:
*   **Format**: `[filename].[engine_prefix].[target_extension]`
*   **Officially Supported Engines** (Custom engines can be defined in `drift.toml`):
    1.  *Envsubst*: Uses suffix **`.envst.[ext]`** (e.g., `dot-bashrc.envst.sh`, `all_proxy.envst.conf`).
    2.  *Mustache*: Uses suffix **`.mustache.[ext]`** (e.g., `home.mustache.nix`, `settings.mustache.json`).
*   **Why this is superior**: Because the terminal extension is the actual target format (like `.sh`, `.nix`, `.json`), text editors instantly apply the correct syntax highlighting, formatters, and LSP environments without requiring custom regex filetype mappings.

---

## 6. Detailed Implementation Specifications

### Package Metadata: `package.toml` Template

Each modular subdirectory under `src/` can contain a `package.toml` to customize its compilation, target locations, deployment types, and life cycles. If omitted, default configurations are applied. This config file has an alternative name `drift_package.toml` if filename `package.toml` is used for other purpose and cannot be used in this package.  
Therefore, config loader should check if `drift_package.toml` exists first then `package.toml` . Regardless of the name, the actual file selected as config file should not be copied into `install/` folder.

```toml
# =====================================================================
# package.toml Template & Specification
# Place this file in: src/<package_name>/package.toml
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

# If true, all file creation, copying, deletion, and symlinking operations
# for this package will be executed utilizing "sudo" elevation.
sudo = false

# ---------------------------------------------------------------------
# Fully-Controlled Directory (FCD) Audit Options
# ---------------------------------------------------------------------
# List of subdirectories (expressed as relative paths under target_directory)
# which are fully owned by this dotfiles repository.
# Stage 1 will recursively scan these folders on the host system. Any wild/untracked
# files found here will be reverse-synchronized back to install/ to prevent lost updates.
fully_controlled_dirs = [
    "sub_dir1",
    "sub_dir2"
]

# ---------------------------------------------------------------------
# Lifecycle Hooks
# ---------------------------------------------------------------------
# Executable scripts located inside the package directory.
# Run before first-time installation (CWD: install/pkg)
pre_install = "pre-install.bash"

# Run after successful first-time installation (CWD: target_directory)
post_install = "post-install.bash"

# Run before any update/deployment (CWD: install/pkg)
pre_update = "pre-update.bash"

# Run after any successful update/deployment (CWD: target_directory)
post_update = "post-update.bash"

# Run after templates are rendered into sandbox (CWD: render/pkg)
post_render = "post-render.bash"

# Timeout in seconds for lifecycle hook script executions (Default: 120)
hook_timeout = 120

```

If the package config toml file is not present, and one of the follwing things happened:  

1. this package is explicitly mentioned in commands.
2. this package is not disabled in workspace config `drift.toml` file, and the user invokes a global operation without specific package name.  

Then an error will be thrown to prevent system corruption, and the render process will not start.  

You can always using CLI `drift new <package-folder> [<package.toml | drift_package.toml>]` to init a package level config in that folder.  

The package toml config file can be a template, the template should be rendered using the render engines defined in workspace config file `drift.toml` . The rendered output is stored at `render/<pkg>/package.toml | drift_package.toml` . The output name depends on the template name. Only one level of rendering is allowed.  

The overall file detection order should be like:  
1. drift_package.toml
2. package.toml
3. drift_package.<engine1>.toml
4. template to drift_package.toml of engine 2 and so on.  
5. package.<engine1>.toml  
6. template to package.toml of engine 2 and so on.  

The order of engine 1, engine 2, engine 3 is undefined, because toml dict doesn't have a reliable key order.


---

### Detailed Workflow Control Flow & Pseudocode

### Overview of Control Flow & Orchestration

The active configuration engine and orchestrator follow a strict sequence designed for predictability, transaction-like integrity, and extensive error recovery.

#### 1. Discovery and Registry Check
Deployment can be triggered in **Bulk Mode** (evaluating all declared active packages) or **Targeted Mode** (focusing on a specific package).
*   **Discovery**: The orchestrator checks workspace declarations in `config/drift.toml` to identify enabled packages, then verifies that `enable_install` is `true` in each package's `package.toml`.
*   **Mid-Operation Registry Interlock**: The state database at `install/state.toml` is queried. If any package is currently in a `"staging"` or `"deploying"` state, execution is aborted unless the `--force` flag is supplied, preventing corruption from a previous midway failure.

#### 2. Stage 1: Alignment Safeguard (System -> Install)
*   The system executes **Primitive 1: Reverse Sync** on all target packages to capture and reconcile any manual, local modifications made directly on the active host system.
*   **The Reverse-Sync Reconciliation Flow**:
    - **Folder Comparison**: The orchestrator triggers `compare_folders(translate_mode="reverse")` with the system target directory as the source and `install/<package>` as the destination.
    - **System Deletions (`diff.deleted`)**: Any files manually deleted on the system that are currently tracked by the repository are symmetrically pruned/removed from the `install/` state database folder.
    - **System Modifications (`diff.modified`)**: Any files manually edited on the system are reverse-copied back to their corresponding repository paths in `install/` (applying reverse dot-prefix translation so system `.bashrc` translates back to repo `dot-bashrc`).
    - **System Additions & Type Changes (`diff.added`)**: New files on the system are processed and synced back only if they reside inside configured **Fully-Controlled Directories (FCD)**, or if they represent a **Type Change / Type Promotion** (meaning the file path or one of its parent paths changed type, which is detected by checking if it appears in `diff.deleted`).
*   **Uncommitted State Check**: After performing reverse-sync on all active packages, the deployer checks if `git -C install status` is dirty. If uncommitted changes are detected (representing active host drift, i.e., Diff B), the deployer **halts immediately**. This acts as a security sentinel, forcing the developer to explicitly review the drift (via `drift diff --system`) and either **Adopt** (by committing the drift in the `install/` repository) or **Dismiss** (by deploying with `--force`) the system changes before template rendering can continue.

#### 3. Stage 2: Sandboxing & Reconciliation (Render -> Stage)
*   **Sandbox Render (Primitive 2)**:
    - **Global Pre-render Resolving**: Before compiling packages, all configured global template variables or input database configs (e.g. `mustache.envst.json` -> `mustache.json` using `envsubst`) are rendered.
    - **Rendering Execution Flow (`render_package.py`)**:
        1. *Sandbox Cleansing*: Clears any pre-existing package folder in `render/` using `shutil.rmtree` to maintain clean state while preserving the underlying `render/.git` repo.
        2. *Metadata Compiling*: Loads and compiles package config from the source folder (supporting on-the-fly parsing of `package.envst.toml` templates). If `enable_render` is false, rendering is skipped.
        3. *Misspelled Ignore Warning*: Checks for a misspelled `.driftignore` and if found (without `.drift_ignore`), logs a warning and automatically copies it under correct name `.drift_ignore`.
        4. *Surgical File Walk*: Traverses the source package directory. Subdirectory ignore files (`.drift_ignore` or `.driftignore`) are blocked with errors. Static files are physically copied. Template files matching any active engine configuration suffix are surgically compiled (engine suffix is stripped from the rendered file name).
        5. *Post-Render Lifecycle Hook*: Triggers the `post_render` hook script (if defined) running with its working directory set to `render/<package>`.
    - **Sandbox Render Commit (Primitive 3)**: Automatically commits the sandbox changes inside the local `render/` repository to maintain a full history of declarative rendering.

*   **Staging Database (Primitive 4 - `stage_repo.py`)**:
    - **Installation Exclusions**: Skips any packages that declared `enable_install` as `false`.
    - **Staging Conflict Safeguard**: If any targeted package in the state database `install/` contains uncommitted local modifications, staging aborts immediately (unless `--force` is used).
    - **Staging Transaction Interlock**: Sets the package state to transient `"staging"` inside `state.toml` before any changes are written. If a package is found in `"staging"` or `"deploying"` state from a previous crash, staging is aborted.
    - **Reconciliation comparison**: Compares `render/<pkg>` with `install/<pkg>` using `compare_folders` in forward mode.
        1. *Deletions*: Tracks files in `install/` that are missing in `render/` (ignoring managed configs). They are moved to `backup/<package>/deleted_files/` and deleted from `install/`.
        2. *Additions/Modifications*: Physical files in `render/` are copied to `install/`.
        3. *Stow Ignore generation*: Copies `.drift_ignore` and `drift_package.toml` to `install/<package>`. It automatically generates `.stow-local-ignore` inside `install/<package>`, appending exclusions for the primary `.drift_ignore` and package config file so GNU Stow ignores them.
    - **Staged Transaction Complete**: Calculates granular file changes (`added_files`, `modified_files`, `deleted_files`) as a `PackageStageChanges` object and updates the state registry database to stable `"staged"`.

#### 4. Stage 2: Physical Deployment Sequence (Primitive 5)
For each redeployable package:
*   **Target Directory Check**: The engine verifies that the package's target directory is absolute and is not nested inside or equal to the workspace root.
*   **State Transition to `"deploying"`**: The state registry database `state.toml` is written to mark the package's state as `"deploying"`.
*   **Collision Guard (Incremental/Full)**:
    - *Symlinked Parent Pre-Check*: Traverses up the target path. If any parent directory is a symlink pointing into the workspace root, aborts immediately.
    - *Unified Audit*: Uses `compare_folders` to compare files in the `install/` base directory with the active target. Collisions are safely backed up to `backup/<package>/overwritten/` (or `deleted_files/` if matched by `.drift_ignore` patterns) and removed from the active system to clear the path.
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

#### 6. Stage 2: Workspace Garbage Collection (Primitive 9 - Bulk Mode Only)
*   **Workspace GC (Post-Deployment)**: If a bulk deployment (all active packages) succeeds, the engine automatically triggers **Primitive 9: Workspace Garbage Collection**.
*   This uninstalls orphan packages (previously installed but now disabled in workspace config) and purges "zombie" directory folders in `render/` and `install/` which lack valid package configuration files, auto-committing the database cleanup to lock in a clean workspace environment.

---

#### 1. Command Orchestration & Deployment Control Flow

```python
def cli_deploy_full_sequence(target_pkg=None, force=False):
    # Load local-only state database
    state_registry = load_state_registry("install/state.toml")
    
    # Verify no packages are currently stuck in mid-deployment transient states
    for pkg_name, state_data in state_registry.get_packages().items():
        if not force and state_data.get("state") in ("staging", "deploying"):
            print(f"[CRITICAL ABORT] Package '{pkg_name}' is in '{state_data.get('state')}' state.")
            print("Run 'drift rollback' to restore workspace integrity first.")
            exit(1)

    # --- Bulk Garbage Collection (Self-Cleaning) ---
    if target_pkg is None:
        active_declared = get_discovered_active_packages()
        for registered_pkg in state_registry.get_packages().keys():
            if registered_pkg not in active_declared:
                print(f"[GARBAGE COLLECTION] Uninstalling orphan package: {registered_pkg}")
                run_primitive_7_uninstall_package(registered_pkg)

    # --- Stage 1: Alignment Check ---
    run_primitive_1_reverse_sync(target_pkg)
    
    if not force and git_repo_is_dirty("install/"):
        print("[ABORT] Stage 1 sentinel found drift inside active configurations (Diff B)!")
        exit(1)

    print("[SUCCESS] Configurations in perfect alignment. Beginning Stage 2...")

    # --- Stage 2: Sandbox Rendering ---
    try:
        run_primitive_2_render(target_pkg)
        run_primitive_3_render_commit(target_pkg)
    except Exception as e:
        print(f"[CRITICAL FAILURE] Render phase failed: {e}")
        exit(2)

    # --- Stage 2: Database Staging and Physical Delivery ---
    try:
        # Step 4: Stage render/ outputs to install/ base, computing package changes
        package_changes = run_primitive_4_stage_render_to_install(target_pkg)
        
        # Step 5: Deploys changes to host system (applying Incremental if package_changes is present)
        run_primitive_5_install_deployment(
            packages_to_redeploy=[c.package_name for c in package_changes],
            package_changes=package_changes,
            force=force
        )
    except Exception as e:
        print(f"[CRITICAL FAILURE] Physical deployment failed midway: {e}")
        print("Please run 'drift rollback' to restore the last clean committed state.")
        exit(2)

    # --- Stage 2: Lock & Commit ---
    try:
        # Step 6: Commit state changes in local database
        run_primitive_6_commit_install_repo(
            commit_message="Deploy: Sync active packages",
            target_pkgs=[c.package_name for c in package_changes] if target_pkg else None
        )
        print("[SUCCESS] Configurations deployed successfully!")
    except Exception as e:
        print(f"[CRITICAL FAILURE] Failed to commit state registry: {e}")
        exit(2)

    # --- Part 4: Post-Deployment Workspace Garbage Collection ---
    if target_pkg is None:
        try:
            print("[INFO] Executing post-deployment workspace garbage collection...")
            run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=False)
        except Exception as e:
            print(f"[WARNING] Garbage collection failed: {e}")


def cli_rollback_recovery(target_pkg=None, force=False):
    # This invokes Primitive 8
    print("================================================================================")
    print("WARNING: Rollback should ONLY be used if a deploy failed midway.")
    print("Running rollback outside of a midway deploy failure will cause the active system")
    print("to be hard-overwritten, discarding any untracked or uncommitted system drifts!")
    print("================================================================================")
    
    if not force:
        state_registry = load_state_registry("install/state.toml")
        if target_pkg is not None and state_registry.get_package_state(target_pkg) != "deploying":
            print(f"[ERROR] Package '{target_pkg}' is not in conflict state, rollback aborted. (use '--force' to ignore.)")
            exit(1)
        if target_pkg is None and not state_registry.has_deploying_package():
            print(f"[ERROR] No package is in conflict state, rollback aborted. (use '--force' to ignore.)")
            exit(1)
        # Require user confirmation or assume it's run strictly under midway failure scenario
        print("[NOTICE] Proceeding with Rollback Recovery (Primitive 8)...")
        
    run_primitive_8_rollback_recovery(target_pkg)


def cli_uninstall_utility(target_pkg, force=False):
    if target_pkg is None:
        print("[ERROR] Uninstall primitive requires a specific package target! e.g. make uninstall package=nvim")
        exit(1)
        
    # --- Uninstall Protection Safeguard ---
    active_declared_packages = get_discovered_active_packages()
    if (target_pkg in active_declared_packages) and (not force):
        print(f"[ERROR] Safeguard: Package '{target_pkg}' is still active/enabled inside config/drift.toml.")
        print("To safely uninstall, please first disable it in config/drift.toml (or set enable_install=false in package.toml),")
        print("or use 'make uninstall package=xxx force=true' to bypass this shield.")
        exit(3)
        
    run_primitive_7_uninstall_package(target_pkg)
```

---

#### 2. Primitive 1: Reverse Sync (Host $\rightarrow$ `install/`)

```python
def run_primitive_1_reverse_sync(target_pkg=None):
    active_packages = get_discovered_active_packages(target_pkg)
    for pkg in active_packages:
        metadata = load_package_metadata(pkg)
        if not metadata.enable_install:
            continue
            
        install_pkg_dir = f"install/{pkg}"
        target_dir = metadata.target_directory
        if not os.path.exists(target_dir):
            continue
            
        ignore_handler = DriftIgnore.load_from_dir(install_pkg_dir)

        # Unified Reverse Folder Comparison
        # System Target is src, Repo Install is dst.
        diff = compare_folders(
            src_dir=target_dir,
            dst_dir=install_pkg_dir,
            ignore_handler=ignore_handler,
            resolve_symlinks=True,
            translate_mode="reverse"
        )
        
        # 1. Handle system deletions
        for rel in diff.deleted:
            repo_rel = translate_dot_prefixes_reverse(rel)
            repo_file = install_pkg_dir / repo_rel
            if os.path.exists(repo_file):
                print(f"System Deletion: Pruning missing counterpart '{repo_file}'.")
                delete_physical_file(repo_file)

        # 2. Handle system modifications
        for rel in diff.modified:
            system_file = target_dir / rel
            repo_file = install_pkg_dir / translate_dot_prefixes_reverse(rel)
            print(f"System Modification: Syncing drift from '{system_file}'.")
            reverse_sync_file_or_dir(system_file, repo_file, ignore_handler)

        # 3. Handle wild files (FCD) or promoted tracked files
        for rel in diff.added:
            if is_managed_config_file(rel):
                continue
                
            is_to_sync = False
            # Condition A: Inside a Fully-Controlled Directory (FCD)
            for fcd_rel in metadata.fully_controlled_dirs:
                if is_relative_to(rel, fcd_rel):
                    is_to_sync = True
                    break
                    
            # Condition B: Part of a tracked path that changed type
            if not is_to_sync:
                for parent in [rel] + list(get_parents(rel)):
                    if parent in diff.deleted:
                        is_to_sync = True
                        break
                        
            if is_to_sync:
                system_file = target_dir / rel
                repo_file = install_pkg_dir / translate_dot_prefixes_reverse(rel)
                print(f"Untracked FCD/Promoted File: Syncing '{system_file}' to repo.")
                reverse_sync_file_or_dir(system_file, repo_file, ignore_handler)
```

---

#### 3. Primitives 4, 5, 6, 7 & 8: Reconcile, Deploy, Commit, Uninstall & Rollback

```python
def run_primitive_4_stage_render_to_install(target_pkg=None):
    packages_to_redeploy = set()
    active_packages = get_discovered_active_packages(target_pkg)
    
    # A. Process Deletions (present in install/ but missing in render/)
    for pkg in active_packages:
        metadata = load_package_metadata(pkg)
        # Packages with enable_install = false are completely skipped from copying
        if not metadata.enable_install:
            continue
            
        install_pkg_dir = f"install/{pkg}"
        if not os.path.exists(install_pkg_dir):
            continue
            
        for file in get_files(install_pkg_dir):
            if not os.path.exists(f"render/{pkg}/{file}"):
                print(f"Pruning deprecated configuration: {pkg}/{file}")
                
                # Delete on active system target
                system_target = resolve_system_target(pkg, file, metadata)
                if metadata.install_method == "stow":
                    de_stow_individual_file(pkg, file)
                elif metadata.install_method == "copy":
                    delete_active_file_with_sudo(system_target, metadata.sudo)
                
                # Physical backup of deprecated database file before deletion
                move_file(f"install/{pkg}/{file}", f"backup/{pkg}/deleted_files/{file}")
                packages_to_redeploy.add(pkg)

    # B. Process Additions and Modifications
    for pkg in active_packages:
        metadata = load_package_metadata(pkg)
        if not metadata.enable_install:
            continue
            
        render_pkg_dir = f"render/{pkg}"
        if not os.path.exists(render_pkg_dir):
            continue
            
        for file in get_files(render_pkg_dir):
            src = f"render/{pkg}/{file}"
            dst = f"install/{pkg}/{file}"
            
            if not os.path.exists(dst):
                print(f"Adding new configuration file: {pkg}/{file}")
                ensure_dir_exists(os.path.dirname(dst))
                copy_file_contents(src, dst)
                packages_to_redeploy.add(pkg)
            elif file_contents_differ(src, dst):
                print(f"Modifying configuration file: {pkg}/{file}")
                copy_file_contents(src, dst)
                packages_to_redeploy.add(pkg)
                
    return list(packages_to_redeploy)


def run_primitive_5_install_deployment(packages_to_redeploy, package_changes=None, force=False):
    state_registry = load_state_registry("install/state.toml")
    
    for pkg in packages_to_redeploy:
        pkg_change = next((c for c in package_changes if c.package_name == pkg), None) if package_changes else None
        deploy_package_impl(workspace_config, pkg, state_registry, "install/state.toml", resolve_symlinks=True, force=force, package_changes=pkg_change)


def deploy_package_impl(workspace_config, pkg, state_registry, state_file, resolve_symlinks, force, package_changes=None):
    install_base = workspace_config.install_path
    metadata = load_config_for_install(install_base, pkg)
    
    if not (force or metadata.enable_install):
        print(f"Skipping package '{pkg}' (enable_install is False)")
        return
        
    target_dir = metadata.target_directory or workspace_config.default_target_path
    
    # Safety Check: Target directory cannot be inside or equal to workspace root
    abs_target = target_dir.absolute()
    abs_drift_root = workspace_config.drift_root.absolute()
    if abs_target == abs_drift_root or abs_target.is_relative_to(abs_drift_root):
        raise ValueError("Safety Abort: Target directory cannot be inside or equal to drift workspace root.")
        
    # Verify/Ensure target folder is writable
    ensure_directory_writable(target_dir, metadata.sudo)
    
    current_state = state_registry.get_package_state(pkg)
    if not force and current_state in ("staging", "deploying"):
        raise RuntimeError("Safety Abort: Package is currently in transient conflict state.")
        
    is_first_time = (current_state is None)
    
    # Set package state to transient "deploying"
    state_registry.set_package_state(pkg, "deploying", install_method=metadata.install_method)
    save_state_registry(state_file, state_registry)
    
    install_pkg_dir = install_base / pkg
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
    
    # If package_changes is None, perform Full Redeploy, else Incremental
    full_redeploy = (package_changes is None)
    current_files = ignore_handler.filter_deployable_files(install_pkg_dir)
    
    if full_redeploy:
        # Prune orphaned files present in state registry manifest but no longer in package
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
        trigger_package_lifecycle_hook(pkg, "pre_install", metadata, workspace_config, cwd_override=install_pkg_dir)
    else:
        trigger_package_lifecycle_hook(pkg, "pre_update", metadata, workspace_config, cwd_override=install_pkg_dir)

    # 3. Physical File Delivery
    stow_version = get_stow_version() if metadata.install_method == "stow" else None
    stow_sufficient = is_stow_version_sufficient(stow_version) if stow_version else False
    
    if full_redeploy:
        run_full_file_delivery(
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
            package_changes=package_changes,
            install_pkg_dir=install_pkg_dir,
            target_dir=target_dir,
            metadata=metadata
        )

    # 4. Trigger Post-deployment Lifecycle Hook (CWD: target_dir)
    if is_first_time:
        trigger_package_lifecycle_hook(pkg, "post_install", metadata, workspace_config)
    else:
        trigger_package_lifecycle_hook(pkg, "post_update", metadata, workspace_config)
        
    # 5. Lock Final State and Manifest
    now_str = datetime.datetime.now().isoformat()
    state_registry.set_package_state(pkg, "installed", last_deployed=now_str, install_method=metadata.install_method)
    
    if full_redeploy:
        state_registry.set_package_deployed_files(pkg, current_files)
    else:
        new_deployed = set(state_registry.get_package_deployed_files(pkg))
        for rel in package_changes.deleted_files:
            new_deployed.discard(rel)
        for rel in package_changes.added_files:
            new_deployed.add(rel)
        state_registry.set_package_deployed_files(pkg, sorted(list(new_deployed)))
        
    save_state_registry(state_file, state_registry)


def run_collision_guard(workspace_config, pkg, install_pkg_dir, metadata, ignore_handler, target_dir, is_first_time, resolve_symlinks, install_base):
    # Level 1 Pre-Check: Prevent parent symlink pollution
    parent_symlink = get_symlinked_parent(target_dir, workspace_config.drift_root)
    if parent_symlink:
         raise RuntimeError(f"Safety Abort: Parent '{parent_symlink}' is a symlink pointing into drift workspace.")

    # Level 2 Pre-Check: Unified Folder Comparison Audit
    diff = compare_folders(
        src_dir=install_pkg_dir,
        dst_dir=target_dir,
        ignore_handler=ignore_handler,
        resolve_symlinks=resolve_symlinks,
        translate_mode="forward",
        src_only=True,
        drift_root=workspace_config.drift_root
    )

    processed_paths = set()

    # Route Internal Symlinks
    for rel in diff.internal_symlinks:
        system_target = resolve_system_target(rel, target_dir)
        processed_paths.add(rel)
        
        handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo, "Internal symlink error", resolve_symlinks)
        
        repo_path = install_pkg_dir / rel
        if repo_path.is_dir() and not repo_path.is_symlink():
             ensure_dir_exists_with_sudo(system_target, metadata.sudo)

    # Route Deletions (Ignored files or type mismatches)
    for rel in diff.deleted:
        if rel in processed_paths: continue
        processed_paths.add(rel)
        
        system_target = resolve_system_target(rel, target_dir)
        if ignore_handler.match_path(rel):
            handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo, "Ignored file cleanup", resolve_symlinks, "deleted_files")
        else:
            handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo, "Type mismatch collision", resolve_symlinks)

    # Route Modifications
    for rel in diff.modified:
        if rel in processed_paths: continue
        processed_paths.add(rel)
        
        system_target = resolve_system_target(rel, target_dir)
        
        # Stow Mode: skip if target link already points to our own package folder
        if metadata.install_method == "stow" and system_target.is_symlink():
            if target_link_is_ours(system_target, install_pkg_dir):
                continue
                
        # Copy Mode: skip collision backup if subsequent update (only backup on first-time deploy)
        if metadata.install_method == "copy" and not is_first_time:
            continue

        handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo, "Deployment collision", resolve_symlinks)

    # Route Matches (Stow mode specific)
    if metadata.install_method == "stow":
        for rel in diff.matches:
            if rel in processed_paths: continue
            processed_paths.add(rel)
            
            system_target = resolve_system_target(rel, target_dir)
            if not system_target.is_symlink():
                handle_collision_error(pkg, rel, system_target, workspace_config, metadata.sudo, "Stow physical collision", resolve_symlinks)


def run_full_file_delivery(pkg, install_base, install_pkg_dir, target_dir, metadata, deployable_files, stow_sufficient):
    if metadata.install_method == "copy":
        run_full_copy_deployment(install_pkg_dir, target_dir, metadata.sudo, deployable_files)
    elif metadata.install_method == "stow":
        if stow_sufficient:
            run_stow_deployment(install_base, target_dir, pkg, metadata.sudo, stow_sufficient)
        else:
            print("[WARN] Stow version insufficient. Falling back to manual symlinking.")
            for rel_file in deployable_files:
                deploy_single_stow_file(rel_file, install_pkg_dir, target_dir, metadata.sudo)


def run_incremental_file_delivery(package_changes, install_pkg_dir, target_dir, metadata):
    # 1. Apply Deletions
    for rel_file in package_changes.deleted_files:
        delete_single_system_file(rel_file, target_dir, metadata.sudo)

    # 2. Apply Additions and Modifications
    for rel_file in package_changes.added_files + package_changes.modified_files:
        if metadata.install_method == "stow":
            deploy_single_stow_file(rel_file, install_pkg_dir, target_dir, metadata.sudo)
        elif metadata.install_method == "copy":
            deploy_single_copy_file(rel_file, install_pkg_dir, target_dir, metadata.sudo)


def run_primitive_6_commit_install_repo(commit_message, target_pkgs=None):
    if target_pkgs:
        run_shell(f"git -C install add {shlex.join(target_pkgs)} state.toml")
        run_shell(f"git -C install commit -m '{commit_message}'")
    else:
        run_shell("git -C install add -A")
        run_shell("git -C install commit -m '{commit_message}'")


def run_primitive_7_uninstall_package(pkg):
    print(f"Starting uninstallation for package: {pkg}...")
    state_registry = load_state_registry("install/state.toml")
    
    if pkg not in state_registry.get_packages():
        print(f"Warning: Package {pkg} is not registered as installed in state.toml. Forcing safety unlink...")
        
    metadata = load_package_metadata(pkg)
    install_pkg_dir = f"install/{pkg}"
    
    if os.path.exists(install_pkg_dir):
        # 1. Physical Unlinking/Deletions
        for file in get_files(install_pkg_dir):
            if is_ignored_by_local_ignore(file, pkg):
                continue
            system_target = resolve_system_target(pkg, file, metadata)
            
            if metadata.install_method == "stow":
                if os.path.islink(system_target):
                    print(f"Unlinking symlink: {system_target}")
                    delete_symlink_with_sudo(system_target, metadata.sudo)
            elif metadata.install_method == "copy":
                if os.path.exists(system_target):
                    print(f"Deleting physical copy: {system_target}")
                    delete_physical_file_with_sudo(system_target, metadata.sudo)
                    
        # 2. Rollback the Collision Guard (Restore original manual host files if any existed)
        backup_overwritten_dir = f"backup/{pkg}/overwritten"
        if os.path.exists(backup_overwritten_dir):
            for file in get_files(backup_overwritten_dir):
                system_target = resolve_system_target(pkg, file, metadata)
                backup_file = f"{backup_overwritten_dir}/{file}"
                print(f"Restoring original overridden configuration: {system_target}...")
                ensure_dir_exists_with_sudo(os.path.dirname(system_target), metadata.sudo)
                move_file_with_sudo(backup_file, system_target, metadata.sudo)
                
        # 3. Physically delete local database copy
        delete_directory(install_pkg_dir)
        
    # 4. Remove registry entry and auto-commit database
    state_registry.remove_package(pkg)
    save_state_registry("install/state.toml", state_registry)
    
    # Auto-commit the local database changes (scoped strictly to state.toml and the deleted directory)
    run_shell(f"git -C install add state.toml")
    run_shell(f"git -C install rm -r --ignore-unmatch {pkg}/")
    run_shell(f"git -C install commit -m 'Uninstall: Removed package {pkg}'")
    print(f"[SUCCESS] Package {pkg} uninstalled and deployment traces cleaned.")


def run_primitive_8_rollback_recovery(target_pkg=None):
    # Lock/reset local state database to last successful committed deploy
    print("Reverting local state database to last successful committed deploy...")
    run_shell("git -C install reset --hard HEAD")
    
    # Trigger full redeploy fallback to restore system files
    packages = get_discovered_active_packages(target_pkg)
    print(f"Executing Full Package Redeploy to restore system files for: {packages}")
    run_primitive_5_install_deployment(packages_to_redeploy=packages, full_redeploy=True)

    state_registry = load_state_registry("install/state.toml")
    # Mark package or all packages back to "installed" state
    state_registry.set_package_state(target_pkg, "installed")
    save_state_registry("install/state.toml", state_registry)

    print("[SUCCESS] Rollback recovery complete. Clean state restored.")
```
```

---

## 7. Philosophical & Operational Benefits

By implementing this architecture, the user reaps distinct Unix-style benefits:
1.  **Strict Demarcation of Merges**: Automation is restricted to simple *reverse-syncing state* and *unilateral overwrite deployment*. The human developer remains the sole merge authority. If active system changes (Diff B) are dirty, the user is presented with standard Git outputs and handles the backport to templates manually.
2.  **No Hand-Crafted State Engine**: By designating `install/` and `render/` as local Git repositories, we avoid writing custom rollback, commit-tracking, and differential history features. Git manages the hard stuff (index, diffs, conflicts).
3.  **Complete, High-Fidelity backups**: Deleted configurations and overwritten links are never silently purged; they are meticulously structured and swept into `backup/<package>/` with clean console reporting.
4.  **Extensible Lifecycles**: Adding complex validation or post-deployment logic is as simple as dropping a standard executable bash script in `src/<package>/pre-update.bash` or `post-install.bash`.
