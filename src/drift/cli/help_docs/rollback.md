# 🔄 Transactional Rollback & Auto-Rollback Configuration (`drift rollback`)

Drift executes package deployments as **transactional, reversible state transitions** backed by a per-package **Write-Ahead Log (WAL)** (`backup/<pkg>/wal/`) and the **`install/` Git state database** (`install/state.toml` and `install/.git`).

If a deployment fails midway—due to a permission error, file collision, or a fatal `pre_install`, `post_install`, `pre_update`, or `post_update` hook script—Drift can restore both your live host filesystem and the `install/` state database to the last committed clean state, either **automatically** during `drift deploy` / `drift apply` or **manually** via `drift rollback`.

---

## 🚀 Part 1: Manual Recovery (`drift rollback`)

### 1. CLI Usage & Flags

```bash
# Roll back all packages currently in a failed midway or uncommitted state
drift rollback

# Roll back specific package(s)
drift rollback nvim sway

# Bypass lifecycle hooks during rollback (when hook scripts themselves are broken)
drift rollback --no-hooks
drift rollback nvim --no-hooks

# Force a hard reset of already-committed "installed" package(s) back to install/ HEAD
drift rollback --force
drift rollback nvim -f

# Output structured machine-readable JSON result
drift rollback --json
```

### 2. When Is a Package Eligible for Rollback?
Without `--force`, `drift rollback` safely targets only packages in an incomplete or uncommitted deployment state:
* **Midway Transaction States (`"staging"`, `"staged"`, `"installing"`)**:
  * `"staging"`: Interrupted while staging compiled files from `render/` to `install/`.
  * `"staged"`: Staged in `install/`, awaiting host delivery (`drift apply`).
  * `"installing"`: Interrupted mid-flight during physical file delivery or lifecycle hook execution.
  * When a package is left in `"staging"` or `"installing"`, subsequent `drift deploy`, `drift stage`, or `drift apply` runs halt with a **Midway Transaction Interlock** until you run `drift rollback` (or pass `--force`).
* **Uncommitted `"installed"` Batch Packages**:
  * During a multi-package deployment (`pkg_a` $\rightarrow$ `pkg_b`), packages are installed sequentially in topological order, and the `install/` Git repository is committed only after the entire batch succeeds (`install-commit`).
  * If `pkg_a` finishes physical installation (`state = "installed"`) and `pkg_b` subsequently fails before the batch is committed, `pkg_a` has uncommitted modifications in `install/` relative to `HEAD` and is automatically eligible for rollback alongside `pkg_b`.

### 3. Safety Interlock & `--force` Hard Reset
* **Default Drift Protection**: If a package is already cleanly committed in `install/` (`state = "installed"` with a clean `install/` Git working tree), running `drift rollback` without `--force` refuses to proceed. This prevents accidentally wiping out uncommitted live host edits that have not yet been adopted via `drift adopt`.
* **Hard Reset (`--force` / `-f`)**: Passing `drift rollback --force [pkgs]` overrides the eligibility check, reverses any recorded WAL actions, resets `install/<pkg>/` to `HEAD`, and re-applies the last committed configuration to the host.

---

## ⚙️ Part 2: Automatic Rollback & The 3-Tier Policy Ladder

When `drift deploy` or `drift apply` encounters a fatal error during package installation, Drift evaluates a **3-tier precedence ladder** (`CLI --rollback` $\rightarrow$ `drift_workspace.toml [settings] rollback` $\rightarrow$ `drift_package.toml [package] auto_rollback`) to decide whether to automatically roll back the entire deployment batch.

### 1. Precedence Summary

| Tier | Scope | Location / Flag | Allowed Values | Default |
| :---: | :--- | :--- | :--- | :--- |
| **Tier 1** | **CLI Invocation** | `drift deploy --rollback <val>` / `drift apply --rollback <val>` | `"auto"`, `"always"` (`"yes"`), `"never"` (`"no"`) | `None` (defers to Tier 2) |
| **Tier 2** | **Workspace Global** | `config/drift_workspace.toml` $\rightarrow$ `[settings] rollback` | `"auto"`, `"always"` (`"yes"` / `true`), `"never"` (`"no"` / `false`) | `"auto"` (defers to Tier 3) |
| **Tier 3** | **Package Level** | `src/<pkg>/drift_package.toml` $\rightarrow$ `[package] auto_rollback` | `true`, `false` | `false` |

* **How Evaluation Works**:
  1. If Tier 1 (`--rollback`) or Tier 2 (`[settings] rollback`) resolves to **`"always"`** (`"yes"` / `true`), Drift **always** triggers automatic whole-batch rollback on any fatal installation failure, regardless of the package's `auto_rollback` setting.
  2. If Tier 1 or Tier 2 resolves to **`"never"`** (`"no"` / `false`), Drift **never** triggers automatic rollback, leaving the workspace in its midway state so you can inspect logs or run `drift rollback` manually.
  3. If the effective policy is **`"auto"`** (the default), Drift checks Tier 3 (**`auto_rollback`** in the failing package's `drift_package.toml`). If `auto_rollback = true`, whole-batch rollback is triggered automatically.

### 2. Configuring Package-Level `auto_rollback` (Tier 3)
In `src/<pkg>/drift_package.toml`, set `auto_rollback = true` under `[package]` for critical packages (such as window managers, shell profiles, SSH configs, or system daemons) that must never be left in a half-deployed state:

```toml
# src/my_service/drift_package.toml
[package]
install_method = "copy"
target_directory = "~/.config/my_service"

# Automatically roll back the entire deployment batch if installing or updating
# this package fails (when effective rollback policy is "auto").
auto_rollback = true

[hooks]
pre_update = "drift_hooks/stop_service.sh"
post_update = "drift_hooks/start_service.sh"

# Control which installation hooks count as fatal failures (default: true).
# Can be a boolean (true / false) or a list of hook names:
# ["pre_install", "post_install", "pre_update", "post_update"]
failure_is_fatal = true
```

> [!NOTE]
> **Interaction with `[hooks] failure_is_fatal`**: Automatic rollback is only triggered by **fatal** installation failures (physical file delivery errors or hooks matched by `failure_is_fatal`). If `failure_is_fatal = false` (or a failing hook is omitted from the `failure_is_fatal` list), the hook failure is logged as a non-fatal warning and does not abort or roll back the deployment.

### 3. Configuring Workspace-Wide `rollback` (Tier 2)
In `config/drift_workspace.toml` (or `config/drift_workspace.local.toml`), configure `[settings] rollback` to set the default policy across all packages in the workspace:

```toml
# config/drift_workspace.toml
[settings]
# Options:
#   "auto"             — Respect each package's [package] auto_rollback (default)
#   "always" / "yes"   — Always roll back the batch on any fatal deploy/apply failure
#   "never"  / "no"    — Never auto-rollback; leave midway state for manual inspection
rollback = "auto"
```

### 4. Overriding Per-Command via CLI `--rollback` (Tier 1)
Both `drift deploy` and `drift apply` accept `--rollback <policy>` to override workspace and package settings for a single run:

```bash
# Force automatic rollback on any failure during this deployment
drift deploy --rollback always

# Suppress automatic rollback to inspect midway files if a hook fails
drift deploy --rollback never

# Use default policy resolution (workspace [settings] rollback -> package auto_rollback)
drift apply --rollback auto
```

---

## 🛠️ Part 3: Under the Hood — 3-Step WAL & Git Recovery

When rollback runs (either automatically or via `drift rollback`), Drift sequences all targeted packages in **reverse topological dependency order** (dependents are rolled back before their prerequisites) and executes a 3-step recovery pipeline per package:

### 1. Write-Ahead Log (`backup/<pkg>/wal/`) & Pre-Overwrite Stashing
During `drift apply` / `drift deploy`, before any host file is mutated:
* Every physical action (`CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `CREATE_DIR`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`) is recorded to `backup/<pkg>/wal/actions.json`.
* If backing up a displaced host file into `backup/<pkg>/overwritten/` or `backup/<pkg>/deleted/` would overwrite an older backup from a previous deployment, the older backup is first stashed into `backup/<pkg>/wal/stash/`.

### 2. Committed Package Recovery (Failed Update)
If the package was previously installed and exists in the `install/` repository's `HEAD` commit:
1. **Step 1 — LIFO WAL Reversal & Stash Restoration**:
   * Reverses all actions in `backup/<pkg>/wal/actions.json` in strict **LIFO (reverse)** order—removing newly created symlinks, copies, and empty directories, and restoring displaced host files from `overwritten/` and `deleted/`.
   * Restores any stashed prior backups from `backup/<pkg>/wal/stash/` back to `overwritten/` and `deleted/`.
2. **Step 2 — Git State Checkout**:
   * Resets `install/<pkg>/` to `HEAD` (`git checkout HEAD -- <pkg>` and `git clean -fd -- <pkg>`) and restores the package's entry in `install/state.toml` from `HEAD:state.toml` with `state = "installed"`.
3. **Step 3 — Incremental Convergence & Hook Restoration**:
   * Runs an incremental installation pass from the restored `HEAD` state with `run_hooks_when_unchanged = true` and `rollback = "never"`.
   * Because Step 1 already reversed the WAL mutations, file delivery is a fast convergence check (`SKIP_IDENTICAL` or residual repair), while `pre_update` and `post_update` hooks run to restore service state.
   * Clears `backup/<pkg>/wal/` and commits the restored state in `install/`.

### 3. Uncommitted First-Time Package Recovery (Failed Initial Install)
If the package was being installed for the very first time (absent from `HEAD:state.toml`):
1. **Step 1 — LIFO WAL Reversal & Stash Restoration**:
   * Undoes all newly created symlinks, copied files, and directories on the host, and restores any pre-existing host files that were moved to `backup/<pkg>/overwritten/`.
2. **Step 2 — Clean State Removal**:
   * Deletes the untracked `install/<pkg>/` directory and removes the package entry from `install/state.toml`.
3. **Step 3 — Skipped**:
   * Zero hooks are executed, leaving the host system as if the first-time installation never happened.

### 4. WAL Hygiene & Garbage Collection
* Upon a successful deployment commit (`drift deploy` / `drift install-commit`), Drift automatically purges completed `backup/<pkg>/wal/` directories for cleanly committed packages.
* Running `drift gc` also sweeps any stale `backup/<pkg>/wal/` folders while strictly preserving active WAL directories for packages that remain eligible for rollback.

---

## 📚 Related Documentation
* `drift help install` — State database (`install/state.toml`), delta staging, and `drift apply`.
* `drift help drift_package.toml` — Package-level `[package] auto_rollback` and `[hooks] failure_is_fatal`.
* `drift help drift_workspace.toml` — Workspace-level `[settings] rollback`.
* `drift help faq` — Troubleshooting broken hooks (`--no-hooks`) and midway failures.
