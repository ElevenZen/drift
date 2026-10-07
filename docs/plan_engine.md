# Full-Cycle Dry-Run Engine: Architectural Modeling & Transactional Planning

This document provides a comprehensive design specification, mathematical modeling rationale, and implementation architecture for Drift's **Full-Cycle Dry-Run & Planning Engine** ([`src/drift/primitives/plan_repo.py`](../src/drift/primitives/plan_repo.py)), powering `drift plan` and `drift deploy --dry-run`.

---

## 1. Architectural Mission & Rationale

### 1.1 The Blind Deployment Problem in Dotfile Management
Traditional dotfile managers and configuration orchestrators suffer from acute dry-run limitations:
* **Shallow Dry-Runs**: Tools like GNU Stow or simple symlink managers only check if a symlink exists. They have no awareness of template compilation, variable rendering, or lifecycle hooks.
* **Partial Pipeline Simulation**: Many tools can dry-run template rendering (e.g. `chezmoi execute-template`) or diff state (e.g. `ansible --check`), but cannot simulate the entire chain from source templates through intermediate staging repositories down to active host filesystem projections.
* **Ambient Repository Pollution**: In multi-stage Git-backed architectures, naive dry-run attempts often touch intermediate repositories (`render/` or `install/`), dirtying the Git working tree, generating untracked files, or mutating lockfiles.
* **Silent Host Overwrite Hazards**: If a user hot-edits a file directly on the host system (e.g. via a GUI preferences menu or emergency edit), running deployment without prior drift visibility results in silent, catastrophic overwrites.

### 1.2 The Full-Cycle Dry-Run Mission
Drift's planning engine solves these problems by providing an **end-to-end, zero-mutation simulation** of the complete deployment pipeline:
1. **Four-Phase Completeness**: Simultaneously simulates Phase 0 (Host Drift Inspection), Phase 1 (Sandbox Template Compilation), Phase 2 (Delta Staging), and Phase 3 (Physical Host Installation).
2. **Ephemeral Sandbox Isolation**: Isolates template compilation in a disposable temporary directory (`tmp_sandbox/render/`), guaranteeing that real `render/.git` and `install/.git` repositories remain 100% untouched.
3. **Path Masking for Symlink Identity**: Bridges the path divergence between temporary sandbox files and canonical `install/` paths, ensuring existing host symlinks evaluate as `SKIP_IDENTICAL` rather than triggering false conflict overwrites.
4. **Decoupled Fault Isolation**: Traps errors per-package across render, stage, and install phases so that a single misconfigured package does not obliterate the visibility of healthy sibling packages.
5. **Batch Conflict Auditing & Pipeline Failure Guidance**: Detects intra-batch cross-package collisions upfront and prominently warns users if a live `drift deploy` would fail.

---

## 2. Mathematical Modeling & Invariants

### 2.1 Formal Pipeline Decomposition
Let $\mathcal{W}$ represent the workspace state, consisting of the declarative source templates $\mathcal{S}$, intermediate render state $\mathcal{R}$, canonical install state $\mathcal{I}$, active host filesystem $\mathcal{H}$, and state registry $\mathcal{M}$:
$$\mathcal{W} = (\mathcal{S}, \mathcal{R}, \mathcal{I}, \mathcal{H}, \mathcal{M})$$

The live deployment workflow is a composition of four state-mutating transitions:
$$\mathcal{W}_{t+1} = (\text{Install} \circ \text{Stage} \circ \text{Render} \circ \text{Sentinel})(\mathcal{W}_t)$$

The planning engine models this sequence as a pure, side-effect-free simulation function $\Pi$:
$$\Pi(\mathcal{W}_t) \to \text{WorkspaceDeployPreview}$$

### 2.2 Strict Invariant Guarantees
During the execution of $\Pi$, the following zero-mutation invariants are strictly preserved:

* **Source Invariance**:
  $$\Delta \mathcal{S} = \varnothing$$
* **Render Repository Git Invariance**:
  $$\Delta \mathcal{R}_{\text{git}} = \varnothing, \quad \Delta \mathcal{R}_{\text{working_tree}} = \varnothing$$
* **Install Repository Git Invariance**:
  $$\Delta \mathcal{I}_{\text{git}} = \varnothing, \quad \Delta \mathcal{I}_{\text{working_tree}} = \varnothing$$
* **Host System Invariance**:
  $$\Delta \mathcal{H} = \varnothing$$
* **Registry Invariance**:
  $$\Delta \mathcal{M} = \varnothing$$

### 2.3 The Symlink Path Mask Problem & Translation Algebra

In symlink deployment mode, existing files on the host $\mathcal{H}$ are symbolic links pointing directly to the canonical install directory:
$$h \in \mathcal{H} \implies \operatorname{readlink}(h) = \mathcal{I}_{\text{canonical}} / \text{pkg} / \text{file}$$

During a dry-run plan, candidate files are generated inside an ephemeral sandbox directory:
$$\text{candidate_file} = \mathcal{R}_{\text{sandbox}} / \text{pkg} / \text{file}$$

If a naive file delivery planner compares $\operatorname{readlink}(h)$ against $\text{candidate_file}$, the equality fails:
$$\operatorname{readlink}(h) \neq \text{candidate_file}$$

Without path masking, the delivery engine would classify every existing, perfectly synchronized symlink on the host as a **Conflict**, generating false `BACKUP_OVERWRITE` and `CREATE_SYMLINK` actions instead of `SKIP_IDENTICAL`.

#### The Path Mask Solution
Drift introduces the `source_dir_mask` primitive in [`src/drift/core/folder_delivery.py`](../src/drift/core/folder_delivery.py). When inspecting a symlink on the host, the inspection algebra evaluates:
$$\operatorname{effective_source} = \mathcal{I}_{\text{mask}} / \text{rel_path} \quad \text{if } \mathcal{I}_{\text{mask}} \neq \text{None} \quad \text{else } \text{candidate_file}$$

$$\text{is_identical} \iff \operatorname{readlink}(h) = \operatorname{effective_source}$$

This guarantees:
1. Host symlinks are evaluated against their intended permanent destination ($\mathcal{I}_{\text{canonical}}$).
2. Content hashing and permission validation inspect the freshly rendered candidate in $\mathcal{R}_{\text{sandbox}}$.
3. Planned `FileAction`s report resolved canonical paths ($\mathcal{I}_{\text{canonical}} \to h$).

---

## 3. Four-Phase Architecture & Call Chain

The planning pipeline is decomposed into four discrete, testable phases coordinated by [`preview_deploy`](../src/drift/primitives/plan_repo.py) (and exposed via [`run_primitive_plan`](../src/drift/primitives/plan_repo.py)):

```text
preview_deploy(workspace_config, target_pkgs, options: DeployOptions)
│
├── Step 1: Discover active target packages from src/ (filter_source_packages_by_target)
├── Step 2: Check midway transaction crash locks in install/state.toml (check_midway_states)
├── Step 3: Load source PackageConfig metadata (load_source_package_metadata, dry_run=True)
├── Step 4: Resolve dependency DAG & topological order (resolve_target_package_order)
│
├── Phase 0: Audit Host Drift (audit_host_drift)
│   └── prepare_reverse_sync(missing_ok=True) -> Dict[str, PackageReverseSyncPlan]
│
├── Ephemeral Sandbox Context (enter_plan_sandbox)
│   │
│   ├── Phase 1: Sandbox Template Compilation (execute_sandbox_render_phase)
│   │   ├── RenderCache & Merkle lockfile validation (render.lock)
│   │   ├── render_package(sandbox_workspace_config, pkg_dir, options)
│   │   └── rebase action paths to canonical render/ -> (render_plans, render_errors)
│   │
│   ├── Phase 2: Sandbox Staging Planning (execute_sandbox_stage_phase)
│   │   ├── plan_package_stage(pkg, install_base, render_base)
│   │   └── rebase action paths to canonical render/ -> (stage_plans, stage_errors)
│   │
│   └── Phase 3: Physical Delivery Planning (execute_sandbox_install_phase)
│       ├── Batch Guard: assert_no_cross_package_conflicts(eligible_pkgs)
│       └── Package Loop (with dependency checks turned OFF):
│           ├── assert_packages_install_ready(no_deps=True)
│           ├── PackageInstallContext.from_package(install_workspace_config, ...)
│           └── plan_package_install(context, deployable_files, deployed_files, ...) -> (install_plans, install_errors)
│
└── Phase 4: Result Assembly & Presentation
    ├── assemble_package_deploy_preview -> Dict[str, PackageDeployPreview]
    └── WorkspaceDeployPreview(status, format_text, to_dict, global_errors, drift_warnings)
```

---

### 3.1 Phase 0: Non-Destructive Host Drift Audit
Before compiling templates, Drift inspects active host files using [`prepare_reverse_sync`](../src/drift/primitives/reverse_sync.py).
* Evaluates differences between `install/` state and live host files.
* Does **not** mutate `install/` or write to `state.toml`.
* Detects runtime modifications (GUI changes, emergency hot-fixes).
* If drift is found on any targeted package, records a `drift_warning` and flags the preview status as `DRIFT_DETECTED` (unless bypassed via `--force`).

### 3.2 Phase 1: Ephemeral Sandbox Template Compilation
Managed within [`enter_plan_sandbox`](../src/drift/primitives/plan_repo.py):
* Creates a temporary scratch directory via `tempfile.TemporaryDirectory(prefix="drift_plan_sandbox_")`.
* Pre-copies existing `render/<pkg>` trees to preserve Merkle lockfile state for fast cache hits.
* Injects a sandboxed [`WorkspaceConfig`](../src/drift/config/workspace_config.py) configured with:
  - `render_directory = tmp_sandbox / render`
  - `render_path_mask = real_workspace.render_path`
  - `install_path_mask = real_workspace.install_path`
  - Isolated `RenderCache()` instance
* Calls [`render_package`](../src/drift/render/render_package.py) in topological DAG order.
* Rebases all generated `FileAction` paths so that planned actions point to canonical `render/` rather than the temporary directory.
* If a package fails compilation, records `render_errors[pkg]` and blocks downstream packages that depend on it.

### 3.3 Phase 2: Sandbox Staging Planning
Executed in [`execute_sandbox_stage_phase`](../src/drift/primitives/plan_repo.py):
* Diffs `sandbox_render/<pkg>` directly against canonical `install/<pkg>`.
* Compiles [`PackageStagePlan`](../src/drift/core/result_models.py) using the declarative delivery engine without touching the filesystem.
* Rebases action paths so source paths reference canonical `render/` and destination paths reference canonical `install/`.
* Isolates per-package staging exceptions into `stage_errors[pkg]`.

### 3.4 Phase 3: Physical Delivery Planning
Executed in [`execute_sandbox_install_phase`](../src/drift/primitives/plan_repo.py):
* Discovers candidate deployable files from `sandbox_render/<pkg>`.
* **Batch Pre-Flight Guard**: Calls [`assert_no_cross_package_conflicts`](../src/drift/primitives/package_assertions.py) across all eligible packages to audit against intra-batch destination collisions (two packages claiming the same file) and inter-package collisions (conflicting with previously installed packages in `state.toml`).
* **Decoupled Package-by-Package Loop**: Iterates through packages in topological order with **dependency checks explicitly turned off** (`full_universe_deps=None` / `no_deps=True`), as dependency ordering was already validated upfront.
* Calls [`assert_packages_install_ready`](../src/drift/primitives/install_repo.py) to verify target directory permissions and hook readiness per package.
* Invokes [`plan_package_install`](../src/drift/primitives/install_repo.py), compiling concrete `FileAction`s (`CREATE_SYMLINK`, `UPDATE_COPY`, `BACKUP_OVERWRITE`, `BACKUP_PRUNE`, `SKIP_IDENTICAL`).

---

## 4. Fault Isolation & Pipeline Failure Semantics

### 4.1 Error Granularity & Boundary Separation

| Error Type | Detection Layer | Impact Scope | Preview Representation |
| :--- | :--- | :--- | :--- |
| **Dependency Cycle / Missing Dep** | Upfront Step 4 | Global | `preview.status = "FAILED"`, `preview.global_errors` |
| **Midway Crash Lock** | Pre-flight Step 2 | Per-Package | `p.error = MidwayTransactionError`, `status = "FAILED"` |
| **Render Compilation Error** | Phase 1 Sandbox | Per-Package & Dependents | `p.error = ...`, blocks dependents, `status = "FAILED"` |
| **Staging Diff Failure** | Phase 2 Sandbox | Per-Package | `p.error = ...`, blocks Phase 3 install, `status = "FAILED"` |
| **Target Directory Unwritable** | Phase 3 Install Loop | Per-Package | `p.error = TargetPermissionError`, sibling packages proceed |
| **Cross-Package Path Collision** | Phase 3 Batch Guard | Multi-Package / Global | Trapped in `global_errors`, prominent failure banner |

### 4.2 Prominent Failure Banner Guidance
When errors occur during planning, Drift guarantees that the user is explicitly warned that a live deployment would fail:
1. In human-readable text mode:
   ```text
   🚨 Global Pre-Flight Errors (Real deploy pipeline will fail):
     ❌ Cross-package destination conflicts detected (1 collision(s)):
       • '~/.config/app.conf': Intra-batch collision between packages ['pkg_a', 'pkg_b']

   💥 Real deployment pipeline will fail due to detected errors.
   ```
2. In machine-readable JSON mode (`--json`):
   ```json
   {
     "status": "FAILED",
     "has_changes": true,
     "has_drift": false,
     "global_errors": [
       "❌ Cross-package destination conflicts detected..."
     ],
     "packages_with_errors": ["pkg_a", "pkg_b"]
   }
   ```
3. Exit codes: Exits with `ExitCode.GENERAL_ERROR` (`1`) on any planning failure, or `ExitCode.DRIFT_DETECTED` (`3`) if host drift was detected without `--force`.

---

## 5. Presentation & Formatting Engine

[`WorkspaceDeployPreview`](../src/drift/core/result_models.py) formats simulation results for terminal viewing and automation consumption:

### 5.1 Terminal Output Conventions
* **Summary Banner**:
  `📊 Summary: N package(s) planned | X with changes | Y unchanged | Z drifted`
* **Unchanged Package Collapsing**: Unchanged packages are collapsed into a clean footer line by default (`✨ 3 package(s) unchanged: git, tmux, zsh`), avoiding visual clutter.
* **Full Expansion (`-a / --all`)**: Displays complete action listings for all packages, including `SKIP_IDENTICAL` actions when run with `-v / --verbose`.
* **Actionable Drift Advice**: When host drift is present, provides immediate command suggestions:
  `💡 Host changes detected. Run 'drift adopt' to absorb changes into src/ or pass '--force' to overwrite.`

### 5.2 Single Source of Truth CLI Routing
Drift supports dual CLI backends (Typer and Argparse). Flag handling is unified through `CommandSpec` and [`handle_plan`](../src/drift/cli/cli_handlers.py):
* `drift deploy --dry-run` and `drift deploy -n` are transparent aliases to `drift plan`.
* Flags supported across both backends:
  - `-a, --all, --show-all`: Expands all package sections.
  - `-f, --force`: Bypasses drift abort status.
  - `-r, --reinstall`: Simulates full reinstallation (bypassing staging skip).
  - `-c, --clean, --no-cache`: Forces cache-free template compilation.
  - `--with-hooks`: Executes pre-flight hooks in isolated scratch sandboxes.
  - `--no-hooks, --no-hook`: Bypasses lifecycle hooks (default).
  - `--no-deps`: Bypasses prerequisite dependency assertions.
  - `--json`: Outputs structured JSON.
