# Core Folder Delivery Engine: Architectural Modeling & Action Planning

This document provides a comprehensive design specification, algorithmic formulation, and call graph analysis for Drift's **Core Folder Delivery Planning Engine** ([`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py)).

---

## 1. Architectural Mission & Rationale

### 1.1 The Multi-Target Delivery Challenge
In dotfile management and system configuration orchestration, moving files from declarative repositories to active host destinations is fraught with subtle filesystem hazards:
* **Asymmetric Node Types**: A destination path on the host might be a concrete regular file, an internal symlink pointing into the workspace, an external foreign symlink, a broken symlink, an empty directory, or a populated directory.
* **Ancestor Inversion**: Deploying a leaf file like `~/.config/nvim/init.lua` may fail if `~/.config/nvim` is unexpectedly a flat file, a broken symlink, or missing altogether. Ancestors must be validated and created from shallowest to deepest before leaf files are touched.
* **Type Collisions**: If a package updates an asset from a flat file to a directory (or vice versa), naive copy/symlink operations fail or corrupt the tree unless the opposing node is cleanly cleared first.
* **Silent Overwrite vs. Backup Routing**: Overwriting unmanaged user files without a safety net leads to permanent data loss. The delivery engine must deterministically route collisions to designated backup subfolders (`overwritten_files` vs. `deleted_files`).
* **Bidirectional Dual-Mode Parity**: The exact same delivery algebra must be capable of forward deployment (`install/` $\to$ host) and reverse synchronization (host $\to$ `install/`), while handling dot-prefix encoding (`dot-config` $\leftrightarrow$ `.config`) transparently.

### 1.2 The Delivery Engine Mission
The folder delivery engine solves these challenges by acting as a **pure, declarative simulation and planning core**:
1. **Side-Effect Free Planning**: Separates inspection and planning from execution. It returns a deterministic, ordered list of [`FileAction`](../src/drift/core/file_action.py) objects.
2. **Exhaustive Collision Matrix**: Handles every permutation of source node type (file, symlink, directory) and target node state (missing, concrete file, symlink to source, foreign symlink, broken symlink, directory).
3. **Ancestor-First Reification**: Inspects and resolves parent directory collisions strictly from shallowest to deepest before evaluating leaf nodes.
4. **Manifest-Driven Orphan Pruning**: Compares candidate deployable files against historical `deployed_files` manifests to safely prune orphaned files without touching unrelated user data.
5. **Universal Primitive Support**: Serves as the shared delivery backbone for installation (`install_repo.py`), reverse sync (`reverse_sync.py`), staging (`stage_repo.py`), uninstallation (`uninstall_repo.py`), detach (`drift detach`), and dry-run planning (`plan_repo.py`).

---

## 2. Core Domain Primitives & Models

```text
DeliveryInspectionContext (Inputs, Policies & Masks)
│
▼
Folder Delivery Planning Pipeline
├── 1. assert_target_dir_outside_drift_root()
├── 2. inspect_ancestor_directories() -> (ENSURE_DIR, BACKUP_OVERWRITE)
├── 3. _inspect_leaf() -> (CREATE_SYMLINK, CREATE_COPY, UPDATE_COPY, SKIP_IDENTICAL)
└── 4. _inspect_orphans() -> (BACKUP_PRUNE, DELETE_ITEM)
│
▼
Sequence[FileAction] (Deterministic, Inspectable Execution Queue)
```

### 2.1 `DeliveryInspectionContext` ([`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py))

An immutable, frozen dataclass encapsulating all runtime parameters required for action planning:

| Field | Type | Description |
| :--- | :--- | :--- |
| `target_dir` | `Path` | Destination directory root on the host or inside `install/` |
| `source_dir` | `Path` | Source directory root (`render/`, `install/`, or host target) |
| `drift_root` | `Path` | Canonical workspace root path (used for safety boundary checks) |
| `install_method` | `InstallMethod` | `SYMLINK` (symbolic linking) or `COPY` (physical file copy) |
| `is_first_time` | `bool` | True if package has never been deployed (triggers first-time collisions) |
| `backup_pkg_dir` | `Optional[Path]` | Base directory for package backups (`<drift_root>/backup/<pkg>/`) |
| `backup_subfolder` | `BackupSubfolder` | Target subfolder (`overwritten_files` vs. `deleted_files`) |
| `source_dir_mask` | `Optional[Path]` | Virtual path mask used during sandboxed dry-runs (`drift plan`) |
| `reverse_mode` | `bool` | Inverts source $\leftrightarrow$ target semantics for reverse synchronization |
| `reinstall` | `bool` | Forces recreation of symlinks/copies even when contents match |

### 2.2 Bidirectional Path Translation Algebra

Drift dotfile packages store hidden dotfiles with a `dot-` prefix inside the repository (e.g. `dot-config/nvim` $\to$ `.config/nvim`) to prevent cluttering Git repository root tools. The context encapsulates this translation algebraically:

$$\text{translate_target_rel_path}(P) = \begin{cases} \operatorname{decode_dot_prefix}(P) & \text{if } \text{reverse_mode} = \text{true} \\ \operatorname{encode_dot_prefix}(P) & \text{if } \text{reverse_mode} = \text{false} \end{cases}$$

$$\text{translate_source_rel_path}(P) = \begin{cases} \operatorname{encode_dot_prefix}(P) & \text{if } \text{reverse_mode} = \text{true} \\ \operatorname{decode_dot_prefix}(P) & \text{if } \text{reverse_mode} = \text{false} \end{cases}$$

$$\text{translate_path}(P) \to (\text{source_dir} / \text{translate_source_rel_path}(P), \;\; \text{target_dir} / \text{translate_target_rel_path}(P))$$

When `source_dir_mask` is active (such as during `drift plan` sandboxing), `mask_source_path(P)` substitutes the virtual mask directory while preserving the relative translation:
$$\text{effective_source} = \begin{cases} \text{source_dir_mask} / \text{translate_source_rel_path}(P) & \text{if } \text{source_dir_mask} \neq \text{None} \\ \text{source_dir} / \text{translate_source_rel_path}(P) & \text{otherwise} \end{cases}$$

---

## 3. High-Level Call Graph & Execution Decomposition

The core delivery engine exposes five primary planning interfaces, each coordinating granular inspection helpers:

```text
plan_folder_delivery(context, deployable_files, deployed_files)
│
├── 1. Boundary Safety Assertion
│   └── assert_target_dir_outside_drift_root(target_dir, drift_root)
│
├── 2. Ancestor Directory Inspection (Shallowest -> Deepest)
│   └── inspect_ancestor_directories(context, deployable_files, handled_targets, actions)
│       └── For each unique ancestor:
│           ├── _check_has_backed_up_ancestor(ancestor_target, backed_up_ancestor_targets)
│           └── _inspect_single_ancestor()
│               └── _inspect_directory_node()
│                   ├── Case 2a: Missing or ancestor backed up -> ENSURE_DIR
│                   ├── Case 2b: Existing concrete dir -> UPDATE_PERMISSION (or prune .drift_keep)
│                   └── Case 2c: Blocked by file or symlink -> _plan_backup_or_delete() + ENSURE_DIR
│
├── 3. Leaf Files Inspection
│   └── For each rel_file in deployable_files:
│       └── _inspect_leaf(context, rel_file, handled_targets, actions, backed_up_ancestor_targets)
│           ├── Branch 3a: Source is Directory -> _inspect_directory_leaf()
│           │   ├── _inspect_directory_node()
│           │   └── If reverse_mode & empty -> CREATE_KEEP_FILE (.drift_keep)
│           │
│           ├── Branch 3b: Source is Symlink -> _resolve_leaf_symlink()
│           │   ├── Broken symlink? -> _plan_backup_or_delete()
│           │   └── Resolves -> Proceed with resolved source
│           │
│           ├── Branch 3c: Physical file deleted on host (reverse_mode) -> _plan_backup_or_delete()
│           │
│           └── Branch 3d: Concrete Leaf File Inspection -> _inspect_resolved_leaf_file()
│               ├── Sub-case 3d.1: Source == Target -> SKIP_IDENTICAL
│               ├── Sub-case 3d.2: Target missing or ancestor backed up -> _plan_file_creation()
│               ├── Sub-case 3d.3: Target is concrete directory -> _plan_backup_or_delete(tree) + create
│               ├── Sub-case 3d.4: Target is Symlink -> _inspect_symlink_leaf()
│               │   ├── Broken symlink -> BACKUP/DELETE + _plan_file_creation()
│               │   ├── Points to effective source:
│               │   │   ├── InstallMethod.SYMLINK & reinstall -> CREATE_SYMLINK
│               │   │   ├── InstallMethod.SYMLINK & normal -> SKIP_IDENTICAL
│               │   │   └── InstallMethod.COPY -> BACKUP/DELETE + CREATE_COPY
│               │   └── Points elsewhere -> BACKUP/DELETE (internal vs external) + create
│               │
│               └── Sub-case 3d.5: Target is Physical File -> _inspect_physical_file_leaf()
│                   ├── Source is symlink -> BACKUP/DELETE + _plan_file_creation()
│                   ├── InstallMethod.SYMLINK -> BACKUP/DELETE + CREATE_SYMLINK
│                   └── InstallMethod.COPY:
│                       ├── is_first_time -> BACKUP/DELETE + CREATE_COPY
│                       ├── contents_differ() -> UPDATE_COPY
│                       ├── permissions_differ() -> UPDATE_PERMISSION
│                       ├── reinstall -> UPDATE_COPY
│                       └── identical -> SKIP_IDENTICAL
│
└── 4. Orphan Files Reconciliation
    └── _inspect_orphans(context, deployable_files, deployed_files)
        ├── orphaned_files = set(deployed_files) - set(deployable_files)
        ├── Skip orphans that now exist as ancestor directories in deployable_files
        └── For each orphaned:
            └── _plan_orphan_prune()
                └── If target exists on host:
                    ├── reverse_mode & concrete dir -> DELETE_TREE
                    └── forward mode -> BACKUP_PRUNE or DELETE_ITEM
```

---

## 4. Specialized Delivery Interfaces & Pipeline Consumers

In addition to `plan_folder_delivery`, `folder_delivery.py` provides targeted planning functions utilized by Drift's primitives:

```text
Drift Primitive Callers
│
├── install_repo.py (Primitive 5: Install / Deploy)
│   ├── plan_folder_delivery() ──────> Plans forward installation (symlink or copy)
│   ├── assert_target_dir_outside_drift_root() ──> Validates deployment safety
│   └── plan_backup_restoration() ───> Rollback recovery on execution failure
│
├── reverse_sync.py (Primitive 1: Reverse Sync)
│   └── plan_folder_delivery(reverse_mode=True) ─> Plans host-to-install synchronization
│
├── stage_repo.py (Primitive 4: Stage Delta Compilation)
│   └── plan_actions_from_folder_diff() ─> Compiles render-to-install staging operations
│
├── uninstall_repo.py (Primitive: Uninstall & Detach)
│   ├── plan_file_removals() ────────> Plans reverse-order deletion of deployed files
│   ├── plan_symlink_conversions() ──> Plans detach (symlink -> physical copy)
│   ├── plan_backup_restoration() ───> Restores historical overwritten files
│   └── assert_target_dir_outside_drift_root()
│
└── plan_repo.py (Full-Cycle Dry-Run Engine)
    └── plan_folder_delivery(source_dir_mask=...) ─> Sandboxed preview simulation
```

### 4.1 Staging Diff Compiler (`plan_actions_from_folder_diff`)
Compiles a [`FolderDiff`](../src/drift/core/folder_diff.py) (comparing `render/<pkg>/` against `install/<pkg>/`) into an atomic list of actions:
1. **Deletions First (`reverse=True`)**: Deletes orphaned files from deepest to shallowest to cleanly clear obsolete paths before additions.
2. **Additions**: Plans `ENSURE_DIR` for directories, `CREATE_COPY` for files.
3. **Modifications**: Differentiates between content modifications (`UPDATE_COPY`) and permission-only adjustments (`UPDATE_PERMISSION`).
4. **Matches**: Generates `SKIP_IDENTICAL` actions for unchanged files, preserving execution visibility.

### 4.2 Backup Restoration (`plan_backup_restoration`)
Plans the safe restoration of historical files backed up in `backup/<pkg>/overwritten_files/`:
* Configures an isolated `DeliveryInspectionContext` with `InstallMethod.COPY` and `is_first_time=True`.
* Employs the full `plan_folder_delivery` pipeline to ensure intermediate directories are safely re-created and permissions are verified.
* Re-routes any secondary collisions into `backup/<pkg>/deleted_files/`, guaranteeing zero data loss during restore attempts.

### 4.3 Symlink Detach (`plan_symlink_conversions`)
Implements `drift detach <pkg>`, converting a live symlinked deployment into standalone physical copies:
* Filters `deployed_files` strictly to active symlinks pointing into `install/<pkg>/`.
* Generates paired actions: `DELETE_ITEM` (unlinking the symbolic pointer) followed immediately by `CREATE_COPY` (copying the canonical file from `install/<pkg>/`).

### 4.4 Clean File Removal (`plan_file_removals`)
Used during package uninstallation:
* Resolves all paths recorded in the `deployed_files` manifest.
* Sorts paths in **reverse lexicographical order** (`sorted(deployed_files, reverse=True)`) so that child files are deleted before parent directory cleanup.
* Generates `DELETE_ITEM` actions for all present files and symlinks.

---

## 5. Decision Matrices & Collision Resolution

### 5.1 Leaf Node Collision Matrix

When delivering a leaf file from source to target, the engine evaluates the physical state of both endpoints:

| Source Node | Target Node State | Action Planned | Reason Recorded |
| :--- | :--- | :--- | :--- |
| **File** | Missing / Non-existent | `CREATE_SYMLINK` / `CREATE_COPY` | Initial file creation |
| **File** | Ancestor backed up | `CREATE_SYMLINK` / `CREATE_COPY` | Target recreated in new directory |
| **File** | Directory (blocking) | `BACKUP_OVERWRITE` (tree) $\to$ `CREATE_*` | Directory blocking file |
| **File** | Symlink $\to$ Correct Source | `SKIP_IDENTICAL` (or `CREATE_SYMLINK` if `reinstall`) | Symlink already points to source |
| **File** | Symlink $\to$ Workspace Internal | `BACKUP_OVERWRITE` $\to$ `CREATE_*` | Conflicting internal symlink |
| **File** | Symlink $\to$ Foreign External | `BACKUP_OVERWRITE` $\to$ `CREATE_*` | Colliding external symlink |
| **File** | Broken Symlink | `BACKUP_OVERWRITE` $\to$ `CREATE_*` | Broken symlink collision |
| **File** | File (First-Time Deploy) | `BACKUP_OVERWRITE` $\to$ `CREATE_*` | Pre-existing file collision |
| **File** | File (Content Differs) | `UPDATE_COPY` (in copy mode) | File content updated |
| **File** | File (Permissions Differ) | `UPDATE_PERMISSION` | Permissions differ (`dst_mode` $\to$ `src_mode`) |
| **File** | File (Content Matches) | `SKIP_IDENTICAL` (or `UPDATE_COPY` if `reinstall`) | File content matches |
| **Symlink** | Broken Symlink | `BACKUP_OVERWRITE` / `DELETE_ITEM` | Broken symlink on host |
| **Directory** | Missing / Non-existent | `ENSURE_DIR` | Directory creation |
| **Directory** | File / Symlink (blocking) | `BACKUP_OVERWRITE` $\to$ `ENSURE_DIR` | File / symlink blocking directory |
| **Empty Dir** | Missing (in reverse mode) | `CREATE_KEEP_FILE` | Track empty folder with `.drift_keep` |

---

## 6. Mathematical Invariants & Safety Guarantees

### 6.1 Order Invariants
The planning engine enforces four strict ordering invariants across all generated `FileAction` lists:

1. **Ancestor Topological Hierarchy**:
   Ancestors are inspected and planned strictly from shallowest to deepest:
   $$\operatorname{depth}(A) < \operatorname{depth}(B) \implies A \text{ precedes } B \text{ in } \text{actions}$$
   This guarantees that if a parent directory is blocked by a file or foreign symlink, the parent directory is backed up and re-created before any child operations execute.

2. **Additions Precede Orphan Pruning**:
   All directory creations, file creations, and content updates are planned **before** orphan deletions:
   $$\forall a \in \text{actions}_{\text{add/update}}, \; \forall b \in \text{actions}_{\text{orphan\_prune}} \implies \operatorname{index}(a) < \operatorname{index}(b)$$
   This ensures that if an application replaces a set of files with new ones, the new files are in place prior to cleanup.

3. **Reverse Lexicographical Deletion**:
   Orphan pruning, diff-based deletions, and uninstallation removals are planned in reverse alphabetical and path depth order:
   $$\operatorname{depth}(P_1) > \operatorname{depth}(P_2) \implies P_1 \text{ is deleted before } P_2$$
   This prevents directory removal errors where a parent directory attempt occurs while child entries remain.

4. **Staging Deletions Precede Additions**:
   In `plan_actions_from_folder_diff`, deletions are planned **before** additions:
   $$\forall d \in \text{actions}_{\text{deleted}}, \; \forall a \in \text{actions}_{\text{added}} \implies \operatorname{index}(d) < \operatorname{index}(a)$$
   This guarantees that if a path transitions from a regular file to a directory (or vice versa), the old node type is unlinked before the new node type is created.

### 6.2 Zero-Mutation Safety Invariant
The delivery planning function $\Phi$ is strictly pure and inspectable:
$$\Phi(\text{context}, \text{deployable}, \text{deployed}) \to \text{actions}$$
Execution of $\Phi$ performs **zero filesystem mutations**. No files are created, copied, modified, or deleted until the returned `actions` are explicitly dispatched to [`execute_delivery_actions()`](../src/drift/core/file_action.py#L321).

### 6.3 Self-Referential Deployment Guard
To prevent catastrophic circular symlinks or recursive workspace overwrites, `assert_target_dir_outside_drift_root` verifies:
$$\operatorname{resolve}(\text{target_dir}) \not\subseteq \operatorname{resolve}(\text{drift_root})$$
If a user misconfigures `target_directory = "."` or points a symlink inside `.drift/` or `install/`, the engine halts immediately with [`InstallCollisionError`](../src/drift/core/exceptions.py), protecting repository integrity.

### 6.4 Forward Pruning Safety Boundary
When pruning orphaned files in forward delivery mode, the engine strictly uses `DELETE_ITEM` (or `BACKUP_PRUNE`), which executes via [`remove_file_or_empty_dir`](../src/drift/utils/file_ops.py). 
* `DELETE_TREE` is strictly prohibited during forward deployment.
* If an orphaned file's parent directory contains unmanaged user files, the directory is preserved on the host.
* `DELETE_TREE` is permitted only in `reverse_mode=True`, where the target is Drift's private internal state database (`install/<pkg>/`).
