# `src/drift/config/` — Layer 2 Configuration & Template Schemas Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 2 (Workspace & Package Configuration Models, Loaders & Default Templates)**
* **Core Responsibility**: Defines strongly-typed configuration domain models and ingestion pipelines for global workspaces (`drift_workspace.toml`), packages (`drift_package.toml`), custom render engines (`[render.<name>]`), declarative host requirements (`[requirements]`), and lifecycle hooks (`[hooks]`), as well as the default TOML scaffolding templates in [`../templates/`](../templates/).
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../hooks/`](../hooks/) (for executing `drift_workspace.py` / `drift_package.py` and triggering lifecycle hooks), and [`../render/`](../render/) (in `package_loader.py` for Phase 1 `PackageConfigNode` DAG compilation).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Leaf Config Schemas**: [`render_engine_config.py`](render_engine_config.py), [`secrets_config.py`](secrets_config.py), [`package_requirements.py`](package_requirements.py), [`package_hooks.py`](package_hooks.py)
2. **Core Config Domain Models**: [`workspace_config.py`](workspace_config.py) $\rightarrow$ [`package_config.py`](package_config.py)
3. **Layered Ingestion & DAG Loaders**: [`workspace_loader.py`](workspace_loader.py) $\rightarrow$ [`package_loader.py`](package_loader.py)

---

## 3. Module & Public API Catalog

### [`render_engine_config.py`](render_engine_config.py) — Custom Render Engine Schema & Registry
* `parse_render_command(raw_cmd, engine_name="") -> List[str]` & `parse_render_input_file(raw_input, base_dir: Path, engine_name="") -> Optional[Path]`: Pure parsers for engine commands and relative/absolute `input_file` validation against `base_dir`.
* `RenderEngineConfig(name, suffix, render_command, input_file=None)`: Validates engine suffix (no dots `.`, not in reserved keywords) and `%i` / `%s` command placeholders (`requires_input_file`), and constructs instances via `.from_dict(name, config_dict, base_dir)`.
* `RenderSourceMatch(path, engine, target_name, status="match")`: Result of matching a template filename against registered engine suffixes.
* `RenderEngineRegistry(engines: Dict[str, RenderEngineConfig])`:
  * `.from_dict(config_dict, base_dir: Path) -> RenderEngineRegistry`: Normalizes relative `input_file` paths against `base_dir` via `RenderEngineConfig.from_dict` (rejecting partially anchored paths and paths escaping `base_dir`).
  * `.overlay(other: RenderEngineRegistry) -> RenderEngineRegistry`: Merges package-level engine definitions over workspace engines with field-level inheritance.
  * `.find_engine_for_file(filename: str) -> Optional[RenderEngineConfig]`: Matches `[filename].[suffix].[ext]` or `[filename].[suffix]`.

### [`secrets_config.py`](secrets_config.py) — Workspace Secrets Schema & Provider (`[secrets]`)
* `parse_decrypt_command(raw_cmd) -> List[str]`: Parses `decrypt_command` via `parse_command_tokens`.
* `SecretsConfig(decrypt_command=[], secrets_file=None, key_file=None)`: Parses `[secrets]` (`decrypt_command`, `secrets_file`, `key_file` with `%i` / `%s` placeholder validation and 3-state hybrid path validation).
* `fetch_workspace_secrets(drift_root: Path, secrets_config: Optional[SecretsConfig] = None) -> Dict[str, str]`: Fetches/decrypts workspace secrets in-memory (falling back to `config/secrets.env` when omitted).

### [`package_requirements.py`](package_requirements.py) — Declarative Host Prerequisites (`[requirements]`)
* `PackageRequirements(os=(), arch=(), distro=(), binaries=(), env=(), ip=())`:
  * `.from_dict(data, package_name="") -> PackageRequirements`: Validates known keys (`os`, `arch`, `distro`, `binaries`, `env`, `ip`).
  * `.check_requirements() -> Tuple[bool, Optional[str]]`: Evaluates platform facts, `shutil.which` binaries, `os.environ` variables, and LAN IP wildcard/CIDR patterns (`match_ip_addresses`).

### [`package_hooks.py`](package_hooks.py) — Package Lifecycle Hooks Schema (`[hooks]`)
* `PackageHooks`: Holds resolved absolute paths for `probe`, `pre_source`, `post_render`, `pre_install`, `post_install`, `pre_update`, `post_update`, `pre_uninstall`, `post_uninstall`, `health`, plus `timeout: int = 120` and `failure_is_fatal: Union[bool, List[str]] = True`.
  * `.from_dict(data, package_name="", base_dir=None, workspace_config=None) -> PackageHooks`: Validates that all relative hook paths reside strictly under `drift_hooks/`, maps stage-specific execution directories (`render_base` vs `install_base` into `.drift/hooks/`), supports Windows-specific overrides (`[hooks.windows]`), and parses `failure_is_fatal`.
  * `.is_failure_fatal(hook_name: str) -> bool`: Evaluates whether a failure in `hook_name` (within `INSTALLATION_HOOK_NAMES`) is fatal to installation.
  * `.trigger_probe(...)`, `.trigger_pre_source(...)`, `.trigger_post_render(...)`, `.trigger_pre_install(...)`, `.trigger_post_install(...)`, `.trigger_pre_update(...)`, `.trigger_post_update(...)`, `.trigger_pre_uninstall(...)`, `.trigger_post_uninstall(...)`, `.trigger_health(...)`.

### [`workspace_config.py`](workspace_config.py) & [`workspace_loader.py`](workspace_loader.py) — Workspace Configuration
* `SettingsConfig(ensure_hooks_executable_in_src=True, hook_inject_non_interactive_envs=True, hook_output_encoding="utf-8", rollback=RollbackPolicy.AUTO)`: Parses `[settings]` with canonical keys and standard aliases.
* `WorkspaceSectionConfig`: Parses `[workspace]` (`source_directory`, `render_directory`, `install_directory`, `backup_directory` as strictly relative `not p.anchor`; `default_target_directory` as strictly absolute `p.is_absolute()`).
* `WorkspaceConfig`: Top-level workspace domain object holding `drift_root`, `workspace`, `settings`, `render_engine_configs`, `secrets: SecretsConfig`, `packages_enable`, `env_resolve: EnvResolve`, `render_cache: RenderCache`, and optional sandbox path masks (`render_path_mask`, `install_path_mask`).
  * `.fetch_secrets() -> Dict[str, str]`: Delegates to `fetch_workspace_secrets(self.drift_root, self.secrets)`.
  * Properties: `.source_path`, `.render_path`, `.install_path`, `.backup_path`, `.default_target_path`.
  * Discovery helpers: `.is_package_enabled(pkg) -> bool`, `.get_package_names_from_source_dir() -> List[str]`, `.filter_source_packages_by_target(target_pkgs=()) -> List[str]`, `.filter_render_packages_by_target(...)`, `.filter_install_packages_by_target(...)`.
* `load_workspace_config(drift_root: Path, check_legacy: bool = True, config_files_override=None, quiet: bool = False) -> WorkspaceConfig`: Full ingestion pipeline loading `drift_workspace.toml` (or `.envst.toml`), merging `.local.toml`, fetching workspace secrets via `fetch_workspace_secrets`, applying `config/drift_workspace.py` (`apply_workspace_hook`), and resolving `[env]` via `resolve_and_interpolate_workspace_config`.

### [`package_config.py`](package_config.py) & [`package_loader.py`](package_loader.py) — Package Configuration
* `PackageDependency(name: str, optional: bool = False)` & `PackageDependencies(items: List[PackageDependency])` (`.required_names`, `.optional_names`, `.all_names`).
* `PackageSectionConfig`: Parses `[package]` (`name`, `enable_render`, `enable_install`, `install_method`, `target_directory`, `source_directory`, `sudo`, `auto_rollback`, `fully_controlled_dirs`, `dependencies`, `hook_file`).
* `PackageConfig`: Top-level package domain object holding `package: PackageSectionConfig`, `hooks: PackageHooks`, `requirements: PackageRequirements`, `render_engine_configs: RenderEngineRegistry`, and `env_resolve: EnvResolve`.
  * `.package_envs()`: Context manager scoping the 6-tier package environment (`self.env_resolve.impact.scope()`) into `os.environ`.
  * `.evaluate_requirements(workspace_config, flags=None) -> Tuple[bool, Optional[str]]`: Evaluates declarative `[requirements]` and `probe` hook before template compilation.
  * Class constructors / loaders:
    * `PackageConfig.from_source_dir(package_dir, workspace_config=None)` / `load_package_config_from_source_dir(...)`: Compiles Phase 1 `PackageConfigNode` DAG into `render/<pkg>/.drift/drift_package.toml`.
    * `PackageConfig.from_render_dir(package_dir, workspace_config=None)` / `load_package_config_from_render_dir(...)`: Reads rendered `.drift/drift_package.toml` from `render/<pkg>/`.
    * `PackageConfig.from_install_dir(package_dir, workspace_config=None)` / `load_package_config_for_install(...)`: Reads staged `.drift/drift_package.toml` from `install/<pkg>/`.

### [`../templates/`](../templates/) — Default Configuration Templates
* [`../templates/drift_workspace_default.toml`](../templates/drift_workspace_default.toml) & [`../templates/drift_workspace_default.py`](../templates/drift_workspace_default.py): Default `drift_workspace.toml` and `drift_workspace.py` templates used by `drift init` and `drift repair`.
* [`../templates/drift_package_default.toml`](../templates/drift_package_default.toml) & [`../templates/drift_package_default.py`](../templates/drift_package_default.py): Default `drift_package.toml` and `drift_package.py` templates used by `drift new` and `drift clone`.

---

## 4. Local Invariants & Modification Rules
1. **Boundary Normalization at `from_dict`**: All path fields, enums (`InstallMethod`, `RollbackPolicy`), and key allowlists (`validate_known_keys`) must be normalized and validated inside `from_dict` so downstream code never performs defensive string parsing.
2. **Configuration Key & Alias Discipline**: Canonical keys follow `<what_you_want_to_do>_<limitation/where/when>`. Aliases exist strictly for singular/plural variance or standard abbreviations.
3. **Global Engines Only for Phase 1 Package Config**: `load_package_config_from_source_dir` must only use `workspace_config.render_engine_configs` to compile `drift_package.<suffix>.toml`.

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/configuration_and_hooks.md`](../../../docs/features/configuration_and_hooks.md), [`docs/features/env_modeling.md`](../../../docs/features/env_modeling.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_config.py
  PYTHONPATH=src python3 -m unittest tests/test_package_dependencies.py
  PYTHONPATH=src python3 -m unittest tests/test_requirements.py
  ```
