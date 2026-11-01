# 🚀 Composite Deployment Pipeline (`drift deploy`)

`drift deploy` is Drift's primary, everyday command for compiling templates, auditing host drift, staging deltas, and delivering configuration packages to your live system in a single transactional workflow.

While low-level primitives (`drift render`, `drift stage`, `drift apply`) execute individual zones in isolation, **`drift deploy` orchestrates the complete 5-step pipeline** from **Zone 1 (`src/`)** through **Zone 2 (`render/`)** and **Zone 3 (`install/`)** to **Zone 4 (Active Host)**—guarded by pre-flight safety checks, the Sentinel drift guard, and automatic Write-Ahead Log (WAL) rollback.

---

## 🛠️ CLI Usage & Flags

```bash
# Deploy all enabled packages in topological dependency order
drift deploy

# Deploy specific enabled package(s)
drift deploy nvim git tmux

# Preview the full deployment in an isolated sandbox without modifying any files (alias for 'drift plan')
drift deploy --dry-run
drift deploy -n nvim

# Deploy a disabled package on the fly without enabling it in drift_workspace.toml
drift deploy --adhoc my_experimental_pkg

# Deploy and automatically uninstall any previously installed packages that are now disabled
drift deploy --gc

# Force re-execution of file delivery and lifecycle hooks even when staged files are unchanged
drift deploy --reinstall
drift deploy -r nvim

# Force clean template re-compilation (bypassing the SHA-256 render_lock.json cache)
drift deploy --clean
drift deploy --no-cache

# Bypass lifecycle hook scripts (probe, pre_source, post_render, pre/post_install, pre/post_update)
drift deploy --no-hooks

# Bypass missing required inter-package dependency errors
drift deploy --no-deps

# Override the automatic rollback policy for this run ('auto', 'always'/'yes', 'never'/'no')
drift deploy --rollback always

# Force deployment, bypassing host drift sentinel, disabled-package checks, missing deps, and midway locks
drift deploy --force
drift deploy -f nvim

# Output structured machine-readable JSON result
drift deploy --json
```

### Summary of `drift deploy` Options

| Flag | Description |
| :--- | :--- |
| `-n`, `--dry-run` | Simulates the full `render → stage → apply` pipeline in an ephemeral sandbox without mutating `render/`, `install/`, or host files (equivalent to `drift plan`). |
| `-f`, `--force` | Bypasses all pre-flight safeguards (`--adhoc`, `--no-deps`, host drift sentinel, and midway transaction locks), snapshots any host drift into `install/` Git history, and overwrites drifted host files. |
| `--adhoc`, `--ad-hoc` | Allows deploying explicitly named package(s) that are currently disabled in `[packages.enable]` without modifying `drift_workspace.toml`. |
| `--gc` | Runs workspace garbage collection (`drift gc`) after deployment to automatically uninstall packages that were disabled or removed from `src/`. |
| `-r`, `--reinstall` | Forces physical re-delivery and lifecycle hook execution (`pre/post_update`) even if staging detected zero file changes between `render/` and `install/`. |
| `-c`, `--clean`, `--no-cache` | Bypasses incremental Merkle-DAG render caching (`render_lock.json`) and recompiles all templates from scratch. |
| `--no-hooks`, `--no-hook` | Skips all package lifecycle hooks across rendering and installation. |
| `--no-deps` | Proceeds even if a required prerequisite in `[package] dependencies` is disabled or missing. |
| `--rollback <policy>` | Overrides the automatic rollback policy on fatal failure (`auto`, `always`/`yes`, `never`/`no`). |

---

## ⚙️ The 5-Step Composite Execution Pipeline

When you run `drift deploy [pkgs]`, Drift executes the following sequence:

### Stage 0: Pre-Flight Validation & Safeguards
1. **Workspace & Git Health Check**: Verifies that `render/.git` and `install/.git` exist and have a valid Git identity (`user.name` / `user.email` or `[settings] git_user_*`) so commits cannot fail mid-pipeline.
2. **Enabled-Package Guard**: If specific packages were passed on the command line and any of them is disabled in `[packages.enable]`, Drift halts with instructions to run `drift enable <pkg>` or pass `--adhoc` (or `--force`).
3. **Midway Transaction Interlock**: Checks `install/state.toml` to ensure no targeted package is stuck in `"staging"` or `"installing"` from a previously interrupted run (instructing you to run `drift rollback` if so).
4. **WAL Hygiene**: Cleans up any leftover `backup/<pkg>/wal/` folders for packages that are already cleanly committed.

### Step 1: Sandbox Template Compilation (`Primitive 2: render`)
* Evaluates `[package.requirements]` (OS, CPU arch, Linux distro, required binaries in `$PATH`, environment variables, LAN IP/subnet, and `probe` hooks). Unmet packages are gracefully skipped before compilation.
* Runs `pre_source` hooks and `drift_package.py` dynamic preprocessors.
* Stitches the 6-tier environment variable DAG and compiles templates in topological engine order into the isolated `render/<pkg>/` sandbox (skipping unchanged files via `render_lock.json` unless `--clean` is passed).
* Runs `post_render` hooks.

### Step 2: Sandbox History Commit (`Primitive 3: render-commit`)
* Commits all newly compiled template outputs into the isolated `render/.git` repository.

### Sentinel Audit: Live System Drift Protection
Immediately after `render/` is up to date—and **before** `install/` or your host filesystem is modified—Drift runs a silent reverse-sync audit (`Primitive 1: reverse-sync`):
* **Convergent Drift Auto-Resolution**: If you made an edit on the host that already matches the freshly rendered output in `render/<pkg>/`, Drift automatically resolves and commits the convergence without interrupting deployment.
* **Divergent Drift Protection**: If any targeted package has uncommitted host edits that differ from `render/<pkg>/`:
  * **Default (Safe Stop)**: `drift deploy` aborts immediately (`ExitCode 10`) before overwriting your work, prompting you to inspect (`drift diff -s <pkg>`), backport (`drift adopt <pkg>`), or force-overwrite (`drift deploy <pkg> --force`).
  * **With `--force` (Snapshot & Overwrite)**: Drift first commits a `"Drift Snapshot"` of your live host edits into the `install/.git` history (and backs up overwritten files during delivery) so your discarded host edits remain recoverable in Git history, then proceeds with deployment.

### Step 3: Delta Staging (`Primitive 4: stage`)
* Sequences targeted packages in **topological dependency order** (Kahn's algorithm) so prerequisites always stage and deploy before dependents.
* Computes file deltas between `render/<pkg>/` and `install/<pkg>/`, mirrors updated files 1:1 into `install/<pkg>/`, and marks modified packages as `"staged"` in `install/state.toml`.

### Step 4: Physical Host Delivery (`Primitive 5: apply`)
* Identifies packages requiring host delivery (packages with staged file/hook/config changes, packages not yet in `"installed"` state, or all targeted packages when `--reinstall` is passed).
* Runs pre-flight collision, permission, cross-package ownership, and `sudo` escalation checks **before** touching the host.
* For each package in topological order:
  1. Executes `pre_install` (first-time) or `pre_update` (update) hook.
  2. Delivers relative symlinks (`install_method = "symlink"`) or physical copies (`install_method = "copy"`), recording every mutation to `backup/<pkg>/wal/actions.json`, backing up untracked colliding host files to `backup/<pkg>/overwritten/`, and sweeping removed orphans to `backup/<pkg>/deleted/`.
  3. Executes `post_install` or `post_update` hook.
  4. Updates `install/state.toml` (`state = "installed"`, `deployed_files`, `target_directory`, `install_method`, `sudo`).
* **Automatic Rollback on Failure**: If file delivery or a fatal installation hook fails, Drift evaluates the 3-tier rollback policy (`--rollback` $\rightarrow$ `[settings] rollback` $\rightarrow$ `[package] auto_rollback`) and automatically rolls back the batch if enabled (see `drift help rollback`).

### Step 5: State Database Commit (`Primitive 6: install-commit`)
* Commits the updated `install/<pkg>/` files and `install/state.toml` into `install/.git` and clears completed `backup/<pkg>/wal/` logs.

### Optional Post-Step: Garbage Collection (`--gc`)
* If `--gc` is passed, Drift runs `Primitive 9: gc` to automatically uninstall any previously installed packages that are now disabled in `drift_workspace.toml` or deleted from `src/`.
* If `--gc` is omitted and disabled packages are still installed on the host, Drift prints a non-destructive reminder hint at the end of deployment.

---

## 🧭 `drift deploy` vs. `drift apply` vs. `drift plan`

| Command | What It Does | When to Use It |
| :--- | :--- | :--- |
| **`drift deploy [pkgs]`** | Runs the full **Render $\rightarrow$ Sentinel Audit $\rightarrow$ Stage $\rightarrow$ Apply $\rightarrow$ Commit** pipeline. | **99% of daily workflows**: after editing templates, variables, configs, or enabling packages. |
| **`drift plan [pkgs]`** (or `drift deploy --dry-run`) | Simulates the full `deploy` pipeline in a temporary sandbox and prints the exact file/hook execution plan. | Before deploying complex changes, new packages, or template refactors. |
| **`drift apply [pkgs]`** | Low-level primitive: deploys files **already staged in `install/`** to the host without re-rendering `src/` or staging `render/`. | Advanced recovery or step-by-step manual pipeline debugging (`render` $\rightarrow$ `stage` $\rightarrow$ `apply`). |

> [!TIP]
> **Why `drift install` Is Not a Command**: In Drift, `install/` is the name of the Zone 3 state database directory. Use **`drift deploy`** for end-to-end installation and updates, or **`drift apply`** for low-level delivery from `install/`.

---

## 📚 Related Documentation
* `drift help overall` — High-level 4-zone architecture and data-flow loop.
* `drift help render` — Sandbox template compilation (`render/`) and DAG engine piping.
* `drift help install` — State database (`install/`) and low-level `drift apply` mechanics.
* `drift help rollback` — Transactional WAL recovery (`drift rollback`) and `auto_rollback` policies.
* `drift help faq` — Everyday troubleshooting recipes (`--force`, `--reinstall`, `--no-hooks`).
