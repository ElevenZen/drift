# `src/drift/models/` — Layer 1 Domain Data Models Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 1 (Domain Options, Contexts, Plans & Result Models)**
* **Core Responsibility**: Houses all strongly-typed domain dataclasses (`*Options`, `*Context`, `*Plan`, status enums, and `*Result` containers) passed across primitive boundaries and serialized for CLI text/JSON output.
* **Dependency Rules**:
  * **Allowed Imports**: [`../core/`](../core/) (`constants.py`, `exceptions.py`, `file_action.py`, `folder_delivery.py`, `mixins.py`, `serialization.py`, `state_registry.py`), [`../utils/`](../utils/), [`../config/`](../config/), and [`../hooks/lifecycle_hooks.py`](../hooks/lifecycle_hooks.py) (`HookExecFlags`).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`. All domain models are re-exported via [`__init__.py`](__init__.py).

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Stage & Leaf Primitive Models**: [`reverse_sync_models.py`](reverse_sync_models.py), [`render_models.py`](render_models.py), [`stage_models.py`](stage_models.py), [`workspace_models.py`](workspace_models.py)
2. **Host Delivery & Adoption Models**: [`install_models.py`](install_models.py), [`uninstall_models.py`](uninstall_models.py), [`adopt_models.py`](adopt_models.py)
3. **Composite Pipeline Models**: [`deploy_models.py`](deploy_models.py) (aggregates `RenderResult`, `ReverseSyncResult`, `StageResult`, `InstallResult`, `GcResult`, and `PackageStagePlan`/`PackageInstallPlan`)

---

## 3. Module & Public API Catalog

### [`reverse_sync_models.py`](reverse_sync_models.py) — Primitive 1 (`drift reverse-sync`) Models
* `PackageReverseSyncPlan(package_name, tracked_actions, fcd_actions)`: Per-package reverse-sync action plan with `.has_changes` and `.format_text()`.
* `ReverseSyncPlan(plans: Dict[str, PackageReverseSyncPlan])`: Batch reverse-sync plan.
* `PackageReverseSyncResult` & `ReverseSyncResult`: Execution results tracking synced packages and file action counts.

### [`render_models.py`](render_models.py) — Primitive 2 (`drift render`) Models
* `RenderOptions(clean=False, dry_run=False, flags=None)`: Controls sandbox compilation and hook execution (`get_hook_flags(settings)`).
* `PackageRenderResult(package_name, status, reason=None, actions=(), ...)`: Single-package render outcome (`"RENDERED"`, `"UP_TO_DATE"`, `"SKIPPED"`) with `.format_text(dry_run=False)`.
* `RenderResult(results, clean_performed=False, dry_run=False)`: Batch render result container.

### [`stage_models.py`](stage_models.py) — Primitive 4 (`drift stage`) Models
* `StageOptions(force=False, no_deps=False, dry_run=False)`: Controls `render/` $\rightarrow$ `install/` staging.
* `PackageStagePlan(package_name, actions, is_new_package=False)`: Per-package staging `FileAction` list with `.has_changes` and `.format_text(dry_run=False)`.
* `StagePlan(pkg_metadata_map, state_registry, packages_stage_order, unchanged_packages, package_plans, options)`: Complete prepared staging plan.
* `StageResult(staged_packages, unchanged_packages, plans, dry_run=False)`: Batch staging execution summary.

### [`install_models.py`](install_models.py) — Primitive 5 (`drift apply`) & Primitive 6 Models
* `InstallOptions(force=False, dry_run=False, reinstall=False, run_hooks_when_unchanged=False, flags=None, no_deps=False, rollback=RollbackPolicy.AUTO)`: Controls physical host installation and automatic rollback policy.
* `PackageInstallContext`: Immutable per-package execution context encapsulating `pkg_name`, `target_dir`, `install_pkg_dir`, `backup_pkg_dir`, `drift_root`, `install_method`, `is_first_time`, `sudo`, `reinstall`, `run_hooks_when_unchanged`, `auto_rollback`, `deployed_files`, `deployable_files`, `target_migrated_from`, `hooks`, `env_resolve`, `.delivery_context`, `.file_action_context` (with `wal_dir = backup_pkg_dir / "wal"`), and `.package_envs()`.
* `PackageInstallPlan(pkg_name, target_dir, install_method, is_first_time, actions, migration_actions=(), target_migrated_from=None, ...)`: Per-package planned host actions with `.can_skip(reinstall, run_hooks_when_unchanged)` and `.format_text(dry_run=False)`.
* `InstallPlan(pkg_metadata_map, state_registry, packages_install_order, options, contexts, package_plans, skipped_packages)`: Prepared batch installation plan.
* `PackageInstallResult`, `InstallResult`, `InstallCommitResult`: Execution result containers.

### [`uninstall_models.py`](uninstall_models.py) — Primitive 7 (`drift uninstall`) Models
* `UninstallOptions(force=False, dry_run=False, detach=False, no_deps=False, flags=None)`: Controls package uninstallation or symlink detachment.
* `PackageUninstallContext`: Self-contained uninstallation context built from `StateRegistry` (retaining `sudo`, `target_dir`, `install_method`, `deployed_files` even if `install/<pkg>/` is missing) with `.file_action_context` and `.package_envs()`.
* `PackageUninstallPlan`, `UninstallPlan`, `RestoredBackup`, `PackageUninstallResult`, `UninstallResult`.

### [`adopt_models.py`](adopt_models.py) — `drift adopt` Models
* `AdoptStatus(str, Enum)`: 5-state adoption status (`UP_TO_DATE`, `SUCCESS`, `PARTIAL`, `SKIPPED`, `FAILED`).
* `ItemAdoptOutcome(str, Enum)`: 3-state single-item outcome (`ADOPTED`, `SKIPPED`, `CONFLICT`; truthy iff `ADOPTED`).
* `AdoptOptions(interactive=False, accept_conflicts=False, force=False, dry_run=False, no_render=False, no_reverse_sync=False, skip_patches=False, flags=None)`.
* `PackageDriftFiles(additions, deletions, modifications, renames, converged_files)`.
* `PackageAdoptPlan` & `AdoptPlan`: Pre-computed adoption plans holding unified patches (`mod_patches`, `rename_patches`) and `.format_text(verbose=False)`.
* `PackageAdoptResult` & `AdoptResult`: Tracks `adopted_*`, `conflicted_files`, `skipped_files`, and `.format_text(dry_run=False, verbose=False)`.

### [`deploy_models.py`](deploy_models.py) — `drift deploy`, `drift rollback` & `drift plan` Models
* `DeployOptions(force=False, dry_run=False, reinstall=False, clean=False, flags=None, no_deps=False, rollback=RollbackPolicy.AUTO)`.
* `CompletedStep`, `DeployFailure(step_index, step_name, package, error_message, error_type, requires_rollback, next_action_type, ..., exit_code=ExitCode.GENERAL_ERROR)`, `DeployResult`. `DeployFailure.exit_code` carries the failing exception's `DriftError.exit_code` (e.g. `3` for `DriftDetectedError`) so CLI text and `--json` modes exit consistently.
* `RollbackResult(rolled_back_packages, skipped_packages, status, message)`: Result model for Primitive 8 (`drift rollback`).
* `PackageDeployPreview` & `WorkspaceDeployPreview`: Full-cycle dry-run simulation models for Primitive 17 (`drift plan`) with `.format_text(show_all=False, verbose=False)`.

### [`workspace_models.py`](workspace_models.py) — Primitives 9–16 & Hook Models
* `GcResult` (Primitive 9 `drift gc`), `NewPackageResult` (Primitive 10 `drift new`), `PackageEnableResult` (`drift enable` / `drift disable`: `status` `SUCCESS`/`UNCHANGED`, per-package `actions`, `shadowed_packages`, `diff`, `format_text()`), `AddResourcePlan` & `AddResourceResult` (Primitive 11 `drift add`).
* `PackageHealthStatus`, `PackageHealthResult`, `HealthResult` (Primitive 12 `drift health`).
* `CloneResult` (Primitive 13 `drift clone`), `RepairCheckDetail` & `RepairResult` (Primitive 14 `drift repair`), `ComponentStatus`, `CheckResult`, `WorkspaceHealthReport` (`workspace_check`).
* `WorkspaceDiffType`, `FileDiffDetail`, `PackageDiffDetail`, `DiffResult` (Primitive 15 `drift diff`).
* `PackageStatus` & `StatusResult` (Primitive 16 `drift status`).
* `HookResult` (`drift hook`).

---

## 4. Local Invariants & Modification Rules
1. **No Raw Dicts Across Boundaries**: All primitive entry points and `prepare_*` / `execute_*` functions must accept and return typed models from this package.
2. **`SerializableModel` & `@always_truthy`**: Result and plan models serialized by `--json` must inherit [`SerializableModel`](../core/serialization.py). Option/plan containers that may have empty collections but should evaluate as `True` in `if options:` checks should inherit [`AlwaysTruthy`](../core/mixins.py).
3. **Pure Formatting Methods**: `.format_text(...)` methods on plans and results must be pure string builders with zero filesystem or state mutations.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/plan_engine.md`](../../../docs/features/plan_engine.md), [`docs/features/drift_adoption_and_fcd.md`](../../../docs/features/drift_adoption_and_fcd.md), [`docs/features/rollback_and_wal.md`](../../../docs/features/rollback_and_wal.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_install.py
  PYTHONPATH=src python3 -m unittest tests/test_adopt.py
  PYTHONPATH=src python3 -m unittest tests/test_plan.py
  ```
