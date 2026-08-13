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
                     src/ (Templates & config.toml)
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
drift [command] [package] [--flags]
```

### A. Initialization: `drift init`
Initializes the active repository as a drift workspace.
*   **Actions**:
    1.  Verifies the main repository is tracked by Git.
    2.  Creates `.gitignore` entries to isolate `render/` and `install/` folders.
    3.  Initializes `render/` and `install/` as independent, untracked local Git repositories.
    4.  Creates default directory templates (`src/`, `config.toml`, `install/state.toml`).
*   **Terminal Output**:
    ```bash
    ✨ Initialized drift workspace!
    📁 Created render/ sandbox Git database.
    📁 Created install/ local state Git database.
    📝 Generated config.toml template.
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
3.  **Sync Render to Install**: Merges changes from `render/` to `install/`, isolating deleted, added, and modified files (`Primitive 4`).
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
*   **Operational Protection**: If run under healthy system conditions (no midway fail flag exists in workspace memory), drift will prompt a prominent confirmation screen:
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
*   **Safeguard**: Aborts with an error if the package is still declared as active/enabled in `config.toml`, preventing accidental uninstalls of packages scheduled to run in bulk deploys, unless `--force` is supplied.

---

## 5. Global Configuration: `drift.toml`

A centralized configuration file located at the repository root controls global environments and registers active packages.

```toml
# =====================================================================
# drift.toml Configuration
# =====================================================================

[workspace]
# Sandbox rendering output path
render_directory = "render"

# Deployment database tracking folder
install_directory = "install"

# Backup archive folder for collisions & deletions
backup_directory = "backup"

# Global default target directory
# Supports home expansion (~ at the beginning).
default_target_directory = "~"

# ---------------------------------------------------------------------
# Enabled Packages Registry
# ---------------------------------------------------------------------
# Key: package folder name under src/
# Value: True/False to enable or disable the package globally
[packages]
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
