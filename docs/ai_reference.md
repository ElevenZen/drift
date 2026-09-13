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
*   **`backup/<package>/`**: Meticulously structured historical backups (`backup/<pkg>/overwritten/`, `backup/<pkg>/deleted_files/`).
*   **`config/`**: Global workspace configuration (`config/drift_workspace.toml`, `drift_workspace.local.toml`, `secrets.env`, `drift_workspace.py`).

---

## 2. Core Primitives & Entry Points Matrix

| Primitive | CLI Command | Source File | Public Entry Point | Return Model |
|---|---|---|---|---|
| **P1** | `drift reverse-sync` | [`src/drift/reverse_sync.py`](../src/drift/reverse_sync.py) | [`run_primitive_1_reverse_sync`](../src/drift/reverse_sync.py#L90) | `ReverseSyncResult` |
| **P2** | `drift render` | [`src/drift/render_package.py`](../src/drift/render_package.py) | [`run_primitive_2_render_packages`](../src/drift/render_package.py#L425) | `List[PackageRenderResult]` |
| **P3** | `drift render-commit` | [`src/drift/render_repo.py`](../src/drift/render_repo.py) | [`run_primitive_3_commit_render_repo`](../src/drift/render_repo.py#L65) | `RenderCommitResult` |
| **P4** | `drift stage` | [`src/drift/stage_repo.py`](../src/drift/stage_repo.py) | [`run_primitive_4_stage_render_to_install`](../src/drift/stage_repo.py#L250) | `List[PackageStageChanges]` |
| **P5** | `drift apply` | [`src/drift/install_repo.py`](../src/drift/install_repo.py) | [`run_primitive_5_install_deployment`](../src/drift/install_repo.py#L920) | `List[PackageInstallResult]` |
| **P6** | `drift install-commit` | [`src/drift/install_repo.py`](../src/drift/install_repo.py) | [`run_primitive_6_commit_install_repo`](../src/drift/install_repo.py#L965) | `InstallCommitResult` |
| **P7** | `drift uninstall` | [`src/drift/uninstall_repo.py`](../src/drift/uninstall_repo.py) | [`run_primitive_7_uninstall_packages`](../src/drift/uninstall_repo.py#L350) | `UninstallResult` |
| **P8** | `drift rollback` | [`src/drift/rollback.py`](../src/drift/rollback.py) | [`run_primitive_8_rollback_recovery`](../src/drift/rollback.py#L75) | `RollbackResult` |
| **P9** | `drift gc` | [`src/drift/workspace_gc.py`](../src/drift/workspace_gc.py) | [`run_primitive_9_purge_workspace_garbage`](../src/drift/workspace_gc.py#L172) | `GcResult` |
| **P10** | `drift new` | [`src/drift/package_creator.py`](../src/drift/package_creator.py) | [`run_primitive_10_create_package`](../src/drift/package_creator.py#L110) | `PackageCreateResult` |
| **P11** | `drift add` | [`src/drift/import_resource.py`](../src/drift/import_resource.py) | [`run_primitive_11_import_resource`](../src/drift/import_resource.py#L220) | `ImportResult` |
| **P12** | `drift health` | [`src/drift/health_check.py`](../src/drift/health_check.py) | [`run_primitive_12_package_health_checks`](../src/drift/health_check.py#L125) | `HealthResult` |
| **P13** | `drift clone` | [`src/drift/clone_repo.py`](../src/drift/clone_repo.py) | [`run_primitive_13_clone_and_bootstrap`](../src/drift/clone_repo.py#L85) | `CloneResult` |
| **P14** | `drift repair` | [`src/drift/repair.py`](../src/drift/repair.py) | [`run_primitive_14_repair_workspace`](../src/drift/repair.py#L180) | `RepairResult` |
| **P15** | `drift diff` | [`src/drift/workspace_diff.py`](../src/drift/workspace_diff.py) | [`run_primitive_diff`](../src/drift/workspace_diff.py#L280) | `DiffResult` |
| **P16** | `drift status` | [`src/drift/workspace_status.py`](../src/drift/workspace_status.py) | [`run_primitive_16_workspace_status`](../src/drift/workspace_status.py#L185) | `StatusResult` |

---

## 3. Frequently Used Core Modules & Functions

### [`file_utils.py`](../src/drift/file_utils.py) (Filesystem & Path Operations)
*   [`resolve_system_target(rel_file, target_dir) -> Path`](../src/drift/file_utils.py#L125): Translates relative path to target host path applying `dot-` prefix translation.
*   [`translate_dot_prefixes(rel_path) -> Path`](../src/drift/file_utils.py#L85): Translates path segments (`dot-config` $\rightarrow$ `.config`).
*   [`reverse_translate_dot_prefixes(rel_path) -> Path`](../src/drift/file_utils.py#L105): Reverses path segments (`.config` $\rightarrow$ `dot-config`).
*   [`compare_folders(src, dst, ignore_handler=None, translate_mode='none') -> FolderDiff`](../src/drift/file_utils.py#L210): Computes `added`, `modified`, `deleted`, `unmodified` between directory trees.
*   [`tree_relative_files(base_path) -> List[Path]`](../src/drift/file_utils.py#L160): Recursively gathers all relative file paths under `base_path`.
*   [`backup_file_or_dir_external(src, backup_path, sudo, resolve_symlinks=False)`](../src/drift/file_utils.py#L320): Backs up host paths to `backup/<pkg>/`.
*   [`remove_file_or_dir_with_sudo(path, sudo)`](../src/drift/file_utils.py#L380): Deletes a file or directory safely (with `sudo` if configured).
*   [`check_sudo_privilege() -> bool`](../src/drift/file_utils.py#L40): Verifies sudo permissions without password prompts (`sudo -n true`).

### [`ignore.py`](../src/drift/ignore.py) (Ignore Engine & GNU Stow Rules)
*   [`DriftIgnore.load_from_dir(render_pkg_dir) -> DriftIgnore`](../src/drift/ignore.py#L71): Loads `.drift_ignore` PCRE patterns (rejects nested ignore files).
*   [`DriftIgnore.for_install_root() -> DriftIgnore`](../src/drift/ignore.py#L102): Creates ignore rules for `install/` root (`state.toml` guard).
*   [`ignore.match_path(rel_path) -> bool`](../src/drift/ignore.py#L155): Evaluates PCRE regex patterns (2-group matching, hardcoded `.drift/` and `MANAGED_CONFIG_FILES` exclusion).
*   [`ignore.filter_deployable_files(install_pkg_dir) -> List[Path]`](../src/drift/ignore.py#L141): Returns non-ignored deployable files.
*   [`ignore.create_stow_ignore_file(target_dir)`](../src/drift/ignore.py#L132): Generates `.stow-local-ignore`.

### [`state_registry.py`](../src/drift/state_registry.py) (State Database & Manifests)
*   [`load_state_registry(path) -> StateRegistry`](../src/drift/state_registry.py#L140): Loads `install/state.toml`.
*   [`registry.set_package_state(pkg, state, last_deployed=None, install_method=None)`](../src/drift/state_registry.py#L65): Updates package state (`"installed"`, `"staging"`, `"deploying"`, `"staged"`).
*   [`registry.set_package_deployed_files(pkg, files)`](../src/drift/state_registry.py#L85): Writes deployed files manifest.
*   [`registry.remove_package(pkg)`](../src/drift/state_registry.py#L105): Unregisters package from `state.toml`.

### [`workspace_config.py`](../src/drift/workspace_config.py) & [`package_config.py`](../src/drift/package_config.py)
*   [`load_workspace_config(drift_root, search_parents=True) -> WorkspaceConfig`](../src/drift/workspace_config.py#L210): Loads layered workspace config, merges `.local.toml`, `.envst.toml`, `secrets.env`, and DAG variables.
*   [`load_package_config(pkg_dir, workspace_config=None) -> PackageConfig`](../src/drift/package_config.py#L220): Loads package config, layered overrides, and renders package envs.
*   [`config.is_package_enabled(pkg) -> bool`](../src/drift/workspace_config.py#L115): Checks if package is active in workspace.

### [`git_utils.py`](../src/drift/git_utils.py) (Sub-Repository Git Management)
*   [`commit_repo_changes(repo_path, message, target_pkgs=(), repo_name="repo")`](../src/drift/git_utils.py#L85): Scoped `git add` and `git commit`.
*   [`is_repo_dirty(repo_path, target_pkgs=()) -> bool`](../src/drift/git_utils.py#L50): Checks porcelain status.

### [`lifecycle_hooks.py`](../src/drift/lifecycle_hooks.py) (Lifecycle Scripts & Python Hooks)
*   [`HookExecFlags`](../src/drift/lifecycle_hooks.py#L40): Execution flags (`dry_run`, `no_hooks`, `force`).
*   [`PackageHooks`](../src/drift/lifecycle_hooks.py#L110): Hook trigger handlers (`trigger_pre_source`, `trigger_post_render`, `trigger_pre_install`, `trigger_post_install`, `trigger_pre_update`, `trigger_post_update`, `trigger_pre_uninstall`, `trigger_post_uninstall`).

### [`exceptions.py`](../src/drift/exceptions.py) & Standard Exit Codes
*   [`InstallCollisionError`](../src/drift/exceptions.py#L31) (`ExitCode.COLLISION_ERROR = 5`): Raised on root escape, target pointing inside workspace, or symlinked parent directory.
*   [`ConfigError`](../src/drift/exceptions.py#L20) (`ExitCode.CONFIG_ERROR = 2`): Invalid TOML/YAML/JSON or DAG cyclic dependency.
*   [`RenderError`](../src/drift/exceptions.py#L26) (`ExitCode.RENDER_ERROR = 4`): Template compilation failure.
*   [`HookExecutionError`](../src/drift/exceptions.py#L45): Script execution timeout or non-zero returncode.

---

## 4. Key Domain Invariants & Rules

1.  **Source Template `dot-` Prefix Rule**:
    *   Files/directories in `src/<pkg>/` targeting hidden host paths **must** use `dot-` (`dot-bashrc` $\rightarrow$ `.bashrc`, `dot-config/` $\rightarrow$ `.config/`).
    *   Raw `.*` files in `src/` (except `.drift_ignore`) are strictly skipped with warning logs.
2.  **Template Engine Suffix Rule**:
    *   Format: `[filename].[engine_suffix].[target_ext]` (e.g. `dot-bashrc.envst.sh`, `home.mustache.nix`).
    *   Engine suffixes **cannot contain dots** (`.`). Reserved suffixes (`drift_package`, `drift_hook`, `drift_ignore`, `drift_workspace`, `drift_hooks`, `drift`) are prohibited.
3.  **Ignore Engine Invariants (`.drift_ignore`)**:
    *   Single file per package root. Nested `.drift_ignore` or `.driftignore` raise `ValueError`.
    *   Uses **PCRE Regex**, NOT glob patterns.
    *   `Group 1` (with `/`): matched against `/rel_path` (`^/sample\.txt$` for root).
    *   `Group 2` (no `/`): matched against `basename` (`\.bak$`).
    *   Hardcoded exclusions: `.drift/` internal directory and `MANAGED_CONFIG_FILES` are never deployed.
4.  **Collision Guard & Safety**:
    *   **Nominal & Canonical Target Check**: Target cannot be inside `drift_root`.
    *   **Parent Symlink Guard**: Parent cannot be a symlink into workspace root (`InstallCollisionError`).
    *   **Host Collisions**: Automatically backed up to `backup/<pkg>/overwritten/` (non-aborting).
5.  **Sentinel Drift Alignment Guard**:
    *   If `reverse-sync` leaves uncommitted changes in `install/` repo (host drift), deployer **halts immediately** to prevent silent overwrites. User must `drift adopt` or `--force`.

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
*   **Coding Conventions (`GEMINI.md`)**:
    *   Functional constructs (`filter`, `map`, comprehensions, generators) preferred over procedural loops.
    *   Decouple data gathering/resolution from execution.
    *   Return strongly-typed dataclasses (`*Result`), never raw dicts/tuples across interfaces.
    *   Always use relative markdown links across documentation.
