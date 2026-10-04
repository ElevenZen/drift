# 🗺️ Drift Roadmap & Project Review

This document serves as both a critical review of the project's current state and a forward-looking roadmap. It merges unimplemented tasks from the original fragmented task list with new strategic directions identified during codebase review.

---

## Part I: Project Review

### Strengths

**The 4-tier transactional model is genuinely novel.**
Most dotfile managers are either "symlink farm" (stow, chezmoi's apply) or "copy and forget." Drift's `src/ → render/ → install/ → host` pipeline with independent Git repos at tier 2 and tier 3 is a real architectural innovation. The sentinel guard that halts deployment on detected host drift — forcing an explicit `adopt` or `--force` decision — solves the "GUI settings silently overwritten" problem that every other tool ignores.

**Clean primitive decomposition.**
The 16-primitive design with typed `*Result` returns is excellent. Each primitive is a self-contained unit with clear inputs, outputs, and side effects. The layered docstrings at the top of each module make call chains immediately graspable. This is better internal documentation than most production codebases.

**Zero-dependency core is a real asset.**
Pure stdlib Python with optional `[rich]` is a strong distribution story. The zipapp packaging, shell wrapper installer, and the fact that it works on Python 3.9+ without pulling in Click/Typer/Jinja2 in core mode is rare and valuable for the target audience — power users who are picky about what runs on their machines.

**The config system is surprisingly powerful.**
In-TOML variable stitching with DAG resolution, 6-tier precedence (Package > Workspace across all tiers), declarative `[env.secrets]` & `secrets.env` isolation with transient clean-room scope, Python hooks at both workspace and package level, `.envst.toml` meta-templates — this is a lot of expressive power without requiring users to learn a template language for configuration itself.

**Functional style is consistent and readable.**
The `filter`/`map`/comprehension-first style with extracted predicates (`is_zombie_package_dir`, `is_valid_file`, etc.) keeps data pipelines declarative. The separation of gathering vs. execution (e.g., `get_pending_delta_worklist` → `run_pending_delta_diff`) is consistently applied throughout.

### Weaknesses & Risks

**Complexity vs. audience mismatch.**
The mental model required — 4 directory tiers, 16 primitives, 6-tier variable precedence, three diff modes, render engines with DAG piping — is steep. chezmoi succeeds partly because its model is simple: source → target, with templates. Drift asks users to understand *why* there's a `render/` and an `install/` and why they're separate Git repos. The README is thorough but overwhelming; it reads more like a specification than an onboarding guide.

**Git subprocess dependency is deep and implicit.**
Despite "zero dependencies," the project fundamentally depends on Git being installed and functioning correctly. Every primitive touches `subprocess.run(["git", ...])`. There's no abstraction layer — Git commands are scattered across `git_utils.py`, `workspace_diff.py`, `render_package.py`, etc. If Git behaves unexpectedly (version differences, partial installs, Windows Git quirks), debugging is hard.

**The `symlink` vs `copy` duality adds cognitive load.**
Two deployment methods with different event ordering semantics — symlink content visible before `pre_update` vs. not — is a footgun. Users will hit subtle bugs where a hook script works with `copy` but breaks with `symlink` because file contents changed earlier than expected.

---

## Part II: Recently Completed Modernizations

### 1. Robust Exception Handling & Catch-Pass Audit
- **Narrowed Exception Boundaries**: Audited and fixed 21 `except Exception: pass` sites across 12 files. Replaced blanket catches with specific exceptions (`(OSError, AttributeError)`, `(socket.error, OSError)`, `(ValueError, TypeError)`) and explicit debug/warning logging, eliminating silent failures.
- **Fail-Fast Workspace Validation**: `assert_workspace_healthy` now immediately halts uninitialized (`NOT_FOUND`) and broken workspaces with actionable remediation hints (`drift init`, `drift repair`).
- **Rich Markup Escaping**: Fixed bracket injection in error boundaries where error strings containing `[...]` crashed Rich console rendering.

### 2. IPv4 & IPv6 Unified System Fact Probing
- **Dual-Stack IP Discovery**: Added dual-stack IPv4 and IPv6 interface enumeration using `libc.getifaddrs` on POSIX and unified `socket.getaddrinfo(..., AF_UNSPEC)` on Windows.
- **Streamlined Configuration**: Removed obsolete `probe_wan_ip` settings and re-injection overhead in favor of pure kernel routing table lookups (zero network packet emissions).

### 3. Modular Decomposition of `file_utils.py`
Monolithic `file_utils.py` was decomposed into single-responsibility, functionally pure utility modules:
- [`path_utils.py`](src/drift/utils/path_utils.py): Pure path manipulations (`expand_path`, `encode_dot_prefix`, `decode_dot_prefix`, `relative_path_between`, `is_relative_to`).
- [`file_inspect.py`](src/drift/utils/file_inspect.py): Read-only inspection (`is_binary_file`, `file_hash`, `normalize_newlines`, `contents_differ`, `permissions_differ`, `is_mode_only_change`, `tree_files`, `is_temp_file`, `is_concrete_dir`, `is_diff_candidate`, `find_symlink_ancestor`).
- [`file_ops.py`](src/drift/utils/file_ops.py): Destructive file operations with unified elevation (`copy_file`, `copy_symlink`, `copy_permissions`, `write_file`, `remove`, `remove_with_parents`, `copy_tree`, `move_tree`, `create_symlink`, `ensure_dir`, `assert_writable`, `prune_empty_parents`, `clear_readonly`).

### 4. Privilege Elevation & Process Execution Streamlining
- **Unified Subprocess Execution**: Merged `run_sudo_command` into `run_command(cmd, sudo=False)` with cross-platform elevation handling and automatic ANSI stripping.
- **Redundant Parameter Elimination**: Removed always-True arguments from privilege checks.

### 5. Domain Naming Standard (`ensure_` vs `assert_` vs `check_`)
Enforced a strict semantic prefix convention codified in [`AGENTS.md`](AGENTS.md):
- **`ensure_`**: Functions that **create or modify state** to guarantee a postcondition (`ensure_dir`, `ensure_rendered_file_hook_permissions`).
- **`assert_`**: **Read-only validation guards** that raise on failure without state modification (`assert_packages_hooks_exist`, `assert_hooks_exist`, `assert_no_legacy_workspace_config`, `assert_no_cyclic_dependencies`, `assert_no_cross_package_conflicts`, `assert_source_file_clean`, `assert_workspace_healthy`, `assert_install_pkg_dirs_clean`, `assert_writable`, `assert_git_repository_health`, `assert_repo_can_commit`, `assert_can_escalate`). Removed anti-pattern `force` arguments from assertions.
- **`check_`**: **Read-only inspection** returning result data values without raising or mutating state (`check_existing_workspace_status`, `check_patch_conflicts`).

### 6. Symmetrical 6-Tier Environment Architecture & `EnvConfig`
- **Symmetrical 4-Table Environment Model**: Both workspace and package configurations share 4 identical sub-tables under `[env]`: `[env.override]`, `[env.secrets]`, `[env.default]`, `[env.fallback]`.
- **Authoritative 6-Tier Precedence**: Fully unified macro hierarchy where **Package > Workspace** is strictly enforced within each macro tier:
  1. **Tier 1 (CLI)**: Ambient Process Environment & CLI Variables (`INITIAL_ENV` / `os.environ`)
  2. **Tier 2 (Override)**: Package `[env.override]` > Workspace `[env.override]`
  3. **Tier 3 (Facts)**: Package Facts (`drift_package_*`) > System Facts (`drift_*`)
  4. **Tier 4 (Secrets)**: Package `[env.secrets]` > Workspace `[env.secrets]` > `config/secrets.env`
  5. **Tier 5 (Default)**: Package `[env.default]` > Workspace `[env.default]`
  6. **Tier 6 (Fallback)**: Package `[env.fallback]` > Workspace `[env.fallback]`
- **Strongly Typed `EnvConfig` & `EnvResolve`**: `EnvConfig` encapsulates the 4 canonical environment tables (`override`, `secrets`, `default`, `fallback`) with pure serializer `to_env_dict()`. `EnvResolve` holds `current: EnvConfig`, `effective: EnvConfig`, and `effective_dict: Dict[str, str]` with pure DAG resolver `resolve_env_configs()`.
- **Decoupled Load vs. Runtime Execution**: Ingestion points (`from_dict`, `from_render_dir`, `from_install_dir`) accept `workspace_config` to compute effective environment tables and facts; runtime execution (`package_envs`) is completely decoupled and operates self-contained on `self.env_resolve.effective_dict` via `env_scope`.
- **Pure In-Memory Loading**: `load_workspace_config` does not mutate `os.environ` or execute side-effects during config construction; CLI initialization centralized in `prepare_cli_environment`.
- **Zero Backward Compatibility Burden**: Removed obsolete property wrappers (`packages`, `render_engine_config`, `drift_root_path`, `env_default`, `secrets`) and table parser shims across config classes.

### 7. Modular Package Assertion Guards & Batch Error Aggregation
- **Dedicated Assertion Module**: Extracted domain validation predicates and invariant guards into [`package_assertions.py`](../src/drift/primitives/package_assertions.py) (`assert_packages_hooks_exist`, `assert_packages_not_in_midway_state`, `assert_packages_install_dirs_exist`, `assert_packages_target_dirs_valid`, `assert_packages_target_dirs_writable`, `assert_no_cross_package_conflicts`, `assert_packages_stage_ready`, `assert_packages_install_ready`).
- **Standardized `packages` Attribute on Exceptions**: All assertion exceptions now carry a typed `packages: Sequence[str]` field (`HookMissingError`, `MidwayTransactionError`, `PackageInstallDirMissingError`, `TargetPermissionError`, `ConfigError`, `InstallCollisionError`, `CrossPackageCollisionError`).
- **Streamlined Collision Hierarchy**: Refactored `CrossPackageCollisionError` to inherit directly from `InstallCollisionError` with unified `packages` reporting and eliminated legacy aliases.
- **Batch Error Collection**: Assertion guards audit the entire package batch, aggregating all conflicting or failing package entries before raising a single unified exception.

### 8. Two-Phase Primitive 5 Decomposition & Generic Topological Sorting
- **Primitive 5 Decomposition**: Deconstructed monolithic `run_primitive_5_install` into two discrete, inspectable sub-stages:
  - `prepare_install(workspace_config, target_pkgs=(), config=None) -> InstallPlan`: Read-only pre-flight audit validating hook readiness, directory permissions, and cross-package path collisions.
  - `execute_install(workspace_config, plan) -> InstallResult`: State-mutating execution phase handling backups, physical deployment, hooks, and state registry tracking.
- **Strongly Typed Deployment Plans**: Introduced `InstallPlan` and `PackageInstallContext` dataclass models.
- **Redundant Loading Elimination**: Refactored `install_one_package_with_error_wrapping` and `install_one_package` to accept `PackageConfig` instances directly instead of repeatedly parsing from `install/`.
- **Generic Kahn's Algorithm Topological Sort**: Extracted pure, generic `topological_sort[T](graph: Mapping[T, Set[T]], ...)` into [`env_utils.py`](../src/drift/utils/env_utils.py) and streamlined `resolve_env_references` to accept `Mapping[str, str]` without redundant allocations.

### 9. Full-Staged Rollback Scope & Rollback-Eligible State Hygiene
- **Full Staged Batch Rollback Guarantee**: When Step 4a (`prepare_install`) fails, all packages staged in Step 3b (`pkgs_to_install`) are designated for rollback. Rolling back all staged packages cleans the uncommitted `install/` Git working tree for both `SYMLINK` and `COPY` packages, permanently preventing subsequent false `🛡️ System drift detected` aborts.
- **Crash Locks vs. Rollback-Eligible State Separation**:
  - `MIDWAY_TRANSACTION_STATES` (`{"staging", "installing"}`): Represents in-flight crashing states where disk mutations were interrupted.
  - `ROLLBACK_ELIGIBLE_STATES` (`{"staging", "staged", "installing"}`): Defines uncommitted transaction states eligible for `drift rollback` without requiring `--force`.
- **Early Midway Detection in Stage 1 Sentinel Check**: `deploy_repo.py` checks `get_rollback_eligible_packages` before reverse-sync, immediately detecting if a previous deployment left uncommitted `"staged"` packages.

### 10. Error Message Stacking Elimination
- **Targeted Exception Logging**: Deduplicated nested error propagation using custom exception tagging (`logged: bool`), logging details at the origin site and suppressing redundant outer repetition across command boundaries.

### 11. Fallback TOML Parser Enhancements
- **Extended TOML Support on Python < 3.11**: Enhanced standard-library fallback parser to support multiline basic (`"""`) and literal (`'''`) strings, inline tables (`{...}`), and array of tables (`[[...]]`) with zero external dependencies.

### 12. Inter-Package Dependency DAG & Lifecycle Ordering
- **Declarative Dependencies**: Added `dependencies` inline list and `[[package.dependencies]]` array-of-tables syntax with required (default) and optional semantics.
- **Topological Sorting Across Lifecycle**: Forward prerequisite ordering for staging (Primitive 4) and installation (Primitive 5); reverse topological ordering for multi-package uninstallation (Primitive 7) and unified rollback recovery (Primitive 8).
- **Pre-Flight Guards & Bypass Flags**: Added `assert_required_package_dependencies_exist`, `assert_no_cyclic_package_dependencies`, and `assert_no_broken_dependencies_on_uninstall`, bypassable via `--no-deps` (`no_deps=True`) and `--force`.
- **`UninstallConfig` & Ghost Package Purge**: Introduced typed `UninstallConfig`, graceful handling for missing install directories, and automatic ghost package purging from `state.toml` in `drift gc` (Primitive 9).

### 13. Unified Delivery Engine & Native Linker (`folder_delivery.py`)
- **Consolidated Delivery Architecture**: Replaced fragmented procedural file deployers and GNU Stow execution with a unified, pure Python delivery engine in [`folder_delivery.py`](../src/drift/core/folder_delivery.py), providing consistent lifecycle semantics across both relative `symlink` projection and atomic physical `copy` modes.
- **Declarative Action Planning**: Decomposed filesystem mutations into discrete, inspectable actions (`CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `UPDATE_PERMISSION`, `DELETE_FILE`, `SKIP_IDENTICAL`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`, `ENSURE_DIR`).
- **Action Type Consolidation**: Merged `REMOVE_DEPLOYED` into canonical `DELETE_FILE` across action models, formatting utilities, execution handlers, and uninstallation plans.
- **Native Symlink Pruning & Directory Lifecycle**: Added native directory-tree creation and parent-directory pruning without external binary dependencies.

### 14. High-Performance Permission Synchronization & Mode Differ Consolidation
- **Granular Permission Detection**: Enhanced `FolderDiff` to isolate mode-only changes into a dedicated sublist during single-pass directory comparison, avoiding expensive redundant byte comparisons.
- **Dedicated `UPDATE_PERMISSION` Action**: Mismatched file permission bits (`0o777`) on identical content now generate fast, metadata-only `UPDATE_PERMISSION` actions instead of `SKIP_IDENTICAL` or full file rewrites.
- **Unified Permission Comparison**: Merged duplicate mode checking helpers (`file_mode_differs`) into a canonical `permissions_differ(file1, file2) -> bool` in [`file_inspect.py`](../src/drift/utils/file_inspect.py) operating over POSIX mode bits (`0o777`), fixing non-executable mode bit change detection (`0o644` vs `0o600`).
- **Dead Code Cleanup**: Removed unused optional arguments (`src_dir`, `dst_dir`, `translate_mode`) from `FolderDiff.is_mode_only_change`.

### 15. Declarative Staging Architecture & Strongly Typed `StageResult` (`stage_repo.py`)
- **Declarative Stage Planning**: Unified Primitive 4 (`drift stage`) onto the declarative delivery engine with `PackageStagePlan` compiled via `plan_package_stage`, mirroring install and uninstall planning architectures.
- **Structural Fidelity Invariant**: Preserved 100% 1:1 structural fidelity between `render/` and `install/`, mirroring payload files and control plane metadata (`.drift/hooks/`, `.drift_ignore`, `drift_package.toml`).
- **Dead Code & Field Elimination**: Removed legacy `PackageStageChanges` and unused `deployable_changes` fields.
- **Strongly Typed `StageResult`**: Replaced raw dictionary returns from `run_primitive_4_stage_render_to_install` and `execute_stage_packages` with `StageResult`, featuring structured properties (`has_changes`, `packages_changed`, `plans`), human-readable terminal formatting (`format_text()`), and complete dictionary-compatible accessors (`keys()`, `values()`, `items()`, `__getitem__`, `__contains__`, `__len__`, `__bool__`).
- **Zero-Mutation Dry-Run Simulation (`--dry-run`)**: Added `dry_run` support to `execute_stage_packages` and `run_primitive_4_stage_render_to_install` (and CLI `drift stage --dry-run`), allowing users to simulate and inspect planned staging operations without modifying `install/` files or the `state.toml` registry.

### 16. Plan & Execute Decomposition for Resource Import (`add_resource.py`)
- **Decomposed Pipeline Architecture**: Refactored Primitive 11 (`drift add`) from a monolithic function into a two-phase pipeline mirroring Primitives 4, 5, and 7:
  - `prepare_add_resources(workspace_config, package_name, import_paths, dry_run=False, flags=None) -> AddResourcePlan`: Read-only pre-flight audit validating package source readiness (`assert_package_source_exists`), executing `pre_source` hooks, resolving worklists with reverse dot-prefix translation, asserting conflict freedom (`assert_no_import_conflicts`), and compiling declarative `FileAction` items (`CREATE_COPY`).
  - `execute_add_resources(workspace_config, plan: AddResourcePlan) -> AddResourceResult`: Physical execution and simulation handler. When `plan.dry_run=True`, simulates imports with zero filesystem mutations to `src/`; when live, delegates directly to `execute_delivery_actions(context, plan.actions)`, eliminating bespoke file-copying loops and single-file execution wrappers.
- **Unified Action Models**: Standardized `AddResourcePlan` to use canonical `FileAction(action_type=FileActionType.CREATE_COPY, src_path=src_on_system, dst_path=dest_path)` with non-optional `plan: AddResourcePlan` embedded directly in `AddResourceResult`.
- **Parameterless `format_text()` & CLI Integration**: Reused shared action formatters (`format_action_line`, `format_action_summary`) in `AddResourcePlan.format_text()`, and wired parameterless `AddResourceResult.format_text()` into `execute_add` for `drift add --dry-run` simulation output.

### 17. Resolved Path Simplification for `FileAction` & Context Decoupling
- **Explicit Resolved Paths at Planning Boundaries**: Replaced redundant and ambiguous path triples `(rel_path, system_target, source_path)` in `FileAction` with explicit, resolved `(src_path, dst_path)`. All path resolution and relative path translation (including dot-prefix encoding/decoding) now occur strictly during planning time.
- **File Action Execution Context Streamlining**: Removed `target_dir`, `install_dir`, `backup_pkg_dir`, and `backup_subfolder` from `FileActionExecutionContext`, reducing it to pure operational execution flags (`sudo: bool = False`, `resolve_symlinks: bool = True`). Execution primitives (`execute_single_action`, `execute_delivery_actions`) operate purely on self-contained `action.src_path` and `action.dst_path` without requiring runtime target or backup directory rebasing.
- **Unified `DeliveryInspectionContext`**: Extracted invariant planning parameters (`target_dir`, `source_dir`, `drift_root`, `install_method`, `is_first_time`, `backup_pkg_dir`, `backup_subfolder`) into a dedicated `DeliveryInspectionContext` dataclass with `resolve_backup_path()` helper. Rescued `plan_folder_delivery` and inspection sub-helpers (`inspect_ancestor_directories`, `_inspect_leaf_file`, `_inspect_symlink_leaf`, `_inspect_physical_file_leaf`, `_inspect_orphans`) from argument propagation sprawl.
- **Robust Backup Restoration Collision Safeguards**: Configured `plan_backup_restoration` to route collision backups directly to `BackupSubfolder.DELETED_FILES` with `is_first_time=True` via `DeliveryInspectionContext`. This guarantees any pre-existing host regular files colliding with restored files are safely preserved into `deleted_files/` via `BACKUP_OVERWRITE` instead of unbacked in-place overwrites (`UPDATE_COPY`), while preventing restored files from mutating the active `overwritten/` backup store.
- **Strict File Primitive Safety & Directory Collision Guards**: Hardened `copy_file`, `create_symlink`, `copy_symlink`, and `write_file` in [`file_ops.py`](src/drift/utils/file_ops.py) to raise `IsADirectoryError` if `dst` (or `src`) is an existing directory instead of silently destroying it with `remove()`. Directory-to-file and file-to-directory type transitions are handled explicitly and declaratively in higher-level planners (`plan_folder_delivery`, `plan_actions_from_folder_diff`).

### 18. Test Suite Expansion & Cleanliness
- Total passing tests maintained at **1142/1142 tests OK** with zero warnings, providing complete coverage across declarative staging, staging simulation (`--dry-run`), resource import planning (`prepare_add_resources`), physical/dry-run execution (`execute_add_resources`), permission synchronization, unified delivery actions, dependency DAG ordering, multiline TOML parsing, error boundaries, uninstallation plans, and incremental render DAG pipelines.

### 19. Incremental Render Subsystem (3-Phase Merkle DAG: Steps 1–4 Completed)
- **On-Demand 3-Phase Merkle DAG Architecture**: Replacing destructive `shutil.rmtree` cleansing with an incremental, topological Merkle DAG pipeline. Unchanged files remain untouched in-place with zero disk I/O, preserving file `mtime` and Git index status. (Steps 1–4 integrated Phase 1 Config and Phase 2 Hooks; Step 5 will integrate Phase 3 Payload).
- **Universal AST & PathNode Model**: Introduced [`PathNode`](src/drift/render/render_dag.py) as the common base for filesystem path nodes (`FileNode`, `DirectoryNode`, `UnknownPathNode`), strictly standardizing on `dst_path` and `src_path` and eliminating all legacy aliases (`output_path`, `input_path`, `file_path`). [`UnknownPathNode`](src/drift/render/render_dag.py) strictly takes `src_path` as unexpanded input with `dst_path=None`.
- **Per-Workspace In-Memory Cache (`RenderCache`)**: Injected into expansion and digestion contexts via `WorkspaceConfig.render_cache`. Features sub-microsecond nanosecond stat fingerprinting (`st_mtime_ns`, `st_size`) to automatically invalidate entries when source files are touched on disk without requiring manual cache clearing.
- **3-Bucket Self-Pruning Lockfile (`render.lock`)**: Persists cryptographic SHA-256 Merkle hashes in TOML format across three isolated buckets (`config_hashes`, `hook_hashes`, `payload_hashes`). Each phase replaces only its own bucket after digestion without clobbering other phases.
- **Multi-Phase Execution & Scoped Pruning**:
  - Phase 1 (PackageConfig): Compiles package configurations and stitches variables within workspace environment scope.
  - Phase 2 (Hooks): Compiles hook scripts within package environment scope, enforces `0o755` permissions, prunes obsolete hooks in `.drift/hooks/`, and triggers `probe` and `pre_source`.
  - Phase 3 (Payload): Expands dotfile payloads post-`pre_source`, handles templates and directory sentinels (`.drift_keep`), prunes obsolete payload files while strictly shielding the entire `.drift/` internal directory, and triggers `post_render`.

---

## Part III: Strategic Tier Ranking

The roadmap is prioritized into four execution tiers based on **architectural ROI**, **system safety guarantees**, and **adoption impact**:

### 🌟 Tier S: Game Changers & Core Value Proposition
* **`drift plan`**: Full dry-run visualization of render, stage, and install actions before modifying host files.
* **`drift doctor`**: Single diagnostic command validating workspace structure, Git repositories, hook permissions, and dependencies.
* **Asymmetric In-Repo Secret Encryption (Age / SSH-key)**: Zero-disk-leakage in-memory secret decryption into the compilation sandbox.
* **Smarter Rollback with WAL (Write-Ahead Log)**: Guarantees 100% reversible rollbacks for physical host filesystem side effects.
* **Unified Layered Import System (`[[imports]]`)**: Layered overlay mounting for package inheritance, file remounting, and external assets.

### 🚀 Tier A: High Value & Ergonomic Wins  

* **Package Dependency Declaration & Topological Install Ordering**: Packages declare explicit `dependencies` in `drift_package.toml` with required/optional semantics, enabling install-order guarantees and pre-flight validation.
* **Command Hooks & Arguments (`shlex`)**: Inline shell commands in hooks without creating wrapper files.
* **Passive File Triggers (Pacman-style Hooks)**: Directory-watching triggers executed once in a consolidated batch after deployment.
* **Audit `check=False` & Error Stacking**: Eliminates error masking and redundant multi-line error boxes.
* **Globbing in `packages.enable`**: Wildcard pattern matching (`desktop_* = false`, `server_* = true`) across machine classes.
* **`drift migrate`**: Seamless one-command migration from GNU Stow / chezmoi / yadm.
* **Native Package Distributions (PyPI, AUR, Homebrew, Nixpkgs)**: Standard package manager availability for release v1.0.

### ⚖️ Tier B: Solid Improvements & Scale Enhancements
* **Workspace-Wide Snippets & Partials (`config/partials/`)**: Boilerplate deduplication across Mustache, Jinja2, and shell templates.
* **Fleet & Server Orchestration Integration**: Non-interactive batch flags, JSON status objects, and Ansible roles for fleet automation.
* **Dynamic Password Manager CLI Provider**: Declarative integration with 1Password (`op`), Bitwarden (`bw`), and `pass`.
* **Incremental / Selective Rendering**: Manifest mtime and hash caching to make large workspace deploys sub-millisecond.
* **Git Abstraction Layer (`GitRepo` class)**: Centralizes Git subprocess calls for clean mocking and unified error translation.
* **Rendered Output Format Validation**: Verifies syntax of rendered JSON/YAML/TOML before staging.
* **Progress Meters & Batch Worklist Summaries**: Step-by-step progress counters (`[3/10]`) and upfront target discovery logs across CLI subcommands.

### 🔧 Tier C: Advanced & Specialized Patterns
* **Binary Deployment Strategy (drift + mise)**: Alternative binary management scheme with mise / nix home-manager.
* **Auto-Rollback for Critical Services**: Automatic revert on deployment failure (depends on WAL).
* **Reorder Render Before Reverse-Sync**: Resolves clean template updates directly in git history.
* **`drift revert`**: Fast undo of last deployment commit in the install repository.
* **Compilation Package Pattern**: Source subfolder builds via `pre_source` hooks.
* **Structured Hook Protocol**: Rich JSON output protocol over fd/file beyond integer exit codes.
* **Windows File-Lock Probing**: File accessibility probing for Windows service updates.

---

## Part IV: Detailed Task Breakdown by Category

### Category 1: Reliability & Error Handling

- [x] **Audit `check=False` subprocess calls across the codebase.** Many `subprocess.run(..., check=False)` calls ignore non-zero return codes. Each should either check and handle the return code, or document why it's intentionally ignored.

- [x] **Fix error message stacking.** Error messages repeat each other because inner error messages are appended to outer error messages, producing redundant multi-line errors. We can design like this: log the details at where original error happens, and for the outer errors, if the inner error is a drift custom error, then the error has been logged, so we do not need to log again, otherwise we should log the errors. That's my draft solution, the try-blocks should be analyzed to give a clear answer.

- [x] **Fix repeating "No packages are enabled" warnings in `drift repair`.**

- [ ] **Smarter rollback with WAL.** Current `drift rollback` does `git checkout` + reinstall, but doesn't rollback backup process or other filesystem side effects. Needs a Write-Ahead Log (WAL) that records each filesystem mutation during deployment, enabling precise reversal. This is prerequisite for reliable auto-rollback. (from old roadmap)

- [ ] **Auto-rollback option for critical services.** Add a `auto_rollback = true` option in package config for packages managing services that should not stop for long. If deployment fails midway, automatically revert. Depends on WAL-based rollback. (from old roadmap)  

- [ ] **Enhance TOML fallback parser** Use a lexer to parse tokens from toml string instead of scan and split.

- [ ] **Windows file-lock probing.** On Windows, probe file accessibility (can append/open) before installation because of file locks. Needs careful placement relative to `pre_update` hook — if user writes stop-service commands there, probing must happen after the hook. Design the failure semantics: does a failed probe count as deployment failure? How does the user restore service state? (from old roadmap)

### Category 2: User Experience & Onboarding

- [ ] **`drift doctor` — unified diagnostic command.** Currently `drift status`, `drift health`, `drift repair`, and `assert_workspace_healthy()` each probe different aspects. A unified `drift doctor` that runs everything — workspace structure, Git repo integrity, config validation, hook script permissions, target directory accessibility, and package health probes — in a single pass with a structured report. Think `brew doctor` or `rustup check`.

- [ ] **Granular progress tracking & step meters across CLI commands.** Add uniform step counters (e.g., `[3/10] Rendering package 'zsh'...`) and structured progress indicators to logging and CLI outputs across all multi-package subcommands (`render`, `stage`, `apply`, `deploy`, `status`, `gc`):
  - Print the resolved target package worklist upfront before executing operations so users have immediate visibility into batch scope.
  - Prepend step indexes `[i/N]` to starting and completion log messages for each package.
  - In Rich console mode, provide clean animated progress meters while preserving full detailed logs in `--debug` and non-interactive pipelines.  

- [ ] **Onboarding-first documentation restructuring.** Split the README into:
  - A 2-minute quickstart (`drift init`, add a file, `drift deploy`, see it work)
  - A concepts page (the 4 tiers, explained with one concrete example)
  - The current README content as a full reference

  The current README tries to do all three and ends up being 675 lines that feel like a spec sheet.

- [ ] **Sample workspace repository.** Add a `samples/` directory with real-world examples: frp config (networks among machines), package manager configs (mise / nix home-manager / linuxbrew), common shell setups. (from old roadmap)

- [ ] **`drift deploy` can call `drift health` at the end.** Post-deployment health verification as an opt-in step. (from old roadmap)  

- [ ] **Backup files listing** The files written to the backup folder can be printed out after the execution of a File Action List.

- [ ] **`drift revert` command.** Quickly revert installed version to a previous Git commit in the install repo, without full rollback semantics. A lighter-weight "undo last deploy." (from old roadmap)

### Category 3: Config & Rendering Enhancements

- [x] **Package dependency declaration & topological install ordering.** Declarative inter-package dependencies (`dependencies` inline list and `[[package.dependencies]]`) with topological staging/installation, reverse-order uninstallation, and lifecycle guards.

- [ ] **Globbing in `packages.enable` match.** Support glob patterns in workspace config package enablement (e.g., `desktop_* = false`). (from old roadmap)

- [ ] **Command hooks and hooks with arguments.** Use `shlex` to parse hook values. When the first word contains `/`, treat it as a file path; otherwise treat it as a command. Support argument passing. (from old roadmap)

- [ ] **Passive File Triggers & Post-Deployment Hook Aggregation (Pacman-style Triggers).** Allow packages to declare passive triggers that observe target directory paths rather than requiring explicit per-package invocation. When any deployed package writes, modifies, or deletes files within a watched directory (e.g. `~/.local/share/fonts/`, `~/.config/fish/completions/`, or a plugin directory):
  - The trigger is automatically queued and activated for that deployment transaction.
  - All activated passive triggers execute once in a consolidated batch at the end of the deploy pipeline (after all active package hooks have completed).
  - Eliminates redundant, repetitive reload scripts across individual plugin packages (e.g., calling `fc-cache` once instead of 15 times when installing multiple font packages).

- [ ] **Rendered output validation.** Render engines can optionally call external validators on rendered output to verify format correctness (`json`, `yaml`, `toml`, etc.) before staging. (from old roadmap)

- [ ] **Reorder render before reverse-sync in deploy pipeline.** If rendered output is identical to the reverse-synced result, the drift can be resolved with a simple Git commit in the install repo, avoiding unnecessary file operations. (from old roadmap)

- [ ] **Unified Layered Import System (`[[imports]]`).** Replace ad-hoc cross-package imports and external asset fetching with a unified layered overlay file system. A package declares an ordered list of `[[imports]]` from internal packages or external sources. The effective package source is stacked in declaration order with the package's local `src/<pkg>/` directory acting as the top-most override layer:
  ```toml
  [package]
  name = "web_service_prod"

  # 1. Base package inheritance (Import All "*")
  [[imports]]
  source = "package:web_service_base"

  # 2. Selective file remount from another internal package
  [[imports]]
  source = "package:common_configs"
  from = "dot-config/shared/logging.conf"
  to = "dot-config/service/logging.conf"

  # 3. External Git repository (cached in .drift/cache/external/)
  [[imports]]
  source = "git:https://github.com/ohmyzsh/ohmyzsh.git"
  to = "dot-oh-my-zsh"
  ref = "v1.2.0"
  refresh = "weekly"

  # 4. External archive / release binary
  [[imports]]
  source = "archive:https://github.com/junegunn/fzf/releases/download/v0.50.0/fzf-0.50.0-linux_amd64.tar.gz"
  from = "fzf"
  to = "dot-local/bin/fzf"
  executable = true

  # Local environment variables override base package variables
  [env.override]
  service_port = "8080"
  ```
  *Key Properties*:
  - **1:1 Pipeline Fidelity**: Layered resolution happens purely during render input preparation; `render/`, `install/`, and host symlinks remain 100% standard 1:1 packages.
  - **Native Drift Adoption**: When host files drift, `drift adopt` writes patches directly into the top layer (`src/<pkg>/`), leaving base packages untouched.
  - **DAG & Cycle Detection**: Internal package dependencies form a DAG validated with `assert_no_cyclic_dependencies`.

- [ ] **Workspace-Wide Snippets & Template Partials Directory (`config/partials/`).** Standardize a workspace-level search path for template partials and reusable snippets across render engines:
  - **Mustache**: Automatically registers `config/partials/` so templates can use `{{> header}}` or `{{> systemd/service}}` with automatic indentation preservation.
  - **Jinja2**: Adds `config/partials/` to the template loader search path for `{% include "header.j2" %}` and macro libraries `{% from "macros.j2" import service_unit %}`.
  - **Envsubst / Shell**: Exposes `$drift_partials_dir` so hook scripts and wrappers can easily `source "$drift_partials_dir/common.sh"`.

- [ ] **`drift plan` command.** Preview the actual files to be rendered, staged, and installed before executing. A dry-run visualization for the full deploy pipeline. Foundation for package dependency planning and cross-package import auditing. (from old roadmap)

- [ ] **Compilation package pattern.** Use package `[env.override]` / `[env.fallback]` to declare build flags (instead of scattering them across CLI `./configure` or `cmake -D...` invocations), render them into a `pre_source` hook that checks for existing build artifacts and compiles on-demand. Build artifacts output to a source subfolder, which the deploy pipeline installs to the target directory (e.g., `/opt` or `~/.local`). (from old roadmap)

### Category 4: Secret Management

- [ ] **Encrypted `secrets.env` for safe repo sync.** Support simple encryption of `secrets.env` so it can be safely committed and synced with the config repository. (from old roadmap)

- [ ] **Asymmetric in-repo secret encryption (Age / SSH-key).** Support encrypted secret files directly in the repository (e.g., `config/secrets.age.env`). Decrypt in-memory using Age keys or existing SSH private keys (`~/.ssh/id_ed25519`). Secrets fed directly into the transient compilation sandbox without ever being written to disk in plain text. (from old roadmap)

- [ ] **Dynamic password manager CLI provider.** Add a declarative `[secrets.provider]` hook in `drift_workspace.toml` for dynamic retrieval from 1Password CLI (`op`), Bitwarden CLI (`bw`), `pass`, or OS native keychains:
  ```toml
  [secrets]
  encrypted_file = "config/secrets.age.env"
  identity = "~/.ssh/id_ed25519"
  # Or dynamic provider:
  # provider_command = "op inject --in-file config/secrets.template.env"
  ```
  (from old roadmap)

### Category 5: Architecture & Performance

- [ ] **Git abstraction layer.** Extract all `subprocess.run(["git", ...])` calls into a thin `GitRepo` class that encapsulates repo path, handles error translation, and supports dry-run inspection. Benefits:
  - Centralized error handling for Git failures
  - Testable without filesystem Git repos (mock the class)
  - Future pluggable backends (libgit2, dulwich) for environments without Git CLI

- [ ] **Incremental / selective rendering.** File-level change detection (comparing `src/` mtimes or content hashes against a cached manifest) that skips unchanged packages. Makes `drift deploy` feel instant for the common case of "I changed one file in one package" in large workspaces.

- [ ] **Parallel package processing.** Packages are independent by design (separate source dirs, configs, target dirs), so rendering, staging, and deployment could run in parallel with `concurrent.futures.ThreadPoolExecutor`. Especially impactful for `drift deploy` across many packages.

- [ ] **Structured hook protocol (beyond exit codes).** Hooks writing JSON to a well-known fd or file, reporting warnings, skip-reasons, or output variables. For example, a `probe` hook could report *what* dependency is missing, not just "failed."

### Category 6: Distribution & Ecosystem

- [ ] **First-class `drift migrate` from chezmoi/stow/yadm.** A dedicated migration path from popular tools (chezmoi state → drift packages, stow directory → drift src/) to reduce adoption friction. The package-per-tool model maps cleanly to how people already organize stow directories.

- [ ] **Fleet & server orchestration integration (Ansible, Salt, Cloud-Init, GitOps).** Design Drift to operate seamlessly under external server management and orchestration tools:
  - **Headless & CI/CD Flags**: Standardize `--batch` / `--yes` (bypass interactive prompts), `--quiet` (minimal stdout for logging pipelines), and deterministic exit codes.
  - **Idempotency & Check-Mode Auditing**: Support `drift plan --json` and `drift status --json` with structured status objects (`status: CLEAN | CHANGED | DRIFTED`) so Ansible (`changed_when`, `check_mode`) or Terraform `local-exec` can audit state without side effects.
  - **Official Integration Recipes & Roles**: Provide reference Ansible roles (`drift_deploy`), Cloud-Init bootstrap scripts, and systemd pull-mode timers (GitOps cron for automated workstation/server synchronization).
  - **Host Fact & Role Tagging**: Leverage Drift's multi-tier variable precedence and host fact matching to allow a single dotfile repository to configure diverse server fleets by hostname, distro, arch, or injected role environment variables.

- [ ] **Binary deployment strategy with drift + mise.** Establish a drift + mise binary deployment scheme as a home-manager alternative. Consider a three-pronged approach (mise + nix home-manager + linuxbrew) to cover the software availability gap. (from old roadmap)

- [ ] **Native package manager distributions.** (from old roadmap)
  - **PyPI**: Automated publishing of `drift-dotfiles` via `twine`.
  - **Homebrew**: Custom Homebrew Formula / Tap (`brew install username/tap/drift`).
  - **AUR**: `PKGBUILD` script for Arch Linux (`yay -S drift-bin`).
  - **Nixpkgs**: Nix derivation to register Drift in the Nix ecosystem.
