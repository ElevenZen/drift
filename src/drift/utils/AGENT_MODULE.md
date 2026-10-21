# `src/drift/utils/` — Layer 0 Foundation Utilities Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 0 (Foundation Utilities)**
* **Core Responsibility**: Provides low-level, domain-agnostic helper functions and foundational primitives for 3-state cross-platform path validation, filesystem mutation/inspection, TOML parsing/serialization, topological environment DAG resolution, Git sub-repository operations, subprocess execution, host platform probing, editor launching, and dynamic Python hook loading.
* **Dependency Rules**:
  * **Allowed Imports**: Standard library modules and minimal base constants/exceptions from [`../core/constants.py`](../core/constants.py), [`../core/exceptions.py`](../core/exceptions.py), and [`../core/mixins.py`](../core/mixins.py).
  * **Forbidden Imports**: Must **never** import from `models/`, `config/`, `hooks/`, `render/`, `primitives/`, or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Pure Data & Parsing**: [`toml_utils.py`](toml_utils.py) $\rightarrow$ [`toml_edit.py`](toml_edit.py), [`config_utils.py`](config_utils.py), [`path_utils.py`](path_utils.py), [`host_facts.py`](host_facts.py)
2. **Environment & Process**: [`process_utils.py`](process_utils.py), [`env_utils.py`](env_utils.py), [`python_hook_utils.py`](python_hook_utils.py)
3. **Filesystem & Git Operations**: [`file_inspect.py`](file_inspect.py), [`file_ops.py`](file_ops.py), [`git_utils.py`](git_utils.py), [`editor_utils.py`](editor_utils.py), [`workspace_root.py`](workspace_root.py)

---

## 3. Module & Public API Catalog

### [`path_utils.py`](path_utils.py) — 3-State Path Taxonomy & Dot-Prefix Translation
* `expand_path(path_input: Union[str, Path]) -> Path`: Expands `~` and environment variables (`$VAR`, `%VAR%`) into a `Path`.
* `check_is_relative_path(path: Path) -> bool`: Returns `True` iff `not path.anchor` (**State 1: Strictly Relative**).
* `check_is_partially_anchored_path(path: Path) -> bool`: Returns `True` iff `bool(path.anchor) and not path.is_absolute()` (**State 2: Partially Anchored**, e.g. Windows `/foo` or `C:foo`).
* `is_relative_to(path: Path, other: Path) -> bool`: Cross-version `Path.is_relative_to` check.
* `encode_dot_prefix(relative_path: Path) -> Path`: Translates `dot-` segments to `.` (`dot-config/nvim` $\rightarrow$ `.config/nvim`). Requires `not relative_path.anchor`.
* `decode_dot_prefix(relative_path: Path) -> Path`: Translates `.` segments back to `dot-` (`.config/nvim` $\rightarrow$ `dot-config/nvim`). Requires `not relative_path.anchor`.
* `relative_path_between(from_dir: Path, to_path: Path) -> Path`: Computes relative path from `from_dir` to `to_path`.
* `compute_relative_symlink_target(source_path: Path, link_parent_dir: Path) -> Path`: Computes relative symlink target pointing from `link_parent_dir` to `source_path`.
* `to_relative_path(path: Path, base_dir: Path) -> Path` & `to_relative_posix(path: Path, base_dir: Path) -> str`: Converts `path` relative to `base_dir`.
* `rebase_path(path: Optional[Path], old_base: Path, new_base: Path) -> Optional[Path]`: Rebases `path` from `old_base` onto `new_base` if relative to `old_base`.

### [`file_inspect.py`](file_inspect.py) — Read-Only Filesystem Inspection & Hashing
* `is_binary_file(file_path: Path) -> bool`: Detects NUL bytes in the first 8 KB.
* `file_hash(file_path: Path) -> str`: Computes SHA-256 hex digest of a file.
* `normalize_newlines(content: bytes, line_ending: LineEnding = LineEnding.LF) -> bytes`: Normalizes CRLF/LF line endings.
* `contents_differ(file1: Path, file2: Path, convert_line_endings: Optional[bool] = None) -> bool`: Compares file contents or symlink targets.
* `permissions_differ(file1: Path, file2: Path) -> bool` & `is_mode_only_change(file1: Path, file2: Path, ...) -> bool`: Compares POSIX executable/mode bits (`0o777`).
* `tree_files(dir_path: Path) -> List[Path]`: Recursively lists relative file paths under `dir_path`.
* `is_temp_file(file_name_or_path: Union[str, Path]) -> bool`: Matches `TEMPORARY_FILE_PATTERNS`.
* `is_concrete_dir(path: Path) -> bool`: Returns `True` iff `path.is_dir() and not path.is_symlink()`.
* `is_internal_lock_file(path: Path, base_dir: Optional[Path] = None) -> bool`: Identifies `.drift/render_lock.json`.
* `check_is_trackable(path: Path, base_dir: Optional[Path] = None) -> bool`: Filters out internal lockfiles, temp files, and concrete directories while keeping symlinks and user files.
* `check_can_symlink() -> bool`: Probes whether the current OS/user can create symlinks.

### [`file_ops.py`](file_ops.py) — Physical Filesystem Mutations (with Optional `sudo`)
* `ensure_dir(path: Path, sudo: bool = False) -> None`: Creates directory hierarchy (`mkdir -p`).
* `assert_writable(path: Path, sudo: bool = False) -> None`: Raises `TargetPermissionError` if `path` (or nearest existing ancestor) is not writable.
* `prune_empty_parents(dir_path: Path, limit_dir: Path) -> None`: Removes empty parent directories upward stopping at `limit_dir`.
* `copy_permissions(src: Path, dst: Path, sudo: bool = False) -> None`: Synchronizes POSIX file mode bits from `src` to `dst`.
* `remove_tree(path: Path, sudo: bool = False) -> None`: Recursively removes a directory tree or file (`rm -rf` / `shutil.rmtree` with read-only clearing).
* `remove_file_or_empty_dir(path: Path, sudo: bool = False) -> bool`: Safely removes a file, symlink, or **empty** directory (`rmdir`); skips non-empty directories with a warning.
* `remove_with_parents(file_path: Path, limit_dir: Optional[Path] = None) -> None`: Removes file and prunes empty parent directories up to `limit_dir`.
* `create_symlink(src: Path, dst: Path, sudo: bool = False) -> None` & `copy_symlink(src: Path, dst: Path, sudo: bool = False) -> None`: Creates or clones relative/target symlinks.
* `copy_file(src: Path, dst: Path, sudo: bool = False, convert_line_endings: Optional[bool] = None, ...) -> None`: Copies file preserving permissions and optional line-ending normalization.
* `write_file(dst: Path, content: Union[str, bytes], sudo: bool = False, permission: Optional[int] = None) -> None`: Atomically writes content to `dst` (temp file + `os.replace`, or `sudo mv`). `permission=None` preserves an existing regular file's mode; new files (or replaced symlinks) get `0o666 & ~umask`.
* `copy_tree(src: Path, dst: Path, sudo: bool = False) -> None` & `move_tree(src: Path, dst: Path, sudo: bool = False) -> None`: Copies or moves directory trees or files.

### [`env_utils.py`](env_utils.py) — 6-Tier Environment Resolution, Kahn's DAG & Scoping
* `EnvConfig`: Immutable container for `default`, `override`, `fallback`, and `secrets` environment mappings.
* `EnvImpact`: Compiled two-phase environment delta (`defaults`, `overrides`, `secret_keys`, `declared_keys`) exposing:
  * `.restricted_env(base_env=os.environ) -> Dict[str, str]`: Closed over `declared_keys` for deterministic Render DAG Merkle hashing.
  * `.full_env(base_env=os.environ) -> Dict[str, str]`: Full environment for TOML interpolation and subprocess execution.
  * `.scope(target_env=os.environ)`: Context manager applying defaults then overrides with automatic secret masking (`****`) in debug logs and guaranteed restoration on exit.
* `EnvResolve`: Holds `current: EnvConfig`, `effective: EnvConfig`, and `impact: EnvImpact`.
* `resolve_env_configs(current_raw, parent_effective=None, lower_secrets=None, all_facts=None, context_name="") -> EnvResolve`: Resolves the 6-tier environment hierarchy in-memory.
* `topological_sort(graph: Mapping[T, Set[T]], error_cls=ValueError) -> List[T]`: Generic Kahn's algorithm topological sort with cycle detection.
* `topological_sort_env(raw_env: Mapping[str, str], error_cls=ConfigError) -> List[str]`: Sorts environment variable keys by `${VAR}` reference dependencies.
* `interpolate_config_dict(data: Dict[str, Any], env: Mapping[str, str], skip_sections=...) -> Dict[str, Any]`: Recursively interpolates `${VAR}` across parsed config dictionaries.
* `python_envsubst(text: str, env: Optional[Mapping[str, str]] = None) -> str`: Pure-Python `envsubst` supporting `$VAR`, `${VAR}`, `${VAR:-default}`, `${VAR:+alt}`, and `\$` escaping.
* `read_plain_secrets_file(file_path: Path, required: bool = False) -> Dict[str, str]`, `parse_env_text(content: str, strict: bool = False, source_desc: str = "env") -> Dict[str, str]`, `parse_secrets_env(drift_root: Path) -> Dict[str, str]`, `parse_env_file(file_path: Path) -> Dict[str, str]`, `env_scope(...)`.

### [`toml_edit.py`](toml_edit.py) — Format-Preserving TOML Text Editing
* `set_toml_table_bools(text, table_keys, updates: Mapping[str, bool], create_table_if_missing=False) -> TomlBoolEditResult`: Line-surgical boolean edits inside a `[table]` section. Rewrites only the value token of existing single-line keys (keeping indentation, key quoting, `=` spacing, trailing comments), inserts missing keys after the section's last assignment, keeps CRLF and a missing final newline as-is, and raises `ValueError` on multi-line values, dotted-key conflicts, or a missing table (unless `create_table_if_missing`).
* `scan_toml_logical_lines(text) -> List[TomlLogicalLine]` & `find_toml_table_span(lines, table_keys) -> Optional[TomlTableSpan]`: Tokenizer-aware structure discovery (reuses `_ParserState` / `_strip_line_comment`), ignoring commented headers and header-like text inside multiline strings.
* `format_toml_key(key) -> str` & `split_toml_dotted_key(raw) -> List[str]`: Bare-vs-quoted key emission and unquoted-dot key splitting.
* `TomlEditAction` (`UPDATED` / `INSERTED` / `UNCHANGED`), `TomlLineKind`, `TomlLogicalLine`, `TomlTableSpan`, `TomlBoolEditResult`.

### [`config_utils.py`](config_utils.py) & [`toml_utils.py`](toml_utils.py) — Config Predicates & TOML Engine
* `partition(pred: Callable[[T], bool], items: Iterable[T]) -> Tuple[List[T], List[T]]`: Splits items into `(truthy_list, falsy_list)`.
* `branch(predicate: Union[bool, Callable[[], Any]], lambda1: Callable[[], T], lambda2: Callable[[], T]) -> T`: Evaluates `predicate` and invokes `lambda1()` if truthy, otherwise `lambda2()`.
* `choose(predicate: Union[bool, Callable[[], Any]], value1: T, value2: T) -> T`: Eager counterpart of `branch()`; returns `value1` / `value2` verbatim (callable values are never invoked). Use `branch()` when either side is expensive, side-effecting, or only valid under its condition.
* `when_not_none(a: Optional[S], b: Union[Callable[[S], Optional[T]], Callable[[], Optional[T]], Optional[T]]) -> Optional[T]`: Chooses/invokes `b` when `a is not None`, otherwise returns `None`.
* `when_none(a: Optional[S], b: Union[Callable[[], T], T]) -> Union[S, T]`: Chooses/invokes fallback `b` when `a is None`, otherwise returns `a`.
* `get_first_from(data: Mapping, keys: Sequence[str], default=None) -> Any`: Returns value for the first matching key alias.
* `get_nested_from(data: dict, keys: Union[str, Sequence[str]], default=None, required=False, is_table=False, context="configuration") -> Any`: Traverses nested dict paths with type/presence validation.
* `parse_bool_value(val: Any, default: bool = False, strict: bool = False, context: str = "") -> bool`: Coerces boolean values.
* `parse_command_tokens(raw_cmd: Any, field_name: str = "command", context: str = "") -> List[str]`: Parses a command string (`shlex.split(..., posix=True)`) or sequence of non-empty strings into `List[str]`.
* `parse_config_path(raw_val: Any, field_name: str = "path", default: Optional[Path] = None, expand: bool = False, context: str = "") -> Path`, `parse_optional_config_path(raw_val: Any, field_name: str = "path", expand: bool = False, dot_as_none: bool = True, base_dir: Optional[Path] = None, must_be_inside_base: bool = False, context: str = "") -> Optional[Path]`, & `resolve_path_within_base_dir(path: Path, base_dir: Path, field_name: str = "path", context: str = "") -> Path`: Coerces `(str, Path)` configuration fields into `Path` or `Optional[Path]` with optional `~` / `$VAR` expansion, `Path(".")` filtering, and strict `base_dir` containment validation.
* `validate_known_keys(data: Mapping, known_keys: Iterable[str], context: str = "", message_prefix: Optional[str] = None) -> None`: Raises `ConfigError` on unknown keys.
* `parse_toml(content: str) -> dict`, `dump_toml(data: dict) -> str`, `merge_toml(dict_a: dict, dict_b: dict) -> dict`: Zero-dependency TOML parser (`tomllib` on Python 3.11+ with custom fallback parser), serializer, and deep table merger.

### [`git_utils.py`](git_utils.py) — Sub-Repository Git Operations
* `GitRename(old_path: Path, new_path: Path)` & `GitStatusChanges(added, modified, deleted, renamed)`: Structured porcelain status models.
* `get_git_status_porcelain(repo_path: Path, subpath: Optional[Union[str, Path]] = None, find_renames: bool = False) -> Optional[str]`
* `has_uncommitted_modifications(repo_path: Path, subpath: Optional[Union[str, Path]] = None) -> bool`
* `parse_git_status_porcelain(porcelain_output: str, pkg_prefix: str = "") -> GitStatusChanges`
* `commit_repo_changes(repo_path: Path, commit_message: str, target_pkgs: Sequence[str] = (), repo_name: str = "repo") -> bool`: Stages (`git add -A`) and commits changes (scoped to `target_pkgs` if provided).
* `commit_staged_repo_changes(repo_path: Path, commit_message: str, repo_name: str = "repo") -> bool`: Commits only currently staged index changes.
* `get_git_toplevel(dir_path: Path) -> Path`: Pure Git toplevel lookup (raises `RuntimeError` outside a work tree). **Never** use it to locate the drift workspace root.
* `assert_git_repository_health(dir_path: Path) -> None`, `check_repo_can_commit(repo_path: Path) -> Optional[str]`, `assert_repo_can_commit(repo_path: Path) -> None`, `git_init_repo(dir_path: Path, name: str) -> bool`, `append_to_gitignore(drift_root: Path, folders_to_ignore: Sequence[str]) -> None`.

### [`workspace_root.py`](workspace_root.py) — Marker-Based Workspace Root Discovery
* `check_is_drift_workspace_root(dir_path: Path) -> bool`: True if `dir_path/config/` contains any `WORKSPACE_ROOT_MARKER_FILE_NAMES` file.
* `iter_self_and_ancestor_dirs(start_dir: Path) -> Iterator[Path]`: Resolved `start_dir` followed by its parents.
* `find_drift_workspace_root(start_dir: Path) -> Optional[Path]`: Nearest marked ancestor (nearest wins for nested workspaces).
* `resolve_drift_workspace_root(start_dir: Path) -> Path`: Same, raising `WorkspaceNotFoundError` (`ConfigError`, exit `2`) with an actionable hint. Used by `DriftCLIContext.get_drift_root()`.

### [`process_utils.py`](process_utils.py), [`host_facts.py`](host_facts.py), [`editor_utils.py`](editor_utils.py) & [`python_hook_utils.py`](python_hook_utils.py)
* `run_command(cmd, cwd=None, env=None, check=True, capture_output=True, text=True, timeout=None, encoding=None, ...)`: Unified subprocess runner with UTF-8/encoding handling and debug logging.
* `has_admin_privileges() -> bool` & `assert_can_escalate() -> None`: Checks root/Admin privileges or passwordless/cached `sudo` capability; raises `AdminPrivilegeError` on failure.
* `SystemFacts`, `init_system_facts() -> Dict[str, str]`, `get_cached_system_facts() -> Dict[str, str]`: Probes and caches `drift_os`, `drift_arch`, `drift_distro`, `drift_hostname`, `drift_user`, and `drift_ip_addresses` without mutating `os.environ`.
* `launch_single_file_editor(file_path: Path) -> None` & `launch_side_by_side_editor(file_pairs: Sequence[Tuple[Path, Path]]) -> None`: Launches `$VISUAL` / `$EDITOR` (`nvim -d`, `vim -d`, `code --wait --diff`, `emacs --eval '(ediff-files ...)'`).
* `load_python_module(module_path: Path, module_name: str)` & `execute_python_hook(hook_path: Path, func_name: str, context: Any, ...)`: Dynamically imports and executes Python preprocessor hook functions.

---

## 4. Local Invariants & Modification Rules
1. **3-State Path Discipline**: Always use `check_is_relative_path(p)` (`not p.anchor`) when requiring relative paths, and `check_is_partially_anchored_path(p)` (`p.anchor and not p.is_absolute()`) when validating hybrid paths before joining `base_dir / p`.
2. **Host Directory Deletion Safety**: `remove_file_or_empty_dir` must never recursively delete non-empty directories (`rmdir` only); recursive `remove_tree` is restricted to internal workspace/sandbox directories or explicitly confirmed source deletions.
3. **Zero Ambient `os.environ` Pollution**: `init_system_facts`, `resolve_env_configs`, and `execute_python_hook` must never mutate `os.environ` directly; only `EnvImpact.scope()` / `env_scope()` may temporarily scope environment variables.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/env_modeling.md`](../../../docs/features/env_modeling.md), [`docs/features/configuration_and_hooks.md`](../../../docs/features/configuration_and_hooks.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_utils.py
  PYTHONPATH=src python3 -m unittest tests/test_path_utils.py
  PYTHONPATH=src python3 -m unittest tests/test_env_utils.py
  PYTHONPATH=src python3 -m unittest tests/test_toml_utils.py
  PYTHONPATH=src python3 -m unittest tests/test_toml_edit.py
  PYTHONPATH=src python3 -m unittest tests/test_host_facts.py
  ```
