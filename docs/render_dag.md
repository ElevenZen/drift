# Render DAG: Dependency Merkle Graph & Incremental Compilation Engine

This document provides a comprehensive design specification, mathematical modeling rationale, and implementation architecture for Drift's **Render DAG (Directed Acyclic Graph)** subsystem.

---

## 1. Architectural Mission & Rationale

### 1.1 The Legacy Procedural Bottleneck
In early versions of Drift, template rendering and static asset distribution were executed through procedural file scanning loops:
* **Redundant I/O**: Every invocation of `drift render` re-scanned directory trees and blindly re-rendered all templates or copied static files, regardless of whether source files or environment variables had changed.
* **Brittle Ordering**: Shared engine inputs, package configs, lifecycle hooks, and dotfile payloads were entangled in ad-hoc, procedural execution sequences.
* **Lack of Ahead-of-Time Inspection**: Without a formal dependency representation, dry-run simulation (`drift render --dry-run`) had to guess execution side-effects or duplicate execution code paths.
* **Collision Hazards**: When multiple source templates or engines targeted identical destination paths (e.g. `file.conf.j2` and `file.conf.tera`), procedural rendering produced unpredictable last-write-wins collisions without deterministic failure guards.

### 1.2 The Merkle DAG Solution
The Render DAG replaces procedural loops with a declarative, typed Abstract Syntax Tree (AST) dependency graph. Every compilable entity—whether a raw text string, an environment dictionary, a static dotfile, a Jinja2 template, or a lifecycle hook—is modeled as a node within an acyclic dependency graph.

Key capabilities delivered by this architecture:
1. **Mathematical Change Invariance**: Cryptographic Merkle hashing guarantees that if inputs, templates, environment variables, and engine definitions are invariant, compilation is skipped.
2. **Deterministic Acyclic Execution**: Depth-first post-order topological sorting guarantees that all prerequisites are validated and digested before their dependents.
3. **Collision-Free Safety**: Centralized destination path translation detects and rejects destination collisions before any filesystem mutation occurs.
4. **Declarative FileAction Simulation**: Node digestion yields strongly typed `FileAction`s, decoupling planning from execution and powering unified `--dry-run` and `--json` workflows.

---

## 2. Mathematical Modeling & Change Invariance

### 2.1 Formal Dependency Relations
Every entity managed during the rendering phase belongs to one of four formal dependency categories:

* **Static Leaf File ($S$)**: A static source file copied 1:1 to the destination:
  $$S_O : [S_I]$$
  where $S_O$ (the destination in `render/`) depends strictly on $S_I$ (the source leaf in `src/`). $S_I$ has no dependencies.
* **Independent Leaf / Template ($T$)**: An unmanaged source template or external asset:
  $$T : []$$
  Template sources represent terminal inputs and depend on no other entities.
* **Engine Output File ($O$)**: A compiled template artifact compiled by a render engine:
  $$O : [I, T, V, E]$$
  where:
  * $I$: Render engine input file (optional; may be an external asset $S$, or transitively another engine output $O'$).
  * $T$: Template source leaf file ($T : []$).
  * $V$: Deterministic environment variable dictionary in the current scope (`JsonNode(pkg_config.env_resolve.impact.restricted_env())`).
  * $E$: Render engine definition (binary, execution flags, suffix rules).
* **Directory Synchronization Target ($D$)**: A directory placeholder ensuring empty directory preservation:
  $$D : []$$
  Preserves structural topology across `src/`, `render/`, and `install/` via `.drift_keep`.

### 2.2 The Principle of Change Invariance
Let $\mathcal{R}$ be a deterministic render engine mapping $(I, T, V, E) \to O$. If none of the input entities undergo state transitions between runs:
$$(\Delta I = \varnothing) \land (\Delta T = \varnothing) \land (\Delta V = \varnothing) \land (\Delta E = \varnothing) \implies (\Delta O = \varnothing)$$

When this condition holds, physical compilation can be safely bypassed. The system validates the existing artifact on disk against its cryptographic proof and marks it as up to date.

### 2.3 Scoping of $V$: Effective Dictionary vs. Ambient Process Environment
In Drift's Merkle DAG, $V$ is intentionally bound to `pkg_config.env_resolve.impact.restricted_env()` rather than Python's raw `os.environ`.

#### The Cache Invariance Risk of Ambient `os.environ`
The host operating system's process environment contains dozens of volatile, session-specific variables that mutate continuously across terminal tabs, subshells, SSH sessions, and desktop managers (e.g. `SHLVL`, `_`, `OLDPWD`, `PWD`, `SSH_AUTH_SOCK`, `TERM_SESSION_ID`, `XDG_SESSION_ID`, `WINDOWID`, `TMUX_PANE`, or ephemeral temp directory paths).

If $V$ were to capture the raw process environment `os.environ`:
$$\Delta V \neq \varnothing \quad \text{(virtually 100% of the time between different shells/sessions)}$$
This would break change invariance on every command invocation, dropping the Merkle cache hit rate to 0%, forcing unnecessary template re-rendering across all packages, and producing continuous churn in `.drift_lock.json`.

#### Deterministic Ingestion via Declarative Fallbacks (`[env.fallback]`)
To preserve change invariance while still enabling dotfile templates and lifecycle hooks to react to changes in ambient host environment variables, Drift provides a declarative bridge via **Tier 6 (`[env.fallback]`)**:

```toml
[env.fallback]
# Declare ambient host variables needed by templates
HOST_EDITOR = "${EDITOR:-vim}"
HOST_THEME  = "${THEME:-dark}"
```

During configuration ingestion, Drift evaluates `[env.fallback]` against the ambient process environment (`os.environ` / Tier 3). If the host environment defines `EDITOR`, its value is resolved into `restricted_env` and tracked deterministically in $V$ (`env_node`).

This pattern yields three critical architectural properties:
1. **Targeted Tracking**: Only host variables explicitly declared in configuration are tracked in the Merkle tree.
2. **Selective Invalidation**: When an ambient variable like `EDITOR` changes, $\Delta V \neq \varnothing$ correctly fires, invalidating and re-rendering only the templates that depend on it.
3. **Session Noise Isolation**: Transient session noise (`SHLVL`, `_`, `OLDPWD`, etc.) is completely excluded from the Merkle DAG digest, guaranteeing stable cache hits across diverse shell environments.

### 2.4 Cryptographic Merkle Hashing
To track invariance deterministically across command runs without relying on unreliable filesystem timestamps, each node in the DAG is identified by a two-component cryptographic hash:

#### 1. Low-Level Disk Hash (`own_hash`)
* **Regular File**: SHA-256 computed over the hybrid path key, POSIX file permissions mode (`st_mode & 0o777`), and raw file content bytes:
  $$\text{own_hash}(F) = \text{SHA256}(\text{path_key} \parallel \text{NUL} \parallel \text{mode} \parallel \text{NUL} \parallel \text{bytes})$$
  Where the path key follows a hybrid scope rule:
  $$\text{path_key}(P) = \begin{cases} P\text{.relative_to}(pkg\_render\_dir) & \text{if } P \in pkg\_render\_dir \\ P\text{.resolve()} & \text{otherwise (e.g. source files in } src/\text{, configs in } config/\text{)} \end{cases}$$
  * **Package-relative for rendered artifacts**: Artifacts in `render/<pkg>/` have identical relative paths in both canonical renders and ephemeral sandbox plans (`drift plan`), eliminating the need for sandbox path spoofing or masks.
  * **Absolute for external dependencies**: Leaf source files and templates in `src/` embed their absolute host paths. Migrating the workspace directory or cloning onto a different machine changes the absolute path, deliberately invalidating cached Merkle hashes and enforcing a clean re-render to evaluate host-specific facts.
* **Directory**: SHA-256 computed over directory identity, hybrid path key, and POSIX mode:
  $$\text{own_hash}(D) = \text{SHA256}(\text{"DIR"} \parallel \text{NUL} \parallel \text{path_key} \parallel \text{NUL} \parallel \text{mode})$$
* **Virtual Nodes (Text / JSON)**: SHA-256 computed over normalized UTF-8 string content.
* **Root Container Nodes**: SHA-256 computed over node class name and package name (`"PackagePayloadNode:pkg_name"`).

#### 2. Merkle Dependency Hash (`merkle_hash`)
The Merkle hash binds a node's intrinsic identity to the cryptographic state of all its direct prerequisites.

For a leaf node without dependencies ($\text{depends_on} = \varnothing$):
$$\text{merkle_hash}(N_{\text{leaf}}) = \text{SHA256}\big(\text{NodeType} \parallel \text{":"} \parallel \text{own_hash}(N)\big)$$

For a node with dependencies, prerequisite hashes are first concatenated with colons:
$$\text{deps_str} = \operatorname{join}\big(\text{":"}, \, [\text{merkle_hash}(D) \mid D \in \text{depends_on}]\big)$$
$$= \text{merkle_hash}(D_1) \parallel \text{":"} \dots \parallel \text{":"} \parallel \text{merkle_hash}(D_k)$$

The final Merkle hash then incorporates $\text{deps_str}$:
$$\text{merkle_hash}(N) = \text{SHA256}\big(\text{NodeType} \parallel \text{":"} \parallel \text{own_hash}(N) \parallel \text{":"} \parallel \text{deps_str}\big)$$

Because digestion traverses the graph in topological order, every dependency's `merkle_hash` is fully computed and verified before its consumer is evaluated.

---

## 3. AST Node Taxonomy & Polymorphic Model

The AST is implemented in [`src/drift/render/render_dag.py`](../src/drift/render/render_dag.py). It cleanly separates virtual data nodes from filesystem path nodes.

### 3.1 Class Hierarchy Overview

* [`Node`](../src/drift/render/render_dag.py): Universal base AST class holding `value: str`, `depends_on: List[Node]`, and `hashes: Optional[NodeHashes]`.
  * [`TextNode`](../src/drift/render/render_dag.py): Represents immutable text data; hashes content on initialization.
    * [`JsonNode`](../src/drift/render/render_dag.py): Represents structured data serialized to deterministic JSON (sorted keys, compact separators). Retains `data: Any` for engine consumption.
  * [`PackageHooksNode`](../src/drift/render/render_dag.py): Root Merkle container for Phase 2 (Hooks).
  * [`PackagePayloadNode`](../src/drift/render/render_dag.py): Root Merkle container for Phase 3 (Payload).
  * [`PathNode[Generic[_DstT, _SrcT]]`](../src/drift/render/render_dag.py): Base class for filesystem entities encapsulating target `dst_path` and source `src_path`.
    * [`DirectoryNode`](../src/drift/render/render_dag.py): Represents empty directory targets synchronized via `.drift_keep`.
    * [`UnknownPathNode`](../src/drift/render/render_dag.py): Transient placeholder for candidate paths awaiting AST expansion.
    * [`FileNode[Generic[_SrcT]]`](../src/drift/render/render_dag.py): Base class for file-specific operations.
      * [`IndependentFileNode`](../src/drift/render/render_dag.py): Unmanaged external asset or template source leaf (`depends_on=[]`).
      * [`StaticFileNode`](../src/drift/render/render_dag.py): Static asset copied 1:1 from `src_path` to `dst_path`.
      * [`CachedNode`](../src/drift/render/render_dag.py): In-memory hit from an earlier phase or run, bypassing re-expansion.
      * [`EngineOutputFileNode`](../src/drift/render/render_dag.py): Compiled template artifact depending on `[I, T, V, E]`.
      * [`PackageConfigNode`](../src/drift/render/render_dag.py): Phase 1 compilation root for package configuration and variable stitching.

### 3.2 Concrete Node Responsibilities

* **`IndependentFileNode`**
  * **Destination (`dst_path`)**: `src_path` (unmanaged leaf source path)
  * **Source (`src_path`)**: Source file path
  * **Prerequisites (`depends_on`)**: `[]`
  * **Digestion Semantics**: Computes low-level disk hash (`own_hash`) of the source file.

* **`StaticFileNode`**
  * **Destination (`dst_path`)**: Target path in `render/<pkg>/...`
  * **Source (`src_path`)**: Source asset in `src/<pkg>/...`
  * **Prerequisites (`depends_on`)**: `[IndependentFileNode(src_path)]`
  * **Digestion Semantics**: Verifies cache match; copies static file 1:1 to destination; emits `CREATE_COPY` (if new) or `UPDATE_COPY` (if existing); computes Merkle hash and updates lockfile state.

* **`DirectoryNode`**
  * **Destination (`dst_path`)**: Target directory path in `render/<pkg>/dir`
  * **Source (`src_path`)**: Optional source directory path
  * **Prerequisites (`depends_on`)**: `[]`
  * **Digestion Semantics**: Creates directory on disk; touches `.drift_keep` placeholder to preserve empty directory structure; emits `ENSURE_DIR`.

* **`EngineOutputFileNode`**
  * **Destination (`dst_path`)**: Stripped output path in `render/<pkg>/file`
  * **Source (`src_path`)**: Template source path in `src/<pkg>/file.<suffix>`
  * **Prerequisites (`depends_on`)**: `[I, T, V, E]` (optional input file node `I`, template node `T`, environment `JsonNode` `V`, engine `JsonNode` `E`)
  * **Digestion Semantics**: Verifies cache match; compiles template via designated render engine (`render_template_to_file`); emits `RENDER_ITEM`.

* **`PackageConfigNode`**
  * **Destination (`dst_path`)**: `render/<pkg>/.drift/drift_package.toml`
  * **Source (`src_path`)**: `src/<pkg>/drift_package.toml` (if present)
  * **Prerequisites (`depends_on`)**: `[UnknownPathNode(sources)..., JsonNode(env)]`
  * **Digestion Semantics**: Merges candidate TOMLs; executes dynamic Python package hook (`apply_package_hook`); resolves and stitches package variables; writes final `drift_package.toml`; prunes obsolete config files; emits `WRITE_CONFIG`.

* **`PackageHooksNode`**
  * **Destination (`dst_path`)**: `None` (Merkle container root)
  * **Source (`src_path`)**: `None`
  * **Prerequisites (`depends_on`)**: List of hook script AST nodes
  * **Digestion Semantics**: Evaluates hook dependencies; prunes obsolete hook scripts and directories in `render/<pkg>/.drift/hooks/`; commits hashes to the `hook_hashes` bucket.

* **`PackagePayloadNode`**
  * **Destination (`dst_path`)**: `None` (Merkle container root)
  * **Source (`src_path`)**: `None`
  * **Prerequisites (`depends_on`)**: List of payload dotfile AST nodes
  * **Digestion Semantics**: Evaluates payload dependencies; prunes obsolete files in `render/<pkg>/` (strictly shielding `.drift/`); commits hashes to the `payload_hashes` bucket.

* **`CachedNode`**
  * **Destination (`dst_path`)**: Existing compiled path
  * **Source (`src_path`)**: Underlying source path
  * **Prerequisites (`depends_on`)**: `[]`
  * **Digestion Semantics**: Reuses valid in-memory session cache result; marks destination as skipped; emits `SKIP_IDENTICAL`.

---

## 4. Graph Expansion & Collision Detection

AST expansion is implemented in [`src/drift/render/render_expansion.py`](../src/drift/render/render_expansion.py). It transforms candidate files and unresolved `UnknownPathNode` instances into a fully resolved DAG.

### 4.1 State Container: `ExpansionContext`
The expansion pipeline relies on `ExpansionContext` to manage traversal state:
* `drift_root`: Absolute workspace anchor.
* `package_name`: Target package name.
* `enable_render`: Boolean flag from package config (if `False`, all files are treated as static files).
* `env_node`: Deterministic `JsonNode` of the effective environment variables (`JsonNode(pkg_config.env_resolve.impact.restricted_env())`, see §2.3).
* `render_engines`: Active `RenderEngineRegistry`.
* `cache`: Process-level `RenderCache`.
* `path_translation`: Dictionary mapping source path prefixes to destination path prefixes.
* `node_refs`: Dictionary mapping canonical string keys (`to_node_key(path)`) to existing AST node instances, ensuring reference deduplication.
* `collision_map`: Dictionary mapping `dst_path -> src_path` to detect output collisions.
* `visiting_paths` & `visiting_engines`: Sets tracking active recursion chains for cycle detection.

### 4.2 Prefix-Based Path Translation
Paths are translated from declarative source locations to compilation destinations using the longest matching prefix rule:
$$T_{\text{path}}(P, \mathcal{M}) = \begin{cases} D_{\text{prefix}} / (P - S_{\text{prefix}}) & \text{if } P \text{ matches prefix } S_{\text{prefix}} \in \mathcal{M} \\ P & \text{otherwise} \end{cases}$$

```python
def translate_path(path, rules):
    # sorted by descending prefix length
    for (src_prefix, dst_prefix) in sorted(rules, key=lambda p: len(p.parts), reverse=True):
        if path.is_relative_to(src_prefix):
            return dst_prefix / path.relative_to(src_prefix)
    return path
```

Prefix translation rules are phase-specific:
* **Workspace Engine Inputs**:
  `config/<rel>` $\to$ `render/<pkg>/.drift/render/workspace/<rel>`
* **Package Engine Inputs**:
  `src/<pkg>/<rel>` $\to$ `render/<pkg>/.drift/render/package/<rel>`
* **Package Configuration (Phase 1 Staging)**:
  `src/<pkg>/drift_package[.local].toml` $\to$ `render/<pkg>/.drift/render/package/drift_package[.local].toml`
* **Lifecycle Hooks (Phase 2)**:
  `src/<pkg>/drift_hooks/<rel>` $\to$ `render/<pkg>/.drift/hooks/<rel>`
* **Dotfile Payload (Phase 3)**:
  `src/<pkg>/<rel>` $\to$ `render/<pkg>/<rel>`

### 4.3 Output Collision Detection
To prevent multiple templates or conflicting static files from competing for the same output path:
```python
def assert_no_render_collisions(dst_path: Path, src_path: Path, collision_map: Dict[Path, Path], package_name: str) -> None:
    if dst_path in collision_map and collision_map[dst_path] != src_path:
        prev_file = collision_map[dst_path]
        raise RenderCollisionError(
            f"Multiple source files in package '{package_name}' render to the same destination path '{dst_path.as_posix()}': "
            f"'{prev_file}' and '{src_path}'."
        )
```
If two distinct source files evaluate to the same stripped output path (e.g., `app.conf.j2` and `app.conf.tera`), compilation halts immediately before any file I/O occurs.

### 4.4 Template Target Protection
Templates are strictly prohibited from dynamically targeting control plane files such as `.drift_ignore`:
```python
def assert_not_driftignore_template_target(dst_path: Path, file_path: Path, package_name: str, drift_root: Path) -> None:
    target_name = encode_dot_prefix(Path(dst_path.name)).name
    if target_name in DRIFT_IGNORE_FILE_NAME_LIST:
        raise ConfigError(
            f"Package '{package_name}' cannot render template '{file_path.name}' to '{target_name}'. "
            f"'{DRIFT_IGNORE_FILE_NAME}' is a static configuration file and must be placed directly at the package root."
        )
```

---

## 5. Three-Phase Execution Pipeline

Package rendering is decomposed into three discrete, non-overlapping phases: **Config $\to$ Hooks $\to$ Payload**.

```
┌────────────────────────────────────────────────────────┐
│ Phase 1: Package Configuration Compilation             │
│ (Workspace Env Scope -> Intermediate Staging -> TOML)  │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 2: Lifecycle Hooks Compilation & Permissions     │
│ (Package Env Scope -> .drift/hooks/ -> probe Hook)     │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│ Phase 3: Dotfile Payload Compilation & Scoped Pruning  │
│ (pre_source Hook -> Digestion -> post_render Hook)     │
└────────────────────────────────────────────────────────┘
```

### 5.1 The Bootstrapping Challenge
A single monolithic compilation pass cannot render a package because of strict chicken-and-egg dependency constraints:
1. **Dynamic Configuration**: Package configuration (`drift_package.toml`) may itself be a template compiled with workspace variables, or transformed via a dynamic Python hook (`drift_package.py`). We cannot inspect package settings (e.g. `enable_render`, custom engines, requirements) until this configuration is compiled.
2. **Hook Execution Requirements**: The pre-flight `probe` hook and pre-processing `pre_source` hook must execute before dotfile payload files are scanned.
3. **Dynamic Source Generation**: The `pre_source` hook frequently downloads, generates, or decrypts source files into `src/<pkg>/`. Discovering payload candidates before `pre_source` runs would miss dynamically generated files.

### 5.2 Phase 1: Package Configuration (`PackageConfigNode`)
* **Scope**: Executed under global workspace environment scope.
* **Staging Isolation**: Source candidates (`drift_package.toml`, `drift_package.local.toml`) compile into an intermediate staging directory (`render/<pkg>/.drift/render/package/`), avoiding collisions with the final config.
* **Digestion Sequence**:
  1. Digested template dependencies are parsed as TOML and merged using `merge_toml()`.
  2. Dynamic Python hook (`drift_package.py`) is invoked via `apply_package_hook()`.
  3. Package variables are resolved and interpolated via `resolve_and_interpolate_package_config()`.
  4. The final stitched configuration is written to `render/<pkg>/.drift/drift_package.toml`.
  5. Obsolete config files in `.drift/` are pruned.
  6. The `config_hashes` bucket in `.drift/render.lock` is updated.
  7. The validated `PackageConfig` model is instantiated.

### 5.3 Phase 2: Lifecycle Hooks (`PackageHooksNode`)
* **Scope**: Scoped strictly under `with pkg_config.package_envs():`.
* **Compilation**: Translates scripts from `src/<pkg>/drift_hooks/` to `render/<pkg>/.drift/hooks/`.
* **Permission Enforcement**: Configured lifecycle hooks are guaranteed POSIX `0o755` permissions across both `src/` and `render/` via [`ensure_configured_hook_permissions()`](../src/drift/render/render_hooks.py).
* **Pruning**: Obsolete hook scripts in `render/<pkg>/.drift/hooks/` are pruned.
* **Lockfile**: Updates `hook_hashes` bucket in `.drift/render.lock`.
* **Execution Gates**:
  1. **Pre-flight Requirements**: Evaluates declarative host facts and runs the dynamic `probe` hook. If requirements fail, the package halts early and returns `SKIPPED`.
  2. **`pre_source` Hook**: Executes to prepare or generate local source dotfiles.

### 5.4 Phase 3: Dotfile Payload (`PackagePayloadNode`)
* **Candidate Discovery**: Discovers files and empty directories in `src/<pkg>/` post-`pre_source`, filtering out internal control plane files (`.drift/`, `drift_hooks/`, config files, and hidden dotfile files not using the canonical `dot-` prefix).
* **Control Plane Ignore**: Copies root `.drift_ignore` into `render/<pkg>/.drift/.drift_ignore`.
* **AST Expansion & Digestion**: Templates are compiled, static files are copied, and `.drift_keep` placeholders are touched in empty folders.
* **Scoped Pruning**: Obsolete payload files are deleted via set subtraction (`existing_payload - active_payload`), **strictly shielding** the entire `.drift/` control plane.
* **Post-Render Hook**: Fires `post_render` hook upon successful completion.

---

## 6. Digestion Subsystem & Topological Evaluation

Graph digestion is implemented in [`src/drift/render/render_digester.py`](../src/drift/render/render_digester.py).

### 6.1 Topological Sorting
The DAG is evaluated using depth-first post-order traversal with 3-state cycle detection:
```python
def topological_sort_nodes(root_node: Node) -> List[Node]:
    visited: Dict[int, int] = {}  # id(node) -> 1 (visiting), 2 (visited)
    result: List[Node] = []

    def dfs(n: Node) -> None:
        nid = id(n)
        if visited.get(nid, 0) == 1:
            raise ValueError(f"Cyclic dependency detected in render graph involving node: '{n.value}'")
        if visited.get(nid, 0) == 2:
            return
        visited[nid] = 1
        for dep in n.depends_on:
            dfs(dep)
        visited[nid] = 2
        result.append(n)

    dfs(root_node)
    return result
```
This guarantees that all input nodes, templates, environment JSON nodes, and engine definitions are evaluated before the output nodes that consume them.

### 6.2 Digestion Flow & Cache Verification
During `node.digest(context)`:
1. **Cache Verification (`check_and_apply_cache`)**:
   * If `context.force` is False, the lockfile checks whether the destination file exists on disk and whether its candidate Merkle hash matches the recorded bucket hash.
   * If matched: disk I/O is skipped, the destination path is appended to `skipped_paths`, `SKIP_IDENTICAL` action is emitted, and the hash is added to `active_hashes`.
2. **Execution on Miss**:
   * If cache misses or file is absent: the node executes its physical operation (compiling template, copying static file, or creating directory).
   * Emits the corresponding `FileAction` (`CREATE_COPY`, `UPDATE_COPY`, `RENDER_ITEM`, `WRITE_CONFIG`, `ENSURE_DIR`).
   * Computes new disk hash and Merkle hash.
   * Records path in `rendered_paths` and registers hash into `active_hashes` and in-memory `RenderCache`.

---

## 7. Multi-Tier Caching & 3-Bucket Merkle Lockfile

Drift uses a two-tier caching hierarchy to deliver fast incremental compilation:

### 7.1 Tier 1: In-Memory Session Cache (`RenderCache`)
Defined in [`src/drift/render/render_cache.py`](../src/drift/render/render_cache.py) and attached per-workspace to `WorkspaceConfig.render_cache`.
* **Scope**: Persists across all package renders during a single Drift command invocation.
* **Microsecond Stat Invalidation**: Stores nanosecond source file fingerprints (`source_mtime_ns`, `source_size`). If a source file is touched between multi-package render steps, the cache invalidates the entry in sub-microsecond time without requiring graph re-expansion.

### 7.2 Tier 2: Persistent 3-Bucket Lockfile (`render_lock.json`)
Defined in [`src/drift/render/render_lock.py`](../src/drift/render/render_lock.py) and persisted to `render/<pkg>/.drift/render_lock.json`.

```json
{
  "version": 1,
  "config_hashes": [
    "1a2b3c...",
    "4d5e6f..."
  ],
  "hook_hashes": [
    "7g8h9i..."
  ],
  "payload_hashes": [
    "a0b1c2...",
    "d3e4f5..."
  ]
}
```

#### 3-Bucket Isolation Invariant
* **`RenderBucket.CONFIG` (`config_hashes`)**: Cryptographic proofs for Phase 1 configuration dependencies.
* **`RenderBucket.HOOKS` (`hook_hashes`)**: Cryptographic proofs for Phase 2 lifecycle hook scripts.
* **`RenderBucket.PAYLOAD` (`payload_hashes`)**: Cryptographic proofs for Phase 3 dotfile templates and static assets.

Each phase atomically rewrites only its designated bucket upon completion:
```python
context.lockfile.update_bucket_hashes(RenderBucket.PAYLOAD, context.active_hashes)
context.save_lockfile()
```
This guarantees self-pruning of stale hashes without cross-phase clobbering.

---

## 8. Declarative FileActions & Dry-Run Planning

The Render DAG is completely decoupled from direct host output through [`FileAction`](../src/drift/core/file_action.py).

### 8.1 Action Types & Visual Representation

| Action Type | Prefix | Description |
|---|---|---|
| `CREATE_COPY` | `➕ [CREATE_COPY]` | Initial static copy |
| `UPDATE_COPY` | `✏️ [UPDATE_COPY]` | Overwrite existing copy |
| `RENDER_ITEM` | `🎨 [RENDER]` | Compile template |
| `WRITE_CONFIG` | `⚙️ [WRITE_CONFIG]` | Write package config |
| `ENSURE_DIR` | `📁 [ENSURE_DIR]` | Ensure directory (with `.drift_keep`) |
| `DELETE_ITEM` | `🗑️ [DELETE_ITEM]` | Prune obsolete item |
| `SKIP_IDENTICAL` | `⏭️ [SKIP_IDENTICAL]` | Skip identical (cache hit) |

### 8.2 Dry-Run Simulation (`--dry-run`)
When `drift render --dry-run` is invoked:
1. `context.dry_run = True` is propagated through `DigestionContext`.
2. DAG construction, path translation, collision detection, and topological sorting execute normally.
3. Node digestion evaluates what *would* happen, appends the planned `FileAction`s to `context.result.actions`, and logs them via `format_action_line()`.
4. Disk write operations (`mkdir`, `copy2`, `render_template_to_file`, `unlink`, `save_to_dir`) are completely suppressed.
5. `PackageRenderResult.actions` provides an exact preview of planned changes.

### 8.3 Functional Metrics Derivation
`PackageRenderResult` derives high-level metrics cleanly using functional filter-map pipelines over `actions`:
```python
rendered_files = [
    to_relative_posix(a.dst_path, render_pkg_dir)
    for a in ctx.result.actions
    if a.action_type == FileActionType.RENDER_ITEM and a.dst_path is not None
]

copied_files = [
    to_relative_posix(a.dst_path, render_pkg_dir)
    for a in ctx.result.actions
    if a.action_type in (FileActionType.CREATE_COPY, FileActionType.UPDATE_COPY)
    and a.dst_path is not None
    and not is_drift_internal_path(a.dst_path, render_pkg_dir)
]
```

---

## 9. Architecture & Call Chain Summary

```
[ CLI: drift render / drift apply ]
                 │
                 ▼
[`render_package.py`: run_primitive_2_render_packages()]
                 │
        ┌────────┴──────────────────────────┐
        ▼                                   ▼
 [Single Package: render_package()]   [Shared RenderCache]
        │
        ├─► Phase 1: load_package_config_from_source_dir()
        │     ├─ build PackageConfigNode & expand UnknownPathNodes
        │     ├─ digest_render_dag() -> merge TOMLs, run Python hooks, stitch envs
        │     └─ update lockfile bucket: config_hashes
        │
        ├─► Phase 2: render_hooks()
        │     ├─ build PackageHooksNode & expand drift_hooks/
        │     ├─ digest_render_dag() -> compile hooks, prune obsolete hooks
        │     ├─ ensure_configured_hook_permissions() (chmod 0o755)
        │     ├─ evaluate_requirements() -> run dynamic probe hook
        │     └─ trigger_pre_source_hook()
        │
        └─► Phase 3: render_package_files()
              ├─ discover candidates post-pre_source
              ├─ build PackagePayloadNode & expand payload AST
              ├─ digest_render_dag() -> compile templates, copy static, ensure dirs
              ├─ prune_obsolete_payload_files() (shielding .drift/)
              ├─ update lockfile bucket: payload_hashes
              ├─ trigger_post_render_hook()
              └─ return PackageRenderResult(actions, rendered_files, copied_files)
```

---

## 10. Source Code Index Matrix

| Subsystem / Responsibility | Module Path | Primary Types & Functions |
|---|---|---|
| **AST Node Definitions** | [`src/drift/render/render_dag.py`](../src/drift/render/render_dag.py) | `Node`, `PathNode`, `FileNode`, `DirectoryNode`, `StaticFileNode`, `EngineOutputFileNode`, `PackageConfigNode`, `PackageHooksNode`, `PackagePayloadNode` |
| **AST Expansion & Paths** | [`src/drift/render/render_expansion.py`](../src/drift/render/render_expansion.py) | `ExpansionContext`, `expand_node_dependencies`, `translate_path`, `assert_no_render_collisions` |
| **Digestion & Pruning** | [`src/drift/render/render_digester.py`](../src/drift/render/render_digester.py) | `topological_sort_nodes`, `digest_render_dag`, `check_and_apply_cache`, `prune_obsolete_payload_files` |
| **Cryptographic Hashes** | [`src/drift/render/render_hasher.py`](../src/drift/render/render_hasher.py) | `compute_node_own_hash`, `compute_merkle_node_hash`, `hash_file_disk`, `hash_directory_disk` |
| **Persistent Lockfile** | [`src/drift/render/render_lock.py`](../src/drift/render/render_lock.py) | `RenderLockfile`, `RenderBucket` |
| **Process Cache** | [`src/drift/render/render_cache.py`](../src/drift/render/render_cache.py) | `RenderCache`, `NodeHashes`, `CachedFileEntry` |
| **Pipeline Orchestrator** | [`src/drift/render/render_package.py`](../src/drift/render/render_package.py) | `run_primitive_2_render_packages`, `render_package`, `render_package_files`, `build_phase3_payload_dag` |
| **Phase 2 Hooks Engine** | [`src/drift/render/render_hooks.py`](../src/drift/render/render_hooks.py) | `render_hooks`, `build_phase2_hooks_dag`, `ensure_configured_hook_permissions` |
| **Phase 1 Config Loader** | [`src/drift/config/package_loader.py`](../src/drift/config/package_loader.py) | `load_package_config_from_source_dir`, `_execute_load_package_config_dag` |
| **Action Primitives** | [`src/drift/core/file_action.py`](../src/drift/core/file_action.py) | `FileAction`, `FileActionType`, `format_action_line`, `format_action_summary` |
