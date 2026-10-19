# `src/drift/core/` — Layer 1–2 Core Domain Engines Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 1–2 (Core Domain Primitives, File Action Executor, WAL, Delivery Planner, Ignore Engine & State Registry)**
* **Core Responsibility**: Implements Drift's foundational domain mechanics: global constants and enums (`RollbackPolicy`, `InstallMethod`, `ExitCode`), typed domain exceptions, JSON serialization, `.drift_ignore` PCRE regex matching, directory tree diffing, the `FileAction` algebra with transactional Write-Ahead Log (`wal/`) recording/reversal, the pure per-path folder delivery planner (`plan_folder_delivery`), and the `install/state.toml` database (`StateRegistry`).
* **Dependency Rules**:
  * **Allowed Imports**: Standard library and [`../utils/`](../utils/).
  * **Forbidden Imports**: Must **never** import from `models/`, `config/`, `hooks/`, `render/`, `primitives/`, or `cli/` (keeping `core/` free of circular dependencies with higher layers).

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Base Definitions**: [`mixins.py`](mixins.py), [`constants.py`](constants.py), [`exceptions.py`](exceptions.py), [`serialization.py`](serialization.py)
2. **Filtering, Diffing & State**: [`ignore.py`](ignore.py), [`folder_diff.py`](folder_diff.py), [`state_registry.py`](state_registry.py)
3. **Action Execution, WAL & Delivery Planning**: [`file_action.py`](file_action.py) $\rightarrow$ [`folder_delivery.py`](folder_delivery.py)

---

## 3. Module & Public API Catalog

### [`constants.py`](constants.py) & [`mixins.py`](mixins.py) — Enums, Constants & Test Mode
* **Enums**:
  * `PackageStage(str, Enum)`: `SOURCE = "source"`, `RENDER = "render"`, `INSTALL = "install"`.
  * `InstallMethod(str, Enum)`: `SYMLINK = "symlink"`, `COPY = "copy"`.
  * `RollbackPolicy(str, Enum)`: `AUTO = "auto"`, `ALWAYS = "always"` (alias `"yes"`), `NEVER = "never"` (alias `"no"`), with `.from_value(val, context)`.
  * `BackupSubfolder(str, Enum)`: `OVERWRITTEN = "overwritten"`, `DELETED = "deleted"`.
  * `DirMode(str, Enum)`: `IGNORE_DIR`, `INCLUDE_ALL_DIR`, `ONLY_EMPTY_DIR`.
  * `ExitCode(IntEnum)`: `SUCCESS = 0`, `GENERAL_ERROR = 1`, `CONFIG_ERROR = 2`, `DRIFT_DETECTED = 3`, `RENDER_ERROR = 4`, `COLLISION_ERROR = 5`.
* **Transaction State Tuples**:
  * `MIDWAY_TRANSACTION_STATES = ("staging", "installing")`: Crash interlocks blocking new operations without `--force`.
  * `ROLLBACK_ELIGIBLE_STATES = ("staging", "staged", "installing")`: States unconditionally eligible for rollback.
* **Test Mode Controls**: `set_test_mode(enabled: bool, enable_logging: bool = False) -> None`, `in_test_mode() -> bool`.
* **Mixins ([`mixins.py`](mixins.py))**: `AlwaysTruthy` base class and `@always_truthy` decorator (`__bool__` always returns `True`).

### [`exceptions.py`](exceptions.py) & [`serialization.py`](serialization.py) — Domain Exceptions & JSON Serialization
* `DriftError(Exception)`: Base domain exception (`exit_code`, `packages: List[str]`, `logged: bool`).
* **Subclasses**: `ConfigError` (`ExitCode.CONFIG_ERROR`), `DriftDetectedError` (`ExitCode.DRIFT_DETECTED`), `RenderError` & `RenderCollisionError` (`ExitCode.RENDER_ERROR`), `InstallCollisionError` & `CrossPackageCollisionError` (`ExitCode.COLLISION_ERROR`), `HookMissingError`, `HookExecutionError(is_fatal: bool)`, `MidwayTransactionError`, `PackageInstallDirMissingError`, `TargetPermissionError`, `AdminPrivilegeError`.
* `SerializableModel` & `serialize_for_json(obj: Any) -> Any`: Recursive dataclass/Path/Enum-to-JSON serializer.

### [`ignore.py`](ignore.py) — `.drift_ignore` PCRE Regex Engine
* `DriftIgnore.load_from_dir(package_dir: Path, is_source: bool = False) -> DriftIgnore`: Loads `.drift_ignore` (from root if `is_source=True`, else `.drift/.drift_ignore`; rejects nested ignore files with `ValueError`).
* `DriftIgnore.match_path(rel_path: Union[str, Path], is_dir: bool = False) -> bool`: Evaluates GNU Stow 2-group PCRE regex patterns (Group 1 with `/` matches `/rel_path`; Group 2 without `/` matches `basename`) and permanently ignores `.drift/`.
* `DriftIgnore.filter_deployable_files(install_pkg_dir: Path, include_empty_dirs: bool = False) -> List[Path]`: Returns sorted relative deployable paths (translating `<dir>/.drift_keep` to `<dir>` when `include_empty_dirs=True`).
* `resolve_deployable_paths_with_empty_dirs(paths: Iterable[Path]) -> Tuple[List[Path], Set[Path]]`: Maps `.drift_keep` stub paths to parent empty directories.

### [`folder_diff.py`](folder_diff.py) — Directory Tree Comparison
* `FolderDiff(added: List[Path], modified: List[Path], deleted: List[Path], matches: List[Path])`.
* `compare_folders(src_dir, dst_dir, ignore_handler=None, src_only=False, translate_mode=None, ...) -> FolderDiff`: Compares two directory trees with optional `dot-` prefix translation (`"forward"` or `"reverse"`).
* `list_folder_paths(base_dir: Path, ignore_handler=None, dir_mode: DirMode = DirMode.IGNORE_DIR) -> List[Path]`: Recursively lists relative paths in `base_dir`.

### [`file_action.py`](file_action.py) — `FileAction` Algebra, Execution & Write-Ahead Log (WAL)
* `FileActionType(str, Enum)`: `CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `CREATE_DIR`, `CREATE_KEEP_FILE`, `SKIP_IDENTICAL`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`, `DELETE_ITEM`, `DELETE_TREE`, `RENDER_ITEM`, `WRITE_CONFIG`, `INFO_MESSAGE`.
* `FileAction`, `NormalFileAction`, `BackupFileAction(backup_pkg_root: Path, ...)`: Action dataclasses with polymorphic `FileAction.from_dict(data)` deserialization and `rebase_file_action(action, old_base, new_base)`.
* `FileActionExecutionContext(sudo=False, resolve_symlinks=True, convert_line_endings=None, wal_dir=None)`.
* `execute_single_action(context: FileActionExecutionContext, action: FileAction) -> None` & `execute_delivery_actions(context: FileActionExecutionContext, actions: Sequence[FileAction]) -> None`: Executes planned actions sequentially and appends reversible actions to `context.wal_dir / "actions.json"`.
* `backup_item(src: Path, backup_dest: Path, backup_pkg_root: Path, sudo: bool = False, resolve_symlinks: bool = True) -> None`: Moves displaced host item to `backup_dest`, stashing any pre-existing backup at `backup_dest` into `backup/<pkg>/wal/stash/` via `rebase_path`.
* **WAL Recovery & Hygiene**:
  * `rollback_package_wal(pkg_backup_root: Path, sudo: bool = False) -> bool`: Replays `wal/actions.json` in reverse (LIFO) order to undo host mutations.
  * `restore_wal_stash(pkg_backup_root: Path, sudo: bool = False) -> None`: Restores stashed prior backups from `wal/stash/` back to `overwritten/` and `deleted/`.
  * `clear_package_wal(pkg_backup_root: Path, sudo: bool = False) -> None`: Removes `backup/<pkg>/wal/`.
* `format_action_line(action, drift_root=None) -> str`, `format_action_summary(actions) -> str`, `is_mutating_action(action) -> bool`, `filter_display_actions(actions, show_skipped=False) -> List[FileAction]`.

### [`folder_delivery.py`](folder_delivery.py) — Pure Per-Path Folder Delivery Planner
* `DeliveryInspectionContext`: Frozen context (`target_dir`, `source_dir`, `drift_root`, `install_method`, `is_first_time`, `backup_pkg_dir`, `backup_subfolder`, `source_dir_mask`, `reverse_mode`, `reinstall`).
* `assert_target_dir_outside_drift_root(target_dir: Path, drift_root: Path) -> None`: Raises `InstallCollisionError` if `target_dir.resolve()` is inside or equal to `drift_root.resolve()`.
* `plan_folder_delivery(context: DeliveryInspectionContext, deployable_files: Sequence[Path], deployed_files: Sequence[Path] = (), empty_dirs: AbstractSet[Path] = frozenset()) -> List[FileAction]`: Pure, side-effect-free planner inspecting ancestor directories (shallowest-first), leaf files/symlinks/empty dirs, and historical orphans.
* `plan_backup_restoration(backup_overwritten_dir, target_dir, drift_root, backup_pkg_dir) -> List[FileAction]`: Compiles backup restoration actions (routing any ancestor collision backups to `deleted/`).
* `plan_symlink_conversions(deployed_files, target_dir, install_pkg_dir) -> List[FileAction]`: Compiles symlink-to-copy actions for `drift uninstall --detach`.
* `plan_file_removals(deployed_files, target_dir, shared_Protected_dirs=frozenset()) -> List[FileAction]`: Compiles `DELETE_ITEM` actions for uninstalled files/dirs (deepest-first).
* `plan_actions_from_folder_diff(diff: FolderDiff, src_dir: Path, dst_dir: Path) -> List[FileAction]`: Compiles staging actions from a `FolderDiff`.

### [`state_registry.py`](state_registry.py) — `install/state.toml` Database & Rollback Eligibility
* `PackageState`: Per-package entry (`state`, `target_directory`, `last_deployed`, `install_method`, `deployed_files`, `sudo`).
* `StateRegistry`: Manages `install/state.toml`:
  * `load_state_registry(filepath: Path) -> StateRegistry`, `save_state_registry(registry: StateRegistry) -> None`, `load_head_state_registry(install_base: Path) -> StateRegistry` (parses `git show HEAD:state.toml`).
  * `.set_package_state(pkg, state, last_deployed=None)`, `.sync_deployed_files(pkg, target_directory, install_method, deployable_files=(), sudo=False)`, `.remove_package(pkg)`.
  * `.get_target_migrated_from(pkg, current_target) -> Optional[Path]`, `.build_destination_ownership_map(exclude_packages=None) -> Dict[Path, str]`.
  * `.get_rollback_eligible_packages(target_packages=None, install_base=None) -> List[str]`.
* `check_is_package_uncommitted_in_install(pkg: str, state_registry: StateRegistry, install_base: Path, head_registry: Optional[StateRegistry] = None) -> bool`: Returns `True` iff `pkg` is `"installed"`, `install/<pkg>` exists on disk, and `install/<pkg>` has uncommitted Git changes or `pkg` is absent from `HEAD:state.toml`.
* `check_is_package_rollback_eligible(pkg: str, state_registry: StateRegistry, install_base: Optional[Path] = None, head_registry: Optional[StateRegistry] = None) -> bool`: Returns `True` iff `install/<pkg>` exists and `pkg` is in `ROLLBACK_ELIGIBLE_STATES` or `check_is_package_uncommitted_in_install` is `True`.

---

## 4. Local Invariants & Modification Rules
1. **Zero-Mutation Planning (`folder_delivery.py`)**: All `plan_*` functions in `folder_delivery.py` must remain 100% read-only; physical mutations occur exclusively in `file_action.py` (`execute_delivery_actions`).
2. **WAL Stashing Before Overwrite (`file_action.py`)**: Whenever `backup_item` writes to `backup/<pkg>/overwritten/` or `deleted/` and a file already exists at `backup_dest`, it must stash the old backup into `backup/<pkg>/wal/stash/` before overwriting.
3. **Ghost Exclusion in Rollback Eligibility (`state_registry.py`)**: `check_is_package_rollback_eligible` must always reject ghost packages whose `install/<pkg>/` directory is missing on disk.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/delivery_engine.md`](../../../docs/features/delivery_engine.md), [`docs/features/rollback_and_wal.md`](../../../docs/features/rollback_and_wal.md), [`docs/features/empty_folder_dataflow.md`](../../../docs/features/empty_folder_dataflow.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_folder_delivery.py
  PYTHONPATH=src python3 -m unittest tests/test_ignore.py
  PYTHONPATH=src python3 -m unittest tests/test_state_registry.py
  PYTHONPATH=src python3 -m unittest tests/test_rollback.py
  ```
