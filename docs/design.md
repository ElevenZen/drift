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

This architecture organizes daily developer workflows into three robust, standard patterns, supporting both **bulk operations (all packages)** and **targeted single-package operations**:

#### Workflow 1: Developing Declarative Changes (The Template Loop)
You decide to modify your global shell variables or edit a Neovim template.
1.  **Edit Source**: You modify `src/nvim/dot-config/nvim/init.lua` or edit `config/envsubst.bash`.
2.  **Verify Evolution (`make diff-A` or `make diff-A package=nvim`)**:
    *   Renders your edits into the `render/` sandbox and commits them.
    *   It prints **Diff A**, showing you exactly how your templates evolved.
3.  **Dry-Run check (`make diff-C`)**:
    *   You run `make diff-C` to see exactly what changes will be applied.
4.  **Deploy (`make deploy`)**:
    *   You run `make deploy`. Since your live system hasn't drifted, Stage 1 completes with a "Clean Slate" status, and Stage 2 runs to instantly apply your new templates to the active environment.

#### Workflow 2: Auditing GUI & Runtime System Drifts (The Drift Audit)
A program (like qBittorrent or a terminal theme tool) has rewritten its configuration file in the background, or you modified a file in your home directory directly to test a setting.
1.  **Audit Drift (`make diff-B` or `make diff-B package=qbittorrent`)**:
    *   You run `make diff-B` to pull active system drift back into `install/` (targeted to qbittorrent or all packages).
    *   It prints **Diff B**, showing you exactly what changes the GUI program or your hot-edits introduced.
2.  **Manual Reconciliation**:
    *   If you want to **Adopt** these changes: You copy/merge those modifications from `install/` back to your declarative templates under `src/`, then commit them in your main repo.
    *   If you want to **Dismiss** these changes: You simply run `make deploy`. Stage 1 will detect the drift, abort the deployment, and print the status. You can then commit in the `install/` repo (without changing anything in `src/`) and re-deploy, or force-deploy to overwrite the drift.

#### Workflow 3: Full Recovery (The Rollback Loop)
A deployment failed midway due to a permission error, or manual system edits corrupted a config directory.
1.  **Rollback (`make rollback` or `make rollback package=nvim`)**:
    *   Reverts the `install/` database to the last successfully committed deployment commit, then triggers a **Full Package Redeploy**, restoring configurations to a known-clean state.

#### Workflow 4: Uninstallation (The Uninstall Loop)
You no longer want a package (e.g., `proxychains`) active on this machine.
1.  **Uninstall (`make uninstall package=proxychains`)**:
    *   Safely removes all symlinks or copied files from the live system.
    *   Restores any original files backed up under `backup/` to their original paths.
    *   Updates the `install/state.toml` to clean the package state.

---

## 3. The 8 Core Primitives

All workflows in this dotfiles system are composed of these eight atomic, sequential primitives, each supporting an optional `<package_name>` target:

```
                          [ Execution: make deploy ]
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
                                                │ 4. Stage Render to Install     │
                                                │ 5. Install Repo Deployment    │
                                                │ 6. Install Repo Commit        │
                                                └───────────────┬───────────────┘
                                                                ▼
                                                        [ Deploy Success ]
```

### Primitive 1: Reverse Sync (System $\rightarrow$ `install/` [Optional: `<package>`])
無条件で、現在の系統の有益な設定状態を `install/` Git リポジトリに逆同期（引き込み）します。
*   **For `stow` Packages**:
    *   *Missing Symlink*: If a symlink in the system is deleted, deletes the counterpart inside `install/`. This generates a physical deletion commit (`Delete`) in the database, representing system ground truth.
    *   *Replaced by Regular File*: If a symlink was replaced by a normal file containing edits, copies that file's contents back into `install/`.
    *   *Fully-Controlled Directories (FCD)*: Scans configured relative subdirectories for new (untracked) files and copies them back into `install/` to be tracked by Git.
*   **For `copy` Packages**:
    *   *Modified*: Compares physical active system files with `install/`. If different, reverse-copies system files back into `install/`.
    *   *Deleted*: If a system file is missing, deletes the counterpart inside `install/`. This generates a physical deletion commit (`Delete`) in the database.
    *   *FCD*: Scans target subdirectories for untracked files and copies them back to `install/`.

### Primitive 2: Render (`src/` $\rightarrow$ `render/` [Optional: `<package>`])
Clears the sandbox package-by-package (or selectively scopes cleaning) to preserve the `render/.git` repository. Then processes files in `src/` (expanding templates via `envsubst`/`mustache` using config files pointed in `config/drift.toml` such as `config/envsubst.bash`/`config/mustache.json`), and places the results in `render/`. No live system files are altered.

### Primitive 3: Render Repo Commit [Optional: `<package>`])
Automatically commits any updates inside the `render/` sandbox Git repository:
```bash
git -C render add -A
git -C render commit -m "Render: Update templates at $(date)"
```

### Primitive 4: Stage Render to Install [Optional: `<package>`])
Reconciles the sandbox `render/` folder into the `install/` database. During this step, the engine compares `render/` and `install/`, and **records exactly which files and packages require redeployment** (due to additions, modifications, or deletions).

### Primitive 5: Install Repo Deployment [Optional: `<package>`])
Applies changes to the physical active system.
*   **For `stow` packages**: Executes individual manual symlinks (Incremental) or runs GNU Stow (Full Deploy).
*   **For `copy` packages**: Copies files to `target_directory` (prefixed with `sudo` if configured).
*   *Note*: When executed contiguously after Primitive 4, this step only deploys the changed packages (Incremental Deploy). When executed independently, it falls back to redeploying selected package or all active packages (Full Redeploy).

### Primitive 6: Install Repo Commit [Optional: `<package>`])
Locks the deployed configurations into the local state database with an automated commit:
*   *Bulk Mode*: Stages and commits the entire `install/` repository.
*   *Targeted Package Mode*: Scopes the stage and commit strictly to the specific package directory and `state.toml` (e.g., `git -C install add install/<package> install/state.toml && git -C install commit`).

### Primitive 7: Uninstall Repo Package (`<package>`)
Removes a package from the system:
1.  **De-stow or Delete**: Unlinks symlinks or deletes physical files belonging to the package.
2.  **Rollback Collision Guard**: Restores any original host files backed up in `backup/<package>/overwritten/` to their original locations on the system.
3.  **Update Registry**: Removes the package from `install/` Git repository and from `install/state.toml`.  
4.  **Auto-Commit**: Automatically commits the database changes inside the `install/` Git repository.

### Primitive 8: Rollback Recovery (Restore Local State Database & Trigger Full Redeploy [Optional: `<package>`])
Restores the system configuration and the local state database to the last known-clean, committed state after a midway failure.
*   **Database Reset**: Performs a hard reset on the local state repository:
    ```bash
    git -C install reset --hard HEAD
    ```
    This completely cleans any half-written or uncommitted states inside the `install/` tracking directory.
*   **Full Redeploy**: Triggers a **Full Package Redeploy** (Primitive 5 with `full_redeploy=True`) for the target package or all enabled active packages to rebuild physical symlinks or file copies.
*   **Operational Mandate & Hazard Warning**: 
    *   **Strict Midway Use**: This primitive **MUST ONLY** be used when a `deploy` command fails midway (i.e., during template rendering, staging render to install, or during physical file copies/symlinking on the active system). Under these mid-failure conditions, the active system may be left in an inconsistent/broken state, and `install/` lacks the final Stage 2 commit.
    *   **Hazard of Misuse**: It **MUST NOT** be run under normal circumstances when no deployment failure occurred. Because this primitive bypasses Stage 1's `Reverse Sync`, running it outside of a recovery scenario will discard all local system drifts and runtime changes (the uncommitted state), causing permanent loss of system configuration drift information (system drift tracking).

---

## 4. User-Facing Operations (CLI Action Mappings)

By combining these primitives, we expose clean, high-signal commands in the CLI (via the `Makefile`) with zero unintended side effects. If a package is specified (e.g. `package=nvim`), operations are targeted to that package; otherwise, they run in bulk across all enabled packages.

### User CLI Commands Reference

The following reference table outlines all developer-facing commands available via the CLI/Makefile:

| Command | Target / Scope | Orchestrated Primitives | Description / Purpose |
| :--- | :--- | :--- | :--- |
| `make diff-A [package=xxx]` | Scoped package or bulk | `Primitive 2 (Render)` $\rightarrow$ `git -C render diff` | **View Template Evolution**: View template edits since the last deployment, completely isolated from active system drift. |
| `make status-A [package=xxx]` | Scoped package or bulk | `Primitive 2 (Render)` $\rightarrow$ `git -C render status` | **Check Template Render Status**: Show which rendered template files in the sandbox differ from their tracked history. |
| `make diff-B [package=xxx]` | Scoped package or bulk | `Primitive 1 (Reverse Sync)` $\rightarrow$ `git -C install diff` | **View Active System Drift**: Show exactly how live system files on the host have drifted or been modified compared to the database. |
| `make status-B [package=xxx]` | Scoped package or bulk | `Primitive 1 (Reverse Sync)` $\rightarrow$ `git -C install status` | **Check Active System Drift Status**: View the list of files modified/deleted directly on the system since last deployment. |
| `make diff-C [package=xxx] [stat=true]` | Scoped package or bulk | `Primitive 1 (Reverse Sync)` $\rightarrow$ `Primitive 2 (Render)` $\rightarrow$ `git diff --no-index` | **Dry-Run Pending Deployment Delta**: Direct comparison of the new sandbox templates with the live system files. |
| `make deploy [package=xxx]` | Scoped package or bulk | **Stage 1 (Sentinel)**: `Primitive 1` <br> **Stage 2 (Sequential)**: `Primitive 2` $\rightarrow$ `3` $\rightarrow$ `4` $\rightarrow$ `5` $\rightarrow$ `6` | **Atomic Safe Deployment**: Safely syncs, compiles, and deploys configurations. Aborts immediately with scoped `Diff B` if system drift is detected. |
| `make rollback [package=xxx]` | Scoped package or bulk | `Primitive 8 (Rollback)` | **Emergency Rollback Recovery**: Reverts database and system files to the last committed state. **Only run on midway deployment failures.** |
| `make uninstall package=xxx [force=true]` | Required package target | `Primitive 7 (Uninstall)` | **Package Uninstallation**: Safely unlinks/deletes a package from the system and restores pre-existing backups. |

---

### Command Specifications & Operational Policies

#### A. View Template Evolution (`make diff-A [package=xxx]`)
*   *Orchestration*: `Primitive 2 (Render)` $\rightarrow$ `git -C render diff`
*   *Purpose*: View template edits after last deployment, isolated from active system drift.

#### B. View Active System Drift (`make diff-B [package=xxx]`)
*   *Orchestration*: `Primitive 1 (Reverse Sync)` $\rightarrow$ `git -C install diff`
*   *Purpose*: Show exactly how live configuration files have drifted on this machine compared to the last deployment.

#### C. View Pending Deployment Delta / Dry-Run (`make diff-C [package=xxx]`)
*   *Orchestration*: `Primitive 1 (Reverse Sync)` $\rightarrow$ `Primitive 2 (Render)` $\rightarrow$ `git diff --no-index install/ render/`
*   *Purpose*: Direct, absolute dry-run preview before committing deployments.

#### D. Full Configuration Deployment (`make deploy [package=xxx]`)
This is a strict **Two-Stage** atomic deployment flow:
*   **Stage 1 (Safety Guard)**:
    1.  Runs `Primitive 1 (Reverse Sync)` (scoped to target package, or bulk).
    2.  Check for drifts:
        *   *Bulk Mode*: Evaluates the entire `install/` repository for changes.
        *   *Targeted Package Mode*: **Only checks the targeted package directory** (`install/<package>/`) and `install/state.toml` for changes. Unrelated drifts in other package directories are ignored.
    3.  **If Drift is Detected**: **Aborts immediately**. The script prints the scoped **Diff B** and forces the user to manually review.
*   **Stage 2 (Sequential Deployment)**:
    1.  Runs Primitives **2** $\rightarrow$ **3** $\rightarrow$ **4** $\rightarrow$ **5** $\rightarrow$ **6** sequentially (scoped to target package, or bulk).
    2.  **Fail-Fast Guard & Recovery Hint**: If any step in this sequence fails, the script **halts immediately and leaves the directory structure untouched** to allow easy debugging. Crucially, the failed deployment command **must output a prominent, clear recovery hint** prompting the user to execute `make rollback` to revert the local database and restore the system files.

#### E. Rollback Recovery (`make rollback [package=xxx]`)
*   *Orchestration*: `Primitive 8 (Rollback)`
*   *Operational Intent*: This is purely an emergency restoration command.
*   *Critical Constraints*:
    *   **Only on Midway Failure**: Must only be executed if a `make deploy` run fails mid-execution (e.g., template expansion error, copy permission error, stow binary crash).
    *   **Anti-Pattern Warning**: Under healthy, non-failing system conditions, this command **should not be used**, as resetting the local state database without executing Primitive 1 first results in losing the active system's drift tracking information.

#### F. Package Uninstallation (`make uninstall package=xxx [force=true]`)
*   *Orchestration*: `Primitive 7 (Uninstall)`
*   *Purpose*: Completely clean a package from the system and update the state database.

---

## 5. Recovery & Edge Case Safeguards

This section defines the core architectural policies required to maintain technical integrity under edge cases and partial execution failures.

### A. State Registry Database (`install/state.toml`)
To safely determine whether a package should execute its `on_install` or `on_update` lifecycle hook, the system maintains a persistent, local-only state registry file at `install/state.toml`.
*   This registry tracks successful package deployments:
    ```toml
    # install/state.toml
    [packages.nvim]
    state = "installed"
    last_deployed = "2026-08-16T21:10:50.123456"
    install_method = "stow"
    deployed_files = ["dot-config/nvim/init.lua", "dot-config/nvim/coc-settings.json"]

    [packages.qbittorrent]
    state = "installed"
    last_deployed = "2026-08-16T21:10:51.987654"
    install_method = "copy"
    deployed_files = ["config.ini"]

    [packages.wezterm]
    state = "deploying"
    install_method = "stow"
    deployed_files = []
    ```
*   **Desired-State Manifest Tracking**:
    To ensure self-healing and robust deletion behavior during standalone executions, retries, or rollbacks without relying on event-driven stages (Primitive 4), the registry tracks the precise relative paths of all successfully deployed files under the `deployed_files` array.
    Upon each full redeployment, the engine compares the current desired files inside `install/<package>/` with the historical `deployed_files` manifest. Any orphaned files found in `deployed_files` but no longer present in `install/` are dynamically treated as delete instructions. They are safely backed up to `backup/<package>/deleted_files/` and surgically pruned from the active host system, ensuring zero file-leaks.
*   When a package is about to be deployed:
    1.  The system reads `install/state.toml`.
    2.  If the package is **not listed** in the registry, it is classified as a **First-Time Installation** and the `on_install` hook is triggered upon successful deploy.
    3.  If the package is **already listed** with state `"installed"`, it is classified as an **Update/Redeploy** and the `on_update` hook is triggered.  
    4.  If the package is **already listed** with state `"deploying"`, it means previous deployment ended in errors. The system should abort current deployment and tells user to call rollback manually.

### B. Physical Conflict Prevention (Collision Guard)
To protect pre-existing manual files from being silently overridden or destroyed during deployment, the Collision Guard strictly enforces three safety rules:
1.  **Stow Method Guard (Every Deploy)**:
    *   If a file in `install/<package>/<path>` is slated to be linked to `~/<path>` via `stow`, and `~/<path>` already exists on the system as a **regular physical file** (not a symlink pointing into `install/`), the system must halt and move the regular file to `backup/<package>/overwritten/<path>` before creating the link.
2.  **Copy Method Guard (First-Time Deploy Only)**:
    *   If a package is being deployed via `copy` for the **very first time** (i.e., not registered in `install/state.toml`), and a physical file already exists at the `target_directory/<path>` on the host, the system must backup the existing target file to `backup/<package>/overwritten/<path>` before copying the new one.
    *   For subsequent updates of `copy` packages, the database file simply overwrites the target file, assuming previous state synchronization has already accounted for drift.
3.  **Ignored Files as Delete Instructions**:
    *   If a file inside the staged package directory (`install/<package>/<path>`) matches the package's `.drift_ignore` patterns, it acts as a dynamic **delete instruction** for the system target.
    *   During collision guard execution, if the corresponding file/link exists at `target_directory/<path>` on the active host system, it is safely backed up to `backup/<package>/deleted_files/<path>` (preserving nested directory paths, not under `overwritten`), and then removed from the active system.

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
1.  **Ignore Filter (`.stow-local-ignore`)**:
    *   The system parses `.stow-local-ignore` at the root of each package directory.
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
# These hook files won't be copied into install directory.
# Run on first-time installation of the package
on_install = "post-install.bash"

# Run after any update/deployment of the package is executed
on_update = "post-update.bash"

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
4. package.<engine1>.toml  
5. template for engine 2 and so on.  

The order of engine 1, engine 2, engine 3 is undefined, because toml dict doesn't have a reliable key order.


---

### Detailed Workflow Pseudocode

#### 1. Command Orchestration (`Makefile` / CLI targets)

```python
# Helper to retrieve active packages list
def get_discovered_active_packages(target_pkg=None):
    # If a specific package is targetted by CLI (e.g. package=nvim)
    if target_pkg is not None:
        return [target_pkg]
        
    # Otherwise read active list from config/drift.toml
    config_registry = load_toml("config/drift.toml")
    enabled_list = []
    for pkg, enabled in config_registry.get("packages", {}).items():
        if enabled:
            # Check package-level metadata overrides
            metadata = load_package_metadata(pkg)
            if metadata.enable_install:
                enabled_list.append(pkg)
    return enabled_list

# View Template Evolution (Diff A)
def cli_show_diff_A(target_pkg=None):
    run_primitive_2_render(target_pkg)
    run_shell(f"git -C render diff {target_pkg}/")

# View Template Evolution (Status A)
def cli_show_status_A(target_pkg=None):
    run_primitive_2_render(target_pkg)
    run_shell(f"git -C render status {target_pkg}/")

# View Active System Drift (Diff B)
def cli_show_diff_B(target_pkg=None):
    run_primitive_1_reverse_sync(target_pkg)
    if target_pkg is not None:
        run_shell(f"git -C install diff {target_pkg}/")
    else:
        run_shell("git -C install diff")

# View Active System Drift (Status B)
def cli_show_status_B(target_pkg=None):
    run_primitive_1_reverse_sync(target_pkg)
    if target_pkg is not None:
        run_shell(f"git -C install status {target_pkg}/")
    else:
        run_shell("git -C install status")

# View Dry-Run Deployment (Diff C)
def cli_show_diff_C(target_pkg=None, stat=False):
    run_primitive_1_reverse_sync(target_pkg)
    run_primitive_2_render(target_pkg)
    git_options = ['--no-index', '--color']
    if stat:
        git_options.append('--stat')
    # Direct comparison of new sandbox with the current system state
    if target_pkg is not None:
        run_shell(f"git diff {' '.join(git_options)} install/{target_pkg}/ render/{target_pkg}/")
    else:
        run_shell("git diff {' '.join(git_options)} install/ render/")

# Full Safe Deployment (make deploy)
def cli_deploy_full_sequence(target_pkg=None):
    state_registry = load_state_registry("install/state.toml")
    # TODO: check for state 'deploying' packages here, if we have deploying package, then abort.

    # --- Bulk Garbage Collection (Self-Cleaning) ---
    if target_pkg is None:
        # Check for orphan packages (present in state.toml but disabled in drift.toml)
        active_declared = get_discovered_active_packages()
        for registered_pkg in state_registry.get("packages", {}).keys():
            if registered_pkg not in active_declared:
                print(f"[GARBAGE COLLECTION] Discovered orphan package '{registered_pkg}'. Executing automatic uninstall...")
                run_primitive_7_uninstall_package(registered_pkg)

    # --- Stage 1: Security Sentinel Checks ---
    run_primitive_1_reverse_sync(target_pkg)
    
    # Scoped Drift check
    drift_dirty = False
    if target_pkg is not None:
        # Targeted mode: Only check target package folder and state.toml
        drift_dirty = git_dir_is_dirty("install/", f"install/{target_pkg}/") or git_file_is_dirty("install/", "install/state.toml")
    else:
        # Bulk mode: Check entire repository
        drift_dirty = git_repo_is_dirty("install/")
        
    if drift_dirty:
        print("[ERROR] Stage 1 sentinel found drift inside active configurations (Diff B)!")
        cli_show_diff_B(target_pkg)
        print("Abort deploy. Please review drift (via make diff-B), Adopt or Dismiss edits, then retry.")
        exit(1)
        
    print("[SUCCESS] Scoped active configurations in perfect alignment. Beginning Stage 2...")
    
    # --- Stage 2: Deploy Sequence ---
    try:
        # Step 2: Render Templates to Sandbox
        run_primitive_2_render(target_pkg)
        
        # Step 3: Lock Render History
        run_primitive_3_render_commit(target_pkg)
    except Exception as e:
        print(f"[CRITICAL FAILURE] Deployment failed in Stage 2 Render sequence: {e}")
        print("Halted immediately to prevent inconsistent system states. Please debug render/ directory.")
        exit(2)
        
    try:
        state_registry.set("packages", pkg, "deploying")
        save_state_registry("install/state.toml", state_registry)
        # Step 4: Stage Sandbox into Local DB & compute changes
        changelist = run_primitive_4_stage_render_to_install(target_pkg)
        # Step 5: Physically deploy changes to host system (Incremental mode)
        run_primitive_5_install_deployment(changelist, full_redeploy=False)
        state_registry.set("packages", pkg, "installed")
        save_state_registry("install/state.toml", state_registry)
        
    except Exception as e:
        print(f"[CRITICAL FAILURE] Deployment failed in Stage 2 Deploy sequence: {e}")
        print("================================================================================")
        print("                           MID-DEPLOYMENT FAILURE DETECTED                      ")
        print("================================================================================")
        print("The deployment has failed midway and the system state may be inconsistent.")
        pkg_arg = f" package={target_pkg}" if target_pkg else ""
        print(f"Please run: 'make rollback {pkg_arg}' to restore the last clean committed state.")
        print("WARNING: Do NOT run rollback under normal non-failing circumstances as it will")
        print("discard all uncommitted local system drift and cause data loss.")
        print("================================================================================")
        exit(2)

    try:
        # Step 6: Commit deployed configurations (Scoped to target package and state.toml if targeted)
        run_primitive_6_install_commit(target_pkg)
        print("[SUCCESS] Scoped configurations deployed successfully!")

    except Exception as e:
        print(f"[CRITICAL FAILURE] Deployment failed in Stage 2 Commit sequence: {e}")
        print("Halted immediately to prevent inconsistent system states. Please debug install/ directory then commit it.")
        exit(2)

# Rollback Recovery (make rollback)
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
        if target_pkg is None and not state_register.has_deploying_package():
            print(f"[ERROR] No package is in conflict state, rollback aborted. (use '--force' to ignore.)")
            exit(1)
        # Require user confirmation or assume it's run strictly under midway failure scenario
        print("[NOTICE] Proceeding with Rollback Recovery (Primitive 8)...")
        
    run_primitive_8_rollback_recovery(target_pkg)

# Uninstallation Utility (make uninstall)
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
        if not os.path.exists(install_pkg_dir):
            continue
            
        # A. Traverse and sync all files tracked by our database
        for relative_path in get_recursive_files(install_pkg_dir):
            system_target = resolve_system_target(pkg, relative_path, metadata)
            local_db_file = f"{install_pkg_dir}/{relative_path}"
            
            if metadata.install_method == "stow":
                # Check if the active symlink was deleted by the user or system
                if not os.path.exists(system_target) and not os.path.islink(system_target):
                    print(f"Stow System Deletion: {system_target} is missing. Pruning in install/...")
                    delete_physical_file(local_db_file)
                    
                # Check if symlink was replaced by a normal file containing local overrides
                elif os.path.isfile(system_target) and not os.path.islink(system_target):
                    print(f"Stow Replaced Link: Link {system_target} replaced by physical file. Pulling contents...")
                    copy_file_contents(system_target, local_db_file)
                    
            elif metadata.install_method == "copy":
                # Check if physical copy was deleted on host
                if not os.path.exists(system_target):
                    print(f"Copy System Deletion: {system_target} is missing. Pruning in install/...")
                    delete_physical_file(local_db_file)
                    
                # Check if physical copy contains modifications
                elif file_contents_differ(local_db_file, system_target):
                    print(f"Copy System Modification: {system_target} has drifted. Reverse-copying back to install/...")
                    copy_file_contents(system_target, local_db_file)

        # B. Audit Fully-Controlled Directory subfolders for wild/untracked files
        for rel_sub_dir in metadata.fully_controlled_dirs:
            target_sub_dir = f"{metadata.target_directory}/{rel_sub_dir}"
            local_sub_dir = f"{install_pkg_dir}/{rel_sub_dir}"
            
            if os.path.exists(target_sub_dir):
                expected_files = get_files_in_local_db(local_sub_dir)
                for file in os.listdir(target_sub_dir):
                    if file not in expected_files and not is_ignored_file(file, pkg):
                        print(f"FCD Untracked File Found: {target_sub_dir}/{file}. Syncing back to install/ to track...")
                        ensure_dir_exists(os.path.dirname(f"{local_sub_dir}/{file}"))
                        copy_file_contents(f"{target_sub_dir}/{file}", f"{local_sub_dir}/{file}")
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


def run_primitive_5_install_deployment(packages_to_redeploy, full_redeploy=False):
    # Retrieve deployment state registry from install/state.toml
    state_registry = load_state_registry("install/state.toml")
    
    for pkg in packages_to_redeploy:
        metadata = load_package_metadata(pkg)
        if not metadata.enable_install:
            continue
            
        # Is this package deployed for the first time?
        is_first_time = (pkg not in state_registry.get("packages", {}))
        
        print(f"Deploying package {pkg}...")
        
        # C. Collision Guard
        for file in get_files(f"install/{pkg}"):
            if is_ignored_by_local_ignore(file, pkg):
                continue
                
            system_target = resolve_system_target(pkg, file, metadata)
            
            # Condition 1: Stow Mode, target exists but is a regular/physical file
            if metadata.install_method == "stow":
                if os.path.exists(system_target) and not os.path.islink(system_target):
                    backup_path = f"backup/{pkg}/overwritten/{file}"
                    print(f"[GUARD WARNING] Stow conflict. Physical file found at {system_target}. Backing up...")
                    move_file_with_sudo(system_target, backup_path, metadata.sudo)
                    
            # Condition 2: Copy Mode, and it's the very first installation of this package
            elif metadata.install_method == "copy" and is_first_time:
                if os.path.exists(system_target):
                    backup_path = f"backup/{pkg}/overwritten/{file}"
                    print(f"[GUARD WARNING] Copy conflict on first install. File found at {system_target}. Backing up...")
                    move_file_with_sudo(system_target, backup_path, metadata.sudo)
        
        # D. Physical Deployment Execution
        if full_redeploy:
            # Full Redeployment Fallback utilizes high-level binary/rsync commands
            if metadata.install_method == "stow":
                run_shell(f"stow --no-folding --dotfiles -t {metadata.target_directory} {pkg}")
            elif metadata.install_method == "copy":
                # Copying without --delete to avoid sweeping unrelated target files
                run_shell_with_sudo(f"cp -r install/{pkg}/* {metadata.target_directory}/", metadata.sudo)
        else:
            # Incremental Deployment operates manually file-by-file with minimal disruption
            for file in get_files(f"install/{pkg}"):
                if is_ignored_by_local_ignore(file, pkg):
                    continue
                src_file = f"install/{pkg}/{file}"
                system_target = resolve_system_target(pkg, file, metadata) # Converts prefixes dot- to .
                
                # Infinite Loop Protection
                parent_dir = os.path.dirname(system_target)
                is_stow_linked_parent = False
                # Walk up directory tree to audit symlinks
                while parent_dir and parent_dir != "/" and parent_dir != os.path.expanduser("~"):
                    if os.path.islink(parent_dir):
                        link_target = os.readlink(parent_dir)
                        if "install/" in link_target:
                            is_stow_linked_parent = True
                            break
                    parent_dir = os.path.dirname(parent_dir)
                    
                if metadata.install_method == "stow":
                    if is_stow_linked_parent:
                        print(f"Skipping incremental stow for {system_target} as its parent directory is already symlinked.")
                        continue
                    create_symlink_manually_with_sudo(src_file, system_target, metadata.sudo)
                elif metadata.install_method == "copy":
                    copy_file_contents_with_sudo(src_file, system_target, metadata.sudo)
                
        # E. Lifecycle Hooks Trigger & State Registry Update
        if is_first_time:
            trigger_package_lifecycle_hook(pkg, "on_install", metadata)
            state_registry.set("packages", pkg, "installed")
            save_state_registry("install/state.toml", state_registry)
        else:
            trigger_package_lifecycle_hook(pkg, "on_update", metadata)


def run_primitive_6_install_commit(target_pkg=None):
    # Lock the deployed configurations inside install/ repo with scoped git actions
    if target_pkg is not None:
        # Scoped commit: stage and commit ONLY target package directory and state.toml
        run_shell(f"git -C install add {target_pkg}/ state.toml")
        run_shell(f"git -C install commit -m 'Deploy: Sync scoped package {target_pkg} at $(date)'")
    else:
        # Bulk commit: stage and commit the entire repository
        run_shell("git -C install add -A")
        run_shell("git -C install commit -m 'Deploy: Sync bulk active packages at $(date)'")


def run_primitive_7_uninstall_package(pkg):
    print(f"Starting uninstallation for package: {pkg}...")
    state_registry = load_state_registry("install/state.toml")
    
    if pkg not in state_registry.get("packages", {}):
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
    state_registry.remove("packages", pkg)
    save_state_registry("install/state.toml", state_registry)
    
    # Auto-commit the local database changes (scoped strictly to state.toml and the deleted directory)
    run_shell(f"git -C install add state.toml")
    # git rm on deleted package files
    run_shell(f"git -C install rm -r --ignore-unmatch {pkg}/")
    run_shell(f"git -C install commit -m 'Uninstall: Removed package {pkg} at $(date)'")
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
    # TODO: set given package or all packages to "installed" state.
    save_state_registry("install/state.toml", state_registry)

    print("[SUCCESS] Rollback recovery complete. Clean state restored.")
```

---

## 7. Philosophical & Operational Benefits

By implementing this architecture, the user reaps distinct Unix-style benefits:
1.  **Strict Demarcation of Merges**: Automation is restricted to simple *reverse-syncing state* and *unilateral overwrite deployment*. The human developer remains the sole merge authority. If active system changes (Diff B) are dirty, the user is presented with standard Git outputs and handles the backport to templates manually.
2.  **No Hand-Crafted State Engine**: By designating `install/` and `render/` as local Git repositories, we avoid writing custom rollback, commit-tracking, and differential history features. Git manages the hard stuff (index, diffs, conflicts).
3.  **Complete, High-Fidelity backups**: Deleted configurations and overwritten links are never silently purged; they are meticulously structured and swept into `backup/<package>/` with clean console reporting.
4.  **Extensible Lifecycles**: Adding a new step after installing Nix or NVim is as simple as dropping a standard executable bash script in `src/<package>/post-update.bash`.
