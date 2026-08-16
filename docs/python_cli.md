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

---

## 2. Competitive Edge & Market Positioning

Comparing **drift** to popular dotfiles managers listed on `dotfiles.github.io/utilities`:

| Feature | **drift** (Python) | **Chezmoi** (Go) | **Dotbot** (Python) | **GNU Stow** (Perl) | **VCSH** (Shell) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Engine** | **Dual Local Git Repos** | Single Repo + BoltDB | None (YAML links) | Symlink Farm | Bare Git on `$HOME` |
| **Pipeline Stages** | **2-Stage (Render $\rightarrow$ DB $\rightarrow$ System)** | 1-Stage (Compile $\rightarrow$ System) | 1-Stage (Link) | 1-Stage (Link) | 1-Stage (Direct Git) |
| **Active Drift Audit**| **Yes (Automatic Reverse Sync)** | Yes (Manual `re-add/merge`)| No | No | Rely on Git status |
| **Dry-Run Fidelity** | **Absolute (Diff C comparison)** | Dry-run on templates | No | Stow `-n` simulation | No |
| **Mid-Fail Rollback** | **Yes (Dedicated Primitive)** | Manual cleanup | No | Stow `-D` unlink | No |

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

By replacing complex Makefiles, drift exposes a rich, intuitive, and colored CLI interface powered by Python's `Typer` and `rich`.

```
drift [--global-flags] [command] [package] [--command-flags]
```

Global Flags:  
`-C, --directory` Run as if drift is started in `<directory>` instead of current working directory.  
`--no-git-root` Stop resolving git root of cwd or -C directory, using the literal path instead.  


### A. Initialization: `drift init`
Initializes the active repository as a drift workspace.  
Only works if the directory is empty or tracked by git.  
*   **Actions**:
    1.  If the directory is empty and not tracked by git, then init an empty git repo.  
    2.  Verifies the main repository is tracked by Git, if not, raise an error.  
    3.  Change to git root, and check if it's already inited with necessary files.  
    4.  Creates `.gitignore` entries to isolate `render/` and `install/` folders.
    5.  Initializes `render/` and `install/` as independent, untracked local Git repositories.
    6.  Creates default directory templates (`src/`, `drift.toml`, `install/state.toml`).
*   **Terminal Output**:
    ```bash
    ✨ Initialized drift workspace!
    📁 Created render/ sandbox Git database.
    📁 Created install/ local state Git database.
    📝 Generated drift.toml template.
    ```

### B. Status Inspection: `drift status [package]`
Analyzes and aggregates the current system alignment. It computes three independent vectors:
*   **Template Status (A)**: Did template files under `src/` evolve compared to `render/`?
*   **System Drift Status (B)**: Has the host system drifted from `install/` due to runtime changes?
*   **Pending Delta (C)**: Are there differences waiting to be deployed from `render/` to `install/`?

#### Terminal Visual Representation:
```bash
$ drift status
🔍 Auditing configuration status across active packages...

📦 nvim (stow-method)
   ├── 💻 Template Source   [CHANGED] ── 2 files modified in src/nvim/
   ├── 🖥️ System Drift       [CLEAN]   ── Active files match state database
   └── 🚀 Deployment Pending [PENDING] ── 2 files to render and link

📦 qbittorrent (copy-method)
   ├── 💻 Template Source   [CLEAN]   ── Up to date with sandboxed render
   ├── 🖥️ System Drift       [DRIFTED] ── dot-config/qBittorrent/qBittorrent.conf modified on host
   └── 🚀 Deployment Pending [BLOCK]   ── Blocked by system drift (Run 'drift diff -s')
```

### C. Change Visualization: `drift diff [package] [options]`
Provides deep comparisons between configuration layers, outputting side-by-side terminal color diffs.

*   **`drift diff [package]` (Default, Pending Delta / Diff C)**:
    Compares what is waiting in `render/` with what is deployed on the host system.
*   **`drift diff [package] --template` (or `-t`, Diff A)**:
    Shows how your source edits changed the compiled sandbox configurations.
*   **`drift diff [package] --system` (or `-s`, Diff B)**:
    Shows system drift—precisely what the host system or GUI apps have altered.

---

### D. Safe Deployment: `drift deploy [package] [--force]`
Deploys configurations using a robust, atomic two-stage deployment engine.

#### Stage 1: Safety Guard (Sentinel)
1.  Triggers a silent **Reverse Sync** (Primitive 1) pulling the current host configuration state into `install/`.
2.  Inspects the `install/` Git tree for changes:
    *   *If changes exist (Drift is detected)*: **Aborts immediately**. It prints a warning showing the active drift (Diff B) and instructs the user:
        ```bash
        ❌ [DEPLOY ABORTED] System drift detected in package 'qbittorrent'!
        Host configurations have drifted from the state database.
        
        👉 Run 'drift diff -s qbittorrent' to view the active system modifications.
        👉 Run 'drift adopt qbittorrent' to incorporate these modifications into your template.
        👉 Run 'drift deploy qbittorrent --force' to discard system drifts and overwrite.
        ```

#### Stage 2: Sequential Compile & Apply
If no drift is detected (or `--force` is supplied):
1.  **Render**: Compiles `src/` templates into `render/` (`Primitive 2`).
2.  **Commit Render**: Automatically commits compiled sandbox history (`Primitive 3`).
3.  **Stage Render to Install**: Merges changes from `render/` to `install/`, isolating deleted, added, and modified files (`Primitive 4`).
4.  **Install Deployment**: Operates manual file-by-file copy/linking with collision checks and infinite stow loop prevention (`Primitive 5`).
5.  **Commit Install**: Scope commits the deployed configurations and `state.toml` inside the `install/` database (`Primitive 6`).

#### 🚨 Midway Fail-Fast Guard & Recovery Hint:
If a compilation, file copy, symlinking, or lifecycle script crashes during Stage 2, **drift halts execution instantly** without modifying database files, and displays a prominent emergency recovery card:

```bash
💥 [CRITICAL FAILURE] deployment failed during Step 5 (Physical Symlinking)!
   PermissionError: [Errno 13] Permission denied: '/home/user/.config/nvim/init.lua'

================================================================================
                           EMERGENCY RECOVERY REQUIRED                          
================================================================================
The deployment has failed midway, leaving your host system in an inconsistent 
and half-written state.

👉 Please fix the error above and run: 'drift rollback nvim'

This command will restore the state database, delete any half-written files, 
and execute a full deployment fallback to restore your system to the last
successfully committed configurations.

⚠️  WARNING: Do not run rollback under normal circumstances. It bypasses
   system drift checking and will discard uncommitted local system adjustments.
================================================================================
```

---

### E. Recovery: `drift rollback [package] [--force]`
*   **Mechanism (Primitive 8)**:
    1.  Resets the local state database to the last clean commit: `git -C install reset --hard HEAD`.
    2.  Computes active packages from configurations.
    3.  Performs a high-level **Full Redeploy** (recreating all symlinks and rewriting physical files) to bring the active host system back into complete alignment with the reset state database.
*   **Operational Protection**: If run under healthy system conditions (no midway fail flag exists in workspace memory such as 'install/state.toml'), drift will prompt a prominent confirmation screen:
    ```bash
    ⚠️  WARNING: No mid-deployment failure was recorded in this workspace.
       Running 'rollback' now will bypass reverse synchronization and hard-reset
       all configuration files on your system, destroying any local drift.
       
       Are you sure you want to proceed? [y/N]: 
    ```

---

### F. Synchronization: `drift adopt [package] [--interactive]`
Enables seamless bidirectional workflows. When GUI tools modify configuration files on the system, the developer can choose to "adopt" those modifications.
*   **Interactive Command Mode**:
    Allows users to interactively inspect each drift file block, choosing whether to backport it to templates, ignore it, or discard it:
    ```bash
    $ drift adopt qbittorrent --interactive
    
    Found system drift in: ~/.config/qBittorrent/qBittorrent.conf
    ------------------------------------------------------------
    + QueueingSystem\MaxActiveDownloads=5
    - QueueingSystem\MaxActiveDownloads=3
    ------------------------------------------------------------
    
    How would you like to reconcile this block?
    [1] Backport to template source (src/qbittorrent/dot-config/qBittorrent/qBittorrent.conf)
    [2] Accept and commit in local state database only (Accept runtime change without changing source template)
    [3] Discard host change (Will be overwritten on next deploy)
    [4] Skip file
    
    Select option [1-4]: 
    ```

---

### G. Uninstallation: `drift uninstall <package> [--force]`
*   **Mechanism (Primitive 7)**:
    1.  Identifies files belonging to the package.
    2.  Removes active symlinks (stow method) or deletes target files (copy method).
    3.  **Collision Guard Rollback**: Restores original physical host files that were backed up inside `backup/<package>/overwritten/` to their original locations.
    4.  Updates the `install/state.toml` database registry.
    5.  Performs a Git stage, directory removal, and auto-commit inside the `install/` Git repository:
        ```bash
        git -C install add state.toml
        git -C install rm -r --ignore-unmatch <package>/
        git -C install commit -m "Uninstall: Removed package <package>"
        ```
*   **Safeguard**: Aborts with an error if the package is still declared as active/enabled in `drift.toml`, preventing accidental uninstalls of packages scheduled to run in bulk deploys, unless `--force` is supplied.

---  

### H. Package Creation: `drift new <package> [config_filename] [--force]`
#### **Common Usage**
Create a new package directory with the default `package.toml` configuration file:
```bash
$ drift new nvim
✨ Package 'nvim' created successfully!
📝 Generated package.toml at src/nvim/package.toml.
```

#### **Details & Deep Probing Logic**
*   **Command Signature**: `drift new <package> [config_filename] [--force / -f]`
*   **Optional Arguments**:
    - `<config_filename>`: Explicitly name the config file as `drift_package.toml` or `package.toml` (defaults to `package.toml`).
    - `--force / -f`: Forcefully overwrites any existing config file inside the package.
*   **Probing Guard**:
    - The CLI first checks if *any* configuration file already exists inside the package directory `src/<package>/`.
    - Specifically, it probes following files if `<config_filename>` is not present:
      1. `drift_package.toml`
      2. `package.toml`
      3. Any engine-templated configuration such as `package.<engine>.toml` or `drift_package.<engine>.toml`.  

      TODO: If config_filename is given, only check if any file will render to that name.  

    - If any configuration file exists:  
      - If `--force` is **not** supplied: The command halts and prints an error, preventing you from accidentally losing an existing configuration.
      - If `--force` **is** supplied: Overwrites the configuration with the default template.
*   **Default Configuration Output**:
    ```toml
    # src/<package>/package.toml
    [package]
    name = "<package>"
    install_method = "stow"
    enable_render = true
    enable_install = true
    target_directory = "~"
    fully_controlled_dirs = []
    sudo = false
    ```

---

### I. Resource Import: `drift add <package> <realfile> [--force]`
#### **Common Usage**
Import a physical active system configuration file (like `~/.config/nvim/init.lua`) into your declarative source folder:
```bash
$ drift add nvim ~/.config/nvim/init.lua
🚀 Imported ~/.config/nvim/init.lua into nvim package!
📁 Copied contents to src/nvim/dot-config/nvim/init.lua (Translated dot-prefix).
```

#### **Details & Deep Logic**
*   **Command Signature**: `drift add <package> <realfile> [--force / -f]`
*   **Link State Safeguard**:
    - The command checks if `realfile` is a symlink pointing into the `install/` state database repository. If it is, the CLI aborts because the file is already under drift's active governance.
*   **Symlink Resolution Policy**:
    - If `realfile` is a symlink pointing *elsewhere* (not to the state repository), or if it's a directory containing symlink entries, drift **recursively resolves all symlinks and copies their actual physical contents** rather than copying the links. This ensures complete package self-containment, portability, and reproducibility across fresh machines.
*   **Path Resolution & prefix Translation**:
    - Computes `package_config.target_directory` (with a fallback to `workspace_config.default_target_directory`).
    - Resolves both paths absolutely and computes the relative path: `rel_path = os.path.relpath(abs_realfile, abs_target_dir)`.
    - **Dot-Prefix Translation (Symmetric Symmetry)**:
      - Directories and files starting with standard dots `.` must be translated to a `dot-` prefix to maintain Git-friendly name handling in the repository.
      - *Example*: `.config/nvim/init.lua` is translated to `dot-config/nvim/init.lua`.
*   **Collision Guard & Backup Policy**:
    - Before writing, drift checks if there's an existing file or template in the source directory that compiles/renders to the same `realfile` (e.g., checking for both `src/<package>/dot-config/nvim/init.lua` and templates like `init.envst.lua`).
    - If a collision occurs:
      - Without `--force`: The command halts and warns of the collision.
      - With `--force`: Drift **safely moves the colliding template/file to `backup/<package>/deleted_files/`** to avoid irreversible data loss, before overwriting it with the new source file.

---  

### TODO: help commands  
TODO:
`drift help` will show the overall model of drift, and its basic work flow.
`drift help package` will show the concept 'package' in drift, and the concept of package config file.
`drift help src` will show what the source directory do in drift and how to write a basic drift package.
`drift help render` will show the concept 'render' in drift and what the render directory do in drift.  
`drift help install` will show the concept 'install process' in drift and what the install directory do in drift.
`drift help {package.toml | drift_package.toml}` will show a package.toml template containing all available options and its meaning.  
`drift help {drift.toml}` will show a drift.toml template containing all available options and its meaning.  

These help documents should print to PAGER unless pipe is used in output.

### J. Low-Level Control Commands
These commands are intended for advanced troubleshooting, continuous integration, or automation scripts.

#### **1. Low-Level Render: `drift render [<package>]`**
##### **Common Usage**
```bash
$ drift render
🚀 Rendering all active package templates to sandbox render/
```
##### **Details**
*   If `<package>` is provided: Recursively compiles templates and copies files for *only* that package.
*   If `<package>` is omitted: Recursively processes *all* enabled packages.

#### **2. Low-Level Commit: `drift render-commit -m "message"`**
##### **Common Usage**
```bash
$ drift render-commit -m "Render: Update Neovim templates"
✨ Committed render sandbox changes.
```
##### **Details**
*   Stages all compiled/copied configurations inside the sandbox using `git add -A` and commits them under the `render/` repository with the specified message. Returns gracefully if the repository is already clean.  

#### **3. Low-Level Reverse Sync: `drift reverse-sync [<package>]`**
##### **Common Usage**
```bash
$ drift reverse-sync nvim
🔍 Pulling live configuration overrides from host system for 'nvim' into install state...
```
##### **Details**
*   **Mechanism (Primitive 1)**: Traverses the live target directories on the system. It checks for files deleted by the user/system, files with modifications (pulling their overrides), and scans Fully-Controlled Directories (FCD) for untracked/new files, copy-syncing them back to `install/`.
*   If `<package>` is provided: Scopes the reverse synchronization strictly to that package.
*   If `<package>` is omitted: Bulk reverse-syncs all enabled packages.

#### **4. Low-Level Sandbox Staging: `drift stage [<package>] [--force]`**
##### **Common Usage**
```bash
$ drift stage nvim
🚀 Staging compiled sandbox templates from render/ to install/ state database...
```
##### **Details**
*   **Mechanism (Primitive 4)**: Reconciles the sandbox `render/` folder into the `install/` state database folder. It computes added, modified, or deleted files between the directories and moves deprecated configs to `backup/`.
*   **Guard**: If the `install/` repository has uncommitted local modifications, this command will abort **unless** the `--force / -f` flag is supplied, in which case it overwrites them.

#### **5. Low-Level State Application: `drift apply [<package>] [--full]`**
##### **Common Usage**
```bash
$ drift apply nvim
🚀 Applying 'nvim' configurations from state database to active host system...
```
##### **Details**
*   **Mechanism (Primitive 5)**: Applies files inside the `install/` state database to the live target paths on the host system. To avoid naming confusion with standard package managers or the registration of new dotfiles, this command is named **`apply`** (representing the physical application of the database state to the host).
*   **Options**:
    - `--full`: Runs a heavy-duty **Full Redeploy** (using high-level Stow or copy directory utilities) instead of the surgical, incremental file-by-file update engine.

#### **6. Low-Level State Commit: `drift install-commit -m "message"`**
##### **Common Usage**
```bash
$ drift install-commit -m "Deploy: Manual update of Neovim configs"
✨ Committed state database changes.
```
##### **Details**
*   **Mechanism (Primitive 6)**: Stages and locks the deployed configurations inside the `install/` local state tracking repository with an automated commit.
*   **Parameters**:
    - `-m "message"` (Required): Specifying the commit message.
    - If a specific package argument was used or is tracked, it scopes the `git add` and `git commit` actions strictly to that package folder (e.g. `install/<package>/`) and `state.toml`. Otherwise, it commits the entire repository.

## 5. Global Configuration: `drift.toml`

A centralized configuration file located at the repository root controls global environments and registers active packages.

```toml
# =====================================================================
# drift.toml Configuration
# =====================================================================

[workspace]
# Source directory for packages, default value is "src"
source_directory = "src"

# Sandbox rendering output path
render_directory = "render"

# Deployment database tracking folder
install_directory = "install"

# Backup archive folder for collisions & deletions
backup_directory = "backup"

# Global default target directory
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

---

## 6. Blueprint for Python Implementation

### A. Dependency Stack
1.  **`typer[all]`**: High-performance, declarative Python CLI framework. Uses `click` under the hood.
2.  **`rich`**: Beautiful terminal formatting, syntax highlighting, markdown rendering, and interactive confirmation menus.
3.  **`GitPython`**: Robust Python API wrapping the Git binary. Handles indexing, diffing, resetting, staging, and committing.
4.  **`jinja2` / `chevron`**: Fast, flexible template rendering engines to support advanced `.mustache` or `.jinja` template structures natively.

### B. High-Signal Console Aesthetics (Rich styling)
To maintain the polished feeling of a modern tool, console feedback utilizes strict semantic colors:
*   `✨` **Gold/Yellow**: Primary action success / Initiation.
*   `🔍` **Cyan**: Analysis, search, and status checks.
*   `🚀` **Green**: Deployments, additions, and successful updates.
*   `❌` **Bold Red**: Sentinel-blocked operations, aborts, and configuration errors.
*   `💥` **Inverted Bold Red**: Critical execution midway crashes.
*   `⚠️` **Orange/Yellow**: Collision guard warnings, safety backup prompts.
