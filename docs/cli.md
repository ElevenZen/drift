# 🛠️ Drift CLI Reference & Command Specification

## 1. Quick Reference: Command Overview

```bash
drift [--global-flags] <command> [arguments...] [--command-flags]
```

### All Available Commands:

| Command | Positional Arguments | Key Flags | Purpose |
| :--- | :--- | :--- | :--- |
| **`init`** | — | `-f` | Initialize a new Drift workspace repository |
| **`new`** | `<pkg>` | `-t`, `-m`, `-f` | Scaffold a new package and `drift_package.toml` |
| **`add`** | `<pkg> <paths...>` | `-n`, `--no-hooks` | Import host files with `dot-` prefix translation |
| **`enable`** | `<packages...>` | `--local`, `-n` | Enable packages in `[packages.enable]` (format-preserving edit) |
| **`disable`** | `<packages...>` | `--local`, `-n` | Disable packages in `[packages.enable]` (does not uninstall) |
| **`status`** | `[packages...]` | `--json` | Audit template evolution and active host drift |
| **`diff`** | `[packages...]` | `-t`, `-s`, `--stat`, `-y` | Visualize diffs across `src/`, `render/`, `install/` |
| **`deploy`** | `[packages...]` | `-f`, `-r`, `-c`, `-n`, `--rollback` | Sentinel-guarded render, stage, apply, and GC |
| **`plan`** | `[packages...]` | `-a`, `-f`, `-r`, `-c`, `--with-hooks` | Full-cycle dry-run deployment preview in sandbox |
| **`rollback`** | `[packages...]` | `-f`, `--no-hooks` | Rollback failed deployments to last clean state |
| **`adopt`** | `[packages...]` | `-i`, `--accept-conflicts`, `-f`, `-n` | Patch live host drifts back into `src/` templates |
| **`uninstall`** | `<packages...>` | `-f`, `--detach`, `-n`, `--no-deps` | Uninstall packages or detach symlinks as copies |
| **`gc`** | — | `-n`, `--no-hooks` | Purge orphan packages, zombie dirs, and stale WALs |
| **`repair`** | — | `-n`, `--json` | Self-heal missing workspace folders and Git DBs |
| **`clone`** | `<git_url> [dest]` | `-b`, `--depth`, `--no-repair` | Clone a Git repo and auto-repair the workspace |
| **`health`** | `[packages...]` | `--from`, `-t`, `-v` | Execute runtime health check probes on packages |
| **`complete`** | `[<shell>]` | `--install`, `--json` | Generate or install shell tab-completion scripts |
| **`help`** | `[topic]` | — | Display interactive mini-manual documentation |
| **`render`** | `[packages...]` | `-c`, `-n`, `--with-hooks` | *(Low-Level)* Compile templates into `render/` |
| **`render-commit`** | `[packages...]` | `-m "msg"` | *(Low-Level)* Commit compiled `render/` changes |
| **`reverse-sync`** | `[packages...]` | `--json` | *(Low-Level)* Sync live host changes into `install/` |
| **`stage`** | `[packages...]` | `-f`, `--no-deps` | *(Low-Level)* Stage `render/` into `install/` DB |
| **`apply`** | `[packages...]` | `-f`, `-n`, `-r`, `--rollback` | *(Low-Level)* Apply `install/` to host filesystem |
| **`install-commit`** | `[packages...]` | `-m "msg"` | *(Low-Level)* Commit deployed state in `install/` |
| **`hook`** | `<pkg> <hook_name>` | `--from <install\|render\|source>` | *(Low-Level)* Trigger a specific lifecycle hook |

* **Note**: All commands accept movable global flags (`--json`, `-v/--verbose`, `--raw-errors`, `--trace`) before or after the subcommand. See Section 3 below for complete per-command flag signatures.

---

## 2. Global Flags & Movable Flags

*   `-C, --directory <DIR>`: Run as if drift was started in `<DIR>` instead of the current working directory.
*   **Workspace discovery**: Drift walks upward from the current (or `-C`) directory and uses the nearest directory containing `config/drift_workspace.toml` (or its `.envst` / `.local` variants) as the workspace root. The enclosing Git toplevel is irrelevant, so workspaces nested inside other repositories (e.g. `samples/<name>/`) work out of the box. `drift init` always initializes the current (or `-C`) directory literally.
*   `-v, --verbose`: Enable verbose (DEBUG) logging output. (*Movable: can be specified before or after subcommands*).
*   `--json`: Output results in structured machine-readable JSON format using typed result models. (*Movable: can be specified before or after subcommands*).
*   `--raw-errors`: Bypass CLI error boundary to print raw exceptions and full Python stack trace without changing the logging level. (*Movable: can be specified before or after subcommands*).
*   `--trace`: Full diagnostic tracing: enables verbose/debug logging output *and* bypasses the CLI error boundary to print full Python stack traces on exceptions. (*Movable: can be specified before or after subcommands*).

---

## 3. Detailed Command Reference

### A. Initialization: `drift init [--force] [--json]`
Initializes the current (or `-C`) directory literally as a drift workspace (never redirected to an enclosing Git toplevel).
*   **Command Signature**: `drift init [--force / -f] [--json]`
*   **Optional Arguments & Flags**:
    - `--force / -f`: Re-initializes workspace files and database sub-repositories even if already initialized.
    - `--json`: Outputs initialization results in structured JSON format.
*   **Actions**:
    1.  If the directory is empty and not tracked by Git, initializes an empty Git repository.
    2.  Verifies the enclosing Git repository is healthy (not bare, detached, or mid-merge/rebase) unless `--force` is given.
    3.  Creates `.gitignore` entries to isolate `render/` and `install/` database files.
    4.  Initializes `render/` and `install/` as independent, untracked local Git repositories.
    5.  Creates default directory structure (`src/`, `config/drift_workspace.toml`, `config/envsubst.bash`, `config/mustache.envst.json`, `install/state.toml`).
*   **Terminal Output**:
    ```bash
    ✨ Initialized drift workspace!
    📁 Created render/ sandbox Git database.
    📁 Created install/ local state Git database.
    📝 Generated drift_workspace.toml template.
    📝 Generated config/envsubst.bash, config/mustache.envst.json, and config/jinja2.mustache.json.
    ```

---

### B. Package Creation: `drift new <package> [--force] [--target <dir>] [--method <symlink|copy>] [--json]`
Create a new package directory with the default `drift_package.toml` configuration file.
*   **Command Signature**: `drift new <package> [--force / -f] [--target / -t <target_directory>] [--method / -m <install_method>] [--json]`
*   **Optional Arguments & Flags**:
    - `--force / -f`: Forcefully overwrites any existing `drift_package.toml` config file inside the package source directory.
    - `--target / -t <target_directory>`: Explicitly configures the deployment target directory inside `drift_package.toml`. Defaults to `default_target_directory` in `drift_workspace.toml`.
    - `--method / -m <install_method>`: Explicitly configures the installation method (`symlink` or `copy`) inside `drift_package.toml`. Defaults to `default_install_method` in `drift_workspace.toml`.
    - `--json`: Outputs a `NewPackageResult` object in JSON format.
*   **Probing Guard**: Halts if a configuration file already exists inside `src/<package>/` unless `--force` is provided.

---

### C. Resource Import: `drift add <package> <paths...> [-n] [--no-hooks] [--json]`
Import physical active system configuration files or directories into your declarative source folder.
*   **Command Signature**: `drift add <package> <paths...> [-n / --dry-run] [--no-hooks / --no-hook] [--json]`
*   **Dry-Run Mode**: Passing `-n / --dry-run` performs all symlink resolutions, prefix translations, and path calculations, previewing changes without touching disk.
*   **Bypass Lifecycle Hooks**: Passing `--no-hooks` (or `--no-hook`) bypasses execution of the `pre_source` hook.
*   **Dot-Prefix Translation**: Files and directories starting with `.` are automatically translated to `dot-` prefixes (e.g., `.config/nvim/init.lua` $\rightarrow$ `dot-config/nvim/init.lua`).
*   **Symlink Resolution Policy**: If an input path is a symlink pointing outside the state database, Drift resolves the target and copies physical contents for reproducibility.

---

### C2. Package Enablement: `drift enable|disable <packages...> [--local] [-n] [--json]`
Toggle `[packages.enable]` entries without opening an editor.
*   **Command Signature**: `drift enable <packages...> [--local] [-n / --dry-run] [--json]` (same for `disable`)
*   **Target File**: `config/drift_workspace.toml`, or its `.envst.toml` template when only the template exists (same precedence as the config loader).
*   **`--local`**: Edits `config/drift_workspace.local.toml` (or `.local.envst.toml`) instead; the file is created with a `[packages.enable]` section if missing.
*   **Format-Preserving Edit**: Only the affected lines change. Comments, blank lines, `${VAR}` expressions, key quoting, and CRLF line endings are kept. New keys are inserted after the last key of `[packages.enable]`.
*   **Verified Write**: The file is parsed before and after the edit. If the re-parsed result differs from the original by anything other than the requested `[packages.enable]` changes, the command aborts with a configuration error and writes nothing.
*   **Dry-Run Mode**: `-n / --dry-run` prints a unified diff of the change (`--json` includes it in the `diff` field) and writes nothing.
*   **Package Validation**: `enable` requires `src/<pkg>/`. `disable` also accepts packages that only exist in `render/`, `install/`, or `[packages.enable]`. `DEFAULT` sets the fallback policy.
*   **Shadowing Warning**: If a base-file change is overridden by `drift_workspace.local.toml`, a warning suggests `--local`.
*   **Config Only**: Neither command deploys or uninstalls. Follow with `drift deploy` or `drift uninstall`.
*   **Not Editable Automatically**: `[packages.enable]` written as an inline table or dotted keys, or values spanning multiple lines, are rejected with a request to edit manually.

---

### D. Status Inspection: `drift status [-n] [--json]`
Analyzes and aggregates current system alignment across three vectors:
*   **Template Status [A]**: Did template files under `src/` evolve compared to `render/`?
*   **System Drift Status [B]**: Has the host system drifted from `install/` due to runtime edits?
*   **Pending Delta [Δ]**: Are there differences waiting to be deployed from `render/` to `install/`?

```bash
$ drift status

Package: nvim
  [A] Template: MODIFIED
      M nvim/init.lua
  [B] System:   CLEAN
  [Δ] Pending:  STAGED
      (+1, ~1, -0 files)
```

---

### E. Change Visualization: `drift diff [packages...] [options] [--json]`
Provides deep comparisons between configuration layers:
*   **`drift diff [packages...]` (Default: Pending Delta / Diff Δ)**: Compares `render/` against `install/`.
*   **`drift diff [packages...] --template` (or `-t`, Diff A)**: Visualizes Template Evolution (`src/` vs `render/`).
*   **`drift diff [packages...] --system` (or `-s`, Diff B)**: Visualizes Active System Drift (`System` vs `install/`).
*   **`drift diff [packages...] --stat`**: Shows a concise diffstat summary of file change counts.
*   **`drift diff [packages...] --side-by-side` (or `-y`)**: Launches an interactive visual diff session in your configured editor (`$VISUAL`, `$EDITOR`, Neovim `nvim -p -d`, Vim `vim -p -d`, VS Code `code --wait --diff`, or GNU Emacs `ediff`) comparing changed file pairs across multiple tabs/windows.
*   **Diff Filtering**: Excludes internal synthetic artifacts (`.gitignore*`) and editor/OS temporary files (`TEMPORARY_FILE_PATTERNS`) while cleanly displaying user modifications to package metadata (`drift_package.toml`, `.drift_ignore`).
*   **`--json`**: Returns a typed `DiffResult` containing per-package added, modified, and deleted files.

---

### F. Safe Deployment: `drift deploy [packages...] [--force] [-r|--reinstall] [-c] [-n|--dry-run] [--rollback <policy>] [--no-deps] [--no-hooks] [--json]`
Deploys configurations using an atomic two-stage compilation and application engine.

*   **Command Options**:
    - `packages...`: Optional package name(s) to deploy. If omitted, performs a global deployment of all active packages.
    - `--force / -f`: Bypasses the Stage 1 Sentinel Drift audit (overriding uncommitted host system changes), ignores midway failed states (`staging`/`installing` in `install/state.toml`), bypasses uncommitted modifications safeguards in `install/`, and automatically ignores missing required package dependencies. *(Note: Does **not** bypass `enable_install = false`).*
    - `-r / --reinstall`: Force full reinstallation of all packages, bypassing stage change skipping. By default, Drift analyzes staging changes (`installed_files`, hook scripts, and `drift_package.toml`) and skips deploying packages whose stage outputs are unchanged. Passing `-r` or `--reinstall` forces all lifecycle hooks and physical file applications to execute regardless of stage changes.
    - `-c / --clean / --no-cache`: Bypasses the render cache and lockfile, forcing clean re-rendering of all templates and static assets during the compilation phase.
    - `-n / --dry-run`: Transparent alias to `drift plan`. Simulates the entire deployment lifecycle without modifying filesystem or Git state.
    - `--rollback <auto|always|yes|never|no>`: Overrides automatic rollback behavior upon fatal installation or hook failure (`Step 4b`), taking precedence over `[settings] rollback` in `drift_workspace.toml` and `[package] auto_rollback` in `drift_package.toml`:
        - `auto`: Defers to workspace `[settings] rollback` (which in turn defers to per-package `auto_rollback = true | false` when set to `"auto"`).
        - `always` / `yes`: Forces automatic whole-batch rollback if any package encounters a fatal failure during physical installation.
        - `never` / `no`: Disables automatic rollback even for packages declaring `auto_rollback = true`.
    - `--no-deps`: Bypasses missing required package dependency checks during topological ordering and staging.
    - `--no-hooks / --no-hook`: Completely bypasses executing all lifecycle hooks across rendering, deployment, and post-deploy garbage collection.
    - `--json`: Outputs a `DeployResult` in structured JSON format.

#### Stage 0: Pre-flight Checks
Verifies Git committability on `render/` and `install/`, discovers target packages, asserts that no targeted package is in an uncommitted midway transaction state (`assert_no_midway_deploy_state`), and purges any stale `backup/<pkg>/wal/` folders for targeted packages that are not in a rollback-eligible state (`purge_redundant_wal_folders`).

#### Stage 1: Template Compilation & Sentinel Drift Audit
1.  **Render**: Compiles `src/` templates into `render/` (`Primitive 2`).
2.  **Commit Render**: Commits compiled sandbox history (`Primitive 3`).
3.  **Reverse-Sync & Convergent Drift Auto-Resolution**:
    - Pulls current host configuration state into `install/` (`Primitive 1`).
    - Automatically detects and commits any **converged files** where the reverse-synced file in `install/<pkg>/` is byte-for-byte and permission-identical to the newly rendered file in `render/<pkg>/` (`resolve_and_commit_converged_drifts`).
    - If uncommitted divergent drift remains without `--force`: **Aborts immediately** (exit code `3`) and guides the user to `drift diff -s` or `drift adopt`.
    - **Forced Deployment (`--force`)**: When `--force` is provided and divergent host drift is detected, Drift immediately commits a **Drift Snapshot** into the `install/` Git history (`"Drift Snapshot: Capture host modifications for <pkgs> before forced deployment"`), preserving live host edits for disaster recovery and rollback auditing.

#### Stage 2: Pre-Flight Planning & Physical Apply
1.  **Staging Pre-Flight (`Step 3a`)**: Read-only staging validation and delta resolution (`prepare_stage_packages`).
2.  **Deployment Pre-Flight (`Step 4a`)**: Validates destination permissions, hook existence, and cross-package path collisions against `render/` (`prepare_install` with `install_path_mask`) *before* mutating `install/` or `state.toml`, eliminating midway rollbacks on pre-flight errors.
3.  **Physical Staging (`Step 3b`)**: Mirrors staged changes from `render/` to `install/` (`execute_stage_packages`).
4.  **Physical Deployment (`Step 4b`)**: Clears any stale WAL (`clear_package_wal`), delivers files via atomic symlink/copy while recording reversible operations in `backup/<pkg>/wal/actions.json` (`execute_install`), and triggers automatic whole-batch rollback via `Primitive 8` if a fatal failure occurs under an active auto-rollback policy.
5.  **Commit Install (`Step 5`)**: Scope-commits deployed configurations in `install/` (`Primitive 6`).

#### Stage 3: Post-deploy Garbage Collection
When deploying globally without specific package names, Drift automatically executes `drift gc` (`Primitive 9`).

---

### G. Full-Cycle Deployment Planning: `drift plan [packages...] [options] [--json]`
Simulates the entire multi-stage deployment pipeline end-to-end in an ephemeral sandbox without mutating the host filesystem, intermediate databases, or Git repositories.

*   **Command Signature**: `drift plan [packages...] [-a / --all] [-f / --force] [-r / --reinstall] [-c / --clean] [--with-hooks] [--no-hooks] [--no-deps] [--json]`
*   **Transparent Deploy Alias**: Running `drift deploy -n` or `drift deploy --dry-run` transparently routes to `drift plan`.
*   **Architectural Specification**: For mathematical modeling, path masking algebra, and fault isolation proofs, consult [`docs/features/plan_engine.md`](features/plan_engine.md).

#### Four-Phase Simulation Pipeline
1.  **Phase 0: Non-Destructive Host Drift Audit**:
    Inspects differences between `install/` state and live host files via `prepare_reverse_sync` without mutating `install/` or `state.toml`. If host drift is detected, sets preview status to `DRIFT_DETECTED` and warns the user (unless bypassed by `--force`).
2.  **Phase 1: Ephemeral Sandbox Template Compilation**:
    Executes in a disposable scratch directory (`tmp_sandbox/render/`). Validates Merkle hashes against existing lockfiles for fast cache hits, compiles changed templates, and rebases planned action paths back to canonical `render/` paths while keeping the real `render/.git` repository 100% clean.
3.  **Phase 2: Sandbox Staging Planning**:
    Diffs `sandbox_render/<pkg>` directly against canonical `install/<pkg>`, generating declarative `PackageStagePlan` actions (`CREATE_COPY`, `UPDATE_COPY`, `DELETE_FILE`, `UPDATE_PERMISSION`, `ENSURE_DIR`) without modifying `install/`.
4.  **Phase 3: Physical Delivery Planning**:
    Discovers candidate files, checks destination directory writability, and runs batch pre-flight guards against intra-batch and cross-package destination collisions. Uses **Symlink Path Masking** (`source_dir_mask`) to evaluate existing host symlinks accurately against canonical `install/` destinations, preventing false conflict reports.

#### Command Options & Flags
*   `packages...`: Optional package name(s) to plan. If omitted, plans all active packages across the workspace.
*   `-a / --all / --show-all`: Displays complete action details for all packages, including unchanged ones (which are collapsed into a single summary line by default).
*   `-f / --force`: Simulates deployment with forced drift bypass. Prevents host drift from halting the preview with exit code `3`.
*   `-r / --reinstall`: Simulates full reinstallation of all packages, bypassing staging delta skipping and previewing all file deliveries.
*   `-c / --clean / --no-cache`: Bypasses render cache and lockfile, forcing clean re-rendering simulation.
*   `--with-hooks`: Authorizes execution of pre-flight lifecycle hooks (`probe` and `pre_source`) in isolated scratch sandboxes to resolve dynamic requirements and generated templates for high-accuracy planning.
*   `--no-hooks / --no-hook`: Bypasses all lifecycle hooks (default behavior during planning for zero-mutation safety).
*   `--no-deps`: Bypasses missing package dependency errors during topological ordering.
*   `--json`: Outputs typed `WorkspaceDeployPreview` in structured JSON format.

#### Fault Isolation & Prominent Failure Guidance
*   **Per-Package Fault Isolation**: Compilation, staging, and installation errors are isolated to the specific failing package, allowing healthy sibling packages to be fully planned and inspected.
*   **Downstream Dependency Blocking**: If package `A` fails template compilation, downstream package `B` (which depends on `A`) is cleanly marked blocked.
*   **Failure Guidance Banner**: If any phase detects an unrecoverable conflict, broken permission, or compilation error, a prominent failure banner is displayed:
    ```text
    🚨 Global Pre-Flight Errors (Real deploy pipeline will fail):
      ❌ Cross-package destination conflicts detected (1 collision(s)):
        • '/home/user/.config/app.conf': Intra-batch collision between packages ['pkg_a', 'pkg_b']

    💥 Real deployment pipeline will fail due to detected errors.
    ```

#### Exit Codes
*   `0`: Success (all planned packages are valid and ready to deploy).
*   `1`: General/Domain Error (syntax error, cyclic dependency, template compilation failure, permission error, cross-package collision).
*   `3`: System drift detected on target packages without `--force`.

---

### H. Recovery: `drift rollback [packages...] [--force] [--no-hooks] [--json]`
Reverts failed or uncommitted deployments and restores system files and `install/` state to the last committed clean state.
*   **Command Options**:
    - `packages...`: Optional package name(s) to rollback. If omitted, discovers all rollback-eligible packages in `install/state.toml` (or all tracked packages when `--force` is passed).
    - `--force / -f`: Bypasses the rollback eligibility check (`check_is_package_rollback_eligible`), allowing a hard reset and redeployment to clean Git `HEAD` even for packages that are in `"installed"` state with no uncommitted `install/` changes.
    - `--no-hooks / --no-hook`: Bypasses execution of lifecycle hooks during rollback recovery.
    - `--json`: Outputs a `RollbackResult` object in JSON format.
*   **Rollback Eligibility Without `--force`**:
    A package is eligible for rollback without `--force` when its `install/<pkg>/` directory exists and either:
    1. Its state in `install/state.toml` is in `ROLLBACK_ELIGIBLE_STATES` (`"staging"`, `"staged"`, `"installing"`), **or**
    2. Its state is `"installed"` and it has uncommitted changes in `install/` (`check_is_package_uncommitted_in_install` — e.g. an earlier package in a multi-package batch that completed `Step 4b` before a later package crashed prior to `Step 5` `install-commit`, or a newly installed package not yet present in `HEAD:state.toml`).
*   **Mechanism (Primitive 8 — 3-Step WAL Recovery Pipeline)**:
    Evaluates target packages in unified reverse topological dependency order (`resolve_package_uninstall_order`):
    1.  **Step 1 (WAL Reversal & Stash Restoration)**: Reverses recorded host mutations from `backup/<pkg>/wal/actions.json` in LIFO order (`rollback_package_wal`) and restores any stashed prior backups from `backup/<pkg>/wal/stash/` (`restore_wal_stash`).
    2.  **Step 2 (Git Checkout / Uncommitted First-Time Pruning)**: Checks out `install/<pkg>/` and `install/state.toml` from `HEAD` for committed packages, or purges untracked `install/<pkg>/` and removes the entry from `state.toml` for uncommitted first-time installations.
    3.  **Step 3 (Incremental Convergence & Hook Restoration)**: For committed packages, runs incremental `run_primitive_5_install(reinstall=False, run_hooks_when_unchanged=True, rollback=RollbackPolicy.NEVER)` with `raise_on_error=False` so any residual host drift is converged and lifecycle hooks (`pre_update` / `post_update`) restore service state without triggering recursive auto-rollback.

---

### I. Synchronization & Bidirectional Drift Adoption: `drift adopt [packages...] [options] [--json]`
Incorporate runtime system and GUI changes back into your declarative source templates under `src/`.

*   **Command Options**:
    - `packages...`: Optional package name(s) to adopt. If omitted, all drifted packages across the workspace are evaluated and adopted.
    - `--interactive / -i`: Enables interactive guided reconciliation for every modified, renamed, added, or deleted file.
    - `--accept-conflicts`: Non-interactive resolution that forces conflicting patches to apply directly into source templates with standard Git merge conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`).
    - `--force / -f`: Bypasses the file-level Git cleanliness safeguard on target source files (`src/<package>/...`), allowing adoption even if targeted source files have uncommitted local modifications.
    - `-n / --dry-run`: Previews all pending additions, deletions, renames, and modifications (including conflict dry-run inspection) without writing changes to disk or advancing Git commits.
    - `--no-hooks / --no-hook`: Bypasses execution of `pre_source` package lifecycle hooks.
    - `--no-render`: Bypasses rendering drifted packages in `src/` to `render/` before evaluating and adopting drifts.
    - `--no-reverse-sync / --no-sync`: Bypasses pulling live host configuration changes into `install/` before evaluating and adopting drifts.
    - `--json`: Outputs a structured `AdoptResult` in JSON format containing lists of adopted additions, deletions, modifications, renames, and overall execution status.

*   **Diff Previews**:
    - **Interactive Mode (`-i`)**: Prints unified diff snippets directly to `stdout` before prompting for reconciliation choices.
    - **Non-Interactive Mode**: Logs unified diffs at `DEBUG` level (visible when running with global `-v` / `--verbose` flag).

*   **Interactive Reconciliation Menus**:
    - **Clean Modifications & Renames (Patch Applies Cleanly)**:
      1. `[1] Adopt modifications / Adopt rename`: Directly applies patch to source template and synchronizes file permissions.
      2. `[2] Adopt and Edit in Editor`: Applies patch/rename, synchronizes permissions, and immediately opens the template in `$EDITOR` / `$VISUAL` for fine-tuning.
      3. `[3] Open Side-by-Side Reference`: Launches side-by-side visual diff (`nvim`, `vim`, `code`, `emacs`) between the template source and the compiled live file for manual review.
      4. `[4] Discard modifications / Discard rename / Restore`: Discards the host drift; the original declarative version will be restored on the next deployment.
      5. `[5] Skip file`: Leaves the file unmerged and unstaged in `install/` as uncommitted local drift.

    - **Conflicted Modifications & Renames (Patch Has Conflicts)**:
      1. `[1] Over-render & Freeze`: Backs up original template to `.bak` and overwrites it with static live content.
      2. `[2] Open Merge Conflict Editor`: Writes merge conflict markers into the template using `patch --merge` and opens `$EDITOR`.
      3. `[3] Open Side-by-Side Reference`: Opens the template alongside the live static drift in side-by-side split.
      4. `[4] Discard / Restore`: Discards drift and restores original template on next deployment.
      5. `[5] Skip file`: Leaves file unmerged and unstaged.

    - **Additions (Inside Fully-Controlled Directories)**:
      1. `[1] Adopt and copy into source package`: Copies the untracked host file into `src/<package>/`.
      2. `[2] Ignore file`: Appends pattern to package `.drift_ignore` and unlinks from `install/` tracking.
      3. `[3] Discard file`: Stages deletion in `install/` state database so it is pruned on next deployment.
      4. `[4] Skip file`: Leaves untracked file as uncommitted drift.

    - **Deletions**:
      1. `[1] Adopt deletion`: Symmetrically deletes matching file/template from `src/<package>/`.
      2. `[2] Discard deletion / Restore`: Restores the deleted file on next deployment.
      3. `[3] Skip file`: Leaves deletion unstaged.

---

### J. Uninstallation & Detachment: `drift uninstall <packages...> [--force] [--detach] [-n] [--no-deps] [--no-hooks] [--json]`
*   **Command Options**:
    - `<packages...>`: Package name(s) to uninstall.
    - `--force / -f`: Bypasses the active package safeguard (allowing uninstallation of packages that are still active/enabled in `config/drift_workspace.toml`) and bypasses remaining package broken dependency guards.
    - `--detach`: Decouples package management while keeping physical configuration files intact on the host system.
    - `-n / --dry-run`: Simulates uninstallation without deleting files from disk.
    - `--no-deps`: Bypasses the reverse dependency safeguard that verifies remaining installed packages will not have missing prerequisite dependencies.
    - `--no-hooks / --no-hook`: Skips `pre_uninstall` and `post_uninstall` lifecycle hooks.
    - `--json`: Returns a typed `UninstallResult` in JSON format.
*   **Standard Mode (Default)**: Removes symlinks/files, restores collision backups, deletes `install/<package>/`, and commits to the state database.
*   **Detach Mode (`--detach`)**: Decouples package management while keeping physical configuration files intact on the host system.

---

### K. Garbage Collection: `drift gc [-n] [--no-hooks] [--json]`
Identifies and purges orphaned and untracked database entities across the workspace.
*   **Orphan Packages**: Uninstalls packages present in `install/` state database but disabled in `drift_workspace.toml`.
*   **Ghost Registry Entries**: Removes entries from `install/state.toml` whose corresponding `install/<pkg>/` directory is missing on disk.
*   **Zombie Folders**: Identifies and purges package subdirectories in `render/` and `install/` that lack valid configuration files or belong to unregistered/disabled packages.
*   **Redundant WAL Folders**: Purges stale `backup/<pkg>/wal/` Write-Ahead Log directories for packages that are cleanly committed (`"installed"` with no uncommitted changes in `install/`), unregistered in `state.toml`, or ghost entries, while preserving WAL folders for any rollback-eligible package.

---

### L. Health Audit & Workspace Repair: `drift repair [-n] [--json]`
Audits and self-heals damaged, missing, or partially-initialized workspace components (directories, Git sub-repositories, `.gitignore` entries, configuration templates, and state registries).

---

### M. Workspace Cloning & Bootstrapping: `drift clone <repository> [destination] [--branch] [--depth] [--json]`
Clones a remote or local Git repository and immediately reconstructs and heals the workspace for deployment.
*   **Command Signature**: `drift clone <repository> [destination] [--branch / -b <branch>] [--depth <depth>] [--no-repair] [--json]`
*   **Real-Time Output Streaming**: Progress from `git clone` is streamed in real time to the terminal. When `--json` is active, streaming output is cleanly suppressed to guarantee pure, machine-readable JSON output.
*   **Autonomous Bootstrap Healing**:
    - **Existing Drift Workspace**: Reconstructs untracked runtime databases (`render/` and `install/` Git repos, `state.toml`, `.gitignore`, `config/secrets.env`).
    - **Legacy Dotfiles Repository**: Migrates root dotfiles into `src/<pkg>/`, generates `drift_package.toml` with `symlink` install method, and registers in `drift_workspace.toml`.

---

### N. Package Runtime Health Probing: `drift health [packages...] [--from install|source] [--timeout] [--json]`
Executes runtime health check probes on packages to verify if deployed services, daemons, terminal environments, and host tools are operating properly.
*   **Command Signature**: `drift health [packages...] [--from <install|source>] [--timeout <seconds>] [--json]`
*   **Execution Invariants**: Reads from `install/<pkg>/` (`--from install`, default) or `src/<pkg>/` (`--from source`), executing with probe script directory (`hook_path.parent`) as active CWD in user space and injecting all package environment variables. The target directory is accessible via `$drift_package_target_dir`.

---

### O. Shell Tab-Completion Generation: `drift complete [<shell>] [--install] [--json]`
Generates zero-latency native shell tab-completion scripts compiled directly from the authoritative CLI schema, or installs them directly into user completion directories.
*   **Command Signature**: `drift complete [shell] [--install / -i] [--json]`
*   **Supported Shells**: `bash`, `zsh`, `fish`, `nu` (or `nushell`). (If omitted, auto-detects from `$SHELL`).
*   **Auto-Installation to Standard Directories (`--install`)**:
    ```bash
    drift complete --install          # Installs completion files for all supported shells (bash, zsh, fish, nu)
    drift complete nu --install       # Installs directly to ~/.config/nushell/completions/drift.nu
    drift complete fish --install     # Installs directly to ~/.config/fish/completions/drift.fish
    drift complete bash --install     # Installs directly to ~/.local/share/bash-completion/completions/drift
    drift complete zsh --install      # Installs directly to ~/.local/share/zsh/site-functions/_drift
    ```
*   **Dynamic Startup Evaluation**:
    ```bash
    # Bash (~/.bashrc)
    eval "$(drift complete bash)"
    # Zsh (~/.zshrc)
    eval "$(drift complete zsh)"

    # Fish (~/.config/fish/config.fish)
    drift complete fish | source

    # Nushell (~/.config/nushell/config.nu)
    use ~/.config/nushell/completions/drift.nu *
    ```

---

### P. Mini User Manual: `drift help [topic]`
Provides built-in documentation with automatic terminal pager fallback.
*   **Syntax**: `drift help [topic]`
*   **Available Topics**: `overall`, `package`, `src`, `render`, `install`, `fcd`, `ignore`, `drift_package.toml`, `drift_workspace.toml`, `workspace`, `health`, `clone`, `faq`.

---

### Q. Low-Level Control Commands
For advanced continuous integration, scripting, and pipeline automation:
1.  **`drift render [packages...] [-c] [-n] [--with-hooks|--no-hooks] [--json]`**:
    Incremental Merkle DAG compilation of declarative source templates into the isolated `render/` sandbox (`Primitive 2`).
    *   **Options & Flags**:
        - `packages...`: Optional package name(s) to render. If omitted, renders all active packages enabled in `config/drift_workspace.toml`.
        - `-c / --clean / --no-cache`: Bypasses the render cache and lockfile (`.drift/render/.drift_lock.json`), forcing clean re-compilation of all templates and static assets.
        - `-n / --dry-run`: Simulates Merkle DAG construction and digestion without modifying the filesystem or touching the live `render/` Git repository. Outputs inspectable `FileAction`s (`RENDER_ITEM`, `CREATE_COPY`, `UPDATE_COPY`, `SKIP_IDENTICAL`, `ENSURE_DIR`, `DELETE_FILE`, `PRUNE_DIR`).
        - `--with-hooks / --with-hook`: During `--dry-run`, authorizes execution of pre-flight preparation hooks (`probe` and `pre_source`) to resolve dynamic requirements and generated source templates for an accurate plan. Templated hooks are rendered into isolated temporary directories so `render/` remains 100% clean. (Mutually exclusive with `--no-hooks`).
        - `--no-hooks / --no-hook`: Bypasses execution of all package lifecycle hooks. Note that in `--dry-run` mode, `--no-hooks` is active by default (zero-mutation simulation).
        - `--json`: Outputs structured `RenderResult` in machine-readable JSON format.
    *   **Incremental Merkle DAG Engine**: Drift constructs a typed Abstract Syntax Tree (AST) dependency graph for each package. Files whose input files, template contents, environment variables, and render engine definitions are invariant are skipped instantly (`SKIP_IDENTICAL` / 0ms) via cryptographic SHA-256 Merkle hashes tracked in `.drift/render/.drift_lock.json`. Obsolete rendered files are automatically scoped and pruned without touching live dotfiles.
2.  **`drift render-commit [packages...] -m "message" [--json]`**: Stages and commits compiled sandbox changes (`Primitive 3`).
3.  **`drift reverse-sync [packages...] [--json]`**: Pulls live host configuration changes into `install/` (`Primitive 1`).
4.  **`drift stage [packages...] [--force] [--no-deps] [--json]`**: Computes delta and stages sandbox to `install/` (`Primitive 4`). Pass `--force` to bypass midway failed state checks and uncommitted modifications in `install/` *(does not bypass `enable_install = false`)*. Pass `--no-deps` to bypass missing prerequisite checks.
5.  **`drift apply [packages...] [--force] [-n] [-r|--reinstall] [--rollback <policy>] [--no-deps] [--no-hooks] [--json]`**: Deploys `install/` state to host paths (`Primitive 5`).
    - `-n / --dry-run`: Simulates deployment and previews the inspectable deployment plan without modifying the host filesystem or executing lifecycle hooks.
    - `-r / --reinstall`: Forces full reinstallation and lifecycle hook execution regardless of change status.
      > [!NOTE]
      > **Non-Deployable Files & Standalone `apply` Pitfall**: Primitive 5's change-detection is purely host-driven: its planner evaluates discrepancies between deployable payload files in `install/<pkg>/` and target host filesystem state. It has no independent awareness of non-deployable control-plane modifications (such as edits to `.drift/hooks/*`, `drift_package.toml`, or `.drift_ignore`). If only a lifecycle hook script or package config was updated while deployable files on the host remain identical, `drift apply` without `-r` will skip the package and bypass hook execution. Always supply `-r / --reinstall` when running standalone `drift apply` after modifying lifecycle hooks or package configurations directly in `install/`. (The high-level `drift deploy` pipeline handles this automatically by tracking staging deltas and passing `reinstall=True` to changed packages).
    - `--force / -f`: Bypasses midway failed state checks (`staging` / `installing` in `install/state.toml`). *(Does not bypass `enable_install = false`)*.
    - `--rollback <auto|always|yes|never|no>`: Overrides automatic whole-batch rollback behavior when a fatal installation or hook failure occurs during `apply` (`auto`, `always`/`yes`, `never`/`no`).
    - `--no-deps`: Bypasses missing prerequisite dependency checks during topological ordering.
    - `--no-hooks / --no-hook`: Bypasses executing lifecycle hooks (`pre_install`, `post_install`, etc.).
6.  **`drift install-commit [packages...] -m "message" [--json]`**: Commits deployed configurations inside `install/` (`Primitive 6`).
7.  **`drift hook <package> <hook-name> [--from <install|render|source>] [--json]`**: Directly executes a specific lifecycle hook script for a single package.

---

## 4. Unified Semantics of the `--force` Flag

Drift enforces a clear architectural distinction between **runtime safety safeguards** and **declarative package configurations**:

> [!IMPORTANT]
> The `--force` flag is designed to override **runtime safety checks, state interlocks, and sentinel barriers** (such as dirty Git states, midway failure locks, collision safeguards, or active package uninstallation restrictions).
>
> **The `--force` flag NEVER overrides declarative package configuration settings** (such as `enable_install = false` in `drift_package.toml`). If a package is declared as non-installable, it will remain excluded from staging and deployment regardless of whether `--force` is supplied.

### Summary Matrix of `--force` Behaviors

| Command | Primitive | Overrides with `--force` | Invariants Respected |
| :--- | :--- | :--- | :--- |
| **`drift new <pkg>`** | Primitive 10 | Existing `drift_package.toml` in `src/<pkg>/` | Package naming rules |
| **`drift deploy`** | P4, P5, Pipeline | Sentinel drift, midway locks, dirty `install/`, missing deps | `enable_install = false` |
| **`drift stage`** | Primitive 4 | Midway locks, dirty `install/`, missing deps | `enable_install = false` |
| **`drift apply`** | Primitive 5 | Midway locks, missing required deps | `enable_install = false` |
| **`drift rollback`** | Primitive 8 | Eligibility check (resets clean `"installed"` to `HEAD`) | Tracked in `install/` |
| **`drift adopt`** | Adopt Pipeline | Dirty Git working tree in `src/<pkg>/` | Valid patch application |
| **`drift uninstall`** | Primitive 7 | Enabled-in-workspace & reverse-dependency guards | Installed status check |
| **`drift init`** | Workspace Init | Existing workspace templates and configs | Root path safety checks |

* **Detailed `--force` Bypasses**:
  * **`drift deploy --force`**: Commits a recoverable Drift Snapshot in `install/.git` to bypass Stage 1 Sentinel Drift audit, clears `"staging"`/`"installing"` mid-failure state locks in `state.toml`, ignores uncommitted modifications in `install/`, and bypasses missing required package dependencies.
  * **`drift stage --force` / `drift apply --force`**: Bypasses `"staging"`/`"installing"` state locks in `state.toml`, uncommitted modifications in `install/` (during `stage`), and missing required package dependencies.
  * **`drift uninstall --force`**: Allows uninstalling packages still marked enabled in `drift_workspace.toml` and bypasses the broken reverse-dependency check on remaining installed packages.

---

## 5. Standardized Process Exit Codes

| Exit Code | Enum Constant | Description |
| :---: | :--- | :--- |
| **`0`** | `SUCCESS` | Operation completed cleanly; status is clean or all probes passed |
| **`1`** | `GENERAL_ERROR` | Runtime failure, hook error, permission failure, or midway lock |
| **`2`** | `CONFIG_ERROR` | Malformed TOML/YAML/JSON config, cyclic dependency, or CLI syntax error |
| **`3`** | `DRIFT_DETECTED` | Sentinel guard tripped: uncommitted host drift in `install/` |
| **`4`** | `RENDER_ERROR` | Template compilation failure or output path collision in `render/` |
| **`5`** | `COLLISION_ERROR` | Target inside `drift_root` or cross-package destination conflict |
| **`6`** | `HEALTH_CHECK_FAILED` | Package health check probe failed or returned non-zero exit code |
| **`7`** | `HOOK_SKIPPED` | Direct hook trigger skipped because hook is unconfigured or disabled |

---

## 6. CLI Schema & Generator Design Architecture

To maintain absolute consistency between the CLI runtime, interactive help, argument parsing, error boundaries, and tab completions across multiple shells, Drift is built on a **Single Source of Truth (SSOT)** architecture.

* **Architecture Flow**:
  * **Single Source of Truth**: [`src/drift/cli/schema.py`](../src/drift/cli/schema.py)
    * **CLI Parser Frontends**:
      * [`src/drift/cli/argparse_backend.py`](../src/drift/cli/argparse_backend.py) (`generate_argparse_parser`)
      * [`src/drift/cli/typer_backend.py`](../src/drift/cli/typer_backend.py) (`generate_typer_app` via Metaprogramming)
      * $\rightarrow$ **Dispatcher**: [`src/drift/cli/cli_handlers.py`](../src/drift/cli/cli_handlers.py) (`DriftCLIContext & handle_*`)
      * $\rightarrow$ **Execution**: [`src/drift/cli/actions.py`](../src/drift/cli/actions.py) (`execute_*`)
    * **Shell Completion Generators**:
      * [`src/drift/cli/completion/bash.py`](../src/drift/cli/completion/bash.py) (`BashGenerator`)
      * [`src/drift/cli/completion/zsh.py`](../src/drift/cli/completion/zsh.py) (`ZshGenerator`)
      * [`src/drift/cli/completion/fish.py`](../src/drift/cli/completion/fish.py) (`FishGenerator`)

### 1. Pure Declarative Schema (`src/drift/cli/schema.py`)
- Defines data models: `OptionSpec`, `PositionalSpec`, `CommandSpec`, `CompletionSchema`, and choice registries (`LIFECYCLE_HOOKS`, `HELP_TOPICS`, `SHELLS`, etc.).
- Defines `MOVABLE_GLOBAL_FLAGS = ["--json", "-v", "--verbose", "--raw-errors", "--trace"]` enabling global flags to appear before or after subcommands.
- Contains zero framework dependencies (`argparse` or `typer`), acting as pure metadata.

### 2. Argparse Parser Generator (`src/drift/cli/argparse_backend.py`)
- Dynamically iterates over `CompletionSchema` to construct an `argparse.ArgumentParser` on demand.
- Attaches movable global flags to subparsers with `default=argparse.SUPPRESS` so flags like `-v` and `--json` resolve accurately in any position.

### 3. Typer Metaprogramming Generator (`src/drift/cli/typer_backend.py`)
- Programmatically constructs `typer.Typer` command functions at runtime using `inspect.Signature` and `inspect.Parameter` synthesis.
- Automatically handles context injection (`ctx: typer.Context`), positional arguments (`typer.Argument`), and option flags (`typer.Option`) with descriptions and mutual exclusion.

### 4. Unified Router & Handlers (`src/drift/cli/cli_handlers.py`)
- Defines `DriftCLIContext` encapsulating root directory discovery, `--json` mode state, and rich terminal printing.
- Implements unified `handle_<command>()` functions wrapped with `cli_error_boundary` to guarantee identical error handling, formatting, and exit codes across all backends.

### 5. Shell Tab-Completion Generators (`src/drift/cli/completion/`)
- **Bash Generator (`bash.py`)**: Produces native `_drift_completion()` with `_drift_packages()` dynamic package scanning and positional argument counters.
- **Zsh Generator (`zsh.py`)**: Produces `#compdef drift` using `_arguments` and `_describe` with interactive documentation menus.
- **Fish Generator (`fish.py`)**: Produces declarative `complete -c drift` rules with condition predicates (`__fish_use_subcommand` / `__fish_seen_subcommand_from`).
