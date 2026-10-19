# `src/drift/render/` — Layer 2–3 Incremental Render DAG & Merkle Compilation Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 2–3 (3-Phase Merkle DAG Compilation, Caching & Lockfile Engine)**
* **Core Responsibility**: Compiles declarative source packages (`src/<pkg>/`) into the isolated `render/<pkg>/` sandbox across 3 non-overlapping phases — Phase 1 (`PackageConfigNode` $\rightarrow$ `.drift/drift_package.toml`), Phase 2 (`PackageHooksNode` $\rightarrow$ `.drift/hooks/`), and Phase 3 (`PackagePayloadNode` $\rightarrow$ `render/<pkg>/` + `.drift/render/`) — backed by cryptographic Merkle hashing, an in-memory nanosecond stat cache (`RenderCache`), and a 3-bucket persistent lockfile (`RenderLockfile`).
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../models/render_models.py`](../models/render_models.py), [`../config/`](../config/), and [`../hooks/`](../hooks/).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Hashing, Cache & Lockfile Primitives**: [`render_hasher.py`](render_hasher.py), [`render_cache.py`](render_cache.py), [`render_lock.py`](render_lock.py), [`render_core.py`](render_core.py)
2. **AST Node Hierarchy & Graph Expansion**: [`render_dag.py`](render_dag.py) $\rightarrow$ [`render_expansion.py`](render_expansion.py)
3. **Topological Digestion & Pruning**: [`render_digester.py`](render_digester.py)
4. **Phase 2 & Phase 3 Orchestrators**: [`render_hooks.py`](render_hooks.py) $\rightarrow$ [`render_package.py`](render_package.py)

---

## 3. Module & Public API Catalog

### [`render_hasher.py`](render_hasher.py), [`render_cache.py`](render_cache.py) & [`render_lock.py`](render_lock.py) — Merkle Hashes, Cache & Lockfile
* `NodeHashes(own_hash: str, merkle_hash: str)` & `CachedFileEntry(hashes: NodeHashes, source_mtime_ns: Optional[int], source_size: Optional[int])`.
* `RenderCache`: Per-workspace in-memory cache (`WorkspaceConfig.render_cache`) mapping destination paths to `CachedFileEntry` with automatic invalidation when `(st_mtime_ns, st_size)` changes on disk.
* `RenderBucket(str, Enum)`: `CONFIG = "config"`, `HOOKS = "hooks"`, `PAYLOAD = "payload"`.
* `RenderLockfile`: Manages `render/<pkg>/.drift/render_lock.json` with 3 isolated hash buckets (`config_hashes`, `hook_hashes`, `payload_hashes`).
* `hash_file_disk(file_path, package_render_dir=None) -> Optional[str]`, `hash_directory_disk(...)`, `compute_node_own_hash(node, package_render_dir=None) -> Optional[str]`, `compute_merkle_node_hash(node, package_render_dir=None) -> Optional[str]`.

### [`render_core.py`](render_core.py) — Low-Level Template Engine Subprocess Execution
* `resolve_render_input_file(engine_config: RenderEngineConfig, base_dir: Path) -> Optional[Path]`: Validates and resolves engine `input_file` using 3-state path rules.
* `render_template(source_path: Path, engine_config: RenderEngineConfig, input_file_override: Optional[Path] = None, ...) -> bytes`: Substitutes `%i` and `%s` in `render_command` (or uses fast built-in `python_envsubst` for default `envsubst`) and returns compiled bytes.
* `render_template_to_file(source_path: Path, target_path: Path, engine_config: RenderEngineConfig, ...) -> None`: Compiles template to `target_path` and copies POSIX permissions from `source_path`.

### [`render_dag.py`](render_dag.py) — Polymorphic Merkle AST Node Hierarchy
* `Node(value: str, depends_on: List[Node], hashes: Optional[NodeHashes])`: Base AST node with `.digest(context) -> List[FileAction]`.
* **Virtual Data Nodes**: `TextNode`, `JsonNode(data)` (deterministic sorted-key JSON hash used for `env_node` and engine config nodes).
* **Filesystem Path Nodes (`PathNode[Generic[_DstT, _SrcT]]`)**:
  * `DirectoryNode(dst_path, src_path=None)`: Empty directory node writing `.drift_keep` (`CREATE_KEEP_FILE`).
  * `UnknownPathNode(src_path)`: Unexpanded source path placeholder resolved during expansion.
  * `FileNode`: Base file node with concrete subclasses:
    * `IndependentFileNode(dst_path)`: Leaf source file (`depends_on=[]`).
    * `StaticFileNode(dst_path, src_path)`: 1:1 copied static asset (`CREATE_COPY` / `UPDATE_COPY` / `SKIP_IDENTICAL`).
    * `CachedNode(dst_path, src_path, hashes)`: In-memory `RenderCache` hit bypassing re-compilation.
    * `EngineOutputFileNode(dst_path, src_path, engine_config, input_node)`: Compiled template node depending on `[input_node, template_source_node, env_node, engine_def_node]` (`RENDER_ITEM`).
    * `PackageConfigNode`: Phase 1 root node compiling/merging base & `.local` configs, running `drift_package.py`, stitching 6-tier `[env]`, and writing `.drift/drift_package.toml` (`WRITE_CONFIG`).
* **Phase Root Containers**: `PackageHooksNode` (Phase 2 root) and `PackagePayloadNode` (Phase 3 root).

### [`render_expansion.py`](render_expansion.py) — AST Graph Expansion & Collision Detection
* `ExpansionContext`: Carries `drift_root`, `package_name`, `enable_render`, `env_node`, `render_engines`, `cache: RenderCache`, `translation_map`, and cycle-detection stacks.
* `expand_node_dependencies(root: Node, ctx: ExpansionContext) -> None`: Recursively replaces `UnknownPathNode` placeholders with concrete `StaticFileNode`, `EngineOutputFileNode`, or `CachedNode` instances while detecting circular engine input chains (`A -> B -> A`).
* `assert_no_render_collisions(nodes: Iterable[Node], package_name: str, ...) -> None`: Raises `RenderCollisionError` if two source files resolve to the same `dst_path` in `render/<pkg>/`.
* `assert_not_driftignore_template_target(target_rel_path: Path, src_path: Path, package_name: str) -> None`: Prohibits templated `.drift_ignore` files (e.g. `.drift_ignore.envst`).

### [`render_digester.py`](render_digester.py) — Topological Digestion, Lockfile Verification & Scoped Pruning
* `DigestionContext(drift_root, package_name, package_render_dir, lockfile, bucket, cache, dry_run=False, ...)` & `DigestionResult(actions, rendered_count, skipped_count, pruned_files, root_hashes)`.
* `topological_sort_nodes(root_node: Node) -> List[Node]`: Post-order DFS topological sort (leaves before dependents).
* `check_and_apply_cache(node: Node, context: DigestionContext) -> Optional[List[FileAction]]`: Compares computed `merkle_hash` against `lockfile.get_hash(bucket, key)` and verifies output existence + permissions on disk, returning `[SKIP_IDENTICAL]` on cache hit.
* `digest_render_dag(root: Node, context: DigestionContext) -> DigestionResult`: Evaluates nodes bottom-up, updates `RenderCache` and `RenderLockfile`, prunes obsolete files in the active bucket (`prune_obsolete_config_files`, `prune_obsolete_hooks`, `prune_obsolete_payload_files`), and atomically saves `.drift/render_lock.json`.

### [`render_hooks.py`](render_hooks.py) & [`render_package.py`](render_package.py) — Phase 2 & Phase 3 Orchestrators
* `build_phase2_hooks_dag(...) -> PackageHooksNode`, `ensure_configured_hook_permissions(...)`, `render_hooks(...) -> DigestionResult`: Compiles `src/<pkg>/drift_hooks/` into `render/<pkg>/.drift/hooks/` and enforces `0o755` permissions on configured hook scripts.
* `build_phase3_payload_dag(...) -> PackagePayloadNode`, `handle_driftignore_file(...)`, `render_package_files(...) -> Tuple[List[FileAction], int, int]`.
* `render_package(package_name: str, workspace_config: WorkspaceConfig, options: Optional[RenderOptions] = None) -> PackageRenderResult`: Executes the full 3-phase package render pipeline (Phase 1 config load $\rightarrow$ requirements & `probe` check $\rightarrow$ `pre_source` hook $\rightarrow$ Phase 2 hooks DAG $\rightarrow$ Phase 3 payload DAG $\rightarrow$ `post_render` hook).

---

## 4. Local Invariants & Modification Rules
1. **3-Bucket Lockfile Isolation**: Phase 1 (`RenderBucket.CONFIG`), Phase 2 (`RenderBucket.HOOKS`), and Phase 3 (`RenderBucket.PAYLOAD`) must only read/write/prune their own bucket in `RenderLockfile`.
2. **Control-Plane Shielding in Phase 3**: `prune_obsolete_payload_files` must strictly shield `render/<pkg>/.drift/` (`is_drift_internal_path`) so Phase 1 configs, Phase 2 hooks, and `.drift/render_lock.json` are never deleted during payload pruning.
3. **Restricted Environment Hashing**: `ExpansionContext.env_node` must always be constructed from `JsonNode(pkg_config.env_resolve.impact.restricted_env())`, never raw `os.environ`.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/render_dag.md`](../../../docs/features/render_dag.md), [`docs/features/empty_folder_dataflow.md`](../../../docs/features/empty_folder_dataflow.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_render.py
  PYTHONPATH=src python3 -m unittest tests/test_render_dag.py
  PYTHONPATH=src python3 -m unittest tests/test_render_cache.py
  ```
