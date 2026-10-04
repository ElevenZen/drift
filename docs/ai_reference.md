# 🤖 Drift Codebase Reference & Architecture Quick-Map (AI Reference)

This document provides a concise, high-density architecture reference, primitive index, core helper catalog, and domain invariant cheat-sheet optimized for AI coding agents and developers.

---

## 1. Directory Topology & 4-Tier Architecture

```
[ Active Host System Target ] (~, ~/.config, /etc)
          ▲                      │ (1) Reverse Sync (`drift reverse-sync` / P1)
          │ (5) Apply / Deploy   ▼
  [ install/ ] (State Database Repo) ── (7) Backup / Restore ──► [ backup/<pkg>/ ]
          ▲
          │ (4) Stage (`drift stage` / P4)
  [ render/ ] (Sandbox Compilation Repo)
          ▲
          │ (2) Render (`drift render` / P2)
    [ src/ ] (Declarative Dotfile Templates)
```

*   **`src/<package>/`**: Declarative source templates and lifecycle hooks (`src/<package>/drift_hooks/`). Hidden files **must** use `dot-` prefix (`dot-bashrc`).
*   **`render/<package>/`**: Clean compilation sandbox. Committed into local `render/.git` repo (P3).
*   **`install/<package>/`**: Staging state database. Committed into local `install/.git` repo (P6). Contains `install/state.toml`.
*   **`backup/<package>/`**: Meticulously structured historical host backups (`backup/<pkg>/overwritten/`, `backup/<pkg>/deleted_files/`).
*   **`config/`**: Global workspace configuration (`config/drift_workspace.toml`, `drift_workspace.local.toml`, `secrets.env`, `drift_workspace.py`).

---

## 2. Core Primitives & Entry Points Matrix

| Primitive | CLI Command | Source File | Public Entry Point | Return Model |
|---|---|---|---|---|
| **P1** | `drift reverse-sync` | [`src/drift/primitives/reverse_sync.py`](../src/drift/primitives/reverse_sync.py) | `run_primitive_1_reverse_sync` | `ReverseSyncResult` |
| **P2** | `drift render` | [`src/drift/render/render_package.py`](../src/drift/render/render_package.py) | `run_primitive_2_render_packages` | `List[PackageRenderResult]` |
| **P3** | `drift render-commit` | [`src/drift/render/render_package.py`](../src/drift/render/render_package.py) | `run_primitive_3_commit_render_repo` | `RenderCommitResult` |
| **P4** | `drift stage` | [`src/drift/primitives/stage_repo.py`](../src/drift/primitives/stage_repo.py) | `run_primitive_4_stage_render_to_install` (`prepare_stage_packages`, `execute_stage_packages`) | `StageResult` |
| **P5** | `drift apply` | [`src/drift/primitives/install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_5_install` (`prepare_install`, `execute_install`) | `InstallResult` |
| **P6** | `drift install-commit` | [`src/drift/primitives/install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_6_commit_install_repo` | `InstallCommitResult` |
| **P7** | `drift uninstall` | [`src/drift/primitives/uninstall_repo.py`](../src/drift/primitives/uninstall_repo.py) | `run_primitive_7_uninstall_packages` | `UninstallResult` |
| **P8** | `drift rollback` | [`src/drift/primitives/rollback_repo.py`](../src/drift/primitives/rollback_repo.py) | `run_primitive_8_rollback_recovery` | `RollbackResult` |
| **P9** | `drift gc` | [`src/drift/primitives/workspace_gc.py`](../src/drift/primitives/workspace_gc.py) | `run_primitive_9_purge_workspace_garbage` | `GcResult` |
| **P10** | `drift new` | [`src/drift/primitives/new_package.py`](../src/drift/primitives/new_package.py) | `run_primitive_10_new_package` | `NewPackageResult` |
| **P11** | `drift add` | [`src/drift/primitives/add_resource.py`](../src/drift/primitives/add_resource.py) | `run_primitive_11_add_resources` (`prepare_add_resources`, `execute_add_resources`) | `AddResourceResult` |
| **P12** | `drift health` | [`src/drift/primitives/package_health.py`](../src/drift/primitives/package_health.py) | `run_primitive_12_package_health_checks` | `HealthResult` |
| **P13** | `drift clone` | [`src/drift/primitives/workspace_clone.py`](../src/drift/primitives/workspace_clone.py) | `run_primitive_13_clone_and_bootstrap` | `CloneResult` |
| **P14** | `drift repair` | [`src/drift/primitives/workspace_repair.py`](../src/drift/primitives/workspace_repair.py) | `run_primitive_14_repair_workspace` | `RepairResult` |
| **P15** | `drift diff` | [`src/drift/primitives/workspace_diff.py`](../src/drift/primitives/workspace_diff.py) | `run_primitive_15_workspace_diff` | `DiffResult` |
| **P16** | `drift status` | [`src/drift/primitives/workspace_status.py`](../src/drift/primitives/workspace_status.py) | `run_primitive_16_workspace_status` | `StatusResult` |

---

## 3. Frequently Used Core Modules & Functions

### [`core/folder_diff.py`](../src/drift/core/folder_diff.py) & [`utils/file_utils.py`](../src/drift/utils/file_utils.py)
*   [`compare_folders(src, dst, ignore_handler=None, translate_mode=None) -> FolderDiff`](../src/drift/core/folder_diff.py): Computes `added`, `modified`, `deleted`, `matches` between directory trees.
*   [`resolve_system_target(rel_file, target_dir) -> Path`](../src/drift/utils/file_utils.py): Translates relative path to target host path applying `dot-` prefix translation.
*   [`translate_dot_prefixes(rel_path) -> Path`](../src/drift/utils/file_utils.py): Translates path segments (`dot-config` $\rightarrow$ `.config`).
*   [`translate_dot_prefixes_reverse(rel_path) -> Path`](../src/drift/utils/file_utils.py): Reverses path segments (`.config` $\rightarrow$ `dot-config`).
*   [`tree_relative_files(base_path) -> List[Path]`](../src/drift/utils/file_utils.py): Recursively gathers all relative file paths under `base_path`.
*   [`backup_file_or_dir_external(src, backup_path, sudo, resolve_symlinks=False)`](../src/drift/core/sync_ops.py): Backs up host paths to `backup/<pkg>/`.
*   [`remove_file_or_dir_with_sudo(path, sudo)`](../src/drift/utils/file_utils.py): Deletes a file or directory safely (with `sudo` if configured).
*   [`check_sudo_privilege() -> bool`](../src/drift/utils/process_utils.py): Verifies sudo permissions without password prompts (`sudo -n true`).

### [`core/folder_delivery.py`](../src/drift/core/folder_delivery.py), [`core/result_models.py`](../src/drift/core/result_models.py) & [`core/serialization.py`](../src/drift/core/serialization.py)
*   [`FileActionType`](../src/drift/core/folder_delivery.py): Enum of discrete planned host operations (`CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`, `SKIP_IDENTICAL`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`, `DELETE_FILE`, `INFO_MESSAGE`).
*   [`FileAction`](../src/drift/core/folder_delivery.py): Dataclass representing a discrete single-file/directory host operation (`action_type`, `src_path`, `dst_path`, `reason`).
*   [`DeliveryInspectionContext`](../src/drift/core/folder_delivery.py): Planning and inspection context encapsulating invariant paths (`target_dir`, `source_dir`, `drift_root`), install mode (`install_method`), first-time flags, and backup routing (`backup_pkg_dir`, `backup_subfolder`).
*   [`FileActionExecutionContext`](../src/drift/core/folder_delivery.py): Execution context encapsulating execution flags (`sudo`, `resolve_symlinks`).
*   [`PackageInstallPlan`](../src/drift/core/result_models.py) & [`PackageUninstallPlan`](../src/drift/core/result_models.py): Strongly-typed dataclass containers for planned package actions, supporting `.format_text(dry_run=False)` summaries.
*   [`plan_folder_delivery(context, deployable_files, deployed_files=()) -> List[FileAction]`](../src/drift/core/folder_delivery.py): Pure, read-only per-path planner inspecting host filesystem state and compiling typed file delivery actions.
*   [`plan_backup_restoration(backup_overwritten_dir, target_dir, drift_root) -> List[FileAction]`](../src/drift/core/folder_delivery.py): Compiles backup restoration into discrete `INFO_MESSAGE`, `ENSURE_DIR`, and `CREATE_COPY` actions with intermediate ancestor collision detection via `plan_folder_delivery`.
*   [`plan_file_removals(deployed_files, target_dir) -> List[FileAction]`](../src/drift/core/folder_delivery.py): Compiles `DELETE_FILE` actions for deployed host items.
*   [`plan_symlink_conversions(deployed_files, target_dir, install_pkg_dir) -> List[FileAction]`](../src/drift/core/folder_delivery.py): Compiles `DELETE_FILE` and `CREATE_COPY` actions converting managed symlinks into physical copies for package detachment.
*   [`execute_delivery_actions(context, actions) -> None`](../src/drift/core/folder_delivery.py): Unified sequential executor applying planned file and backup actions to the host system.
*   [`execute_single_action(context, action) -> None`](../src/drift/core/folder_delivery.py): Executes an individual planned file action using pre-resolved `src_path` and `dst_path`.
*   [`format_action_line(action) -> str`](../src/drift/core/folder_delivery.py) & [`format_action_summary(counts) -> str`](../src/drift/core/folder_delivery.py): Formatting helpers generating uniform action logs and summaries.
*   [`serialize_for_json(obj) -> Any`](../src/drift/core/serialization.py) & [`SerializableModel`](../src/drift/core/serialization.py): Decoupled serialization primitives eliminating circular imports between models and deployment planners.

### [`core/ignore.py`](../src/drift/core/ignore.py) (Ignore Engine & GNU Stow Rules Lineage)
*   [`DriftIgnore.load_from_dir(package_dir, is_source: bool) -> DriftIgnore`](../src/drift/core/ignore.py): Loads `.drift_ignore` PCRE patterns (from package root if `is_source=True`, else `.drift/.drift_ignore`; rejects nested ignore files).
*   [`ignore.match_path(rel_path, is_dir=False) -> bool`](../src/drift/core/ignore.py): Evaluates PCRE regex patterns (2-group matching, directory-only trailing-slash matching, hardcoded `.drift/` exclusion).
*   [`ignore.filter_deployable_files(install_pkg_dir) -> List[Path]`](../src/drift/core/ignore.py): Returns non-ignored deployable files.

### [`core/state_registry.py`](../src/drift/core/state_registry.py) (State Database & Manifests)
*   [`load_state_registry(path) -> StateRegistry`](../src/drift/core/state_registry.py): Loads `install/state.toml`.
*   [`registry.set_package_state(pkg, state, last_deployed=None)`](../src/drift/core/state_registry.py): Updates package state (`"installed"`, `"staging"`, `"installing"`, `"staged"`).
*   [`registry.sync_deployed_files(pkg, target_directory, install_method, deployable_files=(), sudo=False)`](../src/drift/core/state_registry.py): Updates target directory, install method, deployed files manifest, and persists `sudo` privilege.
*   [`registry.get_target_migrated_from(pkg, current_target) -> Optional[Path]`](../src/drift/core/state_registry.py): Detects if package is migrating to a new destination.
*   [`registry.build_destination_ownership_map(exclude_packages=None) -> Dict[Path, str]`](../src/drift/core/state_registry.py): Builds destination ownership mapping across installed packages.
*   [`registry.remove_package(pkg)`](../src/drift/core/state_registry.py): Unregisters package from `state.toml`.

### [`utils/config_utils.py`](../src/drift/utils/config_utils.py), [`utils/toml_utils.py`](../src/drift/utils/toml_utils.py) & [`utils/env_utils.py`](../src/drift/utils/env_utils.py)
*   [`get_nested_from(data, keys, default=None, required=False, is_table=False, context="configuration")`](../src/drift/utils/config_utils.py): Retrieves nested values via dot-delimited key paths or key sequences with optional validation.
*   [`get_first_from(data, keys, default=None)`](../src/drift/utils/config_utils.py): Retrieves the first matching key from alternative candidates.
*   [`validate_known_keys(data, known_keys, context="", message_prefix=None)`](../src/drift/utils/config_utils.py): Enforces strict key allowlists on config mappings.
*   [`parse_bool_value(val, default=False, strict=False, context="")`](../src/drift/utils/config_utils.py): Coerces values (bool, string, int) to boolean with optional strict mode.
*   [`parse_toml(content)`](../src/drift/utils/toml_utils.py), [`dump_toml(data)`](../src/drift/utils/toml_utils.py), [`merge_toml(a, b)`](../src/drift/utils/toml_utils.py): TOML parsing (stdlib `tomllib` on 3.11+, fallback on <3.11), serialization, and table merging.
*   [`env_scope(envs, overwrite=True, env_keep=None, mask_values=False)`](../src/drift/utils/env_utils.py): Scoped environment manager with granular masking (`mask_values=True` or `mask_values=Iterable[str]`).
*   [`env_resolve_scope(env_resolve, overwrite=True, env_keep=INITIAL_ENV)`](../src/drift/utils/env_utils.py): Context manager scoping `EnvResolve` with automatic secret masking.
*   [`topological_sort(graph, error_cls=ValueError) -> List[T]`](../src/drift/utils/env_utils.py): Generic Kahn's algorithm topological sorting for dependency DAGs with cycle detection.
*   [`topological_sort_env(raw_env, error_cls=ConfigError) -> List[str]`](../src/drift/utils/env_utils.py): Computes evaluation order for environment variables referencing internal dependencies.

### [`config/workspace_config.py`](../src/drift/config/workspace_config.py) & [`config/package_config.py`](../src/drift/config/package_config.py)
*   [`load_workspace_config(drift_root, search_parents=True) -> WorkspaceConfig`](../src/drift/config/workspace_config.py): Loads layered workspace config, merges `.local.toml`, `.envst.toml`, `secrets.env`, and DAG variables into `EnvResolve`.
*   [`resolve_and_interpolate_workspace_config(data, secrets_file=None) -> Tuple[Dict[str, Any], EnvResolve]`](../src/drift/config/workspace_config.py): Pure in-memory topological DAG resolution for workspace `[env]` tables (`override`, `secrets`, `default`, `fallback`), returning interpolated dictionary and `EnvResolve`.
*   [`resolve_and_interpolate_package_config(data, package_name, workspace_config=None) -> Tuple[Dict[str, Any], EnvResolve]`](../src/drift/config/package_config.py): Pure in-memory topological DAG resolution and section interpolation for package configurations under 6-tier precedence (Package > Workspace within each macro tier; CLI context at Tier 1).
*   [`PackageConfig.from_dict(data, package_name, base_dir, source_files=(), workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Instantiates strongly-typed package config and computes effective environment tables (`compute_effective_envs(workspace_config)`).
*   [`PackageConfig.package_envs()`](../src/drift/config/package_config.py): Scopes pre-resolved `self.env_resolve.effective_dict` into `os.environ` via `env_resolve_scope` with granular secret masking and zero runtime `workspace_config` requirement.
*   [`PackageConfig.evaluate_requirements(workspace_config, flags=None) -> Tuple[bool, Optional[str]]`](../src/drift/config/package_config.py): Evaluates declarative host requirements (`PackageRequirements`) and dynamic `probe` hook strictly before template rendering begins in Primitive 2 (zero-cost skipping).
*   [`PackageRequirements.from_dict(data, package_name) -> PackageRequirements`](../src/drift/config/package_requirements.py): Parses declarative host platform prerequisites (`os`, `arch`, `distro`, `binaries`, `env`, `ip`).
*   [`load_package_config_from_source_dir(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads, transforms, merges, and validates package configuration from source directory.
*   [`load_package_config_from_render_dir(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads package configuration strictly from `render/<pkg>/` sandbox.
*   [`load_package_config_for_install(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads package configuration strictly from `install/<pkg>/` state database.
*   [`load_package_config_rendered(package_toml_path, package_name, package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads and parses package configuration with explicit `package_dir` and optional `workspace_config`.
*   [`config.is_package_enabled(pkg) -> bool`](../src/drift/config/workspace_config.py): Checks if package is active in workspace.
*   [`WorkspaceConfig.render_cache: RenderCache`](../src/drift/config/workspace_config.py): Per-workspace cache instance injected into expansion and digestion contexts.

### [`render/`](../src/drift/render/) (Incremental Render DAG, Cache, & Lockfile)
*   [`RenderCache`](../src/drift/render/render_cache.py): In-memory cache mapping destination paths (`dst_path`) to `CachedFileEntry(hashes, source_mtime_ns, source_size)`. Injected via `WorkspaceConfig.render_cache`. Features sub-microsecond nanosecond stat fingerprinting (`st_mtime_ns` and `st_size`) to automatically invalidate entries when source files are touched on disk.
*   [`NodeHashes(own_hash, merkle_hash)`](../src/drift/render/render_cache.py): Immutable container storing own hash and topological Merkle tree hash.
*   [`PathNode(dst_path=None, src_path=None)`](../src/drift/render/render_dag.py): Common AST base class for filesystem path nodes (`FileNode`, `DirectoryNode`, `UnknownPathNode`), encapsulating `dst_path` and `src_path` fields and eliminating duplicated path state across derived node types.
*   [`FileNode(dst_path, src_path=None)`](../src/drift/render/render_dag.py) & [`DirectoryNode(dst_path, src_path=None)`](../src/drift/render/render_dag.py): Sibling subclasses of `PathNode` representing file assets and directory synchronization targets respectively.
*   [`UnknownPathNode(src_path)`](../src/drift/render/render_dag.py): Placeholder AST dependency holding an unexpanded input `src_path` in the package source tree before concrete expansion.
*   [`ExpansionContext(drift_root, package_name, enable_render, env_node, render_engines, cache, ...)`](../src/drift/render/render_expansion.py): Context holding required `cache: RenderCache` and path translation tables to expand files and directories into Merkle tree nodes.
*   [`DigestionContext(drift_root, package_name, package_render_dir, lockfile, bucket, cache, ...)`](../src/drift/render/render_digester.py): Context carrying required `cache: RenderCache` and `RenderLockfile` into recursive DAG digestion.
*   [`digest_render_dag(root_node, context) -> DigestionResult`](../src/drift/render/render_digester.py): Digestion engine performing zero-diff file skipping against lockfiles, incremental node caching, and scoped pruning of obsolete `.drift/` internal artifacts and payload files.
*   [`RenderLockfile`](../src/drift/render/render_lock.py): Per-package manifest (`.drift/render.lock`) persisting active Merkle hashes across `config`, `hooks`, and `payload` buckets.

### [`utils/git_utils.py`](../src/drift/utils/git_utils.py) (Sub-Repository Git Management)
*   [`commit_repo_changes(repo_path, message, target_pkgs=(), repo_name="repo")`](../src/drift/utils/git_utils.py): Scoped `git add` and `git commit`.
*   [`has_uncommitted_modifications(repo_path, subpath=None) -> bool`](../src/drift/utils/git_utils.py): Checks porcelain status.

### [`primitives/stage_repo.py`](../src/drift/primitives/stage_repo.py), [`primitives/install_repo.py`](../src/drift/primitives/install_repo.py) & [`primitives/package_assertions.py`](../src/drift/primitives/package_assertions.py)
*   [`prepare_stage_packages(workspace_config, target_pkgs, force) -> StagePlan`](../src/drift/primitives/stage_repo.py): Read-only pre-flight assertion and staging plan preparation with topological dependency ordering.
*   [`plan_package_stage(pkg, install_base, render_base) -> PackageStagePlan`](../src/drift/primitives/stage_repo.py): Pure single-pass comparison between `render/` and `install/` compiling declarative staging actions (`DELETE_FILE`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `ENSURE_DIR`).
*   [`execute_package_stage(context, plan)`](../src/drift/primitives/stage_repo.py): Executes staging actions on `install/` directory via unified delivery engine.
*   [`execute_stage_packages(workspace_config, pkg_metadata, state_registry, ordered_packages=None, dry_run=False) -> StageResult`](../src/drift/primitives/stage_repo.py): Coordinates staging plan compilation and physical file synchronization (or zero-mutation dry-run simulation) from `render/` to `install/`.
*   [`prepare_install(workspace_config, target_pkgs=(), config=None) -> InstallPlan`](../src/drift/primitives/install_repo.py): Read-only pre-flight readiness checks, permission audit, cross-package conflict validation, context creation, and declarative deployment plan compilation with topological dependency ordering.
*   [`PackageInstallContext`](../src/drift/primitives/install_repo.py): Strongly-typed install context with `.file_action_context` property and `.hooks`.
*   [`execute_package_actions(context, plan, resolve_symlinks=True)`](../src/drift/primitives/install_repo.py): Executes planned file actions in deterministic order on the host filesystem.
*   [`execute_package_install_impl(context, plan, state_registry, hook_flags, config) -> PackageInstallResult`](../src/drift/primitives/install_repo.py): Performs state transitions, lifecycle hooks, physical file actions, and registry updates under active package envs.
*   [`execute_package_install(context, plan, state_registry, hook_flags, config) -> PackageInstallResult`](../src/drift/primitives/install_repo.py): Evaluates skip conditions and activates scoped package envs before dispatching to `execute_package_install_impl`.
*   [`execute_install(workspace_config, plan) -> InstallResult`](../src/drift/primitives/install_repo.py): Orchestrates batch physical deployment or simulates zero-mutation dry-run across all planned packages.
*   [`assert_required_package_dependencies_exist(pkg_dependencies_map, universe_names=None)`](../src/drift/primitives/package_assertions.py): Read-only guard raising `ConfigError` if any declared dependency does not exist in the package universe.
*   [`assert_no_cyclic_package_dependencies(pkg_dependencies_map)`](../src/drift/primitives/package_assertions.py): Read-only DAG assertion guard raising `ConfigError` strictly on cycles.
*   [`resolve_package_install_order(pkg_dependencies_map) -> List[str]`](../src/drift/primitives/package_assertions.py): Pure topological sort resolving prerequisite installation order over the package universe (absent dependencies pruned).
*   [`resolve_package_uninstall_order(pkg_dependencies_map) -> List[str]`](../src/drift/primitives/package_assertions.py): Pure reverse topological sort resolving uninstallation order (dependents uninstalled before prerequisites).
*   [`assert_no_broken_dependencies_on_uninstall(packages_to_uninstall, remaining_metadata)`](../src/drift/primitives/package_assertions.py): Read-only pre-flight assertion guard verifying that removing target packages does not break dependencies of remaining installed packages.
*   [`resolve_target_package_order(target_metadata, state_registry, workspace_config, no_deps=False) -> List[str]`](../src/drift/primitives/package_assertions.py): Assembles full package universe and resolves topologically sorted action order for staging and installation.

### [`primitives/uninstall_repo.py`](../src/drift/primitives/uninstall_repo.py) & [`primitives/rollback_repo.py`](../src/drift/primitives/rollback_repo.py)
*   [`UninstallConfig(force=False, dry_run=False, detach=False, no_deps=False, flags=None)`](../src/drift/primitives/uninstall_repo.py): Configuration options controlling package uninstallation behavior.
*   [`PackageUninstallContext(pkg_name, target_dir, install_method, deployed_files, sudo, install_pkg_dir, backup_pkg_dir, drift_root, hooks=None, detach=False, is_missing_install_dir=False)`](../src/drift/primitives/uninstall_repo.py): Minimal uninstallation domain context with derived `.file_action_context` (using `DELETED_FILES` backup subfolder) and scoped `.package_envs()`.
*   [`prepare_uninstall_packages(workspace_config, package_names=(), config=None) -> UninstallPlan`](../src/drift/primitives/uninstall_repo.py): Read-only pre-flight safeguard checks, dependency integrity validation, context creation, and plan assembly with reverse topological dependency ordering.
*   [`execute_package_uninstall(context, plan, hook_flags, dry_run=False) -> PackageUninstallResult`](../src/drift/primitives/uninstall_repo.py): Dispatches uninstallation, detachment, or missing package cleanup for a single package.
*   [`execute_uninstall_packages(workspace_config, plan) -> UninstallResult`](../src/drift/primitives/uninstall_repo.py): Executes package uninstallation plans (or simulates zero-mutation dry-run), updates state registry, and purges missing package directories.
*   [`run_primitive_7_uninstall_packages(workspace_config, package_names=(), config=None) -> UninstallResult`](../src/drift/primitives/uninstall_repo.py): Orchestrates uninstallation or detachment of packages by preparing and executing `UninstallPlan`.
*   [`run_primitive_8_rollback_recovery(workspace_config, target_pkgs=(), force=False, flags=None) -> RollbackResult`](../src/drift/primitives/rollback_repo.py): Recovers from aborted deployments in unified reverse topological order across committed redeployments and uncommitted first-time installs.

### [`primitives/adopt_repo.py`](../src/drift/primitives/adopt_repo.py) (Bidirectional Drift Adoption & Template Sync)
*   [`plan_adopt_repo(workspace_config, package_names=()) -> AdoptPlan`](../src/drift/primitives/adopt_repo.py): Discovers drifts, temporarily stages packages to detect renames, pre-computes unified patches, and restores index without mutation.
*   [`execute_adopt_repo(workspace_config, plan, ...) -> AdoptResult`](../src/drift/primitives/adopt_repo.py): Executes adoption across packages, stages resolved files, and commits staged changes in `install/` repo.
*   [`run_primitive_adopt_drifts(workspace_config, package_names=(), ...) -> AdoptResult`](../src/drift/primitives/adopt_repo.py): Entry point orchestrating planning, dry-run reporting, and execution.
*   [`adopt_one_package_drifts(workspace_config, pkg, interactive, accept_conflicts, ...) -> PackageAdoptResult`](../src/drift/primitives/adopt_repo.py): Reconciles single-package drifts and stages adopted paths in `install/` index without committing (commits are deferred to Layer 5 orchestration or `commit_staged_repo_changes`).
*   [`patch_and_edit(src_file, patch_content, install_file, accept_conflicts, open_editor) -> bool`](../src/drift/primitives/adopt_repo.py): Applies patch, syncs permissions, and optionally launches `$EDITOR`.
*   [`adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, ...) -> Path`](../src/drift/primitives/adopt_repo.py): Symmetrically renames template file in `src/` matching engine suffix, applies patch, and syncs permissions.
*   [`fallback_side_by_side(src_file, install_file) -> bool`](../src/drift/primitives/adopt_repo.py): Visual split-screen diff in `$EDITOR` (`nvim`, `vim`, `code`, `emacs`).

### [`hooks/lifecycle_hooks.py`](../src/drift/hooks/lifecycle_hooks.py), [`hooks/workspace_hook.py`](../src/drift/hooks/workspace_hook.py) & [`hooks/package_hook.py`](../src/drift/hooks/package_hook.py)
*   [`run_workspace_hook(workspace_dir, config, drift_root=None, discovered_packages=None) -> Dict[str, Any]`](../src/drift/hooks/workspace_hook.py): Executes dynamic Python workspace preprocessor hook (`drift_workspace.py`) in-memory with zero `os.environ` mutation.
*   [`run_package_hook(package_dir, config, package_name, workspace_config=None) -> Dict[str, Any]`](../src/drift/hooks/package_hook.py): Executes dynamic Python package preprocessor hook (`drift_package.py`) in-memory with zero `os.environ` mutation.
*   [`HookExecFlags`](../src/drift/hooks/lifecycle_hooks.py): Execution flags (`dry_run`, `no_hooks`, `force`).
*   [`PackageHooks`](../src/drift/hooks/lifecycle_hooks.py): Hook trigger handlers (`trigger_pre_source`, `trigger_post_render`, `trigger_pre_install`, `trigger_post_install`, `trigger_pre_update`, `trigger_post_update`, `trigger_pre_uninstall`, `trigger_post_uninstall`).

### [`core/exceptions.py`](../src/drift/core/exceptions.py) & Standard Exit Codes
*   [`DriftError`](../src/drift/core/exceptions.py) (`ExitCode.GENERAL_ERROR = 1`): Base class for all domain exceptions (`packages: List[str]`, `logged: bool`).
*   [`InstallCollisionError`](../src/drift/core/exceptions.py) (`ExitCode.COLLISION_ERROR = 5`): Raised on root escape, target pointing inside workspace, or symlinked parent directory. Specialized subclass [`CrossPackageCollisionError`](../src/drift/core/exceptions.py) (`packages`, `conflicts`) is raised on cross-package destination path conflicts.
*   [`ConfigError`](../src/drift/core/exceptions.py) (`ExitCode.CONFIG_ERROR = 2`): Invalid TOML/YAML/JSON or invalid target directory path.
*   [`DriftDetectedError`](../src/drift/core/exceptions.py) (`ExitCode.DRIFT_DETECTED = 3`): Uncommitted local modifications in `install/` package directory.
*   [`RenderError`](../src/drift/core/exceptions.py) (`ExitCode.RENDER_ERROR = 4`): Template compilation failure. Subclass [`RenderCollisionError`](../src/drift/core/exceptions.py).
*   [`HookMissingError`](../src/drift/core/exceptions.py): Missing or invalid lifecycle hook file.
*   [`HookExecutionError`](../src/drift/core/exceptions.py): Lifecycle hook execution failure.
*   [`MidwayTransactionError`](../src/drift/core/exceptions.py): Package in midway transaction state ('staging' or 'installing').
*   [`PackageInstallDirMissingError`](../src/drift/core/exceptions.py): Package install directory missing from `install/`.
*   [`TargetPermissionError`](../src/drift/core/exceptions.py): Destination target directory is not writable on host system.

---

## 4. Key Domain Invariants & Rules

1.  **Source Template `dot-` Prefix Rule**:
    *   Files/directories in `src/<pkg>/` targeting hidden host paths **must** use `dot-` (`dot-bashrc` $\rightarrow$ `.bashrc`, `dot-config/` $\rightarrow$ `.config/`).
    *   Raw `.*` files in `src/` (except `.drift_ignore`) are strictly skipped with warning logs.
2.  **Template Engine Suffix Rule**:
    *   Format: `[filename].[engine_suffix].[target_ext]` (e.g. `dot-bashrc.envst.sh`, `home.mustache.nix`).
    *   Engine suffixes **cannot contain dots** (`.`). Reserved suffixes (`drift_package`, `drift_hook`, `drift_ignore`, `drift_workspace`, `drift_hooks`, `drift`) are prohibited.
3.  **Ignore Engine Invariants (`.drift_ignore`)**:
    *   Single file per package root (`src/<pkg>/.drift_ignore`), rendered/staged to `.drift/.drift_ignore`. Nested `.drift_ignore` or `.driftignore` raise `ValueError`.
    *   Uses **PCRE Regex**, derived from GNU Stow's ignore rules (Group 1: `/` prefix matched against `/rel_path`; Group 2: no `/` matched against `basename`), with the sole architectural exception that Drift's internal control plane (`.drift/`) is always automatically ignored and never deployed to the host.
    *   Hardcoded exclusions: `.drift/` internal control plane is never deployed to the host.
4.  **Collision Guard & Safety**:
    *   **Canonical Workspace Root Boundary**: Target directory must be absolute and cannot resolve into or equal `drift_root` (`target_dir.resolve()` vs `drift_root.resolve()`), raising `InstallCollisionError`.
    *   **Decoupled Per-Path Planning**: Replaced monolithic folder comparison with pure, read-only planners (`plan_folder_delivery`, `plan_package_uninstall`). Inspects ancestor directories and leaf files without host mutations, supporting `--dry-run`.
    *   **Host Collisions & Dotfile Translation**: Colliding pre-existing files, conflicting external symlinks, and broken links are safely backed up to `backup/<pkg>/overwritten/` with `dot-` prefix translation (`decode_dot_prefix`), and orphans to `backup/<pkg>/deleted_files/`.
    *   **Isolated Backup Restoration Store**: When restoring backups during uninstallation, ancestor collisions are safely backed up to `deleted_files/` to prevent corrupting the active `overwritten/` backup store.
5.  **Sentinel Drift Alignment Guard**:
    *   If `reverse-sync` leaves uncommitted changes in `install/` repo (host drift), deployer **halts immediately** to prevent silent overwrites. User must `drift adopt` or `drift deploy --force` (which commits a drift snapshot into `install/` history before overwriting).
6.  **CLI Privilege & Sudo Guard**:
    *   Prohibits running CLI under `sudo` on user-owned workspaces to prevent target path mismatch (`$HOME`/`~` expanding to `/root`) and root-owned file corruption in `render/.git` and `install/.git`.
    *   Permitted only if running as true root (`SUDO_USER` unset) or workspace directory is root-owned (`uid == 0`). Elevated deployment is configured per-package via `sudo = true`.
7.  **Stage Structural Fidelity Invariant**:
    *   `install/<pkg>/` mirrors the structure and contents of `render/<pkg>/` with complete 1:1 fidelity.
    *   All package metadata and internal control plane files (`.drift/drift_package.toml`, `.drift/.drift_ignore`, `.drift/hooks/`, `.drift/render/`) are mirrored strictly 1:1.
8.  **Render Engine Scope & Invariants**:
    *   **Global Engines Only for Package Config**: Dynamic package configuration templates (`src/<pkg>/drift_package.envst.toml`) can only be compiled by global workspace render engines (`drift_workspace.toml`), evaluated during workspace bootstrap.
    *   **Package-Level Engines Scope**: Render engines declared in `drift_package.toml` (`[render.<name>]`) operate strictly during package source compilation on **package source dotfiles/templates** (under `src/<pkg>/`) and cannot be used to compile `drift_package.toml` itself.
9.  **Lifecycle Hooks & `source_directory` Isolation**:
    *   **Strict `drift_hooks/` Placement & Dependencies**: All package lifecycle scripts and auxiliary helper dependencies must reside within `src/<pkg>/drift_hooks/` (or be absolute external system binaries). Relative hook paths outside `drift_hooks/` raise `ConfigError`.
    *   **Host Installation & Shared Dependencies via Symlinks**: If a hook script or a file needed by a hook must also be installed to the host target, or if sharing scripts across packages, place a symlink inside `src/<pkg>/drift_hooks/` pointing to the source directory file (or shared script).
    *   **Subfolder `source_directory` Payload Isolation**: If `source_directory` is configured (e.g. `source_directory = "dotfiles"`), source templates render from `src/<pkg>/<source_directory>/` directly to the package root in `render/<pkg>/`, while `src/<pkg>/drift_hooks/` is rendered into `render/<pkg>/.drift/hooks/` and completely excluded from host deployment.
10. **Target Directory Migration & Cross-Package Conflict Audit**:
    *   **Target Directory Migration**: Changing `target_directory` in package configuration triggers an atomic re-targeting during deployment: previous deployed files are undeployed/deleted from the old target, `redeploy = True` is enforced to populate the new target, while uninstallation hooks and backup restoration are NOT executed.
    *   **Cross-Package Destination Conflict Audit**: Before executing physical deployment, Drift audits all destination path claims across the batch and external installed packages in `state.toml`, reporting all intra-batch and inter-package path collisions together ([`CrossPackageCollisionError`](../src/drift/core/exceptions.py)).
    *   **Midway & Rollback Transaction States**: `MIDWAY_TRANSACTION_STATES = ("staging", "installing")` for in-flight crash locks. `ROLLBACK_ELIGIBLE_STATES = ("staging", "staged", "installing")` defines packages with uncommitted state in `install/` eligible for `drift rollback` without `--force`.
11. **Python Preprocessor Hook Clean-Room Invariant**:
    *   Dynamic Python preprocessor hooks (`drift_workspace.py` / `drift_package.py`) execute purely in-memory with **zero footprint on `os.environ`**.
    *   Ambient `os.environ` is never mutated during Python hook execution; all secrets, system facts, package facts, and CLI variables are passed strictly via `context.env`, `context.facts`, and `context.package_facts`.

---

## 5. Development & Testing Commands

*   **Targeted Unit Test (Fast Feedback)**:
    ```bash
    python3 -m unittest tests/test_<module_name>.py
    ```
*   **Full Test Suite**:
    ```bash
    python3 -m unittest discover -s tests
    ```
*   **Log Output Testing (`set_test_mode`)**:
    *   Test mode is enabled by default with logging silenced (`logging.disable(logging.CRITICAL)`).
    *   **Use `set_test_mode(True, enable_logging=True)` to test log output** (with `assertLogs`):
        ```python
        from drift.constants import set_test_mode

        set_test_mode(True, enable_logging=True)
        try:
            with self.assertLogs("drift.module_name", level="WARNING") as cm:
                # perform operation that emits logs/warnings
                self.assertTrue(any("expected message" in msg for msg in cm.output))
        finally:
            set_test_mode(True, enable_logging=False)
        ```
*   **Coding Conventions (`GEMINI.md`)**:
    *   Functional constructs (`filter`, `map`, comprehensions, generators) preferred over procedural loops.
    *   Decouple data gathering/resolution from execution.
    *   Return strongly-typed dataclasses (`*Result`), never raw dicts/tuples across interfaces.
    *   Always use relative markdown links across documentation.
