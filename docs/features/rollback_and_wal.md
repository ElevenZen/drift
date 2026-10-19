# 🔄 State Registry, Write-Ahead Log (WAL), Rollback & Garbage Collection

This document specifies Drift's transactional state registry (`install/state.toml`), Write-Ahead Log (`backup/<pkg>/wal/`), pre-overwrite backup stashing, 3-state `RollbackPolicy` resolution ladder, uncommitted `"installed"` rollback eligibility, 3-step WAL recovery pipeline (`drift rollback`), and workspace garbage collection (`drift gc`).

* **Core Architectural Overview**: [`docs/architecture.md`](../architecture.md)
* **Folder Delivery Planning Engine**: [`docs/features/delivery_engine.md`](delivery_engine.md)
* **Related Subpackage Maps**: [`src/drift/core/AGENT_MODULE.md`](../../src/drift/core/AGENT_MODULE.md), [`src/drift/primitives/AGENT_MODULE.md`](../../src/drift/primitives/AGENT_MODULE.md)

---

## 1. State Registry Database (`install/state.toml`)

Implemented in [`src/drift/core/state_registry.py`](../../src/drift/core/state_registry.py). To track deployed files, classify first-time installs vs. updates, enforce crash interlocks, and detect target directory migrations, Drift maintains `install/state.toml` inside the local `install/` Git repository:

```toml
[packages.nvim]
state = "installed"
target_directory = "~/.config/nvim"
last_deployed = "2026-08-16T21:10:50.123456"
install_method = "symlink"
deployed_files = ["dot-config/nvim/init.lua", "dot-config/nvim/autoload"]
sudo = false
```

### 1.1 Tracked Manifest Fields (`PackageStateEntry`)
* **`state`**: Lifecycle transaction state (`"installed"`, `"staged"`, `"staging"`, `"installing"`).
* **`target_directory`**: Host destination path where files were deployed. Used to detect target migrations (`get_target_migrated_from`), build cross-package ownership maps (`build_destination_ownership_map`), and uninstall packages without needing `src/` metadata.
* **`last_deployed`**: ISO-8601 timestamp of last completed deployment (`None` indicates first-time install $\to$ triggers `pre_install`/`post_install`; non-null triggers `pre_update`/`post_update`).
* **`install_method`**: `"symlink"` or `"copy"`.
* **`deployed_files`**: Sorted list of relative paths deployed on the host (including empty directories as leaf nodes). Used during orphan reconciliation (`_inspect_orphans`) to prune removed files into `backup/<pkg>/deleted/`.
* **`sudo`**: Persisted boolean recording whether root escalation was used, ensuring `drift uninstall`, `drift rollback`, and `drift gc` retain identical privileges even if package configs are altered or missing.

### 1.2 Transaction States & Crash Interlocks
* **Stable States**:
  * `"installed"`: Package is fully applied to the host system.
  * `"staged"`: Package is synchronized from `render/` into `install/` (Primitive 4), awaiting host deployment.
* **Transient Midway States (`MIDWAY_TRANSACTION_STATES = ("staging", "installing")`)**:
  * `"staging"`: Package is currently undergoing `render/` $\to$ `install/` staging.
  * `"installing"`: Package is currently undergoing `install/` $\to$ host deployment.
  * **Safety Interlock**: If any targeted package is in `"staging"` or `"installing"`, subsequent `deploy`, `stage`, or `apply` runs abort immediately with [`MidwayTransactionError`](../../src/drift/core/exceptions.py) unless `--force` is passed, instructing the user to run `drift rollback`.

---

## 2. Write-Ahead Log (`backup/<pkg>/wal/`) & Backup Stashing

Implemented in [`src/drift/core/file_action.py`](../../src/drift/core/file_action.py).

### 2.1 Directory Topology under `backup/<pkg>/`
* `backup/<pkg>/overwritten/`: Displaced pre-existing host files, foreign symlinks, or blocking ancestor nodes (with `dot-` prefix translation via `decode_dot_prefix`).
* `backup/<pkg>/deleted/`: Historically deployed files pruned when removed from a package (or ancestor collisions displaced during backup restoration).
* `backup/<pkg>/wal/`: Transient per-package Write-Ahead Log directory:
  * `backup/<pkg>/wal/actions.json`: Ordered JSON array of [`FileAction`](../../src/drift/core/file_action.py) records applied during the current installation pass.
  * `backup/<pkg>/wal/stash/`: Stashed prior backups from `overwritten/` or `deleted/` that were overwritten during the current transaction.

### 2.2 WAL Recording & Pre-Overwrite Stashing Lifecycle
1. **Pre-Install WAL Reset (`clear_package_wal`)**:
   * At the start of `execute_package_install_impl` ([`src/drift/primitives/install_repo.py`](../../src/drift/primitives/install_repo.py)), any leftover `backup/<pkg>/wal/` directory is cleared before setting state to `"installing"`.
2. **Action Recording (`execute_delivery_actions` / `_append_action_to_wal`)**:
   * As each reversible [`FileAction`](../../src/drift/core/file_action.py) (`CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `CREATE_DIR`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`) executes on the host, it is serialized and appended to `backup/<pkg>/wal/actions.json`.
3. **Backup Stashing (`backup_item`)**:
   * When `backup_item` moves a host path to `backup_dest` (under `backup/<pkg>/overwritten/` or `backup/<pkg>/deleted/`) and `backup_dest` already exists from a previous deployment, `backup_item` moves the existing backup into `backup/<pkg>/wal/stash/<rel_to_pkg_backup_root>` via `rebase_path` before writing the new backup.
4. **LIFO Reversal & Stash Restoration (`rollback_package_wal` & `restore_wal_stash`)**:
   * `rollback_package_wal(pkg_backup_root, sudo)` reads `wal/actions.json` and reverses actions in strict **LIFO (reverse) order**:
     * `CREATE_SYMLINK`, `CREATE_COPY`, `ENSURE_DIR`, `CREATE_DIR` $\to$ removes the created symlink, file, or empty directory (`remove_file_or_empty_dir`).
     * `BACKUP_OVERWRITE`, `BACKUP_PRUNE` $\to$ restores the displaced backup from `src_path` (`overwritten/` or `deleted/`) back to `dst_path` on the host.
   * `restore_wal_stash(pkg_backup_root, sudo)` moves all files in `backup/<pkg>/wal/stash/` back to their original locations under `backup/<pkg>/overwritten/` and `backup/<pkg>/deleted/`.

---

## 3. Rollback Eligibility & Uncommitted `"installed"` Packages

Implemented in [`src/drift/core/state_registry.py`](../../src/drift/core/state_registry.py).

### 3.1 Why `"installed"` Packages Can Require Rollback
In a multi-package deployment batch (`pkg_a` $\to$ `pkg_b`), `execute_install` deploys packages sequentially in topological order:
1. `pkg_a` finishes physical installation and transitions its state in `install/state.toml` to `"installed"`.
2. `pkg_b` fails midway during file delivery or a fatal `post_install`/`post_update` hook.
3. Because **Primitive 6 (`install-commit`)** only runs after the entire batch succeeds, `pkg_a` is marked `"installed"` in the working tree `install/state.toml`, but its changes in `install/<pkg_a>/` and `install/state.toml` are **uncommitted** relative to `HEAD`.

### 3.2 Rollback Eligibility Predicates
1. **[`check_is_package_uncommitted_in_install(pkg, state_registry, install_base, head_registry=None) -> bool`](../../src/drift/core/state_registry.py)**:
   * Returns `True` iff:
     * `pkg` has `state == "installed"` in `state_registry`,
     * `(install_base / pkg).is_dir()` exists on disk, and
     * Either `has_uncommitted_modifications(install_base, pkg)` is `True` **or** `pkg` is absent from committed `HEAD:state.toml` (`load_head_state_registry(install_base)`).
2. **[`check_is_package_rollback_eligible(pkg, state_registry, install_base=None, head_registry=None) -> bool`](../../src/drift/core/state_registry.py)**:
   * Returns `False` if `pkg` is unregistered in `state_registry` or if `install_base` is provided and `not (install_base / pkg).is_dir()` (ghost package).
   * Returns `True` if `pkg`'s state is in `ROLLBACK_ELIGIBLE_STATES = ("staging", "staged", "installing")`.
   * Returns `True` if `install_base` is provided and `check_is_package_uncommitted_in_install(...)` is `True`.
3. **[`StateRegistry.get_rollback_eligible_packages(target_packages=None, install_base=None) -> List[str]`](../../src/drift/core/state_registry.py)**:
   * Loads `head_registry` once and filters candidate packages through `check_is_package_rollback_eligible`.

---

## 4. Automatic Whole-Batch Rollback & 3-State `RollbackPolicy`

Implemented in [`src/drift/primitives/install_repo.py`](../../src/drift/primitives/install_repo.py) and [`src/drift/primitives/deploy_repo.py`](../../src/drift/primitives/deploy_repo.py).

### 4.1 The 3-Tier `RollbackPolicy` Precedence Ladder
Controlled by [`RollbackPolicy`](../../src/drift/core/constants.py) (`AUTO = "auto"`, `ALWAYS = "always"` / `"yes"`, `NEVER = "never"` / `"no"`). When a fatal exception occurs during `execute_install`, `_should_trigger_auto_rollback(context, options, workspace_config)` evaluates:
1. **Tier 1 — CLI Override (`--rollback` / `InstallOptions.rollback`)**:
   * `ALWAYS` (`always` / `yes`): Forces automatic rollback.
   * `NEVER` (`never` / `no`): Suppresses automatic rollback.
   * `AUTO` (`auto`): Defers to Tier 2.
2. **Tier 2 — Workspace Setting (`[settings] rollback` in `drift_workspace.toml`)**:
   * `ALWAYS`: Forces automatic rollback across the workspace.
   * `NEVER`: Suppresses automatic rollback across the workspace.
   * `AUTO` (default): Defers to Tier 3.
3. **Tier 3 — Package Config (`[package] auto_rollback` in `drift_package.toml`)**:
   * Boolean (`true` / `false`, default `false`) on the failing package's `PackageInstallContext`.

### 4.2 Whole-Batch Rollback Execution (`_trigger_batch_auto_rollback`)
* When `_should_trigger_auto_rollback` returns `True`, `_trigger_batch_auto_rollback` collects all rollback-eligible packages across `plan.packages_install_order` (unioned with `attempted_pkgs`) and invokes:
  ```python
  run_primitive_8_rollback_recovery(
      workspace_config,
      target_pkgs=rollback_targets,
      force=False,
      flags=hook_flags,
  )
  ```
* Sets `exc.auto_rolled_back = True` on the fatal exception so `execute_deploy_pipeline` and CLI error handlers report that automatic rollback has already been performed (`requires_rollback = False`).

---

## 5. Primitive 8: 3-Step WAL Rollback Recovery Pipeline (`drift rollback`)

Implemented in [`src/drift/primitives/rollback_repo.py`](../../src/drift/primitives/rollback_repo.py) (`run_primitive_8_rollback_recovery`).

1. **Candidate Selection & Reverse Topological Sorting**:
   * Without `--force`: Selects packages via `state_registry.get_rollback_eligible_packages(target_pkgs, install_base=install_path)`. Rejects non-eligible packages with a diagnostic guiding the user to pass `--force` if they want a hard redeploy of already-committed `"installed"` packages.
   * With `--force`: Targets all specified (or registered) packages present in `install/`.
   * Sorts all candidate packages in a single unified **reverse topological order** ([`resolve_package_uninstall_order`](../../src/drift/primitives/package_assertions.py)) so dependents roll back before prerequisites.
2. **Step 1 — Physical WAL Reversal & Stash Restoration (`_rollback_step1_reverse_wal`)**:
   * Calls `rollback_package_wal(backup_pkg_dir, sudo)` to undo recorded host mutations in LIFO order.
   * Calls `restore_wal_stash(backup_pkg_dir, sudo)` to restore overwritten prior backups from `wal/stash/`.
   * Calls `clear_package_wal(backup_pkg_dir, sudo)` to remove `backup/<pkg>/wal/`.
3. **Step 2 — Git State Checkout & First-Time Pruning (`_rollback_step2_git_checkout`)**:
   * **Committed Packages** (present in `HEAD:state.toml`): Runs `git checkout HEAD -- <pkg>` and `git clean -fd -- <pkg>` in `install/`, and restores the package's `state.toml` entry from `HEAD:state.toml`.
   * **Uncommitted First-Time Packages** (absent from `HEAD:state.toml`): Removes the untracked `install/<pkg>/` directory (`remove_tree`) and deletes the package entry from `install/state.toml`.
4. **Step 3 — Incremental Convergence & Hook Restoration (`_rollback_step3_incremental_install`)**:
   * For committed packages, invokes `run_primitive_5_install` with `InstallOptions(reinstall=False, run_hooks_when_unchanged=True, rollback=RollbackPolicy.NEVER)` and `raise_on_error=False` on `hook_flags`.
   * Because Step 1 already reversed the WAL mutations, file delivery is an idempotent convergence check (`SKIP_IDENTICAL` or residual repair), while `run_hooks_when_unchanged=True` guarantees `pre_update` and `post_update` hooks run to restore service state, and `rollback=RollbackPolicy.NEVER` prevents recursive rollback loops.

---

## 6. Primitive 9: Workspace Garbage Collection (`drift gc`) & WAL Hygiene

Implemented in [`src/drift/primitives/workspace_gc.py`](../../src/drift/primitives/workspace_gc.py) (`run_primitive_9_purge_workspace_garbage`).

1. **Stage 1 — Orphan Package Uninstallation (`uninstall_orphan_packages`)**:
   * Identifies packages marked `"installed"` in `install/state.toml` that are disabled in `drift_workspace.toml` (`not is_package_enabled(pkg)`), missing from `src/`, or have `enable_install = false`.
   * Executes **Primitive 7 (`run_primitive_7_uninstall_packages`)** with `UninstallOptions(force=True, dry_run=dry_run, flags=flags)`.
2. **Stage 1b — Ghost Package Registry Purge (`purge_ghost_registry_entries`)**:
   * Removes entries from `install/state.toml` whose `install/<pkg>/` directory is missing on disk, and commits `state.toml`.
3. **Stage 2 — Database & WAL Folder Purge**:
   * **`purge_render_folders`**: Removes `render/<pkg>/` directories that lack a valid config file (zombies), are disabled in workspace config, or have been deleted from `src/`.
   * **`purge_install_folders`**: Removes **unregistered** `install/<pkg>/` directories that lack a valid config file or are disabled/deleted from `src/`.
   * **`purge_redundant_wal_folders`**: Scans `backup/<pkg>/wal/` (workspace-wide in `drift gc`, or scoped to `target_pkgs` in Stage 0 of `drift deploy`):
     * **Preserves `wal/`** iff `check_is_package_rollback_eligible(pkg, registry, install_base=install_path, head_registry=head_registry)` is `True` (`"staging"`, `"staged"`, `"installing"`, or `"installed"` with uncommitted changes in `install/`).
     * **Purges `wal/`** via `clear_package_wal` for cleanly committed `"installed"` packages, unregistered packages absent from `state.toml`, and ghost packages missing `install/<pkg>/`.
4. **Stage 3 — Scoped Git Database Commits**:
   * Commits purged package directory deletions in `render/.git` and `install/.git`.
