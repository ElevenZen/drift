# 💾 Local State Database & Installation: `install/`

Drift separates staging from host delivery to ensure predictable, reversible, and auditable system changes.
This document introduces both the **Apply Verb (`drift apply`)** and the **Install Repository (`install/` zone)**.

---

## 🚀 Part 1: The Apply Verb (`drift apply`)

The `apply` verb executes the transition from the staged baseline in **Zone 3 (`install/`)** to the live host system in **Zone 4**. It applies changes surgically using either symlink projection or physical atomic copies.

### 1. Pre-Flight Assertion & Collision Guard
Before modifying any live file on your system, Drift runs a comprehensive pre-flight verification:
* **Zero Overwrite Protection**: Untracked or manual files on the host that would collide with deployment are safely backed up to `backup/<package>/overwritten/` before the operation.
* **Orphan Sweeping**: Files removed from package configuration are pruned from the host and swept safely into `backup/<package>/deleted_files/`.
* **Cross-Package Collision Detection**: Proactively verifies destination paths across all targeted packages, guaranteeing that no two packages claim or deploy to the same host path.
* **Nesting Prevention**: Blocks deployment if a target directory is equal to or nested inside your Drift workspace root, preventing destructive recursive symlink loops.
* **Escalation Verification**: Validates `sudo` capability before attempting deployment if any package target requires elevated root privileges.

### 2. Inter-Package Dependency Order & Topological Scheduling
Packages are not installed arbitrarily; they are sequenced through an inter-package dependency Directed Acyclic Graph (DAG):
* **Topological Ordering (Kahn's Algorithm)**: Drift resolves dependency relationships declared in `[package.dependencies]`.
* **Prerequisites Always First**: Prerequisite packages are staged, installed, and updated before dependent packages are processed.
* **Cycle Validation**: Detects and halts on cyclic dependencies with `ConfigError` before any files are touched or modified on the host.
* **Uninstall Reversal**: When removing packages (`drift uninstall`), Drift executes in strict reverse topological order (dependents uninstalled before prerequisites).

### 3. Install Methods (`symlink` vs. `copy`) & Dot-Prefix Translation
Drift supports two distinct deployment strategies configured at the workspace level or per-package (`install_method = "symlink" | "copy"`):
* **`symlink` (Relative Symlink Projection, Default on POSIX)**:
  * Creates relative symbolic links from the host target directory pointing into `install/<pkg>/` via Drift's native linker.
  * Ideal for standard user dotfiles (`~/.config`, `~/.zshrc`, Neovim configs) where edits should be reflected immediately.
* **`copy` (Physical Atomic Delivery, Default on Windows)**:
  * Executes atomic, safe physical file delivery directly to the host destination.
  * Ideal for system daemons, root directories (`/etc`), and services requiring strict update boundaries.
* **Dot-Prefix Translation (`dot-`)**:
  * In both `symlink` and `copy` deployment modes, Drift automatically translates repository `dot-` prefixes (e.g. `dot-config/` $\rightarrow$ `.config/`, `dot-zshrc` $\rightarrow$ `.zshrc`) during deployment, keeping source repositories clean and tool-friendly.

### 4. Update Hook Trigger Order for Different Install Methods
Because Drift separates template staging (Primitive 4: `render/` $\rightarrow$ `install/`) from host delivery (Primitive 5: `install/` $\rightarrow$ host), the timing of file content updates relative to lifecycle hooks depends on the package's `install_method`:

* **`copy` Method (Strict Event Ordering)**:
  1. **Staging (`render/` $\rightarrow$ `install/`)**: Updates `install/<pkg>/` while host files remain 100% untouched.
  2. **`pre_update` Hook**: Executes while host files are strictly at their prior version.
  3. **Physical File Delivery**: Atomic file copy and deletion operations execute on the host.
  4. **`post_update` Hook**: Executes after new files have been written to the host.
  * 👉 **Recommendation**: If your package configuration is watched by active system services or daemons (e.g. `systemd` user units with `inotify` watchers) that must be cleanly stopped in `pre_update` before configuration files change, use **`install_method = "copy"`**.

* **`symlink` Method (Symlink Pointer Semantics)**:
  1. **Staging (`render/` $\rightarrow$ `install/`)**: Modifies file contents in `install/<pkg>/`. Because host paths are symbolic links pointing into `install/<pkg>/`, content changes become visible on the host **before** `pre_update` runs.
  2. **`pre_update` Hook**: Executes.
  3. **Structural Symlink Adjustments**: New symlinks are created and deleted symlinks are pruned.
  4. **`post_update` Hook**: Executes.
  * 👉 **Recommendation**: Ideal for standard user dotfiles where instant reflection and symlink transparency are preferred.

### 5. Update Order of Dependencies in the Same Install Method
When multiple interdependent packages are updated within the same install method (or across methods), Drift processes them in strict forward topological order:
* **Sequential Package Lifecycle**: Each prerequisite package completes its full deployment pipeline:
  `pre_update` $\rightarrow$ file delivery $\rightarrow$ `post_update` $\rightarrow$ state registry commit
  ...before any dependent package begins its deployment lifecycle.
* **Reliable Dependency Invariant**: Dependent packages are guaranteed that all prerequisite binaries, libraries, and configuration files are fully updated, linked, and verified on the host system before their own `pre_update` or `post_update` hooks execute.

### 6. Dry-Run Deployment Simulation (`drift apply --dry-run`)
Pass `--dry-run` to simulate deployment without modifying host files, executing hooks, or altering `state.toml`:
* **Inspectable Execution Plan**: Evaluates pre-existing files, internal/foreign symlinks, directory structures, and orphan files from shallowest to deepest.
* **Deterministic Operation List**: Generates the complete ordered plan (`CREATE_SYMLINK`, `CREATE_COPY`, `UPDATE_COPY`, `SKIP_IDENTICAL`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`, `DELETE_ORPHAN`, `ENSURE_DIR`).
* **Machine-Readable Support**: Combine with `--json` (`drift apply --dry-run --json`) to retrieve the plan programmatically.

### 7. Why `drift install` Is Not a Command
New users frequently expect a `drift install` command due to the `install/` directory name and habits from traditional package managers (`brew install`, `apt install`). However, Drift intentionally does **not** provide a `drift install` command:
* **The "Stale Pipeline" Ambiguity**: In traditional package managers, `install` is an end-to-end operation (compile source $\rightarrow$ deploy to system). If `drift install` were an alias for `drift apply`, executing `drift install` after modifying templates in `src/` would **not** re-render or stage those changes—it would silently deploy stale files already residing in `install/`.
* **Explicit Mental Model**:
  * **Use `drift deploy [pkgs]`** for end-to-end deployment: Compiles templates (`render`), stages deltas (`stage`), and applies configurations to your host system. (This is almost always what you want).
  * **Use `drift apply [pkgs]`** for low-level primitive control: Applies the files that are already staged in the `install/` state database directly to your host without re-compiling templates.
If you accidentally run `drift install`, Drift intercepts the call with a helpful guidance stub explaining this distinction.

---

## 🏛️ Part 2: The Install Repository (`install/` Zone)

The `install/` directory represents the **Zone 3 Local State Database** in Drift's architecture. It is an independent Git repository that maintains the authoritative baseline of what is currently deployed on the host system.

### 1. Structure & 100% 1:1 Mirroring of `render/`
The `install/` repository mirrors the structure of `render/` with complete 1:1 structural fidelity:

```
install/
├── <pkg>/
│   ├── dot-config/             # Mirrored dotfile payload
│   │   └── nvim/
│   │       └── init.lua
│   ├── dot-zshrc
│   └── .drift/                 # Mirrored control plane (hooks, config, assets)
│       ├── drift_package.toml
│       ├── .drift_ignore
│       └── hooks/
├── state.toml                  # Explicit deployment state registry
└── .git/                       # Dedicated state Git repository
```

* **100% 1:1 Structural Fidelity**: Every package directory `install/<pkg>/` mirrors `render/<pkg>/` strictly (`DRIFT_GENERATED_FILES = ()`). All payload files, `.drift_ignore`, `drift_package.toml`, and control-plane directories (`.drift/hooks/`, `.drift/render/`) are preserved strictly 1:1.
* **State Registry Database (`state.toml`)**:
  * Authoritative record of package statuses (`installed`, `installing`, `failed`, `migrating`).
  * Records active target directories, deployment methods, and file manifests.
  * Enables surgical delta computation, target directory migration detection, and atomic rollbacks.

### 2. Capturing System Drift & Reverse-Sync
The `install/` repository acts as the fixed reference baseline between declarative source code and the mutable host filesystem:
* **Capturing System Drift**: When you edit a configuration file directly on the host (e.g. testing changes in `~/.config/nvim/init.lua`) or a tool writes new configuration into a Fully-Controlled Directory (FCD), the host state diverges from `install/`.
* **Drift Auditing (`drift status` & `drift diff`)**:
  * Drift compares active host files against `install/` to detect uncommitted modifications, missing files, or newly created files.
* **Reverse Synchronization (`drift adopt`)**:
  * Adopts live changes from the host and backports them through `install/` back into source templates in `src/`, closing the configuration feedback loop and preventing changes from being lost during future deployments.
* **State Commit Checkpointing (`drift install-commit`)**:
  * Commits deployed state changes into the `install/` Git repository history (automatically executed as step 5 of `drift deploy`).
