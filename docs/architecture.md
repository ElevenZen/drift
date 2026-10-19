# 🏛️ Drift Architecture Model & Big-Picture Reference

This document is the primary architectural entry point for both human developers and AI coding agents. It defines Drift's 4-tier Git-backed mental model, core workflows, the 17 atomic primitives, codebase package layering, cross-cutting domain invariants, and the routing index to subsystem specifications (`docs/features/`) and co-located module maps (`src/drift/*/AGENT_MODULE.md`).

---

## 1. Executive Overview & The 4-Tier Architecture

Dotfiles and non-containerized host configuration managers balance three competing goals:
1. **Precision (Declarative Accuracy)**: Knowing exactly what configuration is compiled and where it is deployed.
2. **Reproducibility**: Reconstructing an entire system environment on a fresh machine from declarative source templates.
3. **Adaptability (Bidirectional Synchronization)**: Safely auditing and adopting runtime changes made by GUI applications, IDEs, or live manual edits on the host without silent overwrites (**Lost Updates**).

### 1.1 Competitive Differentiation

| Feature | **Drift** (Python) | **Chezmoi** (Go) | **Dotbot** (Python) | **GNU Stow** (Perl) | **VCSH** (Shell) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Engine** | **Dual Local Git Repos (`render/`, `install/`)** | Single Repo + BoltDB | None (YAML links) | Symlink Farm | Bare Git on `$HOME` |
| **Pipeline Stages** | **2-Stage (`src/` $\rightarrow$ `render/` $\rightarrow$ `install/` $\rightarrow$ Host)** | 1-Stage (Compile $\rightarrow$ Host) | 1-Stage (Link) | 1-Stage (Link) | 1-Stage (Direct Git) |
| **Active Drift Audit** | **Automatic (`drift reverse-sync` + Sentinel)** | Manual `re-add/merge` | No | No | Git status |
| **Dry-Run Fidelity** | **Full-Cycle Sandbox (`drift plan`)** | Template dry-run | No | Stow `-n` | No |
| **Transactional Rollback** | **WAL + Git Checkout (`drift rollback`)** | Manual cleanup | No | Stow `-D` | No |
| **Machine Output** | **`--json` across all commands** | Partial JSON | No | No | No |

### 1.2 The 4-Tier Directory Topology

Drift separates configuration management into four physical tiers backed by `backup/` and `config/`:

* **Tier 1 — Declarative Source (`src/<pkg>/`, `config/`)**:
  * Tracked in the user's primary Git repository.
  * Contains source templates (`dot-bashrc.envst.sh`), static dotfiles, package metadata (`drift_package.toml`), ignore rules (`.drift_ignore`), and lifecycle hook scripts (`src/<pkg>/drift_hooks/`).
  * Global workspace settings, render engines, and enabled packages live in `config/drift_workspace.toml` (with git-ignored `drift_workspace.local.toml` and `config/secrets.env`).
* **Tier 2 — Sandbox Compilation Zone (`render/<pkg>/`)**:
  * An isolated, untracked local Git repository (`render/.git`).
  * Templates from `src/` are compiled here via the 3-phase Merkle Render DAG (**Primitive 2**) and committed (**Primitive 3**). Rebuilding or wiping `render/` has zero side effects on the host system.
* **Tier 3 — Local State Database (`install/<pkg>/`, `install/state.toml`)**:
  * An isolated, untracked local Git repository (`install/.git`).
  * Mirrors `render/<pkg>/` with 1:1 structural fidelity during staging (**Primitive 4**) and tracks live host drift pulled back during reverse-sync (**Primitive 1**).
  * Contains `install/state.toml`, recording each package's transaction state (`"installed"`, `"staged"`, `"staging"`, `"installing"`), `target_directory`, `install_method`, `deployed_files` manifest, and `sudo` privilege.
* **Tier 4 — Active Host System (`$HOME`, `~/.config`, `/etc`, etc.)**:
  * Live host directories where applications read configurations.
  * Files are delivered in **Primitive 5** as either relative symlinks pointing into `install/<pkg>/` (`install_method = "symlink"`) or physical copies (`install_method = "copy"`).
* **Backup & Transactional WAL Archive (`backup/<pkg>/`)**:
  * `backup/<pkg>/overwritten/`: Displaced pre-existing host files or colliding symlinks (with reverse `dot-` prefix translation).
  * `backup/<pkg>/deleted/`: Historically deployed files pruned when removed from a package.
  * `backup/<pkg>/wal/`: Transient Write-Ahead Log (`wal/actions.json` and `wal/stash/`) recording reversible host actions during active installation.

---

## 2. The 4 Core Operational Workflows

### Workflow 1: Declarative Template Loop (`src/` $\rightarrow$ `render/` $\rightarrow$ `install/` $\rightarrow$ Host)
1. Edit source templates in `src/<pkg>/` or environment variables in `config/`.
2. Inspect template compilation changes via `drift diff --template` (**Diff A**: `src/` vs `render/`) or preview the full end-to-end deployment plan in an ephemeral sandbox via `drift plan`.
3. Run `drift deploy`, which executes the 4-stage deployment pipeline:
   * **Stage 0 (Pre-Flight & WAL Hygiene)**: Verifies Git committability on `render/` and `install/`, asserts no package is stuck in `"staging"` / `"installing"`, and purges stale `backup/<pkg>/wal/` folders for non-rollback-eligible packages ([`purge_redundant_wal_folders`](../src/drift/primitives/workspace_gc.py)).
   * **Stage 1 (Render, Reverse-Sync & Sentinel Drift Audit)**: Compiles `src/` $\rightarrow$ `render/` (**P2**), commits `render/` (**P3**), runs `reverse-sync` (**P1**: Host $\rightarrow$ `install/`), and auto-commits converged drifts ([`resolve_and_commit_converged_drifts`](../src/drift/primitives/adopt_repo.py)). If divergent host modifications remain in `install/`, deployment **halts immediately** with `DriftDetectedError` (Exit Code `3`).
   * **Stage 2 (Stage, Apply & Commit)**: Pre-validates both staging (`prepare_stage_packages`) and installation (`prepare_install` with `install_path_mask`) before mutating `install/`, then stages `render/` $\rightarrow$ `install/` (**P4**), deploys `install/` $\rightarrow$ Host (**P5**) with WAL recording and whole-batch auto-rollback on fatal failure, and commits `install/` (**P6**).
   * **Stage 3 (Post-Deploy Garbage Collection)**: On global `drift deploy`, invokes **Primitive 9 (`drift gc`)** to uninstall newly disabled orphan packages and clean up zombie folders and redundant WAL directories.

### Workflow 2: Host Drift Audit & Adoption Loop (Host $\rightarrow$ `install/` $\rightarrow$ `src/`)
1. When an application or user modifies a deployed file on the host, Stage 1 `reverse-sync` copies the change into `install/<pkg>/`, tripping the Sentinel guard.
2. Inspect the live drift via `drift status` and `drift diff --system` (or side-by-side in `$EDITOR` via `drift diff -s -y`).
3. Reconcile the drift:
   * **Adopt into `src/`**: Run `drift adopt [pkg]` (or interactive `drift adopt -i`) to patch source templates in `src/` and commit `install/`.
   * **Dismiss / Overwrite**: Run `drift deploy --force` to commit a recoverable **Drift Snapshot** into `install/.git` history and overwrite the host with clean templates from `src/`.

### Workflow 3: Transactional WAL & Rollback Recovery Loop
1. **Automatic Whole-Batch Rollback**: When a fatal file delivery error or fatal `post_install`/`post_update` hook failure (`[hooks] failure_is_fatal = true`) occurs during `drift deploy` or `drift apply`, Drift evaluates the 3-tier [`RollbackPolicy`](../src/drift/core/constants.py) ladder (`CLI --rollback` > `[settings] rollback` > `[package] auto_rollback`). When triggered, it automatically rolls back all attempted and rollback-eligible packages in the batch in reverse topological order.
2. **Manual Rollback (`drift rollback`)**: Executes **Primitive 8** across rollback-eligible packages (`"staging"`, `"staged"`, `"installing"`, or `"installed"` with uncommitted changes in `install/`):
   * **Step 1**: Reverses host mutations in LIFO order from `backup/<pkg>/wal/actions.json` and restores stashed backups from `backup/<pkg>/wal/stash/`.
   * **Step 2**: Checks out `install/<pkg>/` and `install/state.toml` from `HEAD` (or prunes uncommitted first-time installs).
   * **Step 3**: Runs an incremental convergence pass (`run_primitive_5_install(reinstall=False, run_hooks_when_unchanged=True, rollback=RollbackPolicy.NEVER)`) to restore service state.

### Workflow 4: Uninstallation & Detachment Loop (`drift uninstall`)
* **Standard Uninstall (`drift uninstall <pkg>`)**: Runs in reverse topological dependency order, triggers `pre_uninstall`/`post_uninstall` hooks, removes deployed symlinks/copies, restores displaced original files from `backup/<pkg>/overwritten/`, and removes the package from `install/state.toml`.
* **Detach Mode (`drift uninstall <pkg> --detach`)**: Converts active symlinks into permanent physical copies on the host and unregisters the package from Drift without restoring backups.

---

## 3. Core Primitives, Orchestrators & Exit Codes Matrix

### 3.1 The 17 Primitives & Composite Orchestrators

| ID | CLI Command | Source Module | Public Entry Point | Return Model |
| :--- | :--- | :--- | :--- | :--- |
| **P1** | `drift reverse-sync` | [`reverse_sync.py`](../src/drift/primitives/reverse_sync.py) | `run_primitive_1_reverse_sync` | `ReverseSyncResult` |
| **P2** | `drift render` | [`render_repo.py`](../src/drift/primitives/render_repo.py) | `run_primitive_2_render_packages` | `RenderResult` |
| **P3** | `drift render-commit` | [`render_repo.py`](../src/drift/primitives/render_repo.py) | `run_primitive_3_commit_render_repo` | `None` |
| **P4** | `drift stage` | [`stage_repo.py`](../src/drift/primitives/stage_repo.py) | `run_primitive_4_stage_render_to_install` | `StageResult` |
| **P5** | `drift apply` | [`install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_5_install` | `InstallResult` |
| **P6** | `drift install-commit` | [`install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_6_commit_install_repo` | `InstallCommitResult` |
| **P7** | `drift uninstall` | [`uninstall_repo.py`](../src/drift/primitives/uninstall_repo.py) | `run_primitive_7_uninstall_packages` | `UninstallResult` |
| **P8** | `drift rollback` | [`rollback_repo.py`](../src/drift/primitives/rollback_repo.py) | `run_primitive_8_rollback_recovery` | `RollbackResult` |
| **P9** | `drift gc` | [`workspace_gc.py`](../src/drift/primitives/workspace_gc.py) | `run_primitive_9_purge_workspace_garbage` | `GcResult` |
| **P10** | `drift new` | [`new_package.py`](../src/drift/primitives/new_package.py) | `run_primitive_10_create_new_package` | `NewPackageResult` |
| **P11** | `drift add` | [`add_resource.py`](../src/drift/primitives/add_resource.py) | `run_primitive_11_add_resources` | `AddResourceResult` |
| **P12** | `drift health` | [`package_health.py`](../src/drift/primitives/package_health.py) | `run_primitive_health_checks` | `HealthResult` |
| **P13** | `drift clone` | [`workspace_clone.py`](../src/drift/primitives/workspace_clone.py) | `run_primitive_clone` | `CloneResult` |
| **P14** | `drift repair` | [`workspace_repair.py`](../src/drift/primitives/workspace_repair.py) | `repair_drift_workspace` | `RepairResult` |
| **P15** | `drift diff` | [`workspace_diff.py`](../src/drift/primitives/workspace_diff.py) | `run_primitive_15_workspace_diff` | `DiffResult` |
| **P16** | `drift status` | [`workspace_status.py`](../src/drift/primitives/workspace_status.py) | `run_primitive_status` | `StatusResult` |
| **P17** | `drift plan` | [`plan_repo.py`](../src/drift/primitives/plan_repo.py) | `preview_deploy` / `run_primitive_plan` | `WorkspaceDeployPreview` |
| **Orch** | `drift deploy` | [`deploy_repo.py`](../src/drift/primitives/deploy_repo.py) | `run_primitive_deploy_pipeline` | `DeployResult` |
| **Orch** | `drift adopt` | [`adopt_repo.py`](../src/drift/primitives/adopt_repo.py) | `run_primitive_adopt_drifts` | `AdoptResult` |
| **Init** | `drift init` | [`workspace_init.py`](../src/drift/primitives/workspace_init.py) | `init_drift_workspace` | `WorkspaceConfig` |

* **Two-Stage Split Entry Points (`prepare_*` / `execute_*`)**:
  * **P1 (`reverse_sync`)**: `prepare_reverse_sync` $\rightarrow$ `execute_reverse_sync`
  * **P4 (`stage_repo`)**: `prepare_stage_packages` $\rightarrow$ `execute_stage_packages`
  * **P5 (`install_repo`)**: `prepare_install` $\rightarrow$ `execute_install`
  * **P7 (`uninstall_repo`)**: `prepare_uninstall_packages` $\rightarrow$ `execute_uninstall_packages`
  * **P11 (`add_resource`)**: `prepare_add_resources` $\rightarrow$ `execute_add_resources`
  * **`adopt_repo`**: `prepare_adopt` $\rightarrow$ `execute_adopt`

### 3.2 Domain Exceptions & Standard Process Exit Codes ([`src/drift/core/exceptions.py`](../src/drift/core/exceptions.py))

| Exit Code | Enum Constant | Primary Exception Class | Meaning |
| :--- | :--- | :--- | :--- |
| **`0`** | `ExitCode.SUCCESS` | — | Operation completed cleanly |
| **`1`** | `ExitCode.GENERAL_ERROR` | `DriftError` (+ runtime subclasses) | Runtime, hook, permission, or transaction error |
| **`2`** | `ExitCode.CONFIG_ERROR` | `ConfigError` | Invalid schema, cyclic dependency, or CLI syntax error |
| **`3`** | `ExitCode.DRIFT_DETECTED` | `DriftDetectedError` | Uncommitted host drift in `install/` during `deploy` |
| **`4`** | `ExitCode.RENDER_ERROR` | `RenderError`, `RenderCollisionError` | Template compilation failure or output path collision |
| **`5`** | `ExitCode.COLLISION_ERROR` | `InstallCollisionError`, `CrossPackageCollisionError` | Target inside `drift_root` or cross-package conflict |

* **`ExitCode.GENERAL_ERROR` (`1`) Subclasses**: Includes `HookMissingError`, `HookExecutionError`, `MidwayTransactionError`, `PackageInstallDirMissingError`, `TargetPermissionError`, and `AdminPrivilegeError`.

---

## 4. Codebase Package Architecture & Dependency Layers

The codebase under `src/drift/` is organized into 8 subpackages ordered strictly bottom-up by dependency layer. **Before modifying or calling code in any subpackage, consult its co-located `AGENT_MODULE.md` file**:

| Layer | Subpackage | Co-located Module Map | Core Scope |
| :--- | :--- | :--- | :--- |
| **Layer 0** | `src/drift/utils/` | [`utils/AGENT_MODULE.md`](../src/drift/utils/AGENT_MODULE.md) | Path validation, file/Git/subprocess ops, TOML, env DAG, host facts |
| **Layer 1** | `src/drift/models/` | [`models/AGENT_MODULE.md`](../src/drift/models/AGENT_MODULE.md) | Dataclasses (`*Options`, `*Context`, `*Plan`, `*Result`) & enums |
| **Layer 1–2** | `src/drift/core/` | [`core/AGENT_MODULE.md`](../src/drift/core/AGENT_MODULE.md) | Constants, exceptions, `.drift_ignore`, folder diff/delivery, WAL, `state.toml` |
| **Layer 2** | `src/drift/config/` | [`config/AGENT_MODULE.md`](../src/drift/config/AGENT_MODULE.md) | `WorkspaceConfig`, `PackageConfig`, `RenderEngineRegistry`, templates |
| **Layer 2** | `src/drift/hooks/` | [`hooks/AGENT_MODULE.md`](../src/drift/hooks/AGENT_MODULE.md) | `HookExecFlags`, subprocess hook runner, Python config hooks |
| **Layer 2–3** | `src/drift/render/` | [`render/AGENT_MODULE.md`](../src/drift/render/AGENT_MODULE.md) | Merkle AST (`render_dag`), graph expansion, `RenderLockfile`, `RenderCache` |
| **Layer 3–4** | `src/drift/primitives/` | [`primitives/AGENT_MODULE.md`](../src/drift/primitives/AGENT_MODULE.md) | Primitives `P1`–`P17`, `deploy_repo`, `adopt_repo`, dependency/workspace guards |
| **Layer 5** | `src/drift/cli/` | [`cli/AGENT_MODULE.md`](../src/drift/cli/AGENT_MODULE.md) | Declarative `schema.py`, Argparse/Typer backends, handlers, tab-completions |

---

## 5. The 14 Core Domain Invariants

1. **Source Template `dot-` Prefix Rule**:
   * Files and directories in `src/<pkg>/` targeting hidden host paths **must** use the `dot-` prefix (`dot-bashrc` $\rightarrow$ `.bashrc`, `dot-config/` $\rightarrow$ `.config/`). Raw `.*` files in `src/` (except `.drift_ignore`) are skipped with a warning.
2. **Template Engine Suffix Rule**:
   * Format: `[filename].[engine_suffix].[target_ext]` (e.g. `dot-bashrc.envst.sh`). Engine suffixes **cannot contain dots (`.`)** and cannot use reserved Drift keywords (`drift_package`, `drift_hook`, `drift_ignore`, `drift_workspace`, `drift_hooks`, `drift`).
3. **Ignore Engine Invariants (`.drift_ignore`)**:
   * Single file per package root (`src/<pkg>/.drift_ignore`), rendered/staged to `.drift/.drift_ignore`. Nested ignore files raise `ValueError`.
   * Evaluated as **PCRE Regex** using GNU Stow's two-group matching rules (Group 1 with `/` matches `/rel_path`; Group 2 without `/` matches `basename`) **before** `dot-` prefix translation or suffix stripping. `.drift/` is permanently excluded from host deployment.
4. **Collision Guard & Decoupled Delivery Planning**:
   * Target directory must be strictly absolute and outside `drift_root` (`target_dir.resolve()` vs `drift_root.resolve()`), raising `InstallCollisionError` otherwise.
   * All host mutations are planned first as pure, inspectable [`FileAction`](../src/drift/core/file_action.py) sequences via [`plan_folder_delivery`](../src/drift/core/folder_delivery.py). Pre-existing colliding files/symlinks are backed up to `backup/<pkg>/overwritten/` (`decode_dot_prefix`), and pruned orphans to `backup/<pkg>/deleted/`.
5. **Sentinel Drift Alignment & Pre-Stage Planning Guard**:
   * `drift deploy` renders `src/` $\rightarrow$ `render/` first, runs `reverse-sync`, and calls `resolve_and_commit_converged_drifts` to auto-commit files in `install/` that match `render/`. If divergent changes remain in `install/`, deployment aborts with `DriftDetectedError`.
   * Before mutating `install/` or `state.toml`, `drift deploy` runs both `prepare_stage_packages` and `prepare_install` (using `install_path_mask`) so any pre-flight failure aborts cleanly with zero rollback needed.
6. **CLI Privilege & Sudo Guard**:
   * Running the `drift` CLI under `sudo` on a user-owned workspace is prohibited (`check_sudo_and_root`) to prevent `$HOME`/`~` expanding to `/root` and root-owned file corruption in `render/.git` and `install/.git`. Root deployment is configured per-package via `sudo = true`.
7. **Stage Structural Fidelity Invariant**:
   * `install/<pkg>/` mirrors `render/<pkg>/` with 100% 1:1 structural fidelity, including `.drift_keep` empty-folder stubs and `.drift/` control-plane files (`.drift/drift_package.toml`, `.drift/.drift_ignore`, `.drift/hooks/`, `.drift/render/`).
8. **Render Engine Scope Invariant**:
   * Package configuration templates (`src/<pkg>/drift_package.<suffix>.toml`) can only be compiled by global workspace render engines (`drift_workspace.toml`) during Phase 1 bootstrap. Package-level `[render.<name>]` engines operate strictly in Phase 2 (hooks) and Phase 3 (payload).
9. **Lifecycle Hooks & `drift_hooks/` Isolation**:
   * All relative lifecycle hook scripts and helper dependencies must reside inside `src/<pkg>/drift_hooks/` (compiled to `.drift/hooks/`). All hooks execute in user space without `sudo` with `cwd = hook_path.parent` and `$drift_package_target_dir` in `os.environ`.
10. **Target Migration, Conflict Audit, WAL & 3-State Auto-Rollback**:
    * Changing `target_directory` triggers atomic migration (undeploys old target paths and forces redeployment at the new target without running uninstall hooks or restoring backups).
    * Cross-package destination conflicts are audited upfront across the batch and `state.toml` (`CrossPackageCollisionError`).
    * Reversible host actions are logged to `backup/<pkg>/wal/actions.json` and overwritten backups are stashed in `backup/<pkg>/wal/stash/`.
    * Rollback eligibility (`check_is_package_rollback_eligible`) covers `"staging"`, `"staged"`, `"installing"`, and `"installed"` packages with uncommitted changes in `install/`. Fatal deploy/apply failures evaluate the 3-tier `RollbackPolicy` ladder (`CLI --rollback` > `[settings] rollback` > `[package] auto_rollback`) to trigger whole-batch rollback.
11. **Python Preprocessor Hook Clean-Room Invariant**:
    * Dynamic Python preprocessor hooks (`drift_workspace.py` and `drift_package.py`) execute purely in-memory with **zero footprint on `os.environ`**, receiving resolved environment and facts via `context.env`, `context.facts`, and `context.package_facts`.
12. **Render DAG & Incremental Merkle Invariance**:
    * Compilation runs in 3 isolated phases: Phase 1 (Config) $\rightarrow$ Phase 2 (Hooks) $\rightarrow$ Phase 3 (Payload).
    * Re-rendering is skipped when $\Delta [I, T, V, E] = \emptyset$ via cryptographic Merkle hashes persisted across 3 isolated buckets in `.drift/render_lock.json` (`config_hashes`, `hook_hashes`, `payload_hashes`) and cached in-memory via `RenderCache`.
13. **Full-Cycle Dry-Run Engine & Path Mask Invariant**:
    * `drift plan` and `drift deploy --dry-run` execute inside an ephemeral sandbox (`tmp_sandbox/render/`) with zero mutations to `render/`, `install/`, host files, or Git state ($\Delta \mathcal{W} = \varnothing$), using `source_dir_mask` to evaluate existing host symlinks against canonical `install/` paths.
14. **3-State Path Taxonomy & Cross-Platform Anchor Invariant**:
    * `pathlib.Path` instances fall into 3 mutually exclusive states: **Strictly Relative** (`not p.anchor`), **Partially Anchored** (`bool(p.anchor) and not p.is_absolute()`), and **Strictly Absolute** (`p.is_absolute()`).
    * Relative fields enforce `not p.anchor`; absolute fields enforce `p.is_absolute()`; hybrid fields explicitly reject Partially Anchored paths (`check_is_partially_anchored_path`) before resolving `p if p.anchor else (base_dir / p)`.

---

## 6. Documentation Navigation & Feature Index

### 6.1 Feature & Subsystem Deep-Dive Specifications (`docs/features/`)

| Topic / Subsystem | Specification Document | Key Concepts Covered |
| :--- | :--- | :--- |
| **Render DAG & Merkle Cache** | [`render_dag.md`](features/render_dag.md) | AST nodes, 3-phase compilation, `render_lock.json`, `RenderCache` |
| **Folder Delivery Engine** | [`delivery_engine.md`](features/delivery_engine.md) | `plan_folder_delivery`, `FileAction` algebra, collision guards |
| **WAL, Rollback & GC** | [`rollback_and_wal.md`](features/rollback_and_wal.md) | `state.toml`, `wal/actions.json` & `stash/`, `RollbackPolicy`, `drift gc` |
| **Config, Hooks & Deps** | [`configuration_and_hooks.md`](features/configuration_and_hooks.md) | Workspace/package TOML, engines, hooks, DAG, `.drift_ignore`, 3-state paths |
| **Drift Adoption & FCDs** | [`drift_adoption_and_fcd.md`](features/drift_adoption_and_fcd.md) | `reverse-sync`, convergent drift, `drift adopt`, `fully_controlled_dirs` |
| **Dry-Run (`drift plan`)** | [`plan_engine.md`](features/plan_engine.md) | Ephemeral sandbox, `source_dir_mask`, fault isolation |
| **Environment Precedence** | [`env_modeling.md`](features/env_modeling.md) | 6-tier precedence, `SystemFacts`, Kahn's DAG, secret masking |
| **Empty Folders** | [`empty_folder_dataflow.md`](features/empty_folder_dataflow.md) | `.drift_keep` synthesis, `CREATE_DIR`, FCD & `adopt` handling |
| **CLI & Tab-Completion** | [`tab_completion.md`](features/tab_completion.md) | Declarative `schema.py`, Argparse/Typer backends, shell compilers |

### 6.2 Top-Level Reference & Project Documents (`docs/`)
* **CLI Command & Flag Manual**: [`docs/cli.md`](cli.md)
* **Strategic Roadmap & Project Review**: [`docs/roadmap.md`](roadmap.md)
* **Domain Knowledge Base**: `docs/knowledge/` (OS packaging mechanisms, file permissions, hook designs, cloud config studies)

### 6.3 Development & Testing Quick Reference
* **Targeted Unit Test (Fast Feedback)**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_<module_name>.py
  ```
* **Full Test Suite**:
  ```bash
  PYTHONPATH=src python3 -m unittest discover -s tests
  ```
* **Testing Log Emissions (`set_test_mode`)**:
  ```python
  from drift.core.constants import set_test_mode

  set_test_mode(True, enable_logging=True)
  try:
      with self.assertLogs("drift.module_name", level="WARNING") as cm:
          ...
  finally:
      set_test_mode(True, enable_logging=False)
  ```
