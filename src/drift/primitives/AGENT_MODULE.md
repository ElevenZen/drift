# `src/drift/primitives/` — Layer 3–4 Operational Primitives & Orchestrators Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 3–4 (Atomic State-Transition Primitives `P1`–`P17` & Composite Pipeline Orchestrators)**
* **Core Responsibility**: Implements all 17 Drift primitives (`reverse-sync`, `render`, `render-commit`, `stage`, `apply`, `install-commit`, `uninstall`, `rollback`, `gc`, `new`, `add`, `health`, `clone`, `repair`, `diff`, `status`, `plan`), the composite workflow orchestrators (`deploy` and `adopt`), workspace bootstrap (`init`), and cross-package dependency/collision guards (`package_assertions.py`).
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../models/`](../models/), [`../config/`](../config/), [`../hooks/`](../hooks/), and [`../render/`](../render/).
  * **Forbidden Imports**: Must **never** import from `cli/` (keeping all primitives callable as pure library APIs independent of CLI presentation). All public symbols are re-exported via [`__init__.py`](__init__.py).

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Cross-Package Guards & Workspace Diagnostics (Layer 3a)**:
   * [`package_assertions.py`](package_assertions.py), [`workspace_check.py`](workspace_check.py), [`workspace_init.py`](workspace_init.py)
2. **Core Forward & Backward Pipeline Primitives (Layer 3b)**:
   * [`reverse_sync.py`](reverse_sync.py) (**P1**), [`render_repo.py`](render_repo.py) (**P2, P3**), [`stage_repo.py`](stage_repo.py) (**P4**), [`install_repo.py`](install_repo.py) (**P5, P6**), [`uninstall_repo.py`](uninstall_repo.py) (**P7**)
3. **Recovery, Maintenance & Authoring Primitives (Layer 3c)**:
   * [`workspace_enable.py`](workspace_enable.py) (`drift enable` / `drift disable`), [`rollback_repo.py`](rollback_repo.py) (**P8**), [`workspace_gc.py`](workspace_gc.py) (**P9**), [`new_package.py`](new_package.py) (**P10**), [`add_resource.py`](add_resource.py) (**P11**), [`package_health.py`](package_health.py) (**P12**), [`workspace_repair.py`](workspace_repair.py) (**P14**), [`workspace_diff.py`](workspace_diff.py) (**P15**), [`workspace_status.py`](workspace_status.py) (**P16**)
4. **Composite Multi-Stage Orchestrators (Layer 4)**:
   * [`adopt_repo.py`](adopt_repo.py) (`drift adopt`), [`plan_repo.py`](plan_repo.py) (**P17** `drift plan`), [`deploy_repo.py`](deploy_repo.py) (`drift deploy`), [`workspace_clone.py`](workspace_clone.py) (**P13** `drift clone`)

---

## 3. Module & Public API Catalog

### [`package_assertions.py`](package_assertions.py) — Pre-Flight Guards & Topological Dependency Sorters
* **Read-Only Assertion Guards (`assert_*` / `check_*`)**:
  * `check_disabled_target_packages(workspace_config, target_packages) -> List[str]` & `assert_target_packages_enabled(workspace_config, target_packages, adhoc=False, force=False, command_name="deploy") -> None`: Blocks explicit deployment/planning of disabled packages unless `adhoc=True` or `force=True` (`PackageDisabledError`).
  * `assert_packages_hooks_exist(pkg_metadata_map, stage=PackageStage.INSTALL) -> None`
  * `assert_install_pkg_dirs_clean(install_base, target_pkgs, force=False) -> None`
  * `assert_packages_not_in_midway_state(state_registry, target_pkgs, force=False, action_name="proceed") -> None`
  * `assert_packages_install_dirs_exist(install_base, target_pkgs) -> None`
  * `assert_packages_target_dirs_valid(pkg_metadata_map, drift_root) -> None`
  * `assert_packages_target_dirs_writable(pkg_metadata_map) -> None`
  * `assert_no_cross_package_conflicts(pkg_metadata_map, state_registry, install_base, source_base_override=None) -> None`: Raises `CrossPackageCollisionError` on intra-batch or inter-package destination collisions (excluding empty directories).
  * `assert_required_package_dependencies_exist(pkg_dependencies_map, universe_names=None) -> None`: Raises `MissingDependencyError` when required dependencies are absent from the installable universe.
  * `assert_no_cyclic_package_dependencies(pkg_dependencies_map) -> None`: Raises `CyclicDependencyError` on circular dependency graphs.
  * `assert_no_broken_dependencies_on_uninstall(packages_to_uninstall, remaining_metadata) -> None`: Raises `MissingDependencyError` when remaining installed packages depend on a package being uninstalled.
* **Topological Sorters (`resolve_*`)**:
  * `resolve_package_install_order(pkg_dependencies_map) -> List[str]` (prerequisites first)
  * `resolve_package_uninstall_order(pkg_dependencies_map) -> List[str]` (dependents first)
  * `resolve_target_package_order(target_metadata, state_registry, workspace_config, no_deps=False) -> List[str]`

### [`reverse_sync.py`](reverse_sync.py) — Primitive 1: Host $\rightarrow$ `install/` Reverse Sync
* `prepare_reverse_sync(workspace_config, target_pkgs=()) -> ReverseSyncPlan`: Pure read-only inspection of tracked files (`gather_tracked_reverse_sync_files`) and scoped FCDs (`gather_fcd_reverse_sync_files`).
* `execute_reverse_sync_plan(workspace_config, plan: ReverseSyncPlan) -> ReverseSyncResult`: Applies planned reverse-sync actions to `install/<pkg>/`.
* `run_primitive_1_reverse_sync(workspace_config, target_pkgs=()) -> ReverseSyncResult`: Composite `prepare_reverse_sync` $\rightarrow$ `execute_reverse_sync_plan`.

### [`render_repo.py`](render_repo.py) — Primitive 2 (`drift render`) & Primitive 3 (`drift render-commit`)
* `run_primitive_2_render_packages(workspace_config, target_pkgs=(), options=None) -> RenderResult`: Orchestrates [`render_package`](../render/render_package.py) across active packages (with optional `--clean` pre-wipe).
* `run_primitive_3_commit_render_repo(workspace_config, commit_message, target_pkgs=()) -> bool`: Commits changes in `render/.git`.

### [`stage_repo.py`](stage_repo.py) — Primitive 4: `render/` $\rightarrow$ `install/` Staging
* `prepare_stage_packages(workspace_config, target_pkgs=(), options=None) -> StagePlan`: Read-only readiness check (`assert_packages_stage_ready`), topological ordering, and single-pass 1:1 diff compilation (`plan_package_stage`).
* `execute_stage_packages(workspace_config, plan: StagePlan) -> StageResult`: Executes staging actions (`"staging"` $\rightarrow$ `"staged"`) or dry-run simulation.
* `run_primitive_4_stage_render_to_install(workspace_config, target_pkgs=(), options=None) -> StageResult`.

### [`install_repo.py`](install_repo.py) — Primitive 5 (`drift apply`) & Primitive 6 (`drift install-commit`)
* `prepare_install(workspace_config, target_pkgs=(), options=None, source_base_override=None) -> InstallPlan`: Read-only readiness audit (`assert_packages_install_ready`), cross-package conflict check, topological sort, context construction (`PackageInstallContext`), and per-path delivery planning (`plan_package_install`).
* `execute_install(workspace_config, plan: InstallPlan) -> InstallResult`: Deploys packages in topological order (`execute_package_install_impl`), clearing stale WAL, setting `"installing"`, running `pre_install`/`pre_update`, syncing `deployed_files`, applying `FileAction`s with WAL recording (`backup/<pkg>/wal/actions.json`), running `post_install`/`post_update` (respecting `failure_is_fatal`), setting `"installed"`, and triggering whole-batch auto-rollback (`_trigger_batch_auto_rollback`) if a fatal failure occurs under `RollbackPolicy`.
* `run_primitive_5_install(workspace_config, target_pkgs=(), options=None) -> InstallResult` & `run_primitive_6_commit_install_repo(workspace_config, commit_message, target_pkgs=()) -> InstallCommitResult`.

### [`uninstall_repo.py`](uninstall_repo.py) — Primitive 7: Package Uninstallation & Detachment
* `prepare_uninstall_packages(workspace_config, package_names=(), options=None) -> UninstallPlan`: Read-only safeguard check (`assert_packages_uninstall_ready`), broken-dependency check, reverse topological sort, and action planning (`plan_package_uninstall`).
* `execute_uninstall_packages(workspace_config, plan: UninstallPlan) -> UninstallResult`: Executes standard uninstallation (file removal + backup restoration from `overwritten/`) or `--detach` symlink conversion (`detach_one_package`), updates `state.toml`, and commits `install/`.
* `run_primitive_7_uninstall_packages(workspace_config, package_names=(), options=None) -> UninstallResult`.

### [`rollback_repo.py`](rollback_repo.py) — Primitive 8: 3-Step WAL Rollback Recovery
* `run_primitive_8_rollback_recovery(workspace_config, target_pkgs=(), force=False, flags=None) -> RollbackResult`: Validates rollback eligibility (`validate_rollback_packages` via `check_is_package_rollback_eligible`), sorts candidates in unified reverse topological order (`resolve_rollback_order`), and executes:
  1. `_rollback_step1_reverse_wal`: LIFO WAL reversal (`rollback_package_wal`), stash restoration (`restore_wal_stash`), and WAL cleanup (`clear_package_wal`).
  2. `_rollback_step2_git_checkout`: Resets `install/<pkg>/` and `state.toml` from `HEAD` (or prunes uncommitted first-time installs).
  3. `_rollback_step3_incremental_install`: Runs incremental `run_primitive_5_install(reinstall=False, run_hooks_when_unchanged=True, rollback=RollbackPolicy.NEVER)` for committed packages.

### [`workspace_gc.py`](workspace_gc.py) — Primitive 9: Workspace Garbage Collection & WAL Hygiene
* `purge_render_folders(...)`, `purge_install_folders(...)`, `purge_redundant_wal_folders(workspace_config, registry=None, target_pkgs=(), dry_run=False, log_level=logging.INFO) -> List[str]`.
* `run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=False, flags=None) -> GcResult`: Orchestrates Stage 1 orphan uninstallation, Stage 1b ghost registry pruning, Stage 2 `render/` + `install/` zombie folder pruning + `purge_redundant_wal_folders`, and Stage 3 scoped Git commits.

### [`adopt_repo.py`](adopt_repo.py) — Bidirectional Drift Adoption (`drift adopt`)
* `check_is_drifted_path_converged(install_pkg_dir, render_pkg_dir, rel_path) -> bool` & `resolve_and_commit_converged_drifts(workspace_config, package_names, dry_run=False) -> Dict[str, List[Path]]`.
* `prepare_adopt(workspace_config, package_names=(), options=None) -> AdoptPlan`, `plan_package_adopt(...) -> PackageAdoptPlan`, `execute_package_adopt(...) -> PackageAdoptResult`, `execute_adopt(workspace_config, plan: AdoptPlan) -> AdoptResult`, `run_primitive_adopt_drifts(workspace_config, package_names=(), options=None) -> AdoptResult`.

### [`deploy_repo.py`](deploy_repo.py) & [`plan_repo.py`](plan_repo.py) — Full-Cycle Deploy & Dry-Run Sandbox (Primitive 17)
* `execute_deploy_pipeline(workspace_config, target_pkgs=(), options=None) -> DeployResult` & `run_primitive_deploy_pipeline(...)`: Executes the deployment pipeline (Disabled-package safeguard via `assert_target_packages_enabled` $\rightarrow$ Stage 0 WAL hygiene & midway check $\rightarrow$ Stage 1 Render + Reverse-Sync + Converged Drift Resolution + Sentinel Check $\rightarrow$ Stage 2 Pre-Stage Planning Guard + Stage + Apply + Commit $\rightarrow$ Optional Stage 3 Global GC when `opts.gc` is `True`, or disabled-installed package hint via `check_disabled_installed_packages` & `format_disabled_installed_packages_hint`, plus `format_deploy_summary`).
* `preview_deploy(workspace_config, target_pkgs=(), options=None) -> WorkspaceDeployPreview` & `run_primitive_plan(...)`: Enforces `assert_target_packages_enabled` and simulates full-cycle deployment inside `enter_plan_sandbox` without mutating workspace or host state.

### [`new_package.py`](new_package.py), [`add_resource.py`](add_resource.py), [`package_health.py`](package_health.py) & `workspace_*.py` (Primitives 10–16 & Init)
* **P10 ([`new_package.py`](new_package.py))**: `run_primitive_10_create_new_package(workspace_config, package_name, target_dir=None, install_method=None, force=False) -> NewPackageResult`.
* **P11 ([`add_resource.py`](add_resource.py))**: `prepare_add_resources(...) -> AddResourcePlan`, `execute_add_resources(...) -> AddResourceResult`, `run_primitive_11_add_resources(...) -> AddResourceResult`.
* **P12 ([`package_health.py`](package_health.py))**: `run_primitive_health_checks(workspace_config, target_pkgs=(), timeout_override=None, from_tier=PackageStage.INSTALL) -> HealthResult`.
* **P13 ([`workspace_clone.py`](workspace_clone.py))**: `run_primitive_clone(git_url, destination=None, branch=None, depth=None, no_repair=False) -> CloneResult`.
* **P14 ([`workspace_repair.py`](workspace_repair.py) & [`workspace_check.py`](workspace_check.py))**: `check_existing_workspace_status(drift_root, quiet=False) -> WorkspaceHealthReport`, `repair_drift_workspace(drift_root, dry_run=False) -> WorkspaceHealthReport`, `build_repair_result(report, dry_run=False) -> RepairResult`.
* **P15 ([`workspace_diff.py`](workspace_diff.py))**: `run_primitive_15_workspace_diff(workspace_config, target_pkgs=(), diff_type=WorkspaceDiffType.PENDING, stat_only=False, side_by_side=False) -> DiffResult`.
* **P16 ([`workspace_status.py`](workspace_status.py))**: `run_primitive_status(workspace_config, target_pkgs=(), list_only=False) -> StatusResult`.
* **Enable/Disable ([`workspace_enable.py`](workspace_enable.py))**: `prepare_packages_enable(workspace_config, packages, options) -> WorkspaceEnableEdit` (read-only: `normalize_enable_package_names`, `assert_packages_enable_targets_exist`, `plan_workspace_enable_edit`), `execute_packages_enable(workspace_config, edit, options) -> PackageEnableResult` (atomic `write_file` with original mode; no write on dry-run or no-op), `run_primitive_packages_enable(...)`. `PackageEnableOptions(enabled, local=False, dry_run=False)`. Config authoring only: never renders, deploys, or uninstalls.
* **Init ([`workspace_init.py`](workspace_init.py))**: `init_drift_workspace(drift_root, force=False) -> WorkspaceConfig` (initializes `drift_root` literally; never redirects to the enclosing Git toplevel).

---

## 4. Local Invariants & Modification Rules
1. **Strict `prepare_*` (Read-Only) vs `execute_*` (Mutation) Split**: Every `prepare_*` function (`prepare_reverse_sync`, `prepare_stage_packages`, `prepare_install`, `prepare_uninstall_packages`, `prepare_add_resources`, `prepare_adopt`) must perform **zero** host or state mutations so pre-flight errors abort cleanly before any transaction state changes.
2. **Pre-Stage Planning Guard in `deploy_repo.py`**: `execute_sequential_compile_and_apply` must run both `prepare_stage_packages` and `prepare_physical_install_step` (with `install_path_mask = workspace_config.render_path`) **before** calling `execute_staging_step`.
3. **Resolved Hook Flags Propagation**: When orchestrators (`deploy_repo`, `install_repo`, `workspace_gc`) invoke sub-primitives, always resolve and pass `opts.get_hook_flags(settings=workspace_config.settings)` rather than raw `opts.flags`.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/architecture.md`](../../../docs/architecture.md), [`docs/features/rollback_and_wal.md`](../../../docs/features/rollback_and_wal.md), [`docs/features/drift_adoption_and_fcd.md`](../../../docs/features/drift_adoption_and_fcd.md), [`docs/features/plan_engine.md`](../../../docs/features/plan_engine.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_deploy.py
  PYTHONPATH=src python3 -m unittest tests/test_stage.py
  PYTHONPATH=src python3 -m unittest tests/test_install.py
  PYTHONPATH=src python3 -m unittest tests/test_uninstall.py
  PYTHONPATH=src python3 -m unittest tests/test_rollback.py
  PYTHONPATH=src python3 -m unittest tests/test_gc.py
  PYTHONPATH=src python3 -m unittest tests/test_adopt.py
  PYTHONPATH=src python3 -m unittest tests/test_plan.py
  PYTHONPATH=src python3 -m unittest tests/test_workspace_enable.py
  ```
