# 📁 Empty Folder Architecture & Dataflow Specification

## 1. Overview & Objectives

In modern system configurations, development environments, and dotfiles management, applications frequently require empty directory hierarchies to function properly (e.g., cache locations, log trees, socket paths, plugin drop-ins, and state directories).

Because Git tracks files (blobs) rather than filesystem directory inodes, preserving empty folders natively in Git requires a sentinel mechanism. Drift solves this through a dedicated stub file (`.drift_keep`) while maintaining a strict boundary invariant: **the sentinel exists solely within internal management repositories and never leaks into declarative source packages or onto the host operating system**.

### Core Invariants

1. **Boundary Isolation**: The `.drift_keep` stub file exists strictly inside internal control planes (`render/` and `install/`). It **never** exists in declarative source packages (`src/`) or on the live host filesystem (`$HOME`).
2. **Structural Fidelity**: An empty folder in `src/` is deployed to the host and stored as a pure, concrete directory (`mkdir -p`) with 1:1 structural fidelity.
3. **No Cross-Package Collisions**: Empty directories do not assert exclusive file ownership in [`src/drift/core/package_assertions.py`](../src/drift/core/package_assertions.py); multiple packages can declare and share the same empty directory path without conflict.
4. **Host Safety First**: Deleting or pruning empty folders on the host never results in recursive tree removal (`rm -rf`) if the directory has since been populated with user files.

---

## 2. Tier Topology & Invariant Matrix

The table below contrasts the representation and lifecycle of empty folders across all Drift state tiers:

| Tier | Directory Representation | `.drift_keep` Present? | Lifecycle / Implementation Primitive |
| :--- | :--- | :--- | :--- |
| **`src/`** (Declarative Source) | Concrete empty folder | **No** | Created by user or `drift adopt` ([`ensure_dir`](../src/drift/utils/file_ops.py)) |
| **`render/`** (Render Engine Output) | Empty folder containing `.drift_keep` | **Yes** (0 bytes) | Populated by [`render_or_copy_file`](../src/drift/primitives/render_package.py) |
| **`install/`** (Staged Git Repository) | Empty folder containing `.drift_keep` | **Yes** (0 bytes) | Synchronized via [`stage_repo`](../src/drift/primitives/stage_repo.py) and tracked in Git |
| **Host System** (Target Filesystem) | Concrete empty folder | **No** | Created via [`plan_folder_delivery`](../src/drift/core/folder_delivery.py) (`CREATE_DIR`) |

---

## 3. Forward Dataflow: Deploy & Install Pipeline

The forward pipeline deploys declarative empty directories from `src/` to the host system through reproducible render and install stages:

```
[src/pkg/empty_folder/]
        │
        ▼  (render_package: scans DirMode.ONLY_EMPTY_DIR)
[render/pkg/empty_folder/.drift_keep] (0-byte stub)
        │
        ▼  (stage_repo: 1:1 directory & metadata mirror)
[install/pkg/empty_folder/.drift_keep] (Git tracks stub)
        │
        ▼  (folder_delivery: filter_deployable_files translates stub -> parent dir)
[Host System: ~/empty_folder/] (pure directory created, .drift_keep omitted)
```

### Detailed Stage Steps

1. **Render Phase ([`src/drift/primitives/render_package.py`](../src/drift/primitives/render_package.py))**:
   - `list_folder_paths(src_pkg_dir, dir_mode=DirMode.ONLY_EMPTY_DIR)` discovers all empty leaf directories in the source package.
   - When [`render_or_copy_file`](../src/drift/primitives/render_package.py) encounters a directory entry, it verifies that the directory is empty and writes an empty `.drift_keep` file inside the rendered target directory.
2. **Stage Phase ([`src/drift/primitives/stage_repo.py`](../src/drift/primitives/stage_repo.py))**:
   - `compare_folder` mirrors all files and directories from `render/` to `install/` with 100% structural fidelity.
   - `.drift_keep` files are committed to the `install/` Git repository, enabling standard Git porcelain status diffs to detect additions, deletions, and modifications of empty directories.
3. **Deployment Planning ([`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py))**:
   - [`filter_deployable_files`](../src/drift/core/ignore.py) with `include_empty_dirs=True` scans deployable files. When it encounters `.drift_keep`, it records the parent directory as a deployable leaf node and filters out the `.drift_keep` file itself.
   - In [`plan_folder_delivery`](../src/drift/core/folder_delivery.py), empty directory leaf nodes generate `FileActionType.CREATE_DIR` actions.
   - Empty directories are **always** created as concrete directories on the host, never symlinked.
4. **State Tracking ([`src/drift/core/state.py`](../src/drift/core/state.py))**:
   - The directory path (e.g. `empty_folder`) is recorded in `deployed_files` inside `StateRegistry`.
5. **Cross-Package Ownership Check ([`src/drift/core/package_assertions.py`](../src/drift/core/package_assertions.py))**:
   - `assert_no_cross_package_conflicts()` invokes `filter_deployable_files(..., include_empty_dirs=False)`.
   - Empty directories are excluded from cross-package conflict validation, permitting multiple packages to declare and share common folder hierarchies.
6. **Uninstallation Planning ([`src/drift/primitives/uninstall_planner.py`](../src/drift/primitives/uninstall_planner.py))**:
   - Reference counting tracks shared directory declarations. When a package is uninstalled, reference counts for deployed paths are decremented.
   - When the reference count reaches 0, `DELETE_ITEM` (previously `DELETE_FILE`) is planned.
   - During execution, [`remove_file_or_empty_dir`](../src/drift/utils/file_ops.py) calls `rmdir()` on empty directories. If the directory has since been populated with files on the host, it safely skips removal with a warning to protect user files.

---

## 4. Backward Dataflow: Reverse-Sync (Host $\rightarrow$ `install/`)

When a user creates, populates, or deletes an empty directory inside a Fully-Controlled Directory (FCD) on the host, reverse synchronization reconciles the changes into `install/`:

```
[Host: Wild Directory Changes inside FCD]
        │
        ├── New Empty Folder Created
        │       ▼
        │   plan_folder_delivery(reverse_mode=True)
        │   -> FileActionType.CREATE_KEEP_FILE
        │   -> Writes install/<pkg>/<dir>/.drift_keep
        │
        ├── Previously Empty Folder Populated with Files
        │       ▼
        │   Ancestor pruning in plan_folder_delivery
        │   -> FileActionType.DELETE_ITEM
        │   -> Deletes install/<pkg>/<dir>/.drift_keep
        │
        └── Folder Deleted on Host
                ▼
            Orphan pruning in reverse mode
            -> FileActionType.DELETE_TREE
            -> Recursively purges install/<pkg>/<dir>/
```

### Key Components & Implementations

* **FCD Host Gathering ([`src/drift/primitives/reverse_sync.py`](../src/drift/primitives/reverse_sync.py))**:
  - `gather_single_fcd_reverse_sync_files()` scans host FCDs using `DirMode.ONLY_EMPTY_DIR` to capture wild empty directories.
  - To inspect already deployed files in `install/`, it uses `with_ignore_keep_file(False)` and delegates to [`resolve_deployable_paths_with_empty_dirs`](../src/drift/core/ignore.py) so `.drift_keep` files are mapped into parent directories.
* **Sentinel Stub Creation**:
  - `_inspect_leaf()` in [`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py) generates `CREATE_KEEP_FILE` when an empty folder candidate is being reverse-synced into `install/`.
* **Sentinel Pruning on Population**:
  - When new files are added inside a previously empty folder, ancestor directory inspection (`_inspect_directory_node`) in [`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py) detects the presence of child files and generates `DELETE_ITEM` to remove the obsolete `.drift_keep` stub.
* **Orphan Pruning & Host Protection Guard**:
  - When an entire directory is removed on the host, orphan pruning in [`_plan_orphan_prune`](../src/drift/core/folder_delivery.py) evaluates `context.reverse_mode`.
  - In `reverse_mode=True` (internal `install/` repo), it plans `DELETE_TREE` to purge the entire pruned directory tree.
  - In forward mode (`reverse_mode=False`, targeting host), it strictly restricts removal to `DELETE_ITEM` ([`remove_file_or_empty_dir`](../src/drift/utils/file_ops.py)), ensuring user-populated host directories are never deleted via `rm -rf`.

---

## 5. Drift Adoption: `drift adopt` (`install/` $\rightarrow$ `src/`)

The `drift adopt` command reconciles Git porcelain diffs from `install/` into declarative source packages in `src/`.

```
Git Porcelain Status in install/
        │
        ▼  get_package_drifts() [src/drift/primitives/adopt_repo.py]
  - Split .drift_keep renames -> deletions + additions
  - Translate .drift_keep additions -> parent directory additions
  - Translate .drift_keep deletions -> parent directory deletions
  - Strip corrupt / non-empty .drift_keep with loud warnings
        │
        ▼  execute_package_adopt()
  - handle_single_addition [directory]:
      ├── Pre-existing in src/   -> Skip cleanly
      ├── Collision with file    -> Conflict warning
      └── Clean addition         -> adopt_addition: ensure_dir() (NO .drift_keep)
  - handle_single_deletion [directory]:
      ├── Empty in src/          -> adopt_deletion: remove_tree()
      └── Non-empty in src/      -> Discard with warning (interactive confirmation guard)
        │
        ▼  Staging in install/
  git -C install/ add -- <pkg>/<rel_dir> (stages .drift_keep addition or deletion)
```

### 1. Translation & Sanitization ([`get_package_drifts`](../src/drift/primitives/adopt_repo.py))

* **Rename Splitting**: Renames involving `.drift_keep` (e.g. `mv dir_a/.drift_keep dir_b/.drift_keep`) are split into a deletion of `dir_a` and an addition of `dir_b`.
* **Path Translation**: Additions and deletions of `<path>/.drift_keep` are translated directly to `<path>`.
* **Corruption Guards**: Any `.drift_keep` that has non-empty contents or appears in `git status` modifications logs a loud `⚠️ [CORRUPT]` warning and is stripped from adopt planning.

### 2. Directory Addition Reconciliation ([`handle_single_addition`](../src/drift/primitives/adopt_repo.py))

* **Pre-existing Directory**: If the target directory already exists in `src/`, the addition is cleanly skipped and marked resolved.
* **File Collision**: If a regular file or template exists with the same name in `src/`, a conflict is raised.
* **Adoption**: [`adopt_addition`](../src/drift/primitives/adopt_repo.py) calls [`ensure_dir`](../src/drift/utils/file_ops.py) in the source package. The `.drift_keep` stub is **never** copied into `src/`.

### 3. Directory Deletion Reconciliation ([`handle_single_deletion`](../src/drift/primitives/adopt_repo.py))

* **Empty Directory**:
  - Non-interactive & interactive: [`adopt_deletion`](../src/drift/primitives/adopt_repo.py) removes the empty directory using [`remove_tree`](../src/drift/utils/file_ops.py).
* **Non-Empty Directory Guard**:
  - **Non-Interactive Mode**: Discards deletion with a warning log, preserving source files (the directory will be restored on next deployment).
  - **Interactive Mode**: [`_prompt_non_empty_directory_deletion_interactive`](../src/drift/primitives/adopt_repo.py) displays:
    1. Itemized list of existing source files that would be destroyed.
    2. Prominent warning on option `[2] Adopt deletion`.
    3. Explicit confirmation prompt `[y/N]` before performing `remove_tree`.

### 4. Git Staging Mechanics

When an adopted directory is resolved, `execute_package_adopt` stages the directory path via:
```bash
git -C <install_path> add -- <pkg>/<rel_dir>
```
Git natively handles both the addition (`A <pkg>/<rel_dir>/.drift_keep`) and deletion (`D <pkg>/<rel_dir>/.drift_keep`) through the directory path argument.

---

## 6. Verification & Test Suite

The empty folder lifecycle is validated by comprehensive unit and integration tests across the test suite:

* [`tests/test_render.py`](../tests/test_render.py): Validates `.drift_keep` generation during package render for empty directories.
* [`tests/test_stage.py`](../tests/test_stage.py): Validates 1:1 mirroring of `.drift_keep` into `install/` staging repo.
* [`tests/test_install.py`](../tests/test_install.py): Validates `filter_deployable_files` translation, `CREATE_DIR` delivery on host, omission of `.drift_keep` on host, and shared empty directory declarations.
* [`tests/test_uninstall.py`](../tests/test_uninstall.py): Validates reference-counted deletion and safe skipping of populated host directories.
* [`tests/test_reverse_sync.py`](../tests/test_reverse_sync.py):
  - `test_reverse_sync_fcd_empty_dir_creates_keep_file`: Verifies `CREATE_KEEP_FILE` in `install/` on new host empty folders.
  - `test_reverse_sync_fcd_empty_dir_deleted_on_host_prunes_keep_file`: Verifies `DELETE_TREE` in `install/` on host deletion.
  - `test_reverse_sync_fcd_empty_dir_populated_prunes_keep_file`: Verifies pruning `.drift_keep` when child files are added.
  - `test_orphan_prune_forward_mode_preserves_populated_host_directory`: Verifies `DELETE_ITEM` safety guard in forward mode.
* [`tests/test_adopt.py`](../tests/test_adopt.py):
  - `test_get_package_drifts_keep_file_addition_translation`: Verifies `.drift_keep` addition translated to parent dir.
  - `test_get_package_drifts_keep_file_deletion_translation`: Verifies `.drift_keep` deletion translated to parent dir.
  - `test_get_package_drifts_keep_file_rename_split`: Verifies keep file rename split into addition and deletion.
  - `test_get_package_drifts_corrupt_keep_file_warning`: Verifies corruption warning on modified/non-empty keep files.
  - `test_handle_single_addition_empty_dir_non_interactive`: Verifies clean directory creation in `src/` without stub file.
  - `test_handle_single_addition_empty_dir_preexisting_skips`: Verifies skipping pre-existing source directories.
  - `test_handle_single_addition_empty_dir_file_collision`: Verifies conflict handling on file/folder collision.
  - `test_handle_single_deletion_empty_dir_non_interactive`: Verifies empty directory deletion in `src/`.
  - `test_handle_single_deletion_non_empty_dir_non_interactive_discards`: Verifies non-interactive discard on non-empty source directories.
  - `test_handle_single_deletion_non_empty_dir_interactive_options`: Verifies interactive discard, confirmed adopt with `remove_tree`, and aborted adopt.
