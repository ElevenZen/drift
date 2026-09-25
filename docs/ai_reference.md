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
| **P4** | `drift stage` | [`src/drift/primitives/stage_repo.py`](../src/drift/primitives/stage_repo.py) | `run_primitive_4_stage_render_to_install` | `List[PackageStageChanges]` |
| **P5** | `drift apply` | [`src/drift/primitives/install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_5_install_deployment` | `List[PackageInstallResult]` |
| **P6** | `drift install-commit` | [`src/drift/primitives/install_repo.py`](../src/drift/primitives/install_repo.py) | `run_primitive_6_commit_install_repo` | `InstallCommitResult` |
| **P7** | `drift uninstall` | [`src/drift/primitives/uninstall_repo.py`](../src/drift/primitives/uninstall_repo.py) | `run_primitive_7_uninstall_packages` | `UninstallResult` |
| **P8** | `drift rollback` | [`src/drift/primitives/rollback_repo.py`](../src/drift/primitives/rollback_repo.py) | `run_primitive_8_rollback_recovery` | `RollbackResult` |
| **P9** | `drift gc` | [`src/drift/primitives/workspace_gc.py`](../src/drift/primitives/workspace_gc.py) | `run_primitive_9_purge_workspace_garbage` | `GcResult` |
| **P10** | `drift new` | [`src/drift/primitives/new_package.py`](../src/drift/primitives/new_package.py) | `run_primitive_10_new_package` | `NewPackageResult` |
| **P11** | `drift add` | [`src/drift/primitives/add_resource.py`](../src/drift/primitives/add_resource.py) | `run_primitive_11_add_resources` | `AddResourceResult` |
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

### [`core/ignore.py`](../src/drift/core/ignore.py) (Ignore Engine & GNU Stow Rules)
*   [`DriftIgnore.load_from_dir(package_dir, is_source: bool) -> DriftIgnore`](../src/drift/core/ignore.py): Loads `.drift_ignore` PCRE patterns (from package root if `is_source=True`, else `.drift/.drift_ignore`; rejects nested ignore files).
*   [`DriftIgnore.for_install_root() -> DriftIgnore`](../src/drift/core/ignore.py): Creates ignore rules for `install/` root (`state.toml` guard).
*   [`ignore.match_path(rel_path) -> bool`](../src/drift/core/ignore.py): Evaluates PCRE regex patterns (2-group matching, hardcoded `.drift/` and `MANAGED_CONFIG_FILES` exclusion).
*   [`ignore.filter_deployable_files(install_pkg_dir) -> List[Path]`](../src/drift/core/ignore.py): Returns non-ignored deployable files.
*   [`ignore.create_stow_ignore_file(target_dir)`](../src/drift/core/ignore.py): Generates `.stow-local-ignore`.

### [`core/state_registry.py`](../src/drift/core/state_registry.py) (State Database & Manifests)
*   [`load_state_registry(path) -> StateRegistry`](../src/drift/core/state_registry.py): Loads `install/state.toml`.
*   [`registry.set_package_state(pkg, state, last_deployed=None)`](../src/drift/core/state_registry.py): Updates package state (`"installed"`, `"staging"`, `"installing"`, `"staged"`).
*   [`registry.sync_deployed_files(pkg, target_directory, install_method, redeploy=False, deployable_files=(), package_changes=None)`](../src/drift/core/state_registry.py): Updates target directory, install method, and deployed files manifest.
*   [`registry.get_target_migrated_from(pkg, current_target) -> Optional[Path]`](../src/drift/core/state_registry.py): Detects if package is migrating to a new destination.
*   [`registry.build_destination_ownership_map(exclude_packages=None) -> Dict[Path, str]`](../src/drift/core/state_registry.py): Builds destination ownership mapping across installed packages.
*   [`registry.remove_package(pkg)`](../src/drift/core/state_registry.py): Unregisters package from `state.toml`.

### [`utils/toml_utils.py`](../src/drift/utils/toml_utils.py) (TOML Parsing & Traversal)
*   [`get_nested_from(data, keys, default=None, required=False, is_table=False, config_source=None)`](../src/drift/utils/toml_utils.py): Retrieves nested values via dot-delimited key paths or key sequences with optional validation.
*   [`get_first_from(data, keys, default=None)`](../src/drift/utils/toml_utils.py): Retrieves the first matching key from alternative candidates.
*   [`validate_known_keys(data, known_keys, context="", message_prefix=None)`](../src/drift/utils/toml_utils.py): Enforces strict key allowlists on config mappings.

### [`config/workspace_config.py`](../src/drift/config/workspace_config.py) & [`config/package_config.py`](../src/drift/config/package_config.py)
*   [`load_workspace_config(drift_root, search_parents=True) -> WorkspaceConfig`](../src/drift/config/workspace_config.py): Loads layered workspace config, merges `.local.toml`, `.envst.toml`, `secrets.env`, and DAG variables into `EnvResolve`.
*   [`resolve_and_interpolate_workspace_config(data, secrets_file=None) -> Tuple[Dict[str, Any], EnvResolve]`](../src/drift/config/workspace_config.py): Pure in-memory topological DAG resolution for workspace `[env]` tables (`override`, `secrets`, `default`, `fallback`), returning interpolated dictionary and `EnvResolve`.
*   [`resolve_and_interpolate_package_config(data, package_name, workspace_config=None) -> Tuple[Dict[str, Any], EnvResolve]`](../src/drift/config/package_config.py): Pure in-memory topological DAG resolution and section interpolation for package configurations under 6-tier precedence (Package > Workspace within each macro tier; CLI context at Tier 1).
*   [`PackageConfig.from_dict(data, package_name, base_dir, source_files=(), workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Instantiates strongly-typed package config and computes effective environment tables (`compute_effective_envs(workspace_config)`).
*   [`PackageConfig.load_package_envs(overwrite=True)`](../src/drift/config/package_config.py) & [`PackageConfig.package_envs(overwrite=True)`](../src/drift/config/package_config.py): Loads / scopes pre-resolved `self.env_resolve.effective_dict` into `os.environ` directly without requiring runtime `workspace_config`.
*   [`load_package_config_from_source_dir(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads, transforms, merges, and validates package configuration from source directory.
*   [`load_package_config_from_render_dir(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads package configuration strictly from `render/<pkg>/` sandbox.
*   [`load_package_config_for_install(package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads package configuration strictly from `install/<pkg>/` state database.
*   [`load_package_config_rendered(package_toml_path, package_name, package_dir, workspace_config=None) -> PackageConfig`](../src/drift/config/package_config.py): Loads and parses package configuration with explicit `package_dir` and optional `workspace_config`.
*   [`config.is_package_enabled(pkg) -> bool`](../src/drift/config/workspace_config.py): Checks if package is active in workspace.

### [`utils/git_utils.py`](../src/drift/utils/git_utils.py) (Sub-Repository Git Management)
*   [`commit_repo_changes(repo_path, message, target_pkgs=(), repo_name="repo")`](../src/drift/utils/git_utils.py): Scoped `git add` and `git commit`.
*   [`has_uncommitted_modifications(repo_path, subpath=None) -> bool`](../src/drift/utils/git_utils.py): Checks porcelain status.

### [`primitives/adopt_repo.py`](../src/drift/primitives/adopt_repo.py) (Bidirectional Drift Adoption & Template Sync)
*   [`run_primitive_adopt_drifts(workspace_config, package_names, ...) -> AdoptResult`](../src/drift/primitives/adopt_repo.py): Entry point reconciling drifts across packages.
*   [`adopt_one_package_drifts(workspace_config, pkg, interactive, accept_conflicts, ...) -> PackageAdoptResult`](../src/drift/primitives/adopt_repo.py): Reconciles single-package additions, deletions, renames, and modifications.
*   [`patch_and_edit(src_file, patch_content, install_file, accept_conflicts, open_editor) -> bool`](../src/drift/primitives/adopt_repo.py): Applies patch, syncs permissions, and optionally launches `$EDITOR`.
*   [`adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, ...) -> Path`](../src/drift/primitives/adopt_repo.py): Symmetrically renames template file in `src/` matching engine suffix, applies patch, and syncs permissions.
*   [`fallback_side_by_side(src_file, install_file) -> bool`](../src/drift/primitives/adopt_repo.py): Visual split-screen diff in `$EDITOR` (`nvim`, `vim`, `code`, `emacs`).

### [`hooks/lifecycle_hooks.py`](../src/drift/hooks/lifecycle_hooks.py) (Lifecycle Scripts & Python Hooks)
*   [`HookExecFlags`](../src/drift/hooks/lifecycle_hooks.py): Execution flags (`dry_run`, `no_hooks`, `force`).
*   [`PackageHooks`](../src/drift/hooks/lifecycle_hooks.py): Hook trigger handlers (`trigger_pre_source`, `trigger_post_render`, `trigger_pre_install`, `trigger_post_install`, `trigger_pre_update`, `trigger_post_update`, `trigger_pre_uninstall`, `trigger_post_uninstall`).

### [`core/exceptions.py`](../src/drift/core/exceptions.py) & Standard Exit Codes
*   [`InstallCollisionError`](../src/drift/core/exceptions.py) (`ExitCode.COLLISION_ERROR = 5`): Raised on root escape, target pointing inside workspace, cross-package file conflict, or symlinked parent directory.
*   [`ConfigError`](../src/drift/core/exceptions.py) (`ExitCode.CONFIG_ERROR = 2`): Invalid TOML/YAML/JSON or DAG cyclic dependency.
*   [`RenderError`](../src/drift/core/exceptions.py) (`ExitCode.RENDER_ERROR = 4`): Template compilation failure.
*   [`HookExecutionError`](../src/drift/core/exceptions.py): Script execution timeout or non-zero returncode.

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
    *   Uses **PCRE Regex**, NOT glob patterns.
    *   `Group 1` (with `/`): matched against `/rel_path` (`^/sample\.txt$` for root).
    *   `Group 2` (no `/`): matched against `basename` (`\.bak$`).
    *   Hardcoded exclusions: `.drift/` internal directory and `MANAGED_CONFIG_FILES` (`.stow-local-ignore`) are never deployed.
4.  **Collision Guard & Safety**:
    *   **Nominal & Canonical Target Check**: Target cannot be inside `drift_root`.
    *   **Parent Symlink Guard**: Parent cannot be a symlink into workspace root (`InstallCollisionError`).
    *   **Host Collisions**: Automatically backed up to `backup/<pkg>/overwritten/` (non-aborting).
5.  **Sentinel Drift Alignment Guard**:
    *   If `reverse-sync` leaves uncommitted changes in `install/` repo (host drift), deployer **halts immediately** to prevent silent overwrites. User must `drift adopt` or `drift deploy --force` (which commits a drift snapshot into `install/` history before overwriting).
6.  **CLI Privilege & Sudo Guard**:
    *   Prohibits running CLI under `sudo` on user-owned workspaces to prevent target path mismatch (`$HOME`/`~` expanding to `/root`) and root-owned file corruption in `render/.git` and `install/.git`.
    *   Permitted only if running as true root (`SUDO_USER` unset) or workspace directory is root-owned (`uid == 0`). Elevated deployment is configured per-package via `sudo = true`.
7.  **Stage Structural Fidelity Invariant**:
    *   `install/<pkg>/` mirrors the structure and contents of `render/<pkg>/` with 1:1 fidelity.
    *   The only files in `install/` not originating from `render/` are dynamically generated stage artifacts (`DRIFT_GENERATED_FILES = (".stow-local-ignore",)`).
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
    *   **Cross-Package Destination Conflict Audit**: Before executing physical deployment, Drift audits all destination path claims across the batch and external installed packages in `state.toml`, reporting all intra-batch and inter-package path collisions together (`InstallCollisionError`).
    *   **Midway Transaction States**: `MIDWAY_TRANSACTION_STATES = ("staging", "installing")`. Packages in midway states require `--force` or `drift rollback` to proceed.

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
