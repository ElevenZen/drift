# Drift Environment Architecture: Modeling & Implementation

## 1. Executive Overview

Drift manages configuration and runtime variables across multi-package dotfile workspaces through a strictly encapsulated, 6-tier precedence model. The system cleanly separates **data gathering**, **pure graph-based resolution**, **two-phase host scoping**, and **cache-stable Merkle DAG hashing**.

This architecture eliminates ambient host pollution, ensures zero global process leakage, guarantees deterministic reproducibility across execution environments, and allows dotfiles to safely reference both auto-detected host facts and user-defined environment variables.

---

## 2. Core Domain Primitives & Modeling

The environment architecture is organized into four distinct structural layers and domain primitives:

```
Layer 4: Scoped Activation Context Managers
  │   - EnvImpact.scope(): Two-phase os.environ mutation context manager
  │   - env_scope(): Ephemeral single-mapping context manager
  │
Layer 3: Configuration Resolution & Interpolation Pipeline
  │   - resolve_env_configs(): Kahn's DAG topological sort across 6 tiers
  │   - interpolate_config_dict(): Full-environment string interpolation
  │
Layer 2: Host Mutation & Snapshot Primitives
  │   - update_env_dict(): Pure dictionary mutation with rollback snapshotting
  │   - restore_env_dict(): Exact state restoration (popping new keys, restoring old values)
  │
Layer 1: Pure Data Containers & Zero-Dependency Probes
      - SystemFacts: Low-level OS, CPU arch, distro, hostname, user, IP detection
      - SYSTEM_FACTS: In-memory process facts cache
      - EnvConfig: Quad-table container (override, secrets, default, fallback)
      - EnvImpact: Two-phase impact representation (overrides, defaults, secret_keys)
      - EnvResolve: Cumulative resolution container (current, effective, impact)
```

### 2.1 Host Facts Detection & Encapsulation ([`src/drift/utils/host_facts.py`](../src/drift/utils/host_facts.py))

* **Zero External Dependencies**: Host facts are probed directly via POSIX `libc` ctypes (`getifaddrs`), `/etc/os-release` parsing, Python standard library platform utilities, and UDP routing sockets.
* **Probed Properties (`SystemFacts`)**:
  * `drift_os`: Normalized operating system identifier (`linux`, `darwin`, `windows`, `freebsd`).
  * `drift_arch`: Normalized CPU architecture (`x86_64`, `arm64`, `x86`).
  * `drift_distro`: Normalized distribution identifier (`ubuntu`, `arch`, `debian`, `fedora`, `macos`, `windows`).
  * `drift_hostname`: Short local machine hostname without domain suffix.
  * `drift_user`: Current login user.
  * `drift_ip_addresses`: Semicolon-delimited non-loopback, non-link-local IPv4/IPv6 addresses.
* **Process Encapsulation (`SYSTEM_FACTS`)**:
  * Facts are stored in a private module dictionary `SYSTEM_FACTS: Dict[str, str]` via [`init_system_facts()`](../src/drift/utils/host_facts.py).
  * Ambient `os.environ` is **never polluted** at startup.
  * Standalone scripts and unit tests lazily populate the cache through [`get_cached_system_facts()`](../src/drift/utils/host_facts.py).

### 2.2 Configuration Containers & Immutability ([`src/drift/utils/env_utils.py`](../src/drift/utils/env_utils.py))

#### `EnvConfig` (Data Container)
Represents the 4 canonical environment tables defined in `drift_workspace.toml` or `drift_package.toml`:
* `override`: Forced overrides (Tier 1). Always takes precedence over ambient environment and defaults.
* `secrets`: Secret variables loaded from `[env.secrets]` or `config/secrets.env` (Tier 4).
* `default`: Standard workspace or package defaults (Tier 5).
* `fallback`: Low-priority fallbacks (Tier 6).

#### `EnvImpact` (Two-Phase Scoping Contract)
Models the exact mutation boundary that a configuration layer applies to the process:
* `overrides: Dict[str, str]`: Variables applied unconditionally (`overwrite=True`). Contains Tier 1 overrides and Tier 2 facts.
* `defaults: Dict[str, str]`: Variables applied conditionally (`overwrite=False`). Contains Tier 4 secrets, Tier 5 defaults, and Tier 6 fallbacks (kept mutually disjoint from overrides).
* `secret_keys: FrozenSet[str]`: Keys masked in all debug logs and diagnostics.
* `declared_keys: Set[str]`: The closed union of keys explicitly declared across overrides and defaults.

#### `EnvResolve` (Resolution Pipeline Container)
Constructed by [`resolve_env_configs()`](../src/drift/utils/env_utils.py):
* `current: EnvConfig`: Environment tables resolved locally for the current layer.
* `effective: EnvConfig`: Cumulative merged tables inherited from parent layers.
* `impact: EnvImpact`: The compiled two-phase impact ready for activation or inspection (automatically built from `effective` and `all_facts`).
* `build_impact(all_facts=None) -> EnvImpact`: Method on `EnvResolve` compiling `effective` tables and authoritative facts into an `EnvImpact`.

---

## 3. The 6-Tier Precedence Hierarchy

Drift enforces a 6-tier precedence hierarchy. Within each tier, **Package** configuration overrides **Workspace** configuration:

| Tier | Category | Source | Overwrites Host? | References Allowed |
| :---: | :--- | :--- | :---: | :--- |
| **Tier 1** | **Override** | `[env.override]` (Package > Workspace) | Yes (`overwrite=True`) | Can reference Tiers 2–6 |
| **Tier 2** | **Facts** | Package Facts > System Facts (`SYSTEM_FACTS`) | Yes (during resolution) | Protected; cannot be overridden by ambient env |
| **Tier 3** | **Ambient Context** | Host `os.environ` & CLI flags | Existing process state | Read-only during resolution |
| **Tier 4** | **Secrets** | `[env.secrets]` (Package > Workspace) > `secrets.env` | No (`overwrite=False`) | Can reference Tiers 5–6 |
| **Tier 5** | **Default** | `[env.default]` (Package > Workspace) | No (`overwrite=False`) | Can reference Tier 6 |
| **Tier 6** | **Fallback** | `[env.fallback]` (Package > Workspace) | No (`overwrite=False`) | Leaf fallback values |

### Topological Reference Resolution (Kahn's Algorithm)
Variables within and across tiers can dynamically reference other variables using `${VAR}` syntax.
* [`topological_sort_env()`](../src/drift/utils/env_utils.py) builds a directed acyclic graph (DAG) of inter-variable dependencies using internal helper `_extract_var_refs`.
* Detects cycles and self-references upfront, raising a structured [`ConfigError`](../src/drift/core/exceptions.py).
* Resolves values sequentially along topological order, allowing earlier-tier variables to reference later-tier fallbacks, secrets, and ambient host variables.

---

## 4. Scoping Boundaries & Dual Environment Views

A fundamental invariant of Drift is the strict separation between **Merkle DAG Incremental Cache Hashing** and **Runtime Process / Template Interpolation**:

### 4.1 Restricted Environment (`impact.restricted_env()`)
* **Purpose**: Incremental build hashing in Phase 1 & Phase 2 Render DAGs ([`ExpansionContext.env_node`](../src/drift/render/render_dag.py)).
* **Behavior**: Derives a dictionary strictly restricted to `impact.declared_keys`.
* **Rationale**: If cache hashing inspected the full `os.environ`, volatile shell variables (such as `SHLVL`, `_`, `SSH_AUTH_SOCK`, `XDG_SESSION_ID`, or terminal window titles) would invalidate the render cache on every command invocation. By restricting hashing strictly to declared keys (plus ambient overrides for declared keys), the Merkle tree remains completely stable across terminal sessions.

### 4.2 Full Environment (`impact.full_env()`)
* **Purpose**: TOML configuration string interpolation ([`interpolate_config_dict()`](../src/drift/utils/env_utils.py)) and hook/subprocess execution.
* **Behavior**: Merges `{**defaults, **base_env, **overrides}`.
* **Rationale**: Enables users to reference common host environment variables (such as `${HOME}`, `${USER}`, `${XDG_CONFIG_HOME}`) in workspace and package configurations (e.g. `target_directory = "${HOME}/.config/app"`) without requiring redundant declarations in `[env.fallback]`.

---

## 5. End-to-End Environment Call Graph & Lifecycle

The lifecycle spans 6 operational phases:

### Phase 1: Startup & Facts Ingestion
1. **CLI Ingestion**: [`prepare_cli_environment()`](../src/drift/cli/actions.py) invokes [`init_system_facts()`](../src/drift/utils/host_facts.py).
2. **Probe Execution**: [`SystemFacts.probe()`](../src/drift/utils/host_facts.py) gathers operating system, CPU architecture, distro, hostname, user, and IP addresses.
3. **In-Memory Storage**: Stored inside `SYSTEM_FACTS: Dict[str, str]`. Ambient `os.environ` remains untouched.

### Phase 2: Workspace Configuration Resolution
1. **Dynamic Workspace Hook**: [`apply_workspace_hook()`](../src/drift/hooks/workspace_hook.py) compiles `real_env = {**secrets_file, **ws_secrets, **os.environ, **system_facts, **ws_overrides}` and passes it to [`WorkspaceHookContext`](../src/drift/hooks/workspace_hook.py).
2. **Workspace Env Parsing**: [`workspace_loader.py`](../src/drift/config/workspace_loader.py) parses `[env]` into an `EnvConfig`.
3. **DAG Resolution**: Calls [`resolve_env_configs()`](../src/drift/utils/env_utils.py) with lower layer secrets from `config/secrets.env`.
4. **Workspace Interpolation**: Calls [`interpolate_config_dict()`](../src/drift/utils/env_utils.py) using `resolved_env.impact.full_env()`.

### Phase 3: Package Configuration Resolution
1. **Fact Gathering**: [`PackageConfig.get_drift_package_facts()`](../src/drift/config/package_config.py) resolves package facts (`drift_package_name`, `drift_package_source_dir`, `drift_package_render_dir`, `drift_package_install_dir`, `drift_package_target_dir`, `drift_package_install_method`).
2. **Dynamic Package Hook**: [`apply_package_hook()`](../src/drift/hooks/package_hook.py) executes `configure_package(context)`.
3. **Package Env Resolution**: [`resolve_env_configs()`](../src/drift/utils/env_utils.py) evaluates package tables against workspace effective tables and `all_facts = {**get_cached_system_facts(), **package_facts}`.
4. **Package Interpolation**: Calls [`interpolate_config_dict()`](../src/drift/utils/env_utils.py) using `resolved_env.impact.full_env()`.
5. **Package Model Instantiation**: Builds [`PackageConfig`](../src/drift/config/package_config.py) containing `env_resolve`.

### Phase 4: Requirements & Probe Evaluation
1. **Scoped Evaluation**: [`PackageConfig.evaluate_requirements()`](../src/drift/config/package_config.py) runs inside `with pkg_config.package_envs():`.
2. **Declarative Validation**: [`check_requirements()`](../src/drift/config/package_requirements.py) checks `os.environ` directly for required operating systems, architectures, hostnames, users, or IP addresses.
3. **Probe Execution**: Dynamic probe hook executes within the activated environment scope.

### Phase 5: Render Pipeline & Merkle DAG Hashing
1. **DAG Expansion & Hashing**: [`render_dag.py`](../src/drift/render/render_dag.py) computes `ExpansionContext.env_node` hash from `pkg_config.env_resolve.impact.restricted_env()`.
2. **Template Expansion**: Template engines (built-in [`python_envsubst()`](../src/drift/utils/env_utils.py), Jinja2, Mustache, Tera) evaluate templates inside `with pkg_config.package_envs():`.

### Phase 6: Stage, Install & Lifecycle Hooks
1. **Pipeline Execution**: Package installations, updates, and uninstalls execute inside [`PackageInstallContext.package_envs()`](../src/drift/primitives/install_repo.py).
2. **Two-Phase Scoping**:
   * Phase 1: Defaults applied conditionally (`overwrite=False`).
   * Phase 2: Overrides applied unconditionally (`overwrite=True`).
3. **Lifecycle Hook Injections**: Hook events wrap child processes in `with env_scope(env_injections, overwrite=True):` to provide transient metadata (`drift_hook_name`, `drift_hook_event`, `drift_hook_status`).
4. **Complete Rollback**: Context managers pop newly introduced variables and restore modified variables to their exact pre-scope values.

---

## 6. Architectural Guarantees & Invariants

* **Process State Purity**: Probing system facts never alters `os.environ`.
* **Zero Ambient Leakage in Tests**: Test runs execute with pristine dots-only stdout (`python3 -m unittest discover -s tests`).
* **Authoritative Package Precedence**: Within Tier 2, package facts (`drift_package_*`) take precedence over system facts (`drift_*`).
* **Cache Immunity**: Terminal session variables cannot invalidate the Phase 1 / Phase 2 render cache because DAG hashing binds strictly to `restricted_env()`.
* **No Legacy Compatibility Burden**: Outdated functions, compatibility shims, and transitional arguments (`inject_system_facts`, `INITIAL_ENV`, `DRIFT_SYSTEM_FACT_KEYS`, `effective_dict`, `extra_facts`, `build_effective_env_dict`) have been deleted.

---

## 7. Reference File Map

* Host Facts Detection & In-Memory Store: [`src/drift/utils/host_facts.py`](../src/drift/utils/host_facts.py)
* Environment Parsing, Resolution & Kahn's DAG: [`src/drift/utils/env_utils.py`](../src/drift/utils/env_utils.py)
* Workspace Configuration Model: [`src/drift/config/workspace_config.py`](../src/drift/config/workspace_config.py)
* Workspace Loader & Interpolation: [`src/drift/config/workspace_loader.py`](../src/drift/config/workspace_loader.py)
* Package Configuration Model: [`src/drift/config/package_config.py`](../src/drift/config/package_config.py)
* Package Loader & Interpolation: [`src/drift/config/package_loader.py`](../src/drift/config/package_loader.py)
* Declarative Requirements Evaluation: [`src/drift/config/package_requirements.py`](../src/drift/config/package_requirements.py)
* Dynamic Workspace Hooks: [`src/drift/hooks/workspace_hook.py`](../src/drift/hooks/workspace_hook.py)
* Dynamic Package Hooks: [`src/drift/hooks/package_hook.py`](../src/drift/hooks/package_hook.py)
* Lifecycle Hook Scoping: [`src/drift/hooks/lifecycle_hooks.py`](../src/drift/hooks/lifecycle_hooks.py)
* Render DAG Hashing: [`src/drift/render/render_dag.py`](../src/drift/render/render_dag.py)
* Core AI Architectural Reference: [`docs/ai_reference.md`](ai_reference.md)
