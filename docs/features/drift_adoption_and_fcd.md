# 🔄 Bidirectional Drift Detection, Adoption (`drift adopt`) & Fully-Controlled Directories (FCD)

This document specifies how Drift detects live host configuration modifications via **Primitive 1 (`drift reverse-sync`)**, automatically resolves converged drifts during `drift deploy`, reconciles divergent changes back into declarative source templates via **`drift adopt`**, and manages **Fully-Controlled Directories (`fully_controlled_dirs`)**.

* **Core Architectural Overview**: [`docs/architecture.md`](../architecture.md)
* **Empty Folder & `.drift_keep` Dataflow**: [`docs/features/empty_folder_dataflow.md`](empty_folder_dataflow.md)
* **Related Subpackage Maps**: [`src/drift/primitives/AGENT_MODULE.md`](../../src/drift/primitives/AGENT_MODULE.md), [`src/drift/models/AGENT_MODULE.md`](../../src/drift/models/AGENT_MODULE.md)

---

## 1. Primitive 1: Reverse Sync (Host $\rightarrow$ `install/`)

Implemented in [`src/drift/primitives/reverse_sync.py`](../../src/drift/primitives/reverse_sync.py) (`run_primitive_1_reverse_sync`).

Rather than scanning the entire target host directory (e.g. `$HOME`), Reverse Sync performs targeted $O(N_{\text{pkg}})$ inspection across two scopes:

### 1.1 Tracked Package Files (`sync_tracked_files`)
* Compares `install/<pkg>/` against `target_directory` for files already tracked in `install/<pkg>/` (`src_only=True, translate_mode="forward"`):
  * **Modified on Host**: Reverse-copied from host back into `install/<pkg>/` (translating leading `.` back to `dot-`).
  * **Deleted on Host**: Symmetrically deleted from `install/<pkg>/`.
  * **Type Changes**: If a file became a directory on the host (or vice-versa), its new representation is synced into `install/<pkg>/`.

### 1.2 Scoped Fully-Controlled Directories (`sync_fully_controlled_dirs`)
* Subdirectories listed under `fully_controlled_dirs` in `drift_package.toml` (relative to `target_directory`) are owned in full by the package.
* Reverse Sync traverses those specific subdirectories on the host (respecting `.drift_ignore`):
  * **Untracked / Wild Files**: Copied into `install/<pkg>/` with reverse `dot-` prefix translation.
  * **Wild Empty Folders**: Scanned with `DirMode.ONLY_EMPTY_DIR`, planning `FileActionType.CREATE_KEEP_FILE` to write `<rel_dir>/.drift_keep` in `install/<pkg>/`.
  * **Populated Empty Folders**: Prunes obsolete `.drift_keep` stubs via `FileActionType.DELETE_ITEM`.
  * **Deleted Subtrees**: Uses `reverse_mode=True` to plan `FileActionType.DELETE_TREE` in `install/<pkg>/`.

---

## 2. Sentinel Drift Alignment & Convergent Drift Auto-Resolution

Implemented in [`src/drift/primitives/deploy_repo.py`](../../src/drift/primitives/deploy_repo.py) and [`src/drift/primitives/adopt_repo.py`](../../src/drift/primitives/adopt_repo.py).

During `drift deploy` (Stage 1):
1. **Render First**: Drift compiles `src/` $\to$ `render/` (Primitive 2) and commits `render/` (Primitive 3).
2. **Reverse Sync**: Drift runs Primitive 1 (`host` $\to$ `install/`).
3. **Convergent Drift Auto-Resolution (`resolve_and_commit_converged_drifts`)**:
   * If a user edited both the host file and the source template in `src/` identically (or a tool updated both), [`check_is_drifted_path_converged(install_pkg_dir, render_pkg_dir, rel_path)`](../../src/drift/primitives/adopt_repo.py) detects that the uncommitted file in `install/<pkg>/` is byte-for-byte and permission-identical to the newly rendered file in `render/<pkg>/`.
   * Converged paths are automatically staged and committed in `install/` (`execute_converged_drifts`) without blocking deployment or modifying `src/`.
4. **Divergent Drift Sentinel Halt**:
   * If divergent uncommitted changes remain in `install/` (Diff B), `drift deploy` **halts immediately** with [`DriftDetectedError`](../../src/drift/core/exceptions.py) (Exit Code `3`).
   * The user must either reconcile changes via `drift adopt` or explicitly override via `drift deploy --force` (which commits a recoverable **Drift Snapshot** into `install/.git` history before overwriting).

---

## 3. Bidirectional Drift Adoption (`drift adopt`)

Implemented in [`src/drift/primitives/adopt_repo.py`](../../src/drift/primitives/adopt_repo.py) and [`src/drift/models/adopt_models.py`](../../src/drift/models/adopt_models.py).

### 3.1 Preparation & Planning Pipeline (`prepare_adopt` $\to$ `plan_package_adopt`)
1. **Pre-Adoption Sync & Render**: Runs `reverse-sync` (unless `options.no_reverse_sync`) and scoped `render` on drifted packages (unless `options.no_render`).
2. **Scoped Git Cleanliness Guard**: Verifies that `src/<pkg>/` has no uncommitted Git modifications (unless `--force` is passed) so template patching has a clean rollback baseline.
3. **Drift Partitioning (`plan_package_adopt`)**:
   * Inspects `git -C install status --porcelain` (with rename detection).
   * Partitions converged paths into `PackageAdoptPlan.converged_files` and divergent paths into `additions`, `deletions`, `modifications`, and `renames`.
   * Pre-computes unified Git diffs (`mod_patches`, `rename_patches`) against `HEAD` and restores the `install/` Git index without mutation.

### 3.2 Reconciling Divergent Operations (`execute_package_adopt`)
Before modifying `src/<pkg>/`, triggers the `pre_source` hook (if configured and not already triggered). Each item returns an [`ItemAdoptOutcome`](../../src/drift/models/adopt_models.py) (`ADOPTED`, `SKIPPED`, or `CONFLICT`):

1. **Modifications (`handle_single_modification` / `patch_and_edit`)**:
   * Resolves the corresponding file or template in `src/<pkg>/` by matching active `RenderEngineRegistry` suffixes (e.g. mapping `install/shell/dot-bashrc` to `src/shell/dot-bashrc.envst.sh`).
   * Rewrites diff headers and applies the unified patch (`patch -u`) to the source file/template, synchronizing POSIX file permissions (`chmod`).
2. **Renames (`handle_single_rename` / `adopt_rename`)**:
   * Symmetrically renames the source file/template in `src/<pkg>/` while preserving its template engine suffix (e.g. `old.envst.sh` $\to$ `new.envst.sh`), applies any content patch, and syncs permissions.
3. **Additions (`handle_single_addition`)**:
   * Copies newly discovered files from `install/<pkg>/` (such as new files inside `fully_controlled_dirs`) into `src/<pkg>/`. For empty directories, creates the concrete folder via `ensure_dir` without copying `.drift_keep`.
4. **Deletions (`handle_single_deletion`)**:
   * **Adopt Deletion**: Removes the corresponding file or empty directory from `src/<pkg>/` and commits the deletion in `install/`.
   * **Discard Deletion (Restore File on Next Deploy)**: Commits the deletion in `install/` **without** deleting the template in `src/`. On the next `drift deploy`, Stage 2 sees the file present in `render/` and missing from `install/`, staging and re-deploying it back onto the host.

### 3.3 Interactive Guided Reconciliation (`drift adopt -i`)
* **Clean Patches**: Displays the unified diff and offers:
  1. `Adopt patch`
  2. `Adopt and Edit in Editor` (`$VISUAL` / `$EDITOR`)
  3. `Open Side-by-Side Reference` ([`fallback_side_by_side`](../../src/drift/primitives/adopt_repo.py) supporting `nvim -d`, `vim -d`, `code --wait --diff`, `emacs --eval '(ediff-files ...)'`)
  4. `Skip`
* **Conflicted Patches (When Template Syntax Blocks Clean Patch Application)**:
  1. `Over-render & Freeze`: Backs up the template to `.bak` and writes the live file content.
  2. `Merge Conflict Editor`: Applies `patch --merge` conflict markers and opens `$EDITOR`.
  3. `Side-by-Side Reference`: Opens source template and live `install/` file side-by-side.
  4. `Skip` (records file in `PackageAdoptResult.conflicted_files`).

### 3.4 FCD Untracked File Reconciliation Matrix (`Adopt` vs `Ignore` vs `Discard`)
When untracked files appear inside a `fully_controlled_dirs` path on the host and are swept into `install/<pkg>/`:

| User Choice | Action on `src/<pkg>/` | Action on `install/<pkg>/` | Effect on Host System |
| :--- | :--- | :--- | :--- |
| **Adopt** | Copies file to `src/<pkg>/` | Stages & commits file | Managed permanently by Drift |
| **Ignore** | Appends rule to `.drift_ignore` | Removes file (`git rm --cached` + unlink) | Untouched on host; ignored in future |
| **Discard / Delete** | Untouched (absent from `src/`) | Stages & commits file (`git add`) | Pruned as orphan (`BACKUP_PRUNE`) on next `deploy` |

### 3.5 5-State `AdoptStatus` Result Model
[`PackageAdoptResult`](../../src/drift/models/adopt_models.py) and [`AdoptResult`](../../src/drift/models/adopt_models.py) classify execution into [`AdoptStatus`](../../src/drift/models/adopt_models.py):
* `UP_TO_DATE`: No drifts detected (or only converged drifts auto-resolved).
* `SUCCESS`: All divergent drifts adopted cleanly (`conflicted_files` and `skipped_files` are empty).
* `PARTIAL`: At least one item adopted, with some items skipped or conflicted.
* `SKIPPED`: User skipped all items interactively with zero conflicts.
* `FAILED`: One or more items conflicted (`conflicted_files` non-empty) with zero items adopted.
