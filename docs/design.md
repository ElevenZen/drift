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

By separating rendering from deployment and turning both folders into Git databases, we gain absolute, safe visibility over configurations without automated, dangerous, or unintended merges on the live system. It natively supports both **`symlink` (symlink-based)** and **`copy` (copy-based)** deployment methods, with built-in privileges management (`sudo`), granular package-level overrides, and strongly-typed machine-readable outputs (`--json`).

---

## 2. High-Level Architecture & Interaction Flows

The architecture separates configurations into four distinct physical and logical tiers:

```
[1. Declarative Source]      src/ (Templates & Scripts)
  │
  ▼  Stage 2: Render (drift deploy)
[2. Sandbox Render Zone]     render/ (isolated Git repo)
  │
  ▼  Stage 2: Staging (Diff Δ / Diff A)
[3. Local State Database]    install/ (live tracking Git repo)
  │ ▲
  │ │  Stage 1: Reverse-sync (Diff B) / drift adopt
  ▼ │  Stage 2: Apply (relative symlinks or physical copy)
[4. Active Host System]      ~/* or /etc/*
```

### Tier Descriptions & Directory Structure

1.  **Declarative Source (`src/`, `config/`)**:
    *   Contains template configurations, raw config files, and global shell variables.
    *   An explicit list of enabled packages for the active machine is defined in **`config/drift_workspace.toml`**.
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
    *   Files here are either relative symlinks pointing to `install/<package>/...` (`symlink` method) or physical file copies (`copy` method).

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
A GUI application (like qBittorrent, desktop theme manager, or IDE preferences) has rewritten its configuration file in the background, or you modified a file in your home directory directly.
1.  **Detection & Sentinel Halt**:
    *   Running `drift deploy` trips the Stage 1 Sentinel guard (exit code `3: DRIFT_DETECTED`) if uncommitted system changes exist.
2.  **Audit & Inspection**:
    *   **Status Overview**: Run `drift status` to inspect which package(s) have drifted (`[B] System: DRIFTED`).
    *   **Terminal Diff**: Run `drift diff -s qbittorrent` to visualize Diff B changes.
    *   **Visual Side-by-Side Diff**: Run `drift diff -s -y qbittorrent` to inspect changed file pairs side-by-side in your editor (`$VISUAL` / `$EDITOR`, such as Neovim, Vim, VS Code, or GNU Emacs).
3.  **Reconciliation**:
    *   **Adopt**: Run `drift adopt qbittorrent` (or `drift adopt qbittorrent -i`) to incorporate modifications from `install/` back to your declarative templates under `src/`.
    *   **Dismiss / Overwrite**: Run `drift deploy qbittorrent --force`. Stage 1 detects the drift, commits a **Drift Snapshot** into the `install/` Git history (guaranteeing disaster recovery auditability), and proceeds to compile, stage, and overwrite active host configurations with clean templates from `src/`.

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

* **Execution Flow (`drift deploy`)**:
  * **Stage 1: Pre-Flight Safety (Reverse Sync & Audit)**:
    * Primitive 1: Reverse Sync (`drift reverse-sync`).
    * Drift Audit: Check `git -C install status --porcelain`.
      * *If Drift Detected (Dirty)*: **ABORT DEPLOY** (Display Diff B, guide `drift adopt` or `--force`).
      * *If Clean Slate*: Proceed to Stage 2.
  * **Stage 2: Core Staged Pipeline**:
    * Primitive 2: Render Packages (`src/` $\rightarrow$ `render/`).
    * Primitive 3: Render Repo Commit (`git -C render commit`).
    * Primitive 4: Stage Render to Install (`render/` $\rightarrow$ `install/`).
    * Primitive 5: Install Repo Deployment (Deploy to host system targets via native relative symlinks or physical copy).
    * Primitive 6: Install Repo Commit (`git -C install commit`).
  * **Stage 3: Post-Deployment Maintenance**:
    * Primitive 9: Workspace GC (Bulk unused repo cleanup).
  * **Deploy Success**

### Primitive 1: Reverse Sync (System $\rightarrow$ `install/` [Low-level: `drift reverse-sync`])
Unconditionally pulls the current host configuration state into the `install/` state Git repository using targeted, $O(N_{\text{pkg}})$ comparisons rather than scanning the entire target host directory (`$HOME`):

1.  **Tracked Package Files Check (`sync_tracked_files`)**:
    *   Runs `compare_folders(src_dir=install/<package>, dst_dir=target_directory, src_only=True, translate_mode="forward")` to probe only the package's tracked files on the host system.
    *   **Deleted on System**: If a tracked file or directory exists in `install/` but is missing on the host system, it is symmetrically removed from the local state repository (`install/`).
    *   **Modified on System**: If a file on the host system contains modifications, it is reverse-copied back to `install/`.
    *   **Type Changes**: If a file changed into a directory on host (or vice-versa), its contents are synced into `install/`.

2.  **Scoped Fully-Controlled Directories (`sync_fully_controlled_dirs`)**:
    *   For directories configured under `fully_controlled_dirs` (FCD), comparisons are scoped strictly to those specific subdirectories (e.g. `~/.config/nvim`), reverse-syncing any wild/untracked files or deletions without traversing the rest of the host filesystem.
    *   **Empty Folder Reverse-Sync**: Scans host FCDs with `DirMode.ONLY_EMPTY_DIR` to discover wild empty directories, planning `FileActionType.CREATE_KEEP_FILE` to create `<rel_dir>/.drift_keep` in `install/<package>/`.
    *   **Sentinel Stub Pruning**: When a previously empty directory is populated with child files on the host, ancestor directory inspection generates `FileActionType.DELETE_ITEM` to prune the obsolete `.drift_keep` stub from `install/`.
    *   **Deleted Directory Tree Pruning**: In reverse mode (`reverse_mode=True`), orphan pruning generates `FileActionType.DELETE_TREE` in `install/` to purge the deleted directory tree from the staging database.

### Primitive 2: Render (`src/` $\rightarrow$ `render/` [Low-level: `drift render`])
*   **Pre-Flight Requirements & Probe Check (Zero-Cost Skipping Before Render)**:
    Before compiling any templates or intermediate input files, Drift evaluates declarative `[requirements]` (OS, CPU architecture, Linux distro, required binaries in `$PATH`, environment variables, and LAN IP/CIDR) and the dynamic `probe` hook. If host requirements are not met, the package is immediately and gracefully skipped (`status = "SKIPPED"`). No template engines are invoked, no intermediate input files are rendered, and no files are written to `render/<pkg>/`, allowing remaining active packages to proceed cleanly.
*   **Empty Folder Sentinel Generation**:
    Scans the package with `DirMode.ONLY_EMPTY_DIR`. For each empty directory in `src/`, [`render_or_copy_file`](../src/drift/primitives/render_package.py) creates a 0-byte `.drift_keep` sentinel file inside the rendered target directory in `render/<pkg>/`, allowing Git to track empty folder hierarchies.
*   **Lifecycle Hook Triggers**: Triggers `pre_source` before reading source templates and `post_render` hook upon successful compilation.
*   **Intermediate Lifecycle Isolation**: Routes source scripts from `src/<pkg>/drift_hooks/` to internal sandbox directory `render/<pkg>/.drift/hooks/`, keeping them strictly isolated from host deployments.
*   **Render Collision Detection (Strict Error)**:
    *   Drift tracks destination output paths across all rendered templates and copied static files within each package.
    *   If multiple source files in a package compile, template-expand, or copy to the same destination relative path in `render/<pkg>/` (e.g. `config.json.envst` and `config.json.mustache`, `init.lua` and `init.lua.envst`, or `drift_hooks/bootstrap.sh` and `drift_hooks/bootstrap.sh.envst`), the rendering pipeline immediately halts with a descriptive `RenderCollisionError` before staging or deployment, preventing silent data overwrite and non-deterministic precedence.

### Primitive 3: Render Repo Commit [Low-level: `drift render-commit`]
Automatically commits any updates inside the `render/` sandbox Git repository.

### Primitive 4: Stage Render to Install [Low-level: `drift stage`]
Reconciles the sandbox `render/` folder into the `install/` database:
*   **Structural Fidelity Invariant**: Preserves the structure and file contents of `render/<pkg>/` inside `install/<pkg>/` with complete 1:1 fidelity, including `.drift_keep` stub files. No synthetic files or ignore artifacts are generated in `install/`. All payload files, `.drift/.drift_ignore`, `.drift/drift_package.toml`, `.drift/hooks/`, and `.drift/render/` are mirrored strictly 1:1.
*   **Topological Staging Sequence**: `prepare_stage_packages` resolves inter-package dependencies across the package universe (`resolve_target_package_order`), sequencing staging actions in topological order (`StagePlan.packages_stage_order`).
*   **Mechanism**: Compiles a declarative staging plan (`PackageStagePlan`) detailing operations (`DELETE_ITEM`, `DELETE_TREE`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `CREATE_KEEP_FILE`) via single-pass folder comparison between `render/` and `install/`. Synchronizes files using the unified delivery engine.
*   **Stage Isolation**: Does **not** touch active system target files. All physical system file operations are deferred to Primitive 5.
*   **State Machine**: Sets the package state to **`"staging"`** (transient guard) at the start, and transitions to **`"staged"`** (stable mid-state) upon successful completion. This indicates the database is ready but the system is not yet updated.

### Primitive 5: Install Repo Deployment [Low-level: `drift apply`]
Applies changes to the physical active system across a two-phase architecture:
*   **Pre-Flight Inspection (`prepare_install`)**: Validates readiness guards (`assert_packages_install_ready`), verifies hook file existence, checks escalation privileges, audits cross-package path collisions, and resolves prerequisite topological deploy order (`InstallPlan`).
*   **Execution Phase (`execute_install`)**: Deploys prerequisites first using strongly-typed `InstallConfig(force, dry_run, flags, no_deps)`.
*   **Collision Guard & Planning (`plan_package_install`)**: Pure read-only per-path planner compiling inspectable `PackageInstallPlan`. Backs up colliding physical files to `backup/<package>/overwritten/` with `dot-` prefix translation (`decode_dot_prefix`), and historical orphans to `backup/<package>/deleted_files/`.
*   **Empty Folder Deployment**: [`filter_deployable_files`](../src/drift/core/ignore.py) with `include_empty_dirs=True` records empty directory leaf nodes into `deployed_files` (translating `.drift_keep` into its parent folder). Empty directories are deployed as concrete directories via `FileActionType.CREATE_DIR` (`mkdir -p`) and never symlinked. Empty directories are excluded from cross-package conflict assertions (`include_empty_dirs=False`), allowing multiple packages to share folder hierarchies.
*   **Privilege & State Recording**: Deploys files with root escalation if `sudo = true` is configured in `drift_package.toml`. The `sudo` boolean, target directory, install method, and `deployed_files` manifest are atomically updated in `install/state.toml`.
*   **Hooks**: Triggers `pre_install` / `pre_update` before deployment, and `post_install` / `post_update` after successful deployment (skipped during `--dry-run`).
*   **State Machine**: Sets the package state to **`"installing"`** (transient guard) at the start, and transitions to **`"installed"`** (final state) upon successful completion (skipped during `--dry-run`).
*   **Symlink Mode**: Deploys relative symlinks from host target paths to `install/<pkg>/` via Drift's native linker.
*   **Copy Mode**: Copies files to `target_directory` (prefixed with `sudo` if configured).

### Primitive 6: Install Repo Commit [Low-level: `drift install-commit`]
Locks the deployed configurations and `state.toml` into the local state database with an automated commit.

### Primitive 7: Uninstall Repo Package [High-level: `drift uninstall`]
Removes or detaches packages from the system using strongly-typed `UninstallConfig(force, dry_run, detach, no_deps, flags)`:
1.  **Declarative Plan-then-Execute Architecture**:
    *   **Context Gathering (`PackageUninstallContext`)**: Gathers domain-level parameters (`pkg_name`, `target_dir`, `install_method`, `deployed_files`, `sudo`, `install_pkg_dir`, `backup_pkg_dir`, `drift_root`, `hooks`, `detach`, `is_missing_install_dir`) directly from `StateRegistry` without retaining heavy `PackageConfig` instances. The recorded `sudo` privilege is preserved from `state.toml`, guaranteeing consistent elevated permissions even if the package config in `install/` was altered or missing.
    *   **Action Execution Context Derivation**: Derives an `FileActionExecutionContext` carrying execution flags (`sudo`, `resolve_symlinks`). Backup restoration collision paths are planned directly into `deleted_files/` via `plan_backup_restoration`'s `backup_subfolder=BackupSubfolder.DELETED_FILES` and `is_first_time=True`, ensuring any pre-existing host files or directories colliding with restored files or ancestors are backed up to `deleted_files/` rather than corrupting the active `overwritten/` backup store or unbacked in-place overwrites.
    *   **Discrete Action Decomposition (`plan_package_uninstall`)**:
        *   *Standard Uninstall*: Compiles `plan_file_removals` (`DELETE_ITEM`) followed by `plan_backup_restoration` (emitting an `INFO_MESSAGE` header, ensuring ancestor directory creation with `ENSURE_DIR`, and restoring original files via `CREATE_COPY`). Deployed empty directories are pruned using [`remove_file_or_empty_dir`](../src/drift/utils/file_ops.py), which safely removes empty folders via `rmdir` while skipping populated directories with a warning. Shared empty directories are protected via reference counting and only removed when their reference count reaches 0.
        *   *Detach Mode*: Compiles `plan_symlink_conversions` (`DELETE_ITEM` symlinks and `CREATE_COPY` physical files from `install/<pkg>/`), leaving `overwritten/` backups intact.
    *   **Unified Action Execution (`execute_package_uninstall`)**: Applies planned operations sequentially via `execute_delivery_actions`.
    *   **Zero-Mutation Dry-Run**: Under `--dry-run`, Drift simulates uninstallation without touching host files, state registry, or executing lifecycle hooks, rendering an inspectable structured summary via `UninstallResult.format_text(dry_run=True)`.
2.  **Dependency Safeguards & Reverse Topological Order**:
    *   **Pre-Flight Broken Dependency Guard**: Invokes `assert_no_broken_dependencies_on_uninstall` to verify that remaining installed packages do not depend on any targeted packages (bypassed if `force=True` or `no_deps=True`).
    *   **Reverse Topological Order**: Resolves `resolve_package_uninstall_order` so dependent packages are uninstalled before prerequisites, guaranteeing cleanup hooks execute while prerequisite configurations remain intact.
    *   **Graceful Missing Directory Handling**: If a package's directory is missing in `install/`, Drift emits a warning and gracefully removes the record from `state.toml` without crashing.
3.  **Standard Uninstall Mode (Default)**:
    *   **Unlink or Delete**: Unlinks symlinks or deletes physical files.
    *   **Rollback Collision Guard**: Restores original host files backed up in `backup/<package>/overwritten/`.
    *   **Update Registry**: Removes the package from the state database (`install/state.toml`) and commits uninstallation.
4.  **Detach/Eject Mode (`--detach`)**:
    *   **Keep Configuration**: Stops managing this package via Drift, but preserves the current configuration files active on the system (e.g. freezing them as permanent configurations).
    *   **Symlink to Copy Conversion**: If the package was installed using `symlink` (symlinking), the engine recursively iterates through the deployed files, removes the symlink, and copies the physical file counterpart from `install/<pkg>/` to the active host target path.
    *   **Backups Kept Intact**: Leaves the user's historical original backups inside `backup/<pkg>/overwritten/` completely untouched (does not restore them).
    *   **Clean Database Decouple**: Unregisters the package from `state.toml` and deletes the local `install/<pkg>` directory, fully decoupling the repository from the active host system without deleting configurations.

### Primitive 8: Rollback Recovery [High-level: `drift rollback`]
Restores system configurations and the local state database to the last clean, committed state after an aborted or failed deployment:
*   **Unified Reverse Topological Order**: Executes a single unified `resolve_package_uninstall_order` across all rollback candidate packages.
*   **Dispatch by Classification**: Dispatches committed packages to redeployment (`rollback_redeploy_committed_package`) and uncommitted first-time packages to uninstallation (`rollback_uninstalled_first_time_package`) one-by-one in ordered sequence.
*   **State Reset**: Resets `install/` to HEAD, purges untracked files via `git clean -fd`, and redeploys committed packages with `force=True`.

### Primitive 9: Workspace Garbage Collection [High-level: `drift gc`]
Identifies and cleans up workspace anomalies, orphaned packages, zombie database directories, and ghost registry records across 3 structured stages:
1.  **Orphan Package Uninstallation (Stage 1)**:
    *   Scans `install/state.toml` for packages marked as `"installed"`.
    *   Cross-references against `config/drift_workspace.toml` (`is_package_enabled(pkg)`). Packages present in state but disabled or removed from configuration declarations are identified as *orphans*.
    *   Executes **Primitive 7 (Uninstall)** with `UninstallConfig(force=True, dry_run=dry_run, flags=flags)`:
        *   Triggers package `pre_uninstall` and `post_uninstall` lifecycle hooks (unless `--no-hooks` is active).
        *   Unlinks symlinks or deletes physical files from active host targets.
        *   Restores original backed-up host files from `backup/<package>/overwritten/`.
        *   Removes `install/<pkg>/` and prunes empty `backup/<pkg>/` directories.
        *   Deletes package records from `install/state.toml`.
2.  **Ghost Package Registry Purge (Stage 1b)**:
    *   Scans `install/state.toml` for packages whose directories in `install/` are missing on disk.
    *   Removes ghost records from `StateRegistry` and saves `state.toml`, committing the cleanup to Git.
3.  **Database Folder Purge (Stage 2 - `render/` and `install/`)**:

    *   **`render/` Purge Rules (`purge_render_folders`)**:
        *   Scans visible subdirectories in `render/` (ignoring `.git`, `config/` via `CONFIG_DIR_NAME`, and `FORBIDDEN_PACKAGE_NAMES`).
        *   Purges a package directory if:
            1. *Zombie folder*: Lacks any valid package config file (`drift_package.toml`, `drift_package.yaml`, `drift_package.json`, `drift_package.yml`).
            2. *Disabled package*: Package is disabled in workspace configuration (`packages.enable.<pkg> = false` or `packages.enable.default = false`).
            3. *Source deleted*: The corresponding package source directory in `src/<pkg>` has been deleted.
        *   Removes the directory via `shutil.rmtree` (or logs under `--dry-run`).
    *   **`install/` Purge Rules (`purge_install_folders`)**:
        *   Scans visible subdirectories in `install/` (ignoring `.git` and `FORBIDDEN_PACKAGE_NAMES`).
        *   Queries `install/state.toml` to protect registered packages (registered packages undergo graceful Stage 1 uninstallation instead of direct disk deletion).
        *   Purges an `install/` package directory if:
            1. *Zombie folder*: Lacks any valid package config file.
            2. *Unregistered & Obsolete*: Directory is **not** registered in `state.toml` AND (is disabled in workspace config OR missing from `src/`).
        *   Removes the directory via `shutil.rmtree` (or logs under `--dry-run`).
3.  **Scoped Database Git Commits (Stage 3)**:
    *   When not running in `--dry-run` mode:
        *   Auto-stages and commits purged folder deletions in the `render/` Git repository (scoped strictly to purged package names).
        *   Auto-stages and commits purged folder deletions in the `install/` Git repository (scoped strictly to purged package names).
4.  **Structured Observability & CLI Options**:
    *   Returns strongly-typed `GcResult` dataclass with detailed uninstalled orphan results, purged zombie lists, and Git commit metadata.
    *   Supports `--dry-run` for non-destructive inspection, `--no-hooks` for bypassing uninstall hooks, and `--json` for machine-readable output.

### Primitive 10: Package Creation [High-level: `drift new`]
Scaffolds a new declarative package inside the `src/` directory:
1.  Creates the `src/<package_name>` directory.
2.  Generates a default `drift_package.toml` with standard safe defaults (e.g. `install_method = "symlink"`).
3.  Features built-in probing guards to prevent accidental overwriting of existing package configurations unless `--force` is used.

### Primitive 11: Resource Import [High-level: `drift add`]
Imports existing, active host system configuration files directly into the declarative source repository using a decoupled **Plan & Execute** pipeline:
1.  **Read-Only Planning Phase (`prepare_add_resources`)**:
    *   Validates package source directory readiness (`assert_package_source_exists`).
    *   Triggers declarative pre-source hooks (`trigger_pre_source_hook`).
    *   Resolves target base directory, render engines, and ignore rules (`DriftIgnore`).
    *   Resolves worklist with symlink capture and dot-prefix reverse translation (`.config` $\rightarrow$ `dot-config`).
    *   Performs global conflict checks (`assert_no_import_conflicts`); halts if an import would collide with existing templates or blocking paths.
    *   Compiles declarative `AddResourcePlan` containing all `PlannedResourceImport` items.
2.  **Execution Phase (`execute_add_resources`)**:
    *   In `--dry-run` simulation mode, prints plan summaries with zero filesystem mutations.
    *   In physical execution mode, creates required parent directories and copies system files into the package source tree.
    *   Returns structured `AddResourceResult` containing the execution outcome and typed `plan`.

### Primitive 12: Package Runtime Health Checks [High-level: `drift health`]
Executes live runtime health check probe scripts declared in `drift_package.toml` (`[hooks] health = ...`):
1.  Executes the declared health probe script with the working directory (`cwd`) set to the probe script's directory (`hook_path.parent`), allowing sibling helpers to be sourced naturally. The package's host target directory is accessible via `$drift_package_target_dir`.
2.  Captures execution exit codes, standard output, standard error, and timing metrics under strict timeout constraints.
3.  Aggregates health diagnostics across all installed packages with rich status displays and machine-readable `--json` summaries.

### Primitive 13: Repository Cloning & Legacy Migration [High-level: `drift clone`]
Clones a remote Git repository and automatically bootstraps the workspace:
1.  **Case A (Drift Workspace)**: Clones the repository and immediately triggers non-destructive self-healing (`repair_drift_workspace`) to reconstruct local databases (`render/.git`, `install/.git`, `state.toml`), `.gitignore` rules, and local config templates (`config/drift_workspace.local.toml`, `config/secrets.env`).
2.  **Case B (Plain / Legacy Dotfiles)**: Migrates plain dotfiles into `src/<pkg_name>/`, initializes full Drift workspace infrastructure, generates `drift_package.toml` and `.drift_ignore`, and enables the package in `config/drift_workspace.toml`.

### Primitive 14: Workspace Diagnostics & Self-Healing [High-level: `drift repair`]
Audits and self-heals workspace structure, repositories, configuration templates, and secrets:
1.  Reconstructs missing Git state databases (`render/.git`, `install/.git`) and `install/state.toml`.
2.  Rebuilds `.gitignore` isolation rules.
3.  Generates default templates for `config/drift_workspace.local.toml` and `config/secrets.env` if missing.

### Primitive 15: Change Visualization & Differential Inspection [High-level: `drift diff`]
Compares configuration layers across templates, sandbox compilations, state databases, and active host files:
1.  **Diff Vectors**:
    *   **Diff Δ (Pending Delta, Default)**: Compares `render/` sandbox against `install/` state DB to show exactly what changes will be applied to the system.
    *   **Diff A (Template Evolution, `-t` / `--template`)**: Compares `src/` declarations against compiled `render/` output.
    *   **Diff B (Active System Drift, `-s` / `--system`)**: Compares active host files against `install/` state DB after reverse-sync.
2.  **Five-Layer Architecture (`workspace_diff.py`)**:
    *   **Layer 1 (Worklist Classification)**: `get_pending_delta_worklist` parses `install/state.toml` and compares rendered vs installed files.
    *   **Layer 2 (Diff Pair Collectors)**: `collect_repo_diff_pairs` and `collect_pending_delta_pairs` extract side-by-side file pairs (generating synthetic empty temp files for added/deleted items).
    *   **Layer 3 (Terminal Git Diff Runners)**: `run_repo_diff` and `run_pending_delta_diff` invoke `git diff --no-index` with `exclude_patterns = DEFAULT_DIFF_EXCLUDE_PATTERNS`.
    *   **Layer 4 (Diff Strategy Dispatchers)**: `run_side_by_side_diff` and `run_terminal_diff` route execution based on `-y` / `--side-by-side`.
    *   **Layer 5 (Primitive Entry Point)**: `run_primitive_15_workspace_diff` coordinates state synchronization and dispatches to visual or terminal mode.
3.  **Interactive Side-by-Side Visual Diffing (`-y` / `--side-by-side`)**:
    *   Automatically probes user environment (`$VISUAL`, `$EDITOR`, Neovim, Vim, VS Code, GNU Emacs).
    *   Formulates editor-specific invocation flags (e.g. `nvim -p -d fileA fileB`, `vim -p -d fileA fileB`, `code --wait --diff fileA fileB`, `emacs -nw --eval '(ediff-files ...)'`) across multi-tab split viewports.
4.  **Pathspec Exclusion Rules**:
    *   Uses `DEFAULT_DIFF_EXCLUDE_PATTERNS` (`:(exclude)*<pattern>*`) to filter internal synthetic artifacts (`.gitignore*`) and editor/OS temp files (`TEMPORARY_FILE_PATTERNS`), while preserving user-authored package configuration modifications (`drift_package.toml`, `.drift_ignore`).

### Primitive 16: Workspace Status Inspection [High-level: `drift status`]
Aggregates and audits current workspace alignment across three distinct dimensions (Template Evolution `[A]`, Active System Drift `[B]`, and Staged Pending Delta `[Δ]`), providing instant health and drift visibility.

---

## 4. User-Facing Operations (CLI Overview)

The `drift` Python command provides a unified interface for all primitives and high-level workflows with `--json` machine-readable output support.

### High-Level Commands (Ordered by Lifecycle)
*   **`drift clone <repository> [destination] [-b/--branch <branch>] [--depth <N>] [--no-repair] [--json]`**: Clones a Git repo and auto-bootstraps/repairs the Drift workspace (Primitive 13).
*   **`drift init [-f/--force] [--no-git-root] [--json]`**: Initializes a new drift workspace.
*   **`drift new <package> [-f/--force] [-t/--target <dir>] [-m/--method <symlink|copy>] [--json]`**: Scaffolds a new dotfiles package (Primitive 10).
*   **`drift add <package> <paths...> [--dry-run] [--no-hooks] [--json]`**: Imports active system files into a declarative package (Primitive 11).
*   **`drift adopt [packages...] [-i/--interactive] [--accept-conflicts] [-f/--force] [--dry-run] [--no-hooks] [--json]`**: Reconciles system drift into templates.
*   **`drift deploy [packages...] [-f/--force] [--no-hooks] [--json]`**: Atomic Two-Stage deployment with Sentinel drift safety guards.
*   **`drift health [packages...] [-t/--timeout <secs>] [-v/--verbose] [--json]`**: Runs runtime health check probes on installed packages (Primitive 12).
*   **`drift uninstall <packages...> [-f/--force] [--detach] [--dry-run] [--no-hooks] [--json]`**: Safely cleans or detaches a package from the system (Primitive 7).
*   **`drift rollback [packages...] [-f/--force] [--no-hooks] [--json]`**: Emergency recovery after midway failure (Primitive 8).
*   **`drift status [packages...] [--json]`**: Audits and aggregates the alignment of templates, system drift, and pending deployments.
*   **`drift diff [packages...] [-t/--template] [-s/--system] [--stat] [-y/--side-by-side] [--json]`**: Visualizes changes between layers (Diff A, Diff B, or Diff Δ).
*   **`drift gc [--dry-run] [--no-hooks] [--json]`**: Cleans orphan packages, purges disabled/missing/zombie database folders in `render/` and `install/`, and auto-commits database purges (Primitive 9).
*   **`drift repair [--dry-run] [--json]`**: Audits and self-heals workspace structure, repositories, config templates, and secrets (Primitive 14).
*   **`drift complete [<shell>] [--install] [--json]`**: Generates or installs native interactive shell tab-completion scripts (bash, zsh, fish, nu).
*   **`drift help [topic]`**: Interactive mini user manual with pager fallback support (topics: `package`, `src`, `render`, `install`, `fcd`, `ignore`, `drift_package.toml`, `drift_workspace.toml`, `workspace`, `health`, `clone`, `faq`).

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

### A. Global Workspace Configuration: `config/drift_workspace.toml` Specification
Rather than scanning the filesystem blindly, the drift engine relies on a centralized workspace configuration file located at `config/drift_workspace.toml` (which can itself be a template named `drift_workspace.envst.toml`). This file orchestrates two main responsibilities:
1. **Workspace Paths & Rendering Engines**: Defines directories (`source_directory`, `render_directory`, `install_directory`, `backup_directory`, `default_target_directory`, `default_install_method`) and template engines with their file suffixes and rendering subprocess commands (e.g. `envsubst`, `mustache`).
2. **Enabled Packages Registry**: Declares exactly which package subfolders under `src/` are globally active via the `[packages.enable]` section.

*   **Active Package Determination**:
    During global operations (like a bulk `drift status` or `drift deploy`), the engine checks the package registry table:
    - If a package is listed as `true`, it is processed.
    - If listed as `false`, it is completely ignored.
    - An optional `DEFAULT = true | false` key specifies whether packages not explicitly listed are enabled or disabled by default. If `DEFAULT` is omitted or `false`, unlisted folders are ignored.

```toml
# =====================================================================
# drift_workspace.toml Configuration
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

# Default deployment method: "symlink" (symlink) or "copy" (physical)
default_install_method = "symlink"

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

#### Meta-Config Templating: `drift_workspace.envst.toml` & `drift_workspace.local.envst.toml`
To allow complete bootstrapping of workspaces under different environment parameters, the workspace config files (`drift_workspace.toml` and `drift_workspace.local.toml`) can themselves be templates named `drift_workspace.envst.toml` or `drift_workspace.local.envst.toml`. Drift automatically compiles them on-the-fly using `envsubst` populated with active system-level environment variables.

For example, a user or provisioning script can compute machine capabilities and export an environment variable containing the desired package roster:
```bash
export DRIFT_PACKAGES="shell = true
nvim = true
cuda_toolkit = true
desktop_hyprland = false
"
```
And author `config/drift_workspace.local.envst.toml`:
```toml
[packages.enable]
DEFAULT = false
${DRIFT_PACKAGES}
```
When Drift loads the workspace configuration, `load_workspace_config_file_with_render` automatically evaluates `${DRIFT_PACKAGES}` into valid TOML key-value pairs.

#### Native TOML Variable Stitching & Topological DAG Resolution
Rather than requiring developers to wrap static configuration files in template extensions (e.g. `drift_workspace.envst.toml` or `drift_package.envst.toml`) and invoke `envsubst`, Drift provides **native, zero-dependency topological variable stitching** across all TOML configuration files (`drift_workspace.toml`, `drift_workspace.local.toml`, `drift_package.toml`, `drift_package.local.toml`).

1. **Topological Inter-Variable Composition**:
   - Variables defined within `[env.default]` (or `[env.override]` / `[env.fallback]`) can reference each other (e.g. `SOCKS_PROXY_HOST = "127.0.0.1"`, `SOCKS_PROXY_PORT = "1080"`, `DRIFT_SAMPLE_SOCKS_PROXY = "socks5h://${SOCKS_PROXY_HOST}:${SOCKS_PROXY_PORT}"`, `DRIFT_SAMPLE_ALL_PROXY = "${DRIFT_SAMPLE_SOCKS_PROXY}"`).
   - Drift constructs an in-memory dependency graph (DAG) and resolves variable evaluations in topological order using Kahn's algorithm.
   - Immediate self-references (e.g. `LOOP = "${LOOP}"`) and cyclic dependencies (e.g. `A -> B -> A`) are detected and rejected with informative `ConfigError` diagnostics.

2. **Unidirectional 2-Stage Evaluation Model**:
   - **Stage 1 (Environment Tables Resolution)**: Environment tables (`[env.default]`, `[env.override]`, `[env.fallback]`, `[env.secrets]`) evaluate first against base environment and facts. Variables defined in environment tables cannot reference fields outside environment sections (e.g. `target_directory`), eliminating cross-section cyclic dependencies.
   - **Stage 2 (Non-Env Interpolation)**: All other configuration fields (`[workspace]`, `[package]`, `[hooks]`, etc.) are recursively interpolated using the resolved environment.

3. **Referencing Rules Across Tiers**:
   - Both workspace and package configs define 4 symmetrical sub-tables under `[env]`: `override`, `secrets`, `default`, and `fallback`.
   - Kahn's topological sort algorithm resolves inter-variable references dynamically across tiers, facts, secrets, and ambient host environment.
   - Non-env fields across `[workspace]`, `[package]`, and `[hooks]` can reference any variable defined in `[env]`, facts, secrets, or workspace environment.

4. **Literal Escaping**:
   - Prepending a backslash (`\$VAR` or `\${VAR}`) prevents interpolation and preserves the literal string, allowing configuration files to pass literal shell variable references to hooks and target configurations without triggering substitution errors.

#### Private Dotenv Vault & Declarative Secrets (`[env.secrets]`, `config/secrets.env`)
To isolate secret tokens, private API keys, and work-specific emails from public dotfiles repositories, Drift provides a secure, local-only secret hierarchy combining declarative `[env.secrets]` tables in workspace and package configurations with a git-ignored Dotenv vault located at `config/secrets.env`.

1. **Strict 6-Tier Variable Precedence**:
   During configuration ingestion, template parsing, and hook execution, variables are resolved in a strict order of precedence (Package > Workspace within each macro tier, highest precedence overrides lower layers):
   - **Tier 1 (CLI)**: Ambient Process Environment & CLI Variables (`INITIAL_ENV` / `os.environ`)
   - **Tier 2 (Override)**: Package `[env.override]` > Workspace `[env.override]`
   - **Tier 3 (Facts)**: Package Facts (`drift_package_*`) > System Facts (`drift_*` protected facts: `drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`, `drift_ip_addresses`)
   - **Tier 4 (Secrets)**: Package `[env.secrets]` > Workspace `[env.secrets]` > `config/secrets.env`
   - **Tier 5 (Default)**: Package `[env.default]` > Workspace `[env.default]`
   - **Tier 6 (Fallback)**: Package `[env.fallback]` > Workspace `[env.fallback]`

2. **Topological DAG Resolution & Pure In-Memory Ingestion**:
   - `resolve_and_interpolate_workspace_config` and `resolve_and_interpolate_package_config` perform pure in-memory DAG topological sorting across all 4 `[env]` tables (`override`, `secrets`, `default`, `fallback`) without mutating `os.environ` during configuration parsing.
   - Variables in `[env]` can reference each other, system facts, secrets, and lower-tier variables.
   - **Rendered Metadata & Sandbox Isolation**: Fully stitched static metadata (including resolved `[env.secrets]`) is stored in `render/<pkg>/.drift/drift_package.toml` and mirrored to `install/<pkg>/.drift/drift_package.toml`. Both `render/` and `install/` are git-ignored by default, allowing downstream lifecycle hooks (`post_install`, `health`, etc.) to execute with complete, deterministic access to all 6 environment tiers without re-parsing source configurations.

3. **Single Ingestion & Explicit Workspace Injection**:
   To avoid redundant disk I/O and repeated file parsing across multi-package workflows:
   - `config/secrets.env` and workspace `[env]` tables are parsed during workspace loading (`load_workspace_config`) and stored as an explicit, strongly-typed `EnvResolve` on `WorkspaceConfig.env_resolve`.
   - Package configuration ingestion (`PackageConfig.from_dict`, `from_render_dir`, `from_install_dir`) receives `workspace_config` at load time, automatically running `compute_effective_envs(workspace_config)` to resolve the full 6-tier hierarchy and package facts into `PackageConfig.env_resolve`.
   - Downstream operations (`package_envs`, lifecycle hooks, requirement checks, template rendering) read directly from the pre-resolved in-memory configuration in $O(1)$ time without needing runtime `workspace_config` passing or re-reading the filesystem.

4. **Transient Clean-Room Isolation (`package_envs`) & Log Masking**:
   To prevent credentials and environment mutations from leaking across operations:
   - When executing package lifecycle shell hooks (`drift_hooks/`) or rendering templates (`with pkg_config.package_envs():`), Drift temporarily loads `pkg_config.env_resolve.effective_dict` into `os.environ` adhering to Tier 1 protection (`INITIAL_ENV`) via the `env_resolve_scope` context manager.
   - Secret values defined in `[env.secrets]` and `secrets.env` are automatically masked in debug logs as `KEY=****`, while non-secret variables remain legible in clear text.
   - Upon exiting the scoped block, `env_resolve_scope` automatically unloads the variables and restores the original environment snapshot, guaranteeing zero state contamination.
   - **Zero `os.environ` Footprint for Python Preprocessors**: By contrast, dynamic Python preprocessor hooks (`configure_workspace` and `configure_package`) operate with **zero footprint on `os.environ`**—they receive resolved facts, secrets, and environment snapshots purely in-memory through `context.env`.

#### Dynamic Workspace Python Hook: `config/drift_workspace.py`
For advanced programmatic workspace configuration (such as dynamically toggling packages based on the operating system, Linux distribution, hostname, CPU architecture, or custom discovery logic), Drift provides a **Dynamic Python Workspace Hook**.

1. **Convention & Entry Point**:
   - By default, Drift looks for `config/drift_workspace.py` (or a custom path defined via `[workspace] hook_file = "..."`).
   - The script defines an entry point:
     ```python
     from typing import Dict, Any
     from drift import WorkspaceHookContext

     def configure_workspace(context: WorkspaceHookContext) -> Dict[str, Any]:
         # Inspect system facts, secrets, and environment
         if context.os == "Linux" and context.distro == "arch":
             context.config.setdefault("packages", {}).setdefault("enable", {})["hyprland"] = True
         return context.config
     ```

2. **`WorkspaceHookContext` Properties**:
   - `context.config`: The parsed TOML configuration dictionary.
   - `context.drift_root`: Absolute path to the workspace root directory.
   - `context.env`: Active in-memory environment dictionary snapshot (including CLI envs, workspace secrets, and host facts). The hook executes without modifying or polluting ambient `os.environ`.
   - `context.facts`: Auto-detected system facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
   - `context.discovered_packages`: List of all package directory names found in `src/`.
   - Convenience properties: `context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`.

3. **Lifecycle Execution**:
   - Executed dynamically during workspace loading (`load_workspace_config`) before package scanning and rendering pipelines begin.
   - The returned transformed configuration dictionary is validated and used to construct the canonical `WorkspaceConfig`.

### B. Custom Render Engines & Template Input Dependencies
Rather than utilizing closed/hardcoded compilation scripts, the drift workspace supports registering flexible, custom-defined template render engines.

#### 1. Custom Render Engine Schema
Under the `[render.<engine_name>]` tables in `drift_workspace.toml`, developers can define arbitrary engines. Each engine declaration supports three main properties:
1.  **`input_file`**: The file path providing active variables or values to the engine (e.g. a shell environment script or JSON dataset). The path must reside within the `config/` base folder (or `src/<pkg>/` for package-level engines); paths resolving outside `base_dir` are strictly forbidden.
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
    *   The compiled static output is saved inside the package's sandbox under `render/<pkg>/.drift/render/workspace/mustache.json` (or `render/<pkg>/.drift/render/package/<rel_path>` for package-scoped engine inputs).
    *   The `mustache` engine is then invoked, substituting `%i` with the absolute path of this rendered file (`render/<pkg>/.drift/render/workspace/mustache.json`).

#### 3. Single-Dependency Constraint per Engine
While multi-level transitive chains are fully supported, each engine's input file can match at most one other engine's suffix pattern. Thus, every engine is limited to a single direct dependency (a 1-to-1 matching relationship per level), forming a dependency tree/forest (without cycles) rather than a complex multi-parent DAG. Double extensions or nested suffixes are strictly evaluated at the outermost matching level:
*   An input named `file.<engine1>.<engine2>.suffix` is evaluated as a template for `engine2` only. The `<engine1>` portion of the name remains treated as passive text, and `file.<engine1>.suffix` is forwarded as the final compiled input file to the parent engine.

#### 4. Directed Acyclic Graph (DAG) Cyclic Detection
Because inputs can depend on the outputs of other engines, compilation order must follow a strictly sequential pipeline.
*   During AST Merkle DAG expansion (`expand_node_dependencies`), candidate files and their engine input dependencies are resolved recursively.
*   During tree expansion, Drift tracks visiting engines and active path expansion chains to detect circular dependency loops (e.g., Engine A's input depends on Engine B's output, and Engine B's input depends on Engine A's output). If a cycle is detected, expansion immediately halts with a descriptive `ValueError` detailing the cyclic chain (`A -> B -> A`).

#### 5. Graceful Disabling & Runtime Safety Check
If a registered engine's `input_file` is not specified or is disabled, the compilation engine handles it gracefully:
*   **Disabled Status**: If an engine configures an empty or missing input file, it is marked as disabled (`is_disabled = True`).
*   **Runtime Safeguard**: If any template file in the repository relies on a disabled engine, the core rendering pipeline checks the engine status and halts compilation immediately with a descriptive `RenderError` (e.g., `Render engine '<name>' is disabled or has an invalid/empty input file`), ensuring that no silent partial configurations are deployed.

#### 6. Package-Level Render Engines & Workspace Engine Cooperation
The primary motivation of **Package-Level Render Engine Configuration** (`[render.<name>]` tables in `drift_package.toml`) is to provide a **self-contained configuration space** for each package. Rather than forcing packages to rely on ambient global workspace settings or shared external inputs, packages encapsulate their own render engines, custom template rules, and localized input files (e.g. `src/<pkg>/env.sh` or `src/<pkg>/data.json`) directly within their directory boundary, ensuring complete modularity and portability across diverse workspaces.

1. **Workspace & Package Cooperation (Overlay & Field-Level Inheritance)**:
   - Packages can define isolated, package-specific render engines or selectively patch global workspace engines.
   - At render time, Drift creates an **effective engine registry** via `workspace_config.render_engine_configs.overlay(pkg_config.render_engine_configs)`:
     - **Field-Level Patching/Inheritance**: When a package specifies an engine already registered at the workspace level, fields explicitly defined in the package (`input_file`, `suffix`, `render_command`) override workspace defaults, while any omitted fields are automatically inherited from the workspace configuration.
     - **New Package Engines**: When a package defines a new engine name not present in the workspace, it registers as a standalone renderer with its own suffix and compilation command.

2. **Early Absolute Path Resolution**:
   - `RenderEngineRegistry.from_dict(render_data, base_dir: Path)` normalizes all relative `input_file` paths immediately upon ingestion:
     - Workspace configuration: `base_dir = drift_root / "config"`
     - Package configuration: `base_dir = src/<package_name>/`
   - All downstream engine stages operate strictly on canonical, absolute file paths without ambiguous working directory guessing. Resolving paths outside `base_dir` or using `base_dir` itself as `input_file` is strictly prohibited and guarded at ingestion.

3. **Multi-Phase Merkle DAG Compilation & Sandboxing (`.drift/`)**:
   - **Phase 1 (Package Config Compilation)**: Global workspace render engines compile package configuration templates via Merkle DAG into `render/<pkg>/.drift/drift_package.toml`.
   - **Phase 2 (Lifecycle Hooks)**: Effective render engines compile hooks under `drift_hooks/` via Merkle DAG into `render/<pkg>/.drift/hooks/`.
   - **Phase 3 (Package Payload & Native Engine Input Digestion)**: AST Merkle DAG expands payload dotfiles and intermediate engine input templates natively (e.g. `config/<rel_path> -> render/<pkg>/.drift/render/workspace/<rel_path>` and `src/<pkg>/<rel_path> -> render/<pkg>/.drift/render/package/<rel_path>`), topologically digesting and caching compilation steps with granular Merkle hashing.
   - **Phase 4 (Downstream Cooperation)**: Downstream primitives (`drift reverse-sync`, `drift adopt`, `drift add`) resolve template suffixes against these effective package engines, ensuring seamless two-way synchronization.

4. **Engine Scope & Boundary Invariants**:
   - **Global Engines Only for Package Config**: Only workspace-level global render engines defined in `config/drift_workspace.toml` can be used to compile package configuration templates (e.g. `src/<pkg>/drift_package.envst.toml`). This is because package configuration files must be rendered during Phase 1 (Workspace Bootstrap) *before* package-level `[render.<name>]` definitions can even be parsed.
   - **Package-Level Engines Scope**: Render engines defined inside `drift_package.toml` (`[render.<name>]`) operate exclusively during Phase 3 on **package source dotfiles and templates** (under `src/<pkg>/`) and *cannot* be used to compile `drift_package.toml` itself.

#### 7. Render Collision Prevention & Reserved Suffix Validation
To prevent nondeterministic builds and accidental file clobbering:
*   **Compile-Time Render Collision Detection**: If multiple source files within a package directory evaluate to the same target output path inside `render/<package_name>/` (e.g. `config.json.envst` and `config.json.mustache`, or static `init.lua` and templated `init.lua.envst`), Drift immediately halts with a `RenderCollisionError` detailing the colliding source files.
*   **Reserved Engine Suffixes**: Engine suffixes that clash with Drift configuration keywords (`drift_package`, `drift_hook`, `drift_ignore`) are strictly forbidden and rejected during engine configuration validation.

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
3. **Unified Render Target Name (`.drift/drift_package.toml`)**:
   - Regardless of whether the original source files are named `drift_package.toml`, or their template/local override counterparts, the final merged TOML dictionary is **always serialized and rendered as `.drift/drift_package.toml`** inside the sandbox directory at `render/<package_name>/.drift/drift_package.toml`.
   - All subsequent package inspections, change visualizations, and staging processes read from this standardized `render/<package_name>/.drift/drift_package.toml` file, ensuring perfect downstream modularity and zero ambiguity.
4. **Exclusion Guard**: The final rendered `drift_package.toml` and `.drift_ignore` are strictly stored inside `.drift/`. The entire `.drift/` directory is marked as an internal control-plane directory and is **never copied** or symlinked onto the active target system, but stays as an index inside `install/<package_name>/.drift/`.

#### Dynamic Package Python Hook: `src/<package_name>/drift_package.py`
For complex packages requiring programmatic adjustments (such as downloading remote configs or secrets from remote servers, dynamically calculating target directories, overriding deployment methods per OS, generating dynamic requirements, or injecting custom environment facts), Drift provides a **Dynamic Python Package Hook**.

> [!TIP]
> **Best Practice — Remote Secrets & Configs Fetching**:
> `drift_package.py` (and `config/drift_workspace.py` at workspace scope) is the **recommended, canonical place** to fetch configuration files or secrets from remote servers (such as 1Password CLI, Bitwarden, HashiCorp Vault, AWS Secrets Manager, or remote HTTP endpoints) and inject them dynamically into `[env.override]` or `[env.fallback]`. Because this hook runs as a preprocessor before variable stitching, any values injected into `context.config["env"]["override"]` participate seamlessly in topological DAG resolution and cross-section template interpolation!

1. **Convention & Entry Point**:
   - By default, Drift looks for `src/<package_name>/drift_package.py` (or a custom path defined via `[package] hook_file = "..."`).
   - The script defines an entry point:
     ```python
     from __future__ import annotations
     from typing import TYPE_CHECKING, Any, Dict

     if TYPE_CHECKING:
         from drift.hooks import PackageHookContext

     def configure_package(context: PackageHookContext) -> Dict[str, Any]:
         # 1. Fetch remote secrets and inject into [env.override]
         # token = subprocess.check_output(["op", "read", "op://vault/item/token"], text=True).strip()
         # context.config.setdefault("env", {}).setdefault("override", {})["API_TOKEN"] = token

         # 2. Dynamically adjust target directory or install method
         if context.os == "darwin":
             context.config.setdefault("package", {})["target_directory"] = "~/Library/Application Support/MyApp"
         return context.config
     ```

2. **`PackageHookContext` Properties**:
   - `context.config`: Parsed package configuration dictionary from `drift_package.toml` and local overrides.
   - `context.package_name`: Name of the package.
   - `context.package_dir`: Absolute path to `src/<package_name>/`.
   - `context.drift_root`: Workspace root path (if present).
   - `context.workspace_config`: Active `WorkspaceConfig` domain instance (if present).
   - `context.env`: Active in-memory environment snapshot (including CLI envs, workspace secrets, host facts, and package facts). The hook executes without modifying or polluting ambient `os.environ`.
   - `context.facts`: Auto-detected system facts (`drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`).
   - `context.package_facts`: Dynamic package facts (`drift_package_name`, `drift_package_source_dir`, `drift_package_src_dir`, `drift_package_render_dir`, `drift_package_install_dir`).
   - Convenience properties: `context.os`, `context.arch`, `context.distro`, `context.hostname`, `context.user`.

3. **Evaluation & Staging Model**:
   - Evaluated during Primitive 2 (Render) when loading the package source directory.
   - **Zero `os.environ` Footprint**: Dynamic Python preprocessor hooks execute purely in-memory with zero mutation of ambient `os.environ`. All secrets, facts, and environment variables are passed explicitly via `context.env`, `context.facts`, and `context.package_facts`.
   - The transformed configuration is processed by native variable stitching and serialized directly into `render/<package_name>/.drift/drift_package.toml` (and staged to `install/<package_name>/.drift/drift_package.toml`).
   - Downstream primitives (`stage`, `apply`, `uninstall`, `health`) read the compiled static TOML directly via `PackageConfig.from_render_dir(package_dir, workspace_config)` / `PackageConfig.from_install_dir(package_dir, workspace_config)`, ensuring zero re-execution overhead and complete determinism while binding workspace environment context.

4. **Control-Plane Exclusion**:
   - `drift_package.py` and custom `hook_file` paths are evaluated during render and excluded from host deployments (and stored under `.drift/hooks/` if hooks).

#### Default Config Template:
```toml
# =====================================================================
# drift_package.toml Template & Specification
# Place this file in: src/<package_name>/drift_package.toml
# =====================================================================

[package]
# Optional dynamic Python configuration hook file (defaults to "drift_package.py" if present)
# hook_file = "drift_package.py"

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
#   - "symlink" : Creates symbolic links from target_directory to install/ folder. (Standard for user dotfiles)
#   - "copy" : Physically copies files from install/ to target_directory. (Standard for system/etc configs)
# Falls back to "default_install_method" in drift_workspace.toml if unspecified.
install_method = "symlink"

# The physical path where this package should be deployed on Unix/Linux/macOS hosts.
# Supports home expansion (~ at the beginning).
# Falls back to "default_target_directory" in drift_workspace.toml if unspecified.
target_directory = "~/.config/example"

# Optional Windows-specific target folder path.
# Used instead of target_directory when running on Windows (win32).
# Supports %USERPROFILE%, %APPDATA%, %LOCALAPPDATA%, ~, etc.
# Aliases accepted: target_directory_windows, target_directory_win32, target_directory_winos, target_directory_win.
# target_directory_windows = "%LOCALAPPDATA%/example"

# ---------------------------------------------------------------------
# Inter-Package Dependencies (Topological Ordering & Prerequisite Guards)
# ---------------------------------------------------------------------
# Dependencies can be required (default, optional = false) or optional (ordering-only).
# Inline list syntax (strings or inline tables):
# dependencies = ["base", { name = "git" }, { name = "nodejs", optional = true }]
# Or array-of-tables syntax:
# [[package.dependencies]]
# name = "python"
# optional = true

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

# ---------------------------------------------------------------------
# Declarative Host Requirements & Platform Filtering
# ---------------------------------------------------------------------
# Packages can declaratively define host platform prerequisites.
# Evaluation occurs strictly before template rendering begins in Primitive 2.
# If any condition is not met, the package is gracefully skipped (status = "SKIPPED")
# without invoking template engines or writing to the render/ sandbox.
[requirements]
# Target operating system(s): "linux", "darwin", "windows"
os = ["linux", "darwin"]

# Allowed CPU architectures: "x86_64", "arm64", "aarch64", etc.
# arch = ["x86_64", "arm64"]

# Allowed Linux distributions: "ubuntu", "arch", "fedora", "debian", etc.
# distro = ["arch", "fedora"]

# Executables required in host $PATH (checked via shutil.which)
binaries = ["curl", "git"]

# Environment variables that must be set and non-empty
# env = ["WAYLAND_DISPLAY"]

# Allowed host LAN IP addresses, wildcard patterns, or CIDR subnets
# ip = ["192.168.1.*", "10.0.0.0/8"]

[hooks]
# ---------------------------------------------------------------------
# Lifecycle Hooks
# ---------------------------------------------------------------------
# Strict Lifecycle Directory Invariant (`drift_hooks/`):
# All package-internal hook scripts and their auxiliary dependencies MUST reside within `src/<pkg>/drift_hooks/`
# (e.g. `drift_hooks/pre-install.bash`, `drift_hooks/lib/helper.sh`). Any relative hook path outside `drift_hooks/`
# is rejected with a ConfigError.
#
# Hook files compile into `render/<pkg>/.drift/hooks/` and stage into `install/<pkg>/.drift/hooks/`.
# Because `.drift/` is an internal sandbox directory, these scripts and dependencies are strictly isolated
# from target host deployment and never deployed or symlinked to the host target.
#
# Deploying Hook Files or Sharing Dependencies:
# - If a hook script or helper file also needs to be deployed to the host (e.g. a tool under `bin/`), create a
#   symlink inside `drift_hooks/` pointing to the source directory file (e.g. `ln -s ../bin/my_tool src/<pkg>/drift_hooks/my_tool`).
# - If sharing hook scripts across packages, create a symlink inside `drift_hooks/` pointing to the shared script.
#
# Working Directory (`cwd`) Semantics:
# All lifecycle hooks always execute with `cwd = hook_path.parent` (the directory containing
# the executed script), allowing sibling helpers (e.g. `source ./lib/helper.sh`) to be sourced
# naturally relative to the script. The target directory is accessible via `$drift_package_target_dir`.
#
# All lifecycle hooks always run in user space without sudo, preserving all 7 tiers of environment variables.
# If a hook requires elevated privileges for a specific operation, use 'sudo' explicitly inside the hook script.

# Run before reading/writing source package files (e.g. generating dynamic templates before render, adopt, or add, CWD: hook_path.parent).
pre_source = "drift_hooks/pre-source.bash"

# Run after templates are rendered into sandbox (CWD: hook_path.parent).
post_render = "drift_hooks/post-render.bash"

# Run before first-time installation (CWD: hook_path.parent).
pre_install = "drift_hooks/pre-install.bash"

# Run after successful first-time installation (CWD: hook_path.parent).
post_install = "drift_hooks/post-install.bash"

# Run before any update/deployment (CWD: hook_path.parent).
pre_update = "drift_hooks/pre-update.bash"

# Run after any successful update/deployment (CWD: hook_path.parent).
post_update = "drift_hooks/post-update.bash"

# Run before package uninstallation (CWD: hook_path.parent).
pre_uninstall = "drift_hooks/pre-uninstall.bash"

# Run after package uninstallation (CWD: hook_path.parent).
post_uninstall = "drift_hooks/post-uninstall.bash"

# Run runtime health check probe on installed package (CWD: hook_path.parent).
health = "drift_hooks/health.bash"

# Timeout in seconds for lifecycle hook script executions (Default: 120)
timeout = 120

# Optional Windows-specific hook overrides (aliases: [hooks.windows], [hooks.win32], [hooks.winos], [hooks.win]).
# [hooks.windows]
# pre_install = "drift_hooks/bootstrap.exe"
# post_install = "drift_hooks/setup.ps1"
# post_update = "drift_hooks/reload_service.bat"
# health = "drift_hooks/health_check.ps1"

# ---------------------------------------------------------------------
# Optional Package-Level Render Engines
# ---------------------------------------------------------------------
# Override workspace render engines or define package-specific engines.
# Unspecified fields in workspace overrides inherit from drift_workspace.toml.
# Relative input_file paths resolve relative to this package directory.
# [render.envsubst]
# input_file = "env.sh"
#
# [render.jinja2]
# input_file = "data.json"
# suffix = "j2"
# render_command = "j2 %i %s"
```

#### Lifecycle Hooks Execution Matrix
All lifecycle hooks execute in user space without `sudo`, with their working directory (`cwd`) set to `hook_path.parent` (the directory containing the executed script), preserving all 6 tiers of environment variables (`$drift_package_*`, `$drift_*`, `[env.override]`, `[env.secrets]`, `[env.default]`, `[env.fallback]`). The host target directory is accessible via `$drift_package_target_dir`.

| Hook Name | Lifecycle Trigger Stage |
| :--- | :--- |
| `probe` | Requirement validation (deploy, render, status) |
| `pre_source` | Before reading/writing templates (render, adopt, add) |
| `post_render` | After sandbox compilation |
| `pre_install` | Before first-time deployment |
| `post_install` | After first-time deployment |
| `pre_update` | Before incremental/full update deploy |
| `post_update` | After incremental/full update deploy |
| `pre_uninstall` | Before unlinking/deleting files |
| `post_uninstall`| After unlinking/deleting files |
| `health` | During `drift health` probe execution |

> [!NOTE]
> **Privilege & Environment Model**: All lifecycle hooks execute in user space without `sudo`, preserving all 6 tiers of environment variables (`$drift_package_*`, `$drift_*`, `[env.override]`, `[env.secrets]`, `[env.default]`, `[env.fallback]`). All hooks have complete access to `config/secrets.env`:
> - **Python Hooks** (`drift_workspace.py`, `drift_package.py`): Directly inspect `context.env` (`Dict[str, str]`).
> - **Subprocess Lifecycle Hook Scripts** (`probe`, `pre_source`, `post_render`, `pre_install`, `post_install`, `pre_update`, `post_update`, `pre_uninstall`, `post_uninstall`, `health`): Automatically inherit `secrets.env` variables in `os.environ` via Tier 4 precedence.
> The execution working directory defaults to `hook_path.parent` (the directory containing the executed script), and `$drift_package_src_dir` is an alias for `$drift_package_source_dir`. If elevated root privileges are required for a command, write `sudo` explicitly within the hook script.

#### Event Ordering & Install Method Semantics (`symlink` vs. `copy`)
Because Drift separates template staging (Primitive 4: `render/` $\rightarrow$ `install/`) from host delivery (Primitive 5: `install/` $\rightarrow$ host), the timing of file content updates relative to lifecycle hooks depends on the package's `install_method`:

*   **`install_method = "copy"` (Strict Event Ordering & Dot-Prefix Translation)**:
    *   Host target files remain strictly in their previous state during the Staging phase (Primitive 4).
    *   `pre_update` executes while host files are strictly at their prior version.
    *   Physical files are then copied, updated, or removed on the host during Primitive 5 using an atomic single-file copy pipeline applying dot-prefix translation (`translate_dot_prefixes`).
    *   `post_update` executes after host files have received the new state.
    *   👉 **Recommendation**: If your package configuration is watched by active system services or daemons (e.g. `systemd` user units with inotify watchers) that must be cleanly stopped in `pre_update` before configuration files change, use **`install_method = "copy"`**.

*   **`install_method = "symlink"` (Symlink Pointers)**:
    *   Because active host paths are symbolic links pointing into `install/<pkg>/`, modifying file contents in `install/` during Staging (Primitive 4) makes content modifications immediately visible on the host **before** `pre_update` runs in Primitive 5.
    *   Structural changes (creating symlinks for new files or pruning deleted symlinks) are applied during Primitive 5 after `pre_update`.
    *   👉 **Recommendation**: Ideal for standard user dotfiles (e.g. `.zshrc`, `.tmux.conf`, Neovim configs) where instant reflection and symlink transparency are preferred.

#### Default Package Environment Variables & Precedence
During package loading (`PackageConfig.from_dict()`, `load_package_config_from_render_dir()`, `load_package_config_for_install()`), `workspace_config` is supplied to compute the effective 6-tier environment (`self.env_resolve: EnvResolve`) and package facts (`drift_package_*`).
At runtime, the drift engine dynamically scopes the pre-resolved package environment into `os.environ` via `with pkg_config.package_envs():` (powered by `env_resolve_scope` with automatic secret masking) without requiring runtime `workspace_config` arguments:
*   **`drift_package_name`**: Name / directory name of the package.
*   **`drift_package_target_dir`**: Resolved absolute destination target directory path on the host system.
*   **`drift_package_source_dir`** / **`drift_package_src_dir`**: Absolute path to the package's source directory in the workspace (`<drift_root>/src/<pkg>`).
*   **`drift_package_render_dir`**: Absolute path to the package's compiled sandbox directory (`<drift_root>/render/<pkg>`).
*   **`drift_package_install_dir`**: Absolute path to the package's state database directory (`<drift_root>/install/<pkg>`).
*   **`drift_package_install_method`**: Resolved deployment method (`symlink` or `copy`).

> [!IMPORTANT]
> **Environment Variable Precedence & Overrides**:
> Variables within package operations follow the strict 6-tier precedence hierarchy (Package > Workspace within each macro tier):
> 1. **Tier 1 (CLI)**: Ambient Process Environment & CLI Variables (`INITIAL_ENV` / `os.environ`)
> 2. **Tier 2 (Override)**: Package `[env.override]` > Workspace `[env.override]`
> 3. **Tier 3 (Facts)**: Package facts (`drift_package_*`) > Host facts (`drift_*`)
> 4. **Tier 4 (Secrets)**: Package `[env.secrets]` > Workspace `[env.secrets]` > `config/secrets.env`
> 5. **Tier 5 (Default)**: Package `[env.default]` > Workspace `[env.default]`
> 6. **Tier 6 (Fallback)**: Package `[env.fallback]` > Workspace `[env.fallback]`
>
> This guarantees that templates and hook scripts always receive the exact, authoritative package attributes regardless of any external or global environment definitions.

These variables are active during:
1.  **Lifecycle Hook Script Executions** (`pre_source`, `post_render`, `pre_install`, `post_install`, `pre_update`, `post_update`).
2.  **Template Compilations** (accessible as `${drift_package_name}`, `${drift_package_target_dir}`, `${drift_package_source_dir}`, etc. in `.envst` / `envsubst` templates).
3.  **Physical Deployment Operations**.

Upon completion of the scoped block, `env_resolve_scope` automatically unloads the variables and restores the original environment snapshot, guaranteeing clean-room environment isolation between packages.

### D. Inter-Package Dependencies & Topological DAG Lifecycle Orchestration

To maintain system integrity and predictable execution order across modular configurations, Drift allows packages to declare explicit inter-package dependencies. Dependencies govern the execution order across the entire lifecycle — staging (Primitive 4), physical deployment (Primitive 5), uninstallation (Primitive 7), and rollback recovery (Primitive 8) — while enforcing prerequisite presence guarantees.

#### 1. Configuration Schema & Declarative Syntax
Dependencies are declared inside `drift_package.toml` within the `[package]` section using either an inline list or an array of tables:

*   **Syntax 1: Inline List (Strings or Inline Tables)**:
    ```toml
    [package]
    name = "neovim"
    dependencies = ["base", { name = "git" }, { name = "nodejs", optional = true }]
    ```
*   **Syntax 2: Array of Tables (`[[package.dependencies]]`)**:
    ```toml
    [[package.dependencies]]
    name = "python"
    optional = true
*   **Syntax Selection**: Choose either inline list syntax or array-of-tables syntax. In compliance with the TOML specification, packages should not mix inline array and array-of-tables syntaxes within the same configuration file.
*   **String Shorthand**: Specifying a bare string `"base"` is shorthand for `{ name = "base", optional = false }`.
*   **Ingestion Validation**: During configuration loading, Drift rejects immediate self-dependencies (`pkg` depending on `pkg`), duplicate dependency declarations for the same package name, and unknown keys under dependency tables with a descriptive `ConfigError`.

#### 2. Dependency Semantics: Required vs. Optional
Dependencies are categorized into two distinct operational semantics:

*   **Required Dependencies (`optional = false`, Default)**:
    *   The referenced package **must exist** in the active package universe (either already recorded as installed in `install/state.toml` or targeted for deployment in the current transaction with `enable_install = true`).
    *   The prerequisite package must be staged and deployed **before** the declaring package.
    *   If a required dependency is missing from the available universe, Drift immediately halts during pre-flight assertions before any physical file operations are executed.
    *   **Bypass Option**: If a user needs to bypass missing prerequisite errors (e.g., during partial recovery or standalone testing), passing `--force` or setting `no_deps = true` in `InstallConfig` / `UninstallConfig` (CLI: `--no-deps`) bypasses the check.
*   **Optional Dependencies (`optional = true`)**:
    *   Acts strictly as an **ordering constraint**.
    *   If the named prerequisite exists in the active package universe, Drift guarantees it is staged and installed prior to the declaring package.
    *   If the named prerequisite is absent from the universe, the dependency edge is silently pruned without raising errors.

#### 3. Domain Data Models
*   [`PackageDependency`](../src/drift/config/package_config.py): Encapsulates a single dependency link (`name: str`, `optional: bool`).
*   [`PackageDependencies`](../src/drift/config/package_config.py): Strongly-typed collection container wrapping `items: List[PackageDependency]`. Provides helper properties:
    *   `required_names`: `Set[str]` of required package dependencies.
    *   `optional_names`: `Set[str]` of optional package dependencies.
    *   `all_names`: `Set[str]` of all declared package dependencies.

#### 4. Decoupled Assertion Guards & Topological Sorting Primitives
Drift enforces a strict separation between read-only validation guards and pure topological DAG sorting algorithms within [`package_assertions.py`](../src/drift/primitives/package_assertions.py):

*   [`assert_required_package_dependencies_exist(pkg_dependencies_map, universe_names=None)`](../src/drift/primitives/package_assertions.py):
    *   Read-only pre-flight guard validating that all required dependencies exist within the available package universe.
    *   Audits all packages in the batch and aggregates all missing dependencies into a single, multi-line diagnostic error before raising `ConfigError`.
*   [`assert_no_cyclic_package_dependencies(pkg_dependencies_map)`](../src/drift/primitives/package_assertions.py):
    *   Read-only DAG assertion guard verifying the dependency graph contains no circular dependencies.
    *   Prunes absent dependency edges before sorting, testing strictly for cycles without failing on missing dependencies.
*   [`resolve_package_install_order(pkg_dependencies_map) -> List[str]`](../src/drift/primitives/package_assertions.py):
    *   Pure topological sorter using Kahn's algorithm via generic [`topological_sort[T]`](../src/drift/utils/env_utils.py).
    *   Prunes absent dependencies and returns package names in valid forward prerequisite order (prerequisites before dependents).
*   [`resolve_package_uninstall_order(pkg_dependencies_map) -> List[str]`](../src/drift/primitives/package_assertions.py):
    *   Pure reverse topological sorter returning package names in reverse dependency order (dependents before prerequisites).
*   [`assert_no_broken_dependencies_on_uninstall(packages_to_uninstall, remaining_metadata)`](../src/drift/primitives/package_assertions.py):
    *   Read-only guard for uninstallation. Evaluates packages that will remain installed on the system to verify none of them require any of the packages targeted for uninstallation.
    *   Aggregates all broken dependency relationships and raises `ConfigError` unless bypassed via `--force` or `no_deps = true` (CLI: `--no-deps`).
*   [`resolve_target_package_order(target_metadata, state_registry, workspace_config, no_deps=False) -> List[str]`](../src/drift/primitives/package_assertions.py):
    *   Centralized resolution helper that constructs the full package universe (already-installed packages from `state.toml` + target batch).
    *   Runs `assert_required_package_dependencies_exist` upfront when `not no_deps`.
    *   Computes and returns the topologically sorted package sequence. If a package directory is missing from `install/`, it gracefully falls back to a default `PackageConfig(PackageSectionConfig(name=pkg))` representation.

#### 5. Multi-Phase Lifecycle Pipeline Integration
Inter-package dependencies are orchestrated across every stage of the Drift lifecycle:

*   **Primitive 4: Stage Render to Install**:
    *   `prepare_stage_packages` constructs the package universe, validates acyclicity, resolves topological order via `resolve_target_package_order`, compiles per-package stage plans, and records the sequence in `StagePlan.packages_stage_order`.
    *   `execute_stage_packages` stages packages in this exact topological order (`StagePlan.packages_stage_order`), preserving update sequence for shared install methods (e.g. `SYMLINK`).
*   **Primitive 5: Install Repo Deployment**:
    *   `prepare_install` validates deployment readiness (`assert_packages_install_ready`), verifies hook file permissions, audits cross-package path collisions, and resolves global topological deployment order in `InstallPlan`.
    *   `execute_install` deploys packages sequentially, guaranteeing prerequisites are installed and active before dependent packages deploy.
    *   Controlled by strongly-typed `InstallConfig(force, dry_run, flags, no_deps)`.
*   **Primitive 7: Uninstall Repo Package**:
    *   Operates using `UninstallConfig(force, dry_run, detach, no_deps, flags)`.
    *   Pre-flight check evaluates `assert_no_broken_dependencies_on_uninstall` (bypassed if `force` or `no_deps`).
    *   Multi-package uninstallation executes in reverse topological order via `resolve_package_uninstall_order`, ensuring dependent packages run their `pre_uninstall` / `post_uninstall` hooks and release files while their prerequisites remain fully operational on the host.
    *   Compiles a declarative `UninstallPlan` (`PackageUninstallPlan`) decomposing operations into fundamental file actions (`DELETE_ITEM`, `CREATE_COPY`, `ENSURE_DIR`, `INFO_MESSAGE`), executing safely with ancestor collision protection.
    *   If a package directory is missing in `install/`, Drift logs a warning and cleans the record from `StateRegistry` without crashing.
*   **Primitive 8: Rollback Recovery**:
    *   Executes a single unified reverse topological sort (`resolve_package_uninstall_order`) across all candidate packages requiring recovery.
    *   Iterates through the ordered list one-by-one, dispatching committed packages to redeployment (`rollback_redeploy_committed_package`) and uncommitted first-time packages to uninstallation (`rollback_uninstalled_first_time_package`) with `UninstallConfig(force=True)`.
*   **Primitive 9: Workspace Garbage Collection**:
    *   Orphan uninstallation is invoked with `UninstallConfig(force=True)`.
    *   Stage 1b scans `install/state.toml` for ghost packages (records whose directories in `install/` are missing on disk), removes them from `StateRegistry`, and commits the cleanup to Git.

#### 6. Orthogonality to Template Imports (`[[imports]]`)
Inter-package dependencies are architecturally orthogonal to the planned template import system (`[[imports]]`):
*   **Template Imports (`[[imports]]`)**: Operates purely during **render input preparation** (Primitive 2), layering template source files from one package into another before sandbox compilation.
*   **Package Dependencies (`dependencies`)**: Operates during **staging, deployment, uninstallation, and rollback execution** (Primitives 4, 5, 7, 8), governing topological execution order and prerequisite availability guarantees across distinct packages.

---

## 6. Execution Safeguards, Policies & Customization

This section defines the core architectural policies, safeguards, and customization guidelines required to maintain technical integrity.

### A. Ignored Files and Name Conversion Rules
Both `symlink` and `copy` deployment strategies natively respect ignore files, prefix transformations, and metadata isolation rules:

1.  **Ignore Filter (`.drift_ignore`) Syntax & Matching Rules**:
    *   **Single Source of Truth & Nested Ignore Rejection**:
        *   Exactly **one** `.drift_ignore` file is allowed at the root of each package directory (`src/<pkg>/.drift_ignore`).
        *   Nested ignore files inside subdirectories (e.g., `src/<pkg>/subfolder/.drift_ignore` or `src/<pkg>/subfolder/.driftignore`) are **strictly prohibited** and immediately raise a `ValueError` during parsing to guarantee a single authoritative ignore configuration per package.
    *   **Legacy Alias & Auto-Migration**:
        *   `.driftignore` is recognized as a legacy alias of `.drift_ignore`. If `.driftignore` is detected at the package root without a `.drift_ignore`, the render engine logs a warning and automatically copies it to `.drift_ignore`.
    *   **Default Ignore Ruleset**:
        If no `.drift_ignore` file is provided in a package, Drift automatically applies a comprehensive default ignore ruleset (`DEFAULT_IGNORE_PATTERNS` / `DEFAULT_DRIFT_IGNORE_CONTENT`) matching GNU Stow standards, development toolchains, and editor caches:
        - **Python Bytecode, Virtual Environments & Caches**: `__pycache__`, `/__pycache__/`, `\.py[cod]$`, `\$py\.class$`, `\.pytest_cache`, `/\.pytest_cache/`, `\.mypy_cache`, `/\.mypy_cache/`, `\.ruff_cache`, `/\.ruff_cache/`, `\.venv`, `/\.venv/`, `^venv$`, `/venv/`
        - **Version Control & Metadata**: `^/\.gitignore`, `\.gitignore`, `\.git`, `\.hg`, `\.svn`, `_darcs`, `CVS`, `\.cvsignore`, `RCS`, `\.+,v`, `\.\#.+`
        - **Editor Backups & Temporary Files**: `.+~`, `\#.*\#`, `.*\.sw[a-p]$`, `.*\.swp$`, `.*\.swo$`, `.*\.un~$`
        - **Operating System Metadata**: `^\.DS_Store$`, `^Thumbs\.db$`
        - **Package Documentation & Licenses**: `^/README.*`, `^/LICENSE.*`, `^/COPYING.*`
        - **Drift Internal Control Plane**: `^/\.drift/`, `^/\.drift$`
    *   **PCRE Regular Expressions (No Globbing)**:
        *   The ignore engine **does NOT use globbing syntax**. Instead, all patterns are parsed and evaluated as **Perl-Compatible Regular Expressions (PCRE)** using Python's `re` module. The matching specification is derived from GNU Stow's ignore rules.
        *   Lines beginning with `#` are treated as comments (unless escaped with a backslash `\#`), and whitespace/blank lines are skipped.
    *   **Two-Group Matching Algorithm**:
        Drift divides loaded regex patterns into two groups based on whether a forward slash `/` is present:
        *   *Group 1: Patterns Containing `/` (Relative Path Matching)*:
            Matched against the file's normalized relative path prefixed with `/` (e.g., `/dot-config/nvim/init.lua`).
            - To anchor a pattern strictly to the package root, start with `^/` (e.g. `^/sample\.txt$`, `^/install.*\.sh$`). Do **not** use `./`.
            - To match a directory anywhere in the tree, use `/dirname/` (e.g., `/cache/`, `/build/`).
        *   *Group 2: Patterns WITHOUT `/` (Basename Matching)*:
            Matched against the file or directory `basename` anywhere within the package hierarchy (e.g. `\.bak$`, `^~`, `\.sw[p-z]$`).
    *   **Match Timing Guard (Pre-Conversion Evaluation)**:
        *   The ignore engine evaluates patterns against native source filenames **before** `dot-` prefix translation or engine suffix extraction takes place.
        *   *Rule*: To ignore a template named `dot-bashrc.envst.sh`, the ignore pattern must match `dot-bashrc.envst.sh` (or `dot-bashrc.*`), not `.bashrc`.
    *   **Hardcoded Implicit Exclusions**:
        *   **Internal `.drift` Directory**: The internal control plane directory (`.drift/`, containing package configuration `.drift/drift_package.toml`, ignore rules `.drift/.drift_ignore`, staged compilation inputs `.drift/render/`, and hooks `.drift/hooks/`) is hardcoded as permanently ignored and is never deployed or symlinked onto active host systems.
    *   **Native Symlink Linker Integration**:
        *   Drift's native relative symlink linker directly inspects `.drift_ignore` (or `DEFAULT_IGNORE_PATTERNS`) from the package's internal control plane (`install/<pkg>/.drift/.drift_ignore`) when computing deployable files via `filter_deployable_files`. 1:1 structural fidelity between `render/` and `install/` is 100% maintained.
    *   **FCD Reverse-Sync & Adoption Integration**:
        *   Untracked host files in Fully-Controlled Directories (FCD) matching `.drift_ignore` are automatically skipped during `reverse-sync`.
        *   During interactive adoption (`drift adopt -i`), selecting option `[2] Ignore` automatically appends the file's relative path pattern to the package's `.drift_ignore` file.

    #### PCRE `.drift_ignore` File Example:
    ```ini
    # =====================================================================
    # .drift_ignore - Package Ignore Specification
    # =====================================================================
    # Ignore any files ending in '.bak' or '.tmp' anywhere in the package
    \.bak$
    \.tmp$

    # Ignore editor swap files
    \.sw[p-z]$

    # Ignore temporary files starting with a tilde
    ^~

    # Ignore a specific directory named 'build' or 'cache' anywhere
    /build/
    /cache/

    # Ignore a specific path relative to package root
    ^/dot-config/coc-settings\.json$

    # Ignore a specific nested folder recursively
    ^/dot-config/nvim/tmp/
    ```

2.  **Prefix Conversion (`dot-` to `.`)**:
    *   To allow developers to easily manage hidden folders in standard git environments, folders and files starting with the prefix `dot-` inside `install/` must be translated to a dot `.` prefix at deployment target paths.
    *   **Mandatory 'dot-' Prefix Rule in Source Templates**: All files and directories inside source packages under `src/` that should be rendered and installed as hidden files/directories (starting with `.`) **must** be named with a `dot-` prefix (e.g., `dot-bashrc`, `dot-config/`). Raw hidden files starting with a dot `.` (such as `.bashrc` or `.env`) are **strictly prohibited** in source packages. The rendering process (`render_package`) will skip any `.*` files (except the special `.drift_ignore` file) in the rendering process and print an information log about them.
    *   *Example*: `install/shell/dot-bashrc` translates to `~/.bashrc`.
    *   *Example*: `install/nvim/dot-config/nvim/` translates to `~/.config/nvim/`.
    *   This translation is enforced symmetrically across both `symlink` and `copy` installation methods.

3.  **Sub-Repository Isolation & Internal `.gitignore` Automation**:
    *   To prevent internal synthetic artifacts and transient editor files from dirtying the database Git trees, Drift automatically generates and maintains `.gitignore` files inside `render/` and `install/` sub-repositories (`DEFAULT_SUBREPO_GITIGNORE_CONTENT`).
    *   **Ignored Patterns**:
        - Synthetic files: `.gitignore*`
        - Editor & OS temporary artifacts (`TEMPORARY_FILE_PATTERNS`): `*~`, `*#*#`, `*.#*`, `*.sw[a-p]`, `*.swp`, `*.swo`, `*.un~`, `*.DS_Store*`, `*Thumbs.db*`
    *   This ensures that running `git status` inside `render/` or `install/` reports pure package deltas without clutter from editor swap files or Drift's internal lockfiles.

4.  **Forbidden Package Names & Workspace Safety Guard**:
    *   To protect the internal architecture and prevent package paths from colliding with internal Git databases or configuration tables, package names are strictly validated against `FORBIDDEN_PACKAGE_NAMES` (`.git`, `render`, `install`, `state`, `backup`, `config`, `src`).
    *   Attempting to create, stage, or deploy a package matching a forbidden name triggers immediate validation aborts.

### B. Naming Convention for Templates (IDE & LSP Friendly)
To guarantee full IDE and Language Server Protocol (LSP) features (e.g., syntax highlighting, linting, autocomplete) for template files within editors (such as VSCode, Neovim, or Emacs), the system enforces a strict suffix naming convention:
*   **Format**: `[filename].[engine_prefix].[target_extension]`
*   **Suffix No-Dot Restriction**: The suffix defined for any render engine (such as `envst` or `mustache`) **cannot contain any dots ('.')**. This is validated during configuration loading, and any engine suffix containing dots will cause validation to fail.
*   **Officially Supported Engines** (Custom engines can be defined in `drift_workspace.toml`):
    1.  *Envsubst*: Uses suffix **`.envst.[ext]`** (e.g., `dot-bashrc.envst.sh`, `all_proxy.envst.conf`).
    2.  *Mustache*: Uses suffix **`.mustache.[ext]`** (e.g., `home.mustache.nix`, `settings.mustache.json`).
*   **Why this is superior**: Because the terminal extension is the actual target format (like `.sh`, `.nix`, `.json`), text editors instantly apply the correct syntax highlighting, formatters, and LSP environments without requiring custom regex filetype mappings.

### C. Unified Declarative Deployment Architecture
Deployment is executed through a single unified declarative pipeline via the per-path deployment planner (`plan_package_install`):
*   **Host-Level Declarative Comparison**: Rather than maintaining separate "surgical" and "full" codepaths driven by staging deltas, the installation engine inspects deployable files directly against host state.
    *   **Identical Files & Symlinks**: Existing relative symlinks pointing to correct targets and existing file copies with identical contents receive `SKIP_IDENTICAL` (zero filesystem I/O, zero link re-creation).
    *   **New or Differing Files**: Missing or modified files receive `CREATE_SYMLINK`, `CREATE_COPY`, or `UPDATE_COPY` (with `BACKUP_OVERWRITE` if colliding with an untracked node).
    *   **Historical Orphan Reconciliation**: Historical files in `deployed_files` that are no longer part of the package receive `BACKUP_PRUNE` (atomically backed up to `backup/<pkg>/deleted_files/` and removed from the host).
*   **Infinite Loop & Ancestor Protection**: Before creating any link or file, intermediate ancestor directories are inspected (`_inspect_ancestor_directories`). If any parent directory is an internal symlink pointing into `drift_root` or is blocked by an existing file, it is backed up to `backup/<package>/overwritten/` and replaced with a concrete directory (`ENSURE_DIR`), preventing circular symlink loops.
*   **Target Migration & Redeployment**: Standalone deployment (`drift apply`), pipeline deployment (`drift deploy`), rollback (`drift rollback`), and `--reinstall` all execute through this unified planner. If a package's target directory migrated (`target_migrated_from`), Drift undeploys from the former destination and plans complete deployment at the new destination.

The program ensures consistent behavior across both `symlink` and `copy` install methods, verifying `enable_install=true` and respecting package metadata, install location, and sudo permissions.

### D. Physical Conflict Prevention (Collision Guard & Deployment Planner)
To protect pre-existing manual files from being silently overridden or destroyed during deployment, Drift dispenses collision guarding into an inspectable, side-effect-free per-path deployment planner (`plan_package_install`), accompanied by centralized pre-flight boundary assertions before any physical operations or lifecycle hooks take place.

#### 1. Centralized Pre-Flight Boundary & Conflict Guards
Before any deployment planning or file operations occur, Drift executes fail-fast pre-flight assertions across all target packages:
1.  **Canonical Workspace Root Boundary Guard**:
    *   Checks whether `target_dir.resolve()` is relative to (or equal to) `drift_root.resolve()` via `assert_packages_ready_for_install`.
    *   Because path resolution expands all symlinks in target path ancestors and leaf destinations, this single canonical check guarantees that no deployment target—whether directly specified or reached via parent symlinks—can point into `drift_root`.
    *   If violated, Drift **aborts immediately** with an `InstallCollisionError` (Exit Code `5`). Deploying into `drift_root` is strictly forbidden to protect the workspace repository from accidental corruption or circular deployment loops.
2.  **Cross-Package Destination Conflict Audit**:
    *   Invokes `assert_no_cross_package_conflicts` across all active packages scheduled for deployment.
    *   Computes the resolved target paths for all deployable files across packages. If two or more packages claim the same host destination path, Drift halts immediately with an `InstallCollisionError` (Exit Code `5`) before modifying any files, preventing inter-package race conditions and destructive overwrites.

#### 2. Declarative Per-Path Planning Pipeline (`plan_package_install`)
Rather than relying on mutating collision routines or monolithic filesystem folder diffing, Drift generates a typed `PackageInstallPlan` using pure per-path inspection helpers:
1.  **Ancestor Directory Inspection (`_inspect_ancestor_directories`)**:
    *   For every deployable file, the planner evaluates all intermediate directory levels between `target_dir` and the destination file, sorted by depth (shallowest to deepest).
    *   Evaluates whether an intermediate path is blocked by a non-directory (e.g. regular file) or is an internal symlink pointing into `drift_root`.
    *   If blocked or internally symlinked, a `BACKUP_OVERWRITE` action is planned (safely backing up the conflicting node to `backup/<package>/overwritten/<path>` with `dot-` prefix translation via `decode_dot_prefix`), followed by an `ENSURE_DIR` action.
    *   Ancestor path inspections are deduplicated across all files in the package to prevent redundant backups or directory creation.
2.  **Leaf File State Machine (`_inspect_symlink_leaf` / `_inspect_physical_file_leaf`)**:
    *   *Symlink Mode*:
        *   **Missing Destination**: Plans `CREATE_SYMLINK`.
        *   **Valid Existing Link**: If the host path is already a relative symlink pointing correctly to the target file in `install/<pkg>/`, plans `SKIP_IDENTICAL`.
        *   **Colliding Node (Stale Symlink, File, or Directory)**: Plans `BACKUP_OVERWRITE` to `backup/<package>/overwritten/<path>` followed by `CREATE_SYMLINK`.
    *   *Copy Mode*:
        *   **Missing Destination**: Plans `CREATE_COPY`.
        *   **Identical Content**: If the host file is a regular file with identical content (`filecmp.cmp`), plans `SKIP_IDENTICAL`.
        *   **Previously Deployed (`state.toml`)**: If the regular file is already tracked in `deployed_files` manifest, plans `UPDATE_COPY` without backup.
        *   **Untracked / Type Collision**: Plans `BACKUP_OVERWRITE` to `backup/<package>/overwritten/<path>` followed by `CREATE_COPY`.
3.  **Orphan Reconciliation (`_inspect_orphans`)**:
    *   Compares the current package files against the historical `deployed_files` manifest stored in `install/state.toml`.
    *   Any previously deployed files that are no longer part of the package are planned as `BACKUP_PRUNE` (swept safely into `backup/<package>/deleted_files/<path>` and physically removed from the host in a single atomic action).
4.  **Dotfile Backup Translation (`decode_dot_prefix`)**:
    *   When saving displaced host files into `backup/<package>/overwritten/` or `backup/<package>/deleted_files/`, leading dots on path components are translated back to `dot-` prefixes (e.g. `.bashrc` $\rightarrow$ `dot-bashrc`, `.config/nvim` $\rightarrow$ `dot-config/nvim`).
    *   This ensures that the backup archive mirrors the exact structural naming conventions of `src/` and `install/`.

#### 3. Inspectable Simulation & Execution Separation
*   **Dry-Run Mode (`drift apply --dry-run`)**:
    *   Passes `dry_run=True` to compile and display the complete `PackageInstallPlan` with planned actions, action counts, and skipped lifecycle hooks without performing any filesystem mutations.
    *   Supports programmatic consumption via `--json`.
*   **Execution Phase (`execute_package_actions`)**:
    *   State mutations strictly follow the pre-computed plan in dependency order: intermediate directory creation, backups, symlink/copy file application, and orphan pruning.

> [!IMPORTANT]
> **Transient `backup/` Directory Policy & User Responsibility**:
> Drift creates timestamped and package-scoped subdirectories under `backup/<package>/overwritten/` and `backup/<package>/deleted_files/` to protect pre-existing host files from silent loss.
> *   **Not Versioned or Tracked by Drift**: The `backup/` folder is intentionally unmanaged and untracked by Git in Drift.
> *   **User Responsibility**: It is solely the user's responsibility to periodically review `backup/`, archive important displaced configurations, or commit them into personal backup storage before cleaning.

### E. Execution Safeguards and Package Exclusion
To enable granular control over modular configurations, the deployment pipeline respects three cascading enablement switches across different execution phases:

#### 1. Global Activation Switch: `drift_workspace.toml [packages.enable]`
*   **Location**: Global workspace config (`config/drift_workspace.toml`).
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
    *   When executing a **Bulk All-Packages Deployment** (`drift deploy` with no targeted package), the system compares the state database `install/state.toml` with the active packages list in `config/drift_workspace.toml` (and respects `enable_install = false` in `drift_package.toml`).
    *   If a package is registered as `"installed"` in `install/state.toml`, but is **no longer active/enabled** in configuration declarations, the post-deployment GC step **automatically executes Primitive 7 (Uninstall) on this orphan package** during Stage 3.
    *   This ensures decommissioned packages are automatically and cleanly purged from the host system, restoring overwritten original files from `backup/<package>/overwritten/`.
    *   **Database Hygiene**: In addition to orphan uninstallation, GC purges disabled or missing package folders from `render/`, cleans unregistered obsolete folders from `install/`, purges zombie directories lacking valid package configurations, and auto-commits database changes into local Git repositories.
2.  **Uninstall Protection Safeguard**:
    *   If a user tries to manually uninstall a package (e.g. `drift uninstall proxychains`), but that package is **still active/enabled** inside `config/drift_workspace.toml` (and has `enable_install != false`), this represents a direct contradiction because the package would simply be re-installed on the next bulk deploy.
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

3.  **Adopting Host-Side Modifications, Renames & New Additions (`drift adopt`)**:
    When manual modifications, file renames, directory additions, or deletions (within Fully-Controlled Directories) are reverse-synced back into `install/`:
    *   **Unified Patch Application & Suffix Resolution**:
        `drift adopt` matches live files in `install/` to their source template counterparts in `src/` by querying active `RenderEngineRegistry` suffixes (e.g. `.envst`, `.mustache`, custom engines). Unified diffs are extracted via `git diff HEAD`, header paths are adjusted, and patches are applied directly to templates.
    *   **Permission & Mode Synchronization**:
        File mode bits (e.g. `chmod 0755` executable permissions) are automatically synchronized from `install/` onto the corresponding source file in `src/` during adoption.
    *   **Empty Folder & Sentinel Stub (`.drift_keep`) Translation**:
        - **Renames**: Any rename involving `.drift_keep` (e.g. `dir_a/.drift_keep` $\rightarrow$ `dir_b/.drift_keep`) is split into a deletion of `dir_a` and an addition of `dir_b`.
        - **Path Translation**: Additions and deletions of `<path>/.drift_keep` are translated directly to `<path>`.
        - **Corruption Guards**: Any modified or non-empty `.drift_keep` file triggers a loud `⚠️ [CORRUPT]` warning and is stripped from adopt planning (`.drift_keep` must always be a 0-byte stub).
        - **Directory Additions**: Clean directory additions call [`ensure_dir`](../src/drift/utils/file_ops.py) to create the folder inside `src/<package>/` (the `.drift_keep` stub is **never** copied into `src/`). Pre-existing directories in source are skipped cleanly; collisions with regular files or templates raise conflicts.
        - **Directory Deletions**: If the source directory is empty, [`adopt_deletion`](../src/drift/primitives/adopt_repo.py) removes it via [`remove_tree`](../src/drift/utils/file_ops.py). If the source directory is non-empty, non-interactive adoption discards the deletion with a warning log to preserve user files (restoring the folder on the next deploy); interactive mode displays an itemized list of existing source files, a prominent danger warning, and requires explicit confirmation (`[y/N]`) before executing `remove_tree`.
        - *(See [`docs/empty_folder_dataflow.md`](empty_folder_dataflow.md) for full architectural dataflow and edge cases.)*
    *   **Interactive Guided Reconciliation (`drift adopt -i`)**:
        - **Diff Inspection**: Shows clean unified diffs before presenting resolution options.
        - **Clean Patches**: Supports direct adoption, immediate post-merge editing in `$EDITOR` (`Adopt and Edit in Editor`), or opening template and live files in side-by-side split view (`Open Side-by-Side Reference` supporting `nvim`, `vim`, `code`, and `emacs`).
        - **Conflicted Patches**: Offers three robust fallback strategies:
          1. *Over-render & Freeze*: Backs up original template to `.bak` and overwrites it with static live content.
          2. *Merge Conflict Editor*: Injects standard merge conflict markers via `patch --merge` and opens `$EDITOR`.
          3. *Side-by-Side Reference*: Opens template and live drift side-by-side for manual merge.
    *   **Non-Interactive Automated Adoption (`drift adopt`)**:
        - Adopts clean modifications, renames, and deletions automatically.
        - Emits unified diffs via `logger.debug(...)` (visible with global `-v` / `--verbose`).
        - Passing `--accept-conflicts` permits automated application of conflicting patches with merge conflict markers.

### H. State Registry Database (`install/state.toml`)
To safely determine whether a package should execute its `pre/post_install` or `pre/post_update` lifecycle hook, track deployed artifacts, verify execution privileges, and detect destination migrations, the system maintains a persistent, local-only state registry file at `install/state.toml`.
*   This registry tracks package lifecycle states:
    ```toml
    # install/state.toml
    [packages.nvim]
    state = "installed"
    target_directory = "~/.config/nvim"
    last_deployed = "2026-08-16T21:10:50.123456"
    install_method = "symlink"
    deployed_files = ["dot-config/nvim/init.lua", "dot-config/nvim/coc-settings.json", "dot-config/nvim/autoload"]
    sudo = false

    [packages.sysctl_config]
    state = "installed"
    target_directory = "/etc"
    last_deployed = "2026-08-16T21:12:00.000000"
    install_method = "copy"
    deployed_files = ["sysctl.d/99-custom.conf"]
    sudo = true

    [packages.qbittorrent]
    state = "staged"
    target_directory = "~/.config/qBittorrent"
    install_method = "copy"
    deployed_files = ["config.ini"]
    sudo = false

    [packages.wezterm]
    state = "installing"
    target_directory = "~/.config/wezterm"
    install_method = "symlink"
    deployed_files = []
    sudo = false
    ```
*   **Tracked Manifest Fields**:
    - **`state`**: Lifecycle state string (`"installed"`, `"staged"`, `"staging"`, `"installing"`).
    - **`target_directory`**: Recorded host target path where package files were deployed. Used to detect destination migrations (`get_target_migrated_from`), build cross-package destination ownership mappings (`build_destination_ownership_map`), and resolve target directories during uninstallation without requiring source package configurations.
    - **`last_deployed`**: ISO-8601 timestamp string of the last successful deployment. Used for hook classification (differentiating first-time installs from updates).
    - **`install_method`**: Method used during physical deployment (`"symlink"` or `"copy"`).
    - **`deployed_files`**: Sorted list of relative paths successfully deployed onto the host, including empty directories as leaf nodes. Historical orphan reconciliation compares desired state in `install/` against this manifest.
    - **`sudo`**: Boolean recording whether the package was installed with root privilege escalation (`sudo = true`). Persisted directly into `PackageUninstallContext` to ensure uninstallation and backup restoration retain identical root privileges even if package configurations in `install/` are missing or modified.
*   **Lifecycle States**:
    - **`"installed"`**: (Stable) The package is fully applied to the host system.
    - **`"staged"`**: (Stable) The package has been successfully staged from `render/` to `install/`, but not yet applied to the system.
    - **`"staging"`**: (Transient) The package is currently undergoing database synchronization (Primitive 4).
    - **`"installing"`**: (Transient) The package is currently being physically applied to the system (Primitive 5).
*   **Safety Abort Logic**:
    When a package enters Primitive 4 or 5, the system checks its current state.
    - **Mid-Operation Safety Interlocks**: If the state is **`"staging"`** or **`"installing"`**, and the `force` flag is not passed, the operation **aborts immediately**. This indicates a previous execution failed midway, leaving the database or system in an inconsistent state. The user is instructed to run `drift rollback` to restore integrity. Passing `--force` overrides this safety interlock.
    - **Declarative Exclusions (`enable_install = false`)**: Packages with `enable_install = false` are never staged or deployed, regardless of whether `--force` is supplied. `--force` only overrides runtime safeguards, not declarative package rules.
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
*   **Empty Directory Capture**: Scans FCDs with `DirMode.ONLY_EMPTY_DIR` to capture wild empty folders, generating `CREATE_KEEP_FILE` actions that write `.drift_keep` stub files into `install/<package>/`.
*   **Sentinel Stub Pruning**: When a previously empty folder is populated with child files on the host, ancestor directory inspection prunes the obsolete `.drift_keep` stub file (`DELETE_ITEM`).
*   This places the workspace in an uncommitted state, signaling an active host configuration drift that must be reconciled.

#### 2. Bidirectional Reconciliation Flow (`drift adopt`)
Developers reconcile discovered untracked FCD additions using `drift adopt`, which executes the following underlying Git and ignore engine mechanics depending on the selection:
*   **Scoped Git Cleanliness Safeguard Check**: Prior to modifying any file inside `src/`, `drift adopt` verifies that the specific target package's source directory (`src/<package>/`) is completely Git clean. This ensures uncommitted draft changes in other active packages do not block the adoption workflow.

*   **Adopt (Keep in Repository)**:
    1.  *Source Copy*: For regular files, the file is copied from `install/<package>/` to `src/<package>/` (reverting prefix translations like `.` back to `dot-` and preparing template configurations if desired). For empty directories, [`ensure_dir`](../src/drift/utils/file_ops.py) creates the pure folder in `src/<package>/` without copying the `.drift_keep` sentinel.
    2.  *Commit / Staging*: Staging `install/` via `git -C install add -- <pkg>/<rel_path>` acknowledges the change, permanently aligning state.
*   **Ignore (Keep on System, Stop Tracking)**:
    1.  *State Unlink*: The untracked file or directory is deleted from the `install/<package>/` state database (`git rm --cached` and physical removal), restoring state cleanliness.
    2.  *Ignore Registration*: The relative path pattern (with a trailing `/` for directories) is appended to the package's `.drift_ignore` PCRE ignore configuration.
    3.  *The Result*: During all future `reverse-sync` sweeps, the ignore engine sees that the physical host path matches `.drift_ignore` and skips syncing it, allowing the untracked path to reside on the active host system without registering as database drift.
*   **Discard/Delete (Remove from System)**:
    1.  *State Indexing (`git add`)*: Instead of immediately unlinking, Drift stages and tracks the untracked file or directory inside the local `install/` Git repository by executing `git -C install add <file_path>`.
    2.  *Staging Delta Promotion*: On the subsequent `drift deploy` (Stage 2) run, because the file/directory is tracked in `install/` but is completely absent from the newly compiled `render/` sandbox output, the staging promotion compiler (`drift stage` / Primitive 4) automatically flags it as an **orphaned deletion** (tracked in state, but missing from compiled declarations).
    3.  *The Result*: A delete instruction is generated for this file/directory, and during the physical deployment phase (`drift apply` / Primitive 5), it is symmetrically and cleanly deleted from the active host system, restoring pristine configuration baseline alignment!

### J. CLI Privilege Safeguards & Sudo Workspace Protection
To prevent target directory mismatches and permission corruption across internal sub-repositories and state databases, Drift enforces strict privilege boundaries when executing workspace actions:

1.  **Dual Hazards of Running Under `sudo`**:
    *   *Hazard 1: Target Path Mismatch (`$HOME` / `~` Expansion)*:
        Running the CLI under `sudo` causes environment variables such as `$HOME` and path expansion `~` to evaluate to `/root` instead of the invoking user's home directory. This causes relative or tilde-based deployment targets (e.g. `default_target_directory = "~"`) to resolve incorrectly to `/root`, unintentionally deploying user dotfiles into root's directory tree.
    *   *Hazard 2: Root File Ownership Pollution*:
        Executing compilation and staging under `sudo` creates internal database commits, lockfiles, rendered templates, and state metadata (`render/.git`, `install/.git`, `install/state.toml`, `.drift/`) with `root:root` ownership and restricted permissions. Subsequent non-sudo invocations by the regular user will fail with fatal `PermissionError` exceptions when attempting to write, lock, or update Git databases.

2.  **Privilege Enforcement & Permitted Scenarios**:
    When executing actions against a workspace directory (`drift_root`), `check_sudo_and_root` inspects execution privileges and directory ownership, permitting execution strictly under two valid scenarios:
    *   *Scenario 1: True Root Session*: The current process is running as the actual root user (`os.getuid() == 0` without `SUDO_USER` set in environment variables, such as inside root containers or direct root logins).
    *   *Scenario 2: Root-Owned Workspace*: The workspace root directory (`drift_root`) itself is owned by root (`uid == 0`). In this case, writing root-owned database artifacts causes no ownership mismatch.

3.  **Remediation & Granular Elevation (`sudo = true`)**:
    If a non-root workspace is invoked under `sudo`, Drift immediately halts with exit code `1` and outputs a clear multi-step guide:
    *   *Granular Privilege Elevation (Recommended)*: If specific packages need elevated permissions to deploy files into system directories (e.g., `/etc/`), configure `sudo = true` inside the individual `src/<package>/drift_package.toml`. Drift will selectively elevate only the physical file copying, symlinking, and pruning operations via `sudo` while keeping workspace databases cleanly owned by the user and preserving correct `$HOME` target resolution.
    *   *Dedicated Root Dotfiles*: If the workspace is specifically intended to manage root dotfiles, place the repository in root's home directory (e.g., `/root/...`).
    *   *Explicit Override*: If the user intentionally wishes to manage a root-owned workspace, they can chown the workspace directory via `sudo chown -R root:root <drift_root>`.

---

## 7. Detailed Implementation Specifications

### Overview of Control Flow & Orchestration

The active configuration engine and orchestrator follow a strict sequence designed for predictability, transaction-like integrity, and extensive error recovery.

#### 1. Discovery and Registry Check
Deployment can be triggered in **Bulk Mode** (evaluating all declared active packages) or **Targeted Mode** (focusing on a specific package).
*   **Discovery**: The orchestrator checks workspace declarations in `config/drift_workspace.toml` to identify enabled packages, then verifies that `enable_install` is `true` in each package's `drift_package.toml`.
*   **Mid-Operation Registry Interlock**: The state database at `install/state.toml` is queried. If any package is currently in a `"staging"` or `"installing"` state, execution is aborted unless the `--force` flag is supplied, preventing corruption from a previous midway failure.

#### 2. Stage 1: Alignment Safeguard (System -> Install)
*   The system executes **Primitive 1: Reverse Sync** on all target packages to capture and reconcile any manual, local modifications made directly on the active host system.
*   **The Reverse-Sync Reconciliation Flow**:
    - **Targeted Tracked Comparison**: Probes only package files in `install/<package>` on host (`src_only=True, translate_mode="forward"`), avoiding full `$HOME` directory traversals.
    - **System Deletions**: Tracked files manually deleted on the system are symmetrically removed from the `install/` state database folder.
    - **System Modifications & Type Changes**: Files edited on the system (or transformed into directories) are reverse-copied back to `install/` with dot-prefix translation (`.bashrc` $\rightarrow$ `dot-bashrc`).
    - **Scoped FCD Sync & Empty Folders**: Any wild/untracked files inside configured **Fully-Controlled Directories (FCD)** are discovered and synced back via scoped subtree comparisons. Newly created wild empty directories on the host generate `CREATE_KEEP_FILE` actions to establish `.drift_keep` in `install/<package>/`, while populated directories prune obsolete sentinel stubs.
*   **Uncommitted State Check**: After performing reverse-sync on all active packages, the deployer checks if `git -C install status` is dirty. If uncommitted changes are detected (representing active host drift, i.e., Diff B), the deployer **halts immediately**. This acts as a security sentinel, forcing the developer to explicitly review the drift (via `drift diff --system`) and either **Adopt** (via `drift adopt`) or **Dismiss** (by deploying with `--force`) the system changes before template rendering can continue.

#### 3. Stage 2: Sandboxing & Reconciliation (Render -> Stage)
*   **Sandbox Render (Primitive 2)**:
    - **Global Pre-render Resolving**: Before compiling packages, all configured global template variables or input database configs (e.g. `mustache.envst.json` -> `mustache.json` using `envsubst`) are rendered.
    - **Rendering Execution Flow (`render_package.py`)**:
        1. *Sandbox Cleansing*: Clears any pre-existing package folder in `render/` using `shutil.rmtree` to maintain clean state while preserving the underlying `render/.git` repo.
        2. *Metadata Compiling*: Loads and compiles package config from the source folder (supporting on-the-fly parsing of `package.envst.toml` templates). If `enable_render` is false, rendering is skipped.
        3. *Pre-Source Lifecycle Hook*: Triggers the `pre_source` hook script (if defined) running with working directory set to the script's parent directory (`cwd = hook_path.parent`) to dynamically generate/update source templates or dynamic system files prior to compilation.
        4. *Misspelled Ignore Warning*: Checks for a misspelled `.driftignore` and if found (without `.drift_ignore`), logs a warning and automatically copies it under correct name `.drift_ignore`.
        5. *Surgical File Walk & Hook Sandbox Compilation*: Traverses the source package directory. Dedicated lifecycle hook scripts in `src/<package>/drift_hooks/` are compiled into `render/<package>/.drift/hooks/` (with template expansion), isolating scripts from deployable dotfiles. Subdirectory ignore files (`.drift_ignore` or `.driftignore`) are blocked with errors. Static files are physically copied. Template files matching any active engine configuration suffix are surgically compiled (engine suffix is stripped from the rendered file name). Empty directories are populated with 0-byte `.drift_keep` sentinel files so Git tracks the directory structure.
        6. *Post-Render Lifecycle Hook*: Triggers the `post_render` hook script (if defined) running with its working directory set to the script's parent directory (`cwd = hook_path.parent`).
    - **Sandbox Render Commit (Primitive 3)**: Automatically commits the sandbox changes inside the local `render/` repository to maintain a full history of declarative rendering.

*   **Staging Database (Primitive 4 - `stage_repo.py`)**:
    - **Structural Fidelity Invariant**: Staging preserves the physical directory structure and contents of `render/<package>` into `install/<package>` with 100% 1:1 fidelity, including `.drift_keep` sentinel files. No synthetic files or ignore artifacts are generated in `install/`.
    - **Installation Exclusions**: Skips any packages that declared `enable_install` as `false` (this declarative exclusion is strictly preserved and never bypassed, even when `--force` is used).
    - **Staging Conflict Safeguard**: If any targeted package in the state database `install/` contains uncommitted local modifications, staging aborts immediately (unless `--force` is used).
    - **Staging Transaction Interlock**: Sets the package state to transient `"staging"` inside `state.toml` before any changes are written. If a package is found in `"staging"` or `"installing"` state from a previous crash, staging is aborted unless `--force` is provided.
    - **Reconciliation & Synchronization Pipeline**:
        1. *Single-Pass Plan Compilation (`plan_package_stage`)*: Compares `render/<package>` and `install/<package>` without ignore filtering (`ignore_handler=None`), maintaining 100% 1:1 structural fidelity. Compiles changes into an inspectable `PackageStagePlan` with `FileAction`s (`DELETE_ITEM`, `DELETE_TREE`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `CREATE_KEEP_FILE`).
        2. *Unified Delivery Execution (`execute_package_stage`)*: Dispatches actions to `execute_delivery_actions`, performing physical file deletion, directory creation, file copying, and fast permission synchronization.
        3. *Ignore & Metadata Synchronization*: All `.drift/` control plane metadata (`.drift_ignore`, `drift_package.toml`, `.drift/hooks/`, `.drift/render/`) are mirrored strictly 1:1 without extra ignore shims.
    - **Staged Transaction Complete (or Dry-Run Simulation)**: In live execution, updates the state registry database to stable `"staged"` and returns a structured `StageResult` containing changed packages and their `PackageStagePlan`s. In dry-run mode (`dry_run=True`), compiles and returns the full `StageResult` with zero mutations to `install/` or `state.toml`.

#### 4. Stage 2: Physical Deployment Sequence (Primitive 5)
For each redeployable package:
*   **Target Directory Check**: The engine verifies that the package's target directory is absolute and is not nested inside or equal to the workspace root (`target_dir.resolve()` relative to `drift_root.resolve()`).
*   **Cross-Package Conflict Guard**: Verifies no two deployable packages target identical host file paths (`assert_no_cross_package_conflicts`). Empty directories are excluded from conflict assertions (`include_empty_dirs=False`), allowing shared directory trees across packages.
*   **Target Migration Check**: If the target directory changed from previous deployments (`target_migrated_from`), Drift undeploys files from the former host location and forces a full redeployment.
*   **State Transition to `"installing"`**: The state registry database `state.toml` is written to mark the package's state as `"installing"`.
*   **Declarative Plan Compilation (`plan_package_install`)**:
    - *Intermediate Directory Inspection*: Evaluates all intermediate directory levels between `target_dir` and the destination file, planning `BACKUP_OVERWRITE` and `ENSURE_DIR` if blocked by files or internal symlinks.
    - *Leaf Inspection*: Evaluates leaf destinations according to `install_method` (`symlink` or `copy`), planning `CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `SKIP_IDENTICAL`, or `BACKUP_OVERWRITE`. Empty directories are planned as `CREATE_DIR` (`mkdir -p`) and never symlinked.
    - *Orphan Reconciliation*: Compares deployable files against historical `deployed_files`, planning `BACKUP_PRUNE` to `backup/<pkg>/deleted_files/` and physical removal.
    - *Dry-Run Preview*: If `--dry-run` is active, displays the plan summary and exits without modifying the host filesystem or executing lifecycle hooks.
*   **Lifecycle Pre-Hook**: The package's `pre_install` (first-time install) or `pre_update` (subsequent update) executable script is triggered, running with its working directory set to the script's parent directory (`cwd = hook_path.parent`).
*   **Target Manifest Synchronization**: Synchronizes deployable targets to `state.toml` before physical delivery so crashes have an authoritative list for recovery.
*   **Plan Execution Phase (`execute_package_actions`)**:
    - Applies planned operations in topological dependency order: intermediate directory creation, backups to `backup/<pkg>/overwritten/`, atomic file copying or relative symlink creation, and orphan pruning. Elevated permissions are used if `sudo = true` is configured.
*   **Lifecycle Post-Hook**: Triggers `post_install` or `post_update` executable scripts, running with its working directory set to the script's parent directory (`cwd = hook_path.parent`). The host target directory is accessible via `$drift_package_target_dir`.
*   **State Registry Lock**: The state database is updated: the package's state is set to `"installed"`, a deployment timestamp is written, `target_directory`, `install_method`, and `sudo` flag are saved, and the list of successfully deployed paths (including empty directories) is recorded in the `deployed_files` manifest inside `state.toml`.

#### 5. Stage 2: Final State Commit (Primitive 6)
*   The updated configurations and `state.toml` file are staged and committed into the local-only `install/` Git repository, locking the environment into a clean, reproducible state.

#### 6. Stage 3: Post-Deployment Workspace Garbage Collection (Primitive 9 - Bulk Mode Only)
*   **Bulk Deployment Automation**: If a bulk deployment (all active packages) succeeds, the engine automatically triggers **Primitive 9: Workspace Garbage Collection**.
    *   *Note*: When running targeted deployments for specific packages (`drift deploy <pkg>`), Stage 3 GC is bypassed to keep surgical operations isolated.
*   **Decomposed 4-Phase Execution Pipeline**:
    1.  *Orphan Package Uninstallation*: Compares `install/state.toml` against active packages declared in `drift_workspace.toml`. Packages marked as `"installed"` in state but disabled or removed from configuration declarations are automatically uninstalled via **Primitive 7 (Uninstall)** with `force=True` (triggering lifecycle hooks, unlinking/deleting active host files, restoring original backups from `backup/<package>/overwritten/`, pruning package directories, and synchronizing `state.toml`).
    2.  *Render Database Purge*: Traverses `render/` to identify and remove directories that are zombies (lacking valid config), disabled in workspace config, or whose source folder `src/<pkg>` was deleted. Internal directories (`.git`, `config/`) and forbidden names are strictly preserved.
    3.  *Install Database Purge*: Traverses `install/` to identify and remove directories that are zombies or are not registered in `state.toml` while disabled/missing from `src/`. Registered packages are protected from raw filesystem removal and handled exclusively via graceful uninstallation in step 1.
    4.  *Scoped Git Database Commits*: Auto-stages and commits purged folder deletions in `render/` and `install/` repositories, scoping commits specifically to purged package paths.

---

## 8. Philosophical & Operational Benefits

By implementing this architecture, the user reaps distinct Unix-style benefits:
1.  **Strict Demarcation of Merges**: Automation is restricted to simple *reverse-syncing state* and *unilateral overwrite deployment*. The human developer remains the sole merge authority. If active system changes (Diff B) are dirty, the user is presented with standard Git outputs and handles the backport to templates manually (or via `drift adopt`).
2.  **No Hand-Crafted State Engine**: By designating `install/` and `render/` as local Git repositories, we avoid writing custom rollback, commit-tracking, and differential history features. Git manages the hard stuff (index, diffs, conflicts).
3.  **Complete, High-Fidelity Backups**: Deleted configurations and overwritten links are never silently purged; they are meticulously structured and swept into `backup/<package>/` with clean console reporting.
4.  **Extensible Lifecycles**: Adding complex validation or post-deployment logic is as simple as dropping a standard executable bash script in `src/<package>/pre-update.bash` or `post-install.bash`.
5.  **Structured Observability**: Built-in `--json` support across all primitives and CLI entry points allows agentic pairing, automation tools, and CI/CD pipelines to interact with strongly-typed result models.
