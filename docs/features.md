# 🧭 Drift Feature Guide

This guide is the long-form companion to the [README](../README.md). It is organized around Drift's three pillars, preceded by the core model and followed by end-to-end workflows.

* **[Part I — The Model](#part-i--the-model)**: sandbox isolation, the data-flow loop, config-as-a-package
* **[Part II — Compile Safely](#part-ii--compile-safely)**: environment hierarchy & secrets, render engines, Merkle caching, ignore rules
* **[Part III — Deploy Transactionally](#part-iii--deploy-transactionally)**: `drift plan`, collision guard, deployment methods, dependencies, lifecycle hooks, GC, detach
* **[Part IV — Reconcile Drift](#part-iv--reconcile-drift)**: reverse-sync, the Sentinel guard, `drift adopt`
* **[Part V — Workflows](#part-v--workflows)**: new config, adopting GUI changes, rollback, bootstrapping a machine

For internals (primitives, invariants, call chains), see [architecture.md](architecture.md) and the subsystem specs in [`features/`](features/).

---

## Part I — The Model

### 🛡️ Absolute Sandbox Isolation (Dual-Git Architecture)
Dotfile templating shouldn't put your live home directory at risk. Drift operates with **four decoupled layers**:
*   **Layer 1: Declarative Source (`src/`)**: Your raw, templated package dotfiles.
*   **Layer 2: Compilation Workspace (`render/`)**: An isolated Git database. Templates are compiled here on-the-fly. If a template render fails, **compilation halts instantly with zero impact on your system**.
*   **Layer 3: Local State Database (`install/`)**: A dedicated Git repository tracking your "last known good" state, backed by an explicit metadata database (`state.toml`).
*   **Layer 4: Active System Host (e.g. `~/`)**: The physical target paths.

Because Layer 2 and Layer 3 are isolated Git repositories, Drift can diff, stage, and transactionalize your configurations before a single symlink is modified on your host.

### 🔄 The Drift Data-Flow Loop

Rather than running isolated commands, Drift operates as a continuous, closed-loop state machine:

1. **Declarative Source** (`src/` & `config/`): compiled by `drift deploy` into…
2. **Sandbox Render Zone** (`render/`, isolated Git repo): staged as a delta into…
3. **Local State Database** (`install/`, tracking Git repo): applied (symlink / copy) onto…
4. **Active Host System** (`~/*`, `/etc/*`): whose live edits flow back into tier 3 via reverse-sync (Diff B), and into tier 1 via `drift adopt`.

### 💻 Modular "Config-as-a-Package" (One Unified Repo from Servers to Laptops)
Unlike monolithic dotfile managers that force you to deploy entire configurations wholesale or maintain separate git branches per machine, Drift structures every tool as an **independent, self-contained package** under `src/<pkg>/` (with its own `drift_package.toml`, target paths, deployment methods, and lifecycle hooks).

A **single, unified dotfiles repository** can effortlessly power everything from minimal cloud servers to high-performance GPU workstations and personal laptops:

> [!TIP]
> **Declarative `[secrets]` Providers & Dynamic Python Hooks**: For encrypted secret files (`age`, `gpg`, `sops`) or CLI password managers (`op`, `bw`, `pass`), use the declarative `[secrets]` table in `drift_workspace.toml` (see Section 4 below). For custom programmatic secret retrieval (e.g., querying HTTP APIs or injecting dynamic keys per machine), you can also use Python workspace/package hooks to populate `context.config["env"]["secrets"]`, `context.config["env"]["default"]`, or `context.config["env"]["override"]` before downstream templates compile.  

*   **Granular Machine Enablement (`config/drift_workspace.local.toml`)**:
    You can selectively enable or disable packages on each machine using the gitignored `config/drift_workspace.local.toml` override without modifying version-controlled source files:
    ```toml
    # config/drift_workspace.local.toml (Machine-specific override)
    [packages.enable]
    DEFAULT = false
    shell = true            # Enabled on all machines
    nvim = true             # Enabled on all machines
    cuda_toolkit = true     # Enabled only on high-performance GPU compute nodes
    desktop_hyprland = false# Disabled on headless servers, enabled on laptops
    ```

*   **Declarative Host Requirements & Platform Filtering (`[requirements]`)**:
    Instead of writing defensive shell scripts to check operating systems or installed binaries, packages can declaratively define host platform prerequisites in `drift_package.toml` via `[requirements]` (or `[package.requirements]`):
    ```toml
    # src/sway/drift_package.toml
    [package]
    name = "sway"
    install_method = "symlink"

    [requirements]
    os = ["linux"]                     # Target OS: "linux", "darwin", "windows"
    arch = ["x86_64", "aarch64"]        # CPU architectures
    distro = ["arch", "fedora"]         # Linux distributions
    binaries = ["sway", "waybar"]       # Executables required in host $PATH
    env = ["WAYLAND_DISPLAY"]           # Required non-empty environment variables
    ip = ["192.168.1.*", "10.0.0.0/8"]  # LAN IP match (exact, wildcard, or CIDR)
    ```
    *   **Executed Before Rendering (Zero-Cost Pre-Flight Skip)**: Requirement evaluation happens strictly **before template rendering begins** in Primitive 2. If any condition is not met, Drift **gracefully skips the package** (`status="SKIPPED"`) with an informative log message without executing template engines, compiling intermediate input files, writing to the `render/<pkg>/` sandbox, or interrupting the deployment of remaining packages.
    *   **Dynamic Complement with `probe` Hook**: For custom requirements that cannot be expressed declaratively (e.g. checking GPU vendor or hardware features), pair `[requirements]` with a dynamic `probe` lifecycle hook (`[hooks] probe = "drift_hooks/check_gpu.sh"`).

*   **Dynamic Python Workspace Hook (`config/drift_workspace.py`)**:
    For complete programmatic control, you can author a native Python hook (`config/drift_workspace.py` or configured via `[workspace] hook_file = "..."` relative to `config/`). Automatically scaffolded during `drift init`, this hook executes dynamically during configuration pre-processing without requiring external wrapper scripts, providing direct access to detected system facts (`drift_os`, `drift_hostname`, `drift_distro`, `drift_arch`, `drift_user`), hardware attributes, and discovered packages:
    ```python
    # config/drift_workspace.py
    from __future__ import annotations
    from typing import TYPE_CHECKING, Any, Dict

    if TYPE_CHECKING:
        from drift.hooks import WorkspaceHookContext

    def configure_workspace(context: WorkspaceHookContext) -> Dict[str, Any]:
        """Dynamically configure workspace packages and environment on the fly."""
        cfg = context.config
        facts = context.facts
        os_name = facts.get("drift_os", "")
        hostname = facts.get("drift_hostname", "")

        # 1. Dynamically compute enabled package roster
        enable = cfg.setdefault("packages", {}).setdefault("enable", {})
        enable["shell"] = True
        enable["nvim"] = True
        enable["cuda_toolkit"] = (os_name == "linux" and "gpu" in hostname)
        enable["desktop_hyprland"] = (os_name == "linux" and "laptop" in hostname)
        enable["macos_settings"] = (os_name == "darwin")

        # 2. Dynamically inject workspace-level environment variables / remote secrets
        env_default = cfg.setdefault("env", {}).setdefault("default", {})
        if os_name == "darwin":
            env_default["HOMEBREW_PREFIX"] = "/opt/homebrew"

        # (Optional) Download secrets or global tokens from remote vaults / APIs
        # env_secrets = cfg.setdefault("env", {}).setdefault("secrets", {})
        # env_secrets["GITHUB_TOKEN"] = fetch_vault_secret("github_token")

        return cfg
    ```

*   **Dynamic Python Package Hook (`src/<pkg>/drift_package.py` or `drift_hooks/`)**:
    Individual packages can also define a native Python hook (`src/<pkg>/drift_package.py` or configured via `[package] hook_file = "..."`), auto-scaffolded during `drift new <pkg>`. Defining `configure_package(context: PackageHookContext)` allows you to procedurally customize package behavior based on host facts, workspace context, and environment. You can dynamically adjust `target_directory`, inject custom environment overrides or remote secrets, or set `enable_install = False` to strictly disable a package on incompatible machines, OS families, or architectures:
    ```python
    # src/my_app/drift_package.py (or src/my_app/drift_hooks/drift_package.py)
    from __future__ import annotations
    from typing import TYPE_CHECKING, Any, Dict

    if TYPE_CHECKING:
        from drift.hooks import PackageHookContext

    def configure_package(context: PackageHookContext) -> Dict[str, Any]:
        """Dynamically configure package and disable installation on incompatible machines."""
        cfg = context.config
        pkg = cfg.setdefault("package", {})

        # Strictly disable package deployment on incompatible systems or architectures
        if context.os not in ("linux", "darwin") or context.arch == "arm64":
            pkg["enable_install"] = False
            return cfg

        # Dynamically customize target destination based on OS
        if context.os == "darwin":
            pkg["target_directory"] = "~/Library/Application Support/my_app"
        else:
            pkg["target_directory"] = "~/.config/my_app"

        # (Optional) Dynamically fetch package-specific secrets or configuration
        # env_override = cfg.setdefault("env", {}).setdefault("override", {})
        # env_override["APP_LICENSE_KEY"] = fetch_package_license("my_app")

        return cfg
    ```
    > **Isolation in `drift_hooks/`**: If you prefer keeping the package root clean or want the Python configuration hook stored alongside other lifecycle scripts and automatically excluded from host deployment, you can place it inside `drift_hooks/` (e.g. `src/<pkg>/drift_hooks/drift_package.py` with `hook_file = "drift_hooks/drift_package.py"`). Anything inside `drift_hooks/` is compiled into `.drift/hooks/` and is permanently ignored during deployment.

*   **Dynamic Host Profiling via Meta-Config Templating (`config/drift_workspace.local.envst.toml`)**:
    For automated fleet deployments across servers and laptops using shell environments, you can author a local configuration template:
    ```toml
    # config/drift_workspace.local.envst.toml (Template rendered on-the-fly)
    [packages.enable]
    DEFAULT = false
    ${DRIFT_PACKAGES}
    ```
    When Drift runs, `load_workspace_config_file_with_render` automatically evaluates `${DRIFT_PACKAGES}` into valid TOML key-value pairs, giving you dynamic, zero-touch machine provisioning!

---

## Part II — Compile Safely

### 🧩 Multi-Tier Environment Hierarchy & In-TOML Variable Stitching

Drift provides a strongly typed **6-tier environment variable precedence hierarchy** (Package > Workspace within each macro tier) with native topological DAG resolution (`$VAR`, `${VAR}`) directly within any TOML configuration file (`drift_workspace.toml`, `drift_package.toml`, and their `.local.toml` counterparts).

#### 📊 6-Tier Environment Precedence

| Tier | Name | Source | Description |
| :---: | :--- | :--- | :--- |
| **1** | **Override** | `[env.override]` | Explicit high-priority overrides (overwrites all lower tiers including ambient shell/CLI) |
| **2** | **Facts** | Package & system facts (`drift_*`) | Auto-probed host attributes and paths (authoritative internal metadata) |
| **3** | **CLI Context** | Ambient process (`os.environ`) & CLI | Command-line environment variables (takes precedence over secrets/defaults/fallbacks) |
| **4** | **Secrets** | `[env.secrets]`, `[secrets]`, `secrets.env` | Private credentials and tokens (decrypted in-memory and masked in logs) |
| **5** | **Default** | `[env.default]` | Standard package/workspace baseline values |
| **6** | **Fallback** | `[env.fallback]` | Soft floor defaults (evaluated only when unset across all upper tiers) |

#### 🔄 Resolution Cascade (`secret >> fallback >> default >> override`)
Environment tables are resolved using Kahn's topological DAG algorithm with strict unidirectional evaluation flow:
1. **`[env.secrets]`**: Evaluated first against host environment, system facts, and `[secrets]` / `config/secrets.env` values.
2. **`[env.fallback]`**: Evaluated with access to resolved secrets.
3. **`[env.default]`**: Evaluated with access to resolved secrets and fallback values.
4. **`[env.override]`**: Evaluated with full access to all resolved tables.

#### 🔐 Declarative Secret Decryption & CLI Providers (`[secrets]`)
Instead of storing plaintext credentials on disk, you can commit encrypted secret files (e.g., Age, GPG, OpenSSL, SOPS) directly into your repository or query password manager CLIs (`op`, `bw`, `pass`) via the `[secrets]` table in `config/drift_workspace.toml` (or machine-specific `config/drift_workspace.local.toml`). Secrets are decrypted **strictly in memory** (`shell=False`) and validated as UTF-8 `.env` (`KEY=VALUE`) pairs with zero disk leakage:

```toml
# config/drift_workspace.toml
[secrets]
# Example 1: Asymmetric in-repo encryption with Age & SSH private key
secrets_file = "secrets.env.age"           # %s: Relative to 'config/' (or absolute / ~/ / $VAR)
key_file = "~/.ssh/id_ed25519"             # %i: Optional identity/key file
decrypt_command = "age -d -i %i %s"        # Executed in-memory; stdout parsed as .env

# Example 2: 1Password / Bitwarden CLI provider (no key_file needed)
# secrets_file = "secrets.template.env"
# decrypt_command = "op inject --in-file %s"

# Example 3: Standalone command without secrets_file or key_file
# decrypt_command = "bw get notes drift-workspace-secrets"

# Example 4: Custom plaintext secrets file (when decrypt_command is omitted)
# secrets_file = "~/private/drift_secrets.env"
```

*   **Token Placeholders (`%s` & `%i`)**: `%s` expands to the resolved `secrets_file` path and `%i` expands to the resolved `key_file` path (relative paths resolve against `config/`). Each file is only required when its corresponding placeholder appears in `decrypt_command` (or when `decrypt_command` is omitted, in which case `secrets_file` is read directly as a plaintext `.env` file).
*   **Automatic `config/secrets.env` Fallback**: When `[secrets]` is omitted or left empty, Drift automatically loads `config/secrets.env` if present. Explicit entries in `[env.secrets]` take precedence over values loaded from `[secrets]` or `config/secrets.env`.

#### 📝 Concrete TOML Example

```toml
# config/drift_workspace.toml
[workspace]
default_target_directory = "${DRIFT_BASE_CONFIG}/drift"

[env.secrets]
# Tier 4: Private credentials (log-masked and transient)
VAULT_API_TOKEN = "${SECRETS_FILE_TOKEN:-token_123}"

[env.default]
# Tier 5: Standard topological variable stitching
DRIFT_HOST = "127.0.0.1"
DRIFT_PORT = "8080"
DRIFT_API_URL = "http://${DRIFT_HOST}:${DRIFT_PORT}/api"
DRIFT_BASE_CONFIG = "~/.config"

[env.fallback]
# Tier 6: Soft fallback baseline (used only if not defined in outer environment)
EDITOR = "nano"
```

```toml
# src/my_daemon/drift_package.toml
[package]
name = "my_daemon"
target_directory = "${DAEMON_HOME}/${drift_package_name}"

[env.override]
# Tier 1: Forced package-level override
DAEMON_DEBUG = "1"
DAEMON_SERVICE_URL = "${DAEMON_HOST}:${DAEMON_PORT}"

[env.default]
# Tier 5: Package-scoped defaults referencing package facts
DAEMON_HOST = "localhost"
DAEMON_PORT = "9090"
DAEMON_HOME = "${HOME}/services"

[env.fallback]
# Tier 6: Soft fallback
DAEMON_LOG_LEVEL = "info"
```

*   **Derived Variables**: Define inter-connected variables (e.g. `DRIFT_API_URL = "http://${DRIFT_HOST}:${DRIFT_PORT}/api"`) with automatic cycle detection.
*   **Cross-Section References**: Non-env sections (`target_directory`, `hooks`, etc.) dynamically reference any resolved variable without circular dependencies.
*   **Values-Only Scope**: Variable stitching operates **strictly within configuration field values** (strings, arrays, and numbers), never in TOML keys, table names, or section headers. (For dynamic keys or sections, use Python workspace hooks or `.envst.toml` templates).
*   **Package Fact Injections**: Automatically reference dynamic package and host facts (`${drift_package_name}`, `${drift_package_source_dir}`, `${drift_os}`, `${drift_arch}`) directly in your package configuration.
*   **Literal Escaping**: Use `\$VAR` or `\${VAR}` to preserve literal text when needed.
*   **Dynamic Programmatic Generation via Python Hooks (Preprocessor Stage)**: If in-TOML variable stitching doesn't cover your dynamic generation needs and you want to calculate configurations programmatically (e.g., executing Python logic, querying host hardware/APIs, or generating dynamic section tables), you can use native **Python workspace hooks** (`config/drift_workspace.py`) and **Python package hooks** (`src/<pkg>/drift_package.py`).
    > **Pipeline Execution Order**:
    > 1. **Multi-File Merge**: Candidate files (`drift_workspace.toml`, `drift_workspace.local.toml`, or custom layers) and templates are sequentially loaded and merged (`load_workspace_config_files_layered` / `render_load_package_config_dict`).
    > 2. **Dynamic Python Hook (Preprocessor)**: Executes *before* variable stitching, receiving the raw configuration dictionary with full access to resolved host facts and environment variables via `context`.
    > 3. **Variable Stitching & Topological Resolution (Compiler)**: Resolves all environment tables (`[env.secrets]`, `[env.fallback]`, `[env.default]`, `[env.override]`) and interpolates `${VAR}` across all non-env fields.
    > 4. **Validation & Model Instantiation**: Builds validated, strongly-typed configuration objects.

### 🔗 Custom Render Engines & DAG Pipeline Piping

Drift lets you declare custom render engines in `drift_workspace.toml` to compile any template format (e.g. `envsubst`, `mustache`, `jinja2`) into your `render/` sandbox:

#### A. Defining a Custom Render Engine
Register an engine by specifying its target file suffix, optional input variables file, and execution shell command:
```toml
# config/drift_workspace.toml
[render.envsubst]
suffix = "envst"                                         # Compiles any file ending with .envst
input_file = "envsubst.bash"                              # Injected environment script from config/
render_command = "bash -c 'source %i && envsubst < %s'"  # %i=input_file, %s=template
```
*   `%s`: Automatically replaced by the path to the template file being compiled.
*   `%i`: Automatically replaced by the resolved path to `input_file`.
*   `render_command = "internal"`: Built-in zero-dependency variable replacement engine (`[render.var]`).

#### B. Piping Engines Together (DAG Dependency Pipelines)
When an engine's `input_file` is itself a template (e.g. `mustache` needing a JSON configuration generated from environment variables), Drift automatically builds a dependency graph and pipes them in order:
```toml
[render.mustache]
suffix = "mustache"
input_file = "mustache.envst.json"                       # This .envst.json template is compiled by envsubst first!
render_command = "mustache %i %s"
```
*   **Automatic Dependency Ordering**: Drift compiles `config/mustache.envst.json` into `mustache.json` using the `envsubst` engine first, then passes the compiled JSON file as `%i` to `mustache`.
*   **Cycle Detection**: Drift validates the engine dependency graph to prevent infinite compilation loops, raising a clear `CyclicDependencyError` if circular references occur.
*   **Deferred Compilation**: Compilation is only performed when an enabled package in your workspace actually relies on the template engine, preventing unnecessary bottlenecks.

#### C. Package-Level Render Engines (Self-Contained Config Space & Portable Media)
In addition to workspace-level engines, packages can define custom render engines or override workspace engines directly in `drift_package.toml` via `[render.<name>]` tables:
```toml
# src/nvim/drift_package.toml
[render.custom_lua]
suffix = "tmpl"
input_file = "theme_vars.json"                             # Resolved relative to src/nvim/
render_command = "python3 render_template.py %i %s"
```
*   **Self-Contained Portable Media**: By declaring custom template engines, rules, and localized input data (e.g. `src/<pkg>/theme_vars.json` or `src/<pkg>/env.sh`) directly inside the package boundary, each Drift package becomes a **self-contained, fully portable configuration medium**. Packages can be freely shared, copied, or migrated across different repositories without requiring global engine definitions in `drift_workspace.toml`.
*   **Compilation Scope & Engine Rules**:
    *   **Global Engines Only for Package Config**: Only global workspace engines defined in `drift_workspace.toml` can compile dynamic package configuration templates (e.g. `src/<pkg>/drift_package.envst.toml`). This is because package configuration must be compiled during workspace bootstrap *before* package-level `[render.<name>]` definitions can be discovered or parsed.
    *   **Package-Level Engines Scope**: Render engines defined inside `drift_package.toml` (`[render.<name>]`) operate strictly during package source compilation on **package source dotfiles and templates** (under `src/<pkg>/`) and cannot be used to compile `drift_package.toml` itself.
*   **Field-Level Inheritance**: When overriding a workspace engine, unspecified attributes (`suffix`, `render_command`) are inherited automatically from the workspace engine, allowing you to override only `input_file` for that specific package.
*   **Intermediate Sandboxing (`.drift/render/`)**: Package-specific input templates compile into `render/<pkg>/.drift/render/`, keeping control plane compilation cleanly isolated from deployable dotfiles.
*   **Two-Way Synchronization**: Downstream commands (`drift reverse-sync`, `drift adopt`, `drift add`) seamlessly resolve template suffixes against these package-level engines.

### ⚡ Incremental Merkle DAG Compilation & Caching

Instead of relying on slow, procedural loops that blindly re-render templates and copy files on every run, Drift models the entire rendering subsystem as an **Abstract Syntax Tree (AST) Directed Acyclic Graph (DAG)** governed by **mathematical change invariance**:

*   **Cryptographic Merkle Invariance**: Every compilable entity computes a SHA-256 hash incorporating raw file bytes, POSIX file permissions mode (`0o755` vs `0o644`), destination paths, engine configuration, and deterministic scoped environment variables. If inputs, templates, environments, and engine rules haven't changed:

    ```text
    (ΔI = ∅) ∧ (ΔT = ∅) ∧ (ΔV = ∅) ∧ (ΔE = ∅) ⟹ (ΔO = ∅)
    ```

    where:
    *   $I$: Render engine input file (optional auxiliary asset or parent template output).
    *   $T$: Template source leaf file ($T : []$).
    *   $V$: Deterministic environment variable dictionary in the current scope.
    *   $E$: Render engine definition (suffix pattern, render command template).
    *   $O$: Compiled destination artifact in the `render/` sandbox ($O : [I, T, V, E]$).

    Drift binds each node to its direct prerequisites through dual-component Merkle cryptographic hashing:

    ```text
    own_hash(F)      = SHA256(path || mode || bytes)
    children_hash(N) = join(merkle_hash(D) for D in depends_on)
    merkle_hash(N)   = SHA256(NodeType || own_hash(N) || children_hash(N))
    ```

    If `merkle_hash(N)` matches the stored lockfile proof, compilation is bypassed instantly ($\mathcal{O}(1)$ cache hit / `SKIP_IDENTICAL` / 0ms overhead), avoiding redundant filesystem I/O and costly subprocess template engines.
*   **3-Phase AST Compilation Pipeline**:
    *   **Phase 1: Package Config & Environment AST**: Resolves inheritance, dynamic Python preprocessor hooks, and variable stitching into immutable, validated configuration objects.
    *   **Phase 2: Lifecycle Hooks AST**: Compiles `src/<pkg>/drift_hooks/` into `render/<pkg>/.drift/hooks/` and enforces executable `0o755` permissions across both source and sandbox files.
    *   **Phase 3: Payload Dotfiles AST**: Resolves engine dependencies, compiles templates, copies static assets, and automatically prunes obsolete rendered files while strictly protecting internal control plane metadata (`.drift/`).
*   **Lockfile Persistence (`.drift_lock.json`)**: Drift tracks digested cryptographic hashes in `render/<pkg>/.drift/render/.drift_lock.json`. Even across terminal sessions, repeated runs verify lockfile hashes for lightning-fast incremental synchronization. Force a clean re-compilation anytime with `-c / --clean / --no-cache`.
*   **Inspectable Dry-Run & Simulation (`--dry-run` & `--with-hooks`)**:
    *   **Pure Simulation (`drift render --dry-run`)**: Traverses the Merkle DAG and produces a complete execution plan of strongly typed `FileAction`s (`RENDER_ITEM`, `CREATE_COPY`, `UPDATE_COPY`, `SKIP_IDENTICAL`, `ENSURE_DIR`, `DELETE_FILE`, `PRUNE_DIR`) without modifying the filesystem or Git state.
    *   **Accurate Planning with Pre-Flight Hooks (`--with-hooks`)**: In pure dry-run, lifecycle hooks are bypassed by default for zero-mutation safety. Pass `--with-hooks` (`drift render --dry-run --with-hooks`) to execute pre-flight requirement probes (`probe`) and dynamic source generators (`pre_source`) inside isolated temporary sandboxes, giving you a 100% accurate deployment plan while keeping `render/` 100% clean and untouched.

### 🕵️ PCRE-Based Ignorance Rules
Drift uses standard Perl-Compatible Regular Expressions (PCRE) for its package ignore files (`.drift_ignore`). Its pattern matching syntax is derived from GNU Stow's ignore file specification (matching both relative path prefixes and basenames), with the sole architectural exception that Drift's internal control plane (`.drift/`) is always automatically ignored and never deployed to the host.
*   **Single Ignore File Restriction**: Drift strictly enforces exactly one `.drift_ignore` per package root, preventing fragmented and hard-to-audit nested ignore rules.
*   **Match Timing Guard**: Patterns are matched against native repository filenames *before* prefix expansion (e.g., matching `dot-bashrc` instead of `.bashrc`), eliminating translation bypasses.
*   **Render-Time Junk Filtering**: Machine-generated junk from the default list (Python `__pycache__/` and `*.pyc` written by package hooks, tool caches, VCS metadata, editor swap/backup files, `.DS_Store`) is skipped while compiling `src/` into `render/`, so it never reaches `render/` or `install/`. Deploy-policy defaults such as `^/README.*` and `^/LICENSE.*` are *not* enforced at render time and remain fully controlled by each package's `.drift_ignore`.

---

## Part III — Deploy Transactionally

### 🔍 Full-Cycle Dry-Run & Transactional Planning Engine (`drift plan`)
Drift eliminates blind deployments through an end-to-end, multi-stage dry-run engine (`drift plan` or `drift deploy -n / --dry-run`). Rather than merely verifying that symlinks can be created, Drift simulates the **complete compilation, staging, and deployment lifecycle** in an ephemeral sandbox without touching the host filesystem, intermediate databases, or Git repositories:

*   **Four Simulation Phases**:
    1.  **Phase 0: Host Drift Audit**: Non-destructively audits live host configurations against `install/` state via `prepare_reverse_sync`.
    2.  **Phase 1: Ephemeral Sandbox Template Compilation**: Compiles templates in a disposable temporary directory, verifying Merkle DAG cache hits while keeping `render/` 100% clean.
    3.  **Phase 2: Sandbox Staging Planning**: Compiles staging diff plans from sandbox render outputs to canonical `install/`.
    4.  **Phase 3: Physical Delivery Planning**: Audits cross-package collisions, checks directory writability, and compiles concrete host delivery actions (`CREATE_SYMLINK`, `UPDATE_COPY`, `BACKUP_OVERWRITE`, etc.).
*   **Symlink Path Masking**: Bridges the path divergence between ephemeral sandbox paths and canonical `install/` locations, ensuring existing host symlinks evaluate as `SKIP_IDENTICAL` rather than triggering false overwrite warnings.
*   **Fault Isolation & Downstream Blocking**: Traps errors per-package across render, stage, and install phases. A single broken package never obscures healthy sibling packages, while downstream dependents are cleanly marked as blocked.
*   **Pre-Flight Batch Conflict Audit & Failure Guidance**: Detects intra-batch collisions upfront (two packages targeting the same host destination) and displays prominent failure warnings if a real deployment would fail.
*   **Safe Hook Execution (`--with-hooks`)**: In pure dry-run, lifecycle hooks are bypassed by default for zero-mutation safety. Pass `--with-hooks` (`drift plan --with-hooks`) to execute pre-flight requirement probes (`probe`) and dynamic source generators (`pre_source`) in isolated scratch directories for 100% accurate dynamic template planning.
*   **Automated Tooling (`--json`)**: Serializes typed `WorkspaceDeployPreview` models containing global errors, per-package plans, and drift flags for CI/CD pipelines.

For full mathematical proofs, path mask algebra, and call chain specifications, consult the [Dry-Run Architecture Specification](features/plan_engine.md).

### 🛑 Proactive Collision Guard & Safeguards
Drift values your data integrity. Before any physical stage or deployment execution, the **Collision Guard** runs a multi-category safety audit:
*   **Zero Overwrite of Manual Files**: Any conflicting manual file on the host system is safely backed up to `backup/<package>/overwritten/` before deployment.
*   **Pruned Files Swept**: Deleted files are cleanly swept to `backup/<package>/deleted/`.
*   **Symlink Nesting Block**: Drift blocks deployments if the target directory written in configurations is equal to or nested inside the Drift workspace root directory.

> [!IMPORTANT]
> **Transient `backup/` Policy & User Responsibility**: The `backup/` folder stores displaced original files and pruned artifacts created during deployment collisions. **`backup/` is a local, unversioned directory that is neither tracked nor saved in Git by Drift.** Users are responsible for inspecting `backup/`, preserving critical historical assets, or committing them to private archival storage as needed.

### 📦 Deployment Methods (`symlink` vs. `copy`) & Event Ordering

Drift supports two deployment mechanisms declared in `drift_package.toml` (or defaulted via `default_install_method` in `drift_workspace.toml`):

| Feature | `symlink` (POSIX Default) | `copy` (Windows Default) |
| :--- | :--- | :--- |
| **Mechanism** | Relative symlink to `install/<pkg>/` | Atomic physical file copy |
| **Dot Translation** | `dot-` $\rightarrow$ `.` | `dot-` $\rightarrow$ `.` |
| **Storage Overhead** | Zero (symlink pointer) | Duplicate physical file |
| **Hot-Edits & Inotify** | Instant via symlink | Managed via deploy passes |
| **Windows Behavior** | Falls back to copy | Native file copy |
| **Lifecycle Ordering** | Content live at Staging | Strict (`pre_update` $\rightarrow$ copy $\rightarrow$ `post_update`) |

#### ⚠️ Event Ordering & Lifecycle Hook Semantics

Because Drift decouples template staging (Primitive 4: `render/` $\rightarrow$ `install/`) from host delivery (Primitive 5: `install/` $\rightarrow$ host), the timing of file updates relative to hook scripts differs:

*   **`symlink` (Symlink Pointers)**:
    Since the host file is a symlink directly targeting `install/<pkg>/`, updating file contents in `install/` during Staging (Primitive 4) makes those modifications immediately visible to the host **before** `pre_update` executes in Primitive 5. New file symlinks and deleted symlinks are still processed after `pre_update`.
*   **`copy` (Discrete Physical Files — Recommended for Daemons)**:
    Host files remain completely untouched at their previous version until Primitive 5 copies them over. This guarantees that `pre_update` runs while host files are **strictly in their old state**, followed by atomic physical file delivery, and finally `post_update`.

> [!TIP]
> **When to use `copy`**: If you are managing background services or daemons (e.g. `systemd` services with `inotify` watchers) that must be cleanly stopped in `pre_update` *before* configuration contents change on disk, configure the package with **`install_method = "copy"`**.

### 🔗 Inter-Package Dependencies & Topological Lifecycle Ordering
Packages can declare explicit runtime and staging dependencies on other packages using the `dependencies` array in `drift_package.toml`:
```toml
# src/zsh/drift_package.toml
[package]
name = "zsh"
dependencies = ["git", "starship", "fzf"]
```
*   **Automatic Topological Staging & Deployment**: Drift constructs a directed acyclic graph (DAG) across all enabled packages and sorts them topologically using Kahn's algorithm. Base dependencies are guaranteed to be compiled, staged, and deployed *before* dependent configurations.
*   **Cycle Detection & Missing Safeguards**: Circular dependencies (`CyclicDependencyError`) and missing prerequisite packages are caught upfront before any filesystem state is altered. When deploying, staging, or applying packages with missing dependencies, pass `--ignore-missing-dependencies` (or `--allow-missing-dependencies`) to bypass the check.
*   **Reverse Topological Uninstallation**: During package uninstallation (`drift uninstall`), packages are safely decoupled and removed in **reverse topological order**, ensuring dependent packages are dismantled before their underlying requirements.
*   **Downstream Protection**: Drift verifies that uninstalling a package will not break other installed packages that still depend on it, halting with an actionable error unless `--force` or `--ignore-missing-dependencies` is explicitly provided.

### 🪝 Robust Lifecycle Hook Integration
Packages can declare automated hook scripts inside `drift_package.toml` to integrate with external packages:
```toml
[package]
install_method = "symlink"

[requirements]
os = ["linux", "darwin"]
binaries = ["curl", "systemctl"]

[hooks]
probe = "drift_hooks/check_gpu.sh"
pre_source = "drift_hooks/generate_dynamic_templates.sh"
pre_install = "drift_hooks/bootstrap.sh"
post_update = "drift_hooks/reload_plugins.sh"
pre_uninstall = "drift_hooks/stop_daemon.sh"
health = "drift_hooks/health_check.sh"
timeout = 60
```

#### 🛡️ Pre-Flight Gatekeeper: Declarative Requirements & Dynamic Probe (Evaluated Before Rendering)
Requirement validation is performed strictly **before template rendering begins** (in Primitive 2). Drift evaluates a two-tier pre-flight gatekeeper:
1. **Declarative Host Requirements (`[requirements]`)**: Validates host criteria (`os`, `arch`, `distro`, `binaries`, `env`, `ip`) against automatically probed system facts without spawning subprocesses.
2. **Dynamic Probe Hook (`probe`)**: If declarative checks pass, executes the custom script defined in `probe = "drift_hooks/check_gpu.sh"`.

Because this evaluation occurs **prior to template compilation**:
* **Zero Unnecessary Rendering**: Template engines are never invoked, intermediate input files are not compiled, and no files are written to the `render/<pkg>/` sandbox for skipped packages.
* **Graceful Skipping**: If either check fails, Drift **gracefully skips the package** (`status="SKIPPED"`) with an informative reason in logs and summary outputs.
* **Uninterrupted Workflow**: Remaining active packages continue to compile, stage, and deploy without failure.

#### Hook Reference & Unified Working Directory (`cwd`)
Drift executes all lifecycle hooks with **unified working directories** (`cwd = hook_path.parent`, the directory containing the executed script) and automatic 6-tier environment variable injection (including host facts and package configs). The host target directory is accessible via `$drift_package_target_dir`.

| Hook | Trigger Timing | Commands |
| :--- | :--- | :--- |
| `probe` | Pre-flight requirement check | `deploy`, `render`, `status` |
| `pre_source` | Before reading or writing templates | `deploy`, `render`, `adopt`, `add` |
| `post_render` | After sandbox compilation | `deploy`, `render` |
| `pre_install` | Before initial package installation | `deploy`, `apply`, `rollback` |
| `post_install` | After initial package installation | `deploy`, `apply`, `rollback` |
| `pre_update` | Before updating installed package | `deploy`, `apply`, `rollback` |
| `post_update` | After updating installed package | `deploy`, `apply`, `rollback` |
| `pre_uninstall` | Before unlinking or deleting files | `deploy`, `uninstall`, `gc` |
| `post_uninstall` | After unlinking or deleting files | `deploy`, `uninstall`, `gc` |
| `health` | Runtime service health verification | `health` |

> [!IMPORTANT]
> **Unified Hook Working Directory (`cwd`)**: Across all lifecycle hooks, the execution working directory defaults to `hook_path.parent` (the directory containing the executed script). This allows sibling helper scripts (e.g., `. ./helper.sh` or `. ./lib/utils.sh`) to be sourced naturally relative to the script regardless of execution stage. The destination target directory is accessible via `$drift_package_target_dir`.

> [!TIP]
> **Strict `drift_hooks/` Placement, Dependencies & Symlinks**:
> * **Dedicated Placement**: All package lifecycle scripts and auxiliary helper dependencies MUST reside within `src/<pkg>/drift_hooks/` (e.g., `src/<pkg>/drift_hooks/bootstrap.sh`, `src/<pkg>/drift_hooks/lib/helper.sh`). Any relative hook path outside `drift_hooks/` is rejected with a `ConfigError`.
> * **Automatic Host Isolation**: Drift compiles `drift_hooks/` into `render/<pkg>/.drift/hooks/` and stages it into `install/<pkg>/.drift/hooks/`. Because `.drift/` is an internal Drift metadata directory, hook scripts, Python configuration hooks, and helper libraries are completely isolated from deployment and never deployed or symlinked to the host target directory.
> * **Deploying Hook Files or Sharing Dependencies**: If a hook script or a file needed by a hook also needs to be deployed to the host (e.g. a CLI tool under `bin/`), or if sharing hook scripts across packages, create a symlink inside `src/<pkg>/drift_hooks/` (e.g. `ln -s ../bin/my_cli src/<pkg>/drift_hooks/my_cli`).

> [!NOTE]
> **Privilege & Environment Model**: All lifecycle hooks execute **in user space without `sudo`**, preserving all 6 tiers of environment variables (`$drift_package_*`, `$drift_*`, `[env.override]`, `[env.secrets]`, `[env.default]`, `[env.fallback]`). If elevated root privileges are required for a specific command (e.g., restarting a system daemon), write `sudo` explicitly within the hook script. Note: `$drift_package_src_dir` is an alias for `$drift_package_source_dir`.

*   **Bypassing Hooks**: Pass `--no-hooks` (or `--no-hook`) to skip lifecycle hooks on any deployment command (`deploy`, `apply`, `render`, `adopt`, `add`, `uninstall`, `rollback`, `gc`).
*   **Dry-Run Hook Execution (`--with-hooks`)**: In simulation mode (`drift render --dry-run`), lifecycle hooks default to disabled (`--no-hooks` is active). Passing `--with-hooks` authorizes execution of pre-flight preparation hooks (`probe` and `pre_source`) in isolated temporary sandboxes without mutating `render/`, resolving requirements and dynamic templates for an accurate render plan.
*   **Direct Hook Execution**: Trigger any hook in isolation via `drift hook <pkg> <hook> [--from source|install]` (where stage is `source` or `install`).

### 🧹 Autonomous Garbage Collection (Self-Cleaning)  
Garbage collection is triggered automatically at the end of a bulk `drift deploy` (when deploying all packages across the workspace) or executed on demand using the explicit `drift gc` command (with optional `--dry-run` inspection).

When you toggle packages to `false` in `drift_workspace.toml` or delete package source folders, Drift's **Garbage Collection** automatically uninstalls the orphaned host files, purges untracked "zombie" folders inside `render/` and `install/`, and **commits the purges inside the database Git repositories**. 
*   **Automatic Trigger on Global Deploy**: Running `drift deploy` without package arguments automatically sweeps and purges stale database packages at the end of the deployment cycle.
*   **Manual Trigger**: Run `drift gc` anytime to clean orphaned state or `drift gc --dry-run` to preview purges safely.
*   **Isolated Commit Scoping**: The GC process only commits the specific directories it purges, ensuring unrelated system modifications are left untouched and auditable.

### 🔌 Decouple & Eject Packages on Demand (Detach Mode)
Sometimes, you want to stop managing a configuration through a dotfile manager but keep the configurations permanently active on your host system. 
*   **Keep Active Configurations**: Drift supports a dedicated **Detach Mode (`drift uninstall <pkg> --detach`)** that unregisters the package without deleting any files on your system.
*   **Symlink to Copy Conversion**: If the package was installed via symlinks, the detach engine automatically replaces every system-level symlink with its actual, physical file copy. Your configuration is "frozen" as an independent file on your host target.
*   **Backups Untouched**: Your historical original system backups inside `backup/<package>/overwritten/` are kept completely intact (not restored or deleted).
*   **Decoupled Registry**: Cleanly deletes database directories and unregisters the package from `state.toml`, safely letting you "eject" a package on demand.

---

## Part IV — Reconcile Drift

### 🔄 Capture & Adopt GUI/System Tool Settings (Intelligent Reverse-Sync)
Modern desktop environments, IDEs, and system utilities frequently write configuration modifications directly to your active files (e.g., when you adjust settings in a GUI preferences dialog, alter theme colors in a desktop control panel, or customize keybindings through an application UI).

In traditional dotfile managers, **these GUI-driven updates are silently lost—either completely ignored or blindly wiped away and overwritten during your next template deployment pass**.

Drift fundamentally resolves this mismatch by treating configuration as a **continuous, two-way loop with built-in safety sentinels**:

* **Live edit on the host** (GUI tweak, installer, hot-edit) is reverse-synced into `install/`; `drift deploy` trips the Sentinel guard.
  * **1. Inspect**
    * `drift status`: overview (`[B] System: DRIFTED`)
    * `drift diff -s [-y]`: line-by-line diff (`-y` opens your visual editor)
  * **2. Decide**
    * Keep: `drift adopt <pkg> [-i]` backports the patch into `src/`
    * Discard: `drift deploy <pkg> --force` overwrites the host from `src/`

#### 🛡️ The Real-World Workflow:
1. **Trigger (Sentinel Alert or Background Tweaks)**:
   - When you make changes through a GUI application, tweak tools, or edit files directly in `$HOME`, Drift automatically tracks these live adjustments.
   - If you run `drift deploy`, Drift's **Stage 1 Sentinel Guard** audits host alignment. If uncommitted system modifications exist, deployment safely **halts immediately with exit code `3` (`DRIFT_DETECTED`)** to prevent accidental lost updates.

2. **Audit & Inspection**:
   - **High-Level Overview (`drift status`)**: Run `drift status` to see an overview of which package(s) have drifted on the host (`[B] System: DRIFTED`).
   - **Terminal Diff (`drift diff -s`)**: Run `drift diff -s` (or `drift diff -s <pkg>`) to inspect exact line-by-line differences between the live host system and the state database.
   - **Visual Side-by-Side Review (`drift diff -s -y`)**: Pass `-y` (or `--side-by-side`) to launch an interactive visual side-by-side session in your configured editor (`$VISUAL` / `$EDITOR`, such as Neovim, Vim, VS Code, or GNU Emacs) across multi-tab split viewports.

3. **Reconciliation Decision**:
   - **Option A: Adopt Changes into Templates (`drift adopt <pkg>`)**:
     If you want to keep the GUI/system adjustments, run:
     ```bash
     drift adopt <pkg>               # Automatically backports patches to src/<pkg>/
     drift adopt <pkg> --interactive # Interactively review each modified/added file
     ```
     Drift extracts the live edits as clean Git patches, translates dotfile prefixes, safely backports changes into your declarative templates in `src/`, and marks the state database as clean. You can then run `drift deploy` normally.
   - **Option B: Discard & Force-Restore Templates (`drift deploy <pkg> --force`)**:
     If you want to reject the GUI changes and restore the pristine templated configuration declared in `src/`, run:
     ```bash
     drift deploy <pkg> --force      # Bypasses Sentinel guard and overwrites host files
     ```
     Drift overrides the active drift and reinstalls clean files compiled from your source templates.

---

## Part V — Workflows

### Scenario A: Declaring & Deploying a New Configuration (One-Way Forward Flow)
*When you want to manage a new configuration file (e.g., Neovim config) with declarative templates.*

1.  **Scaffold**: Run `drift new nvim -t ~/.config/nvim` to create a package directory.
    *   **Folder Structure Relieved**: By setting `target_directory = "~/.config/nvim"` inside `src/nvim/drift_package.toml`, you no longer need nested directories like `dot-config/nvim/` on disk. Files are put directly inside `src/nvim/`.
2.  **Author**: Add template or files into `src/nvim/` (e.g., `src/nvim/init.envst.lua` containing `${ENV_VAR}`).
3.  **Plan & Verify**: Run `drift plan` (or `drift deploy -n`) to preview the complete multi-stage execution plan (template compilation, staging diffs, host symlink/copy actions, drift audits, and collision checks) in an ephemeral sandbox without altering any system or Git state.
4.  **Deploy**: Run `drift deploy` (which triggers the functions: Render $\rightarrow$ Commit Render $\rightarrow$ Stage $\rightarrow$ Install Deployment $\rightarrow$ Commit Install).
    *   All templates are compiled inside the sandbox, changes staged into the local state database, and configurations safely copied/linked onto your target active host target path (`~/.config/nvim/init.lua`).

### Scenario B: Adopting GUI/System Utility Changes (Reverse-Sync & Backport Flow)
*When a GUI app (like qBittorrent, VSCode, or system themes) writes settings directly on disk, and you want to merge them back into your source templates.*

1.  **Detect**: Run `drift status` or `drift diff -s` to inspect active system drifts.
2.  **Reverse-Sync**: Drift automatically reverse-syncs system adjustments into your local state repository (`install/`).
3.  **Adopt & Merge**: Run `drift adopt nvim --interactive`.
    *   Drift pre-stages all changes in `install/` to activate Git's rename detection.
    *   It extracts system edits as a programmatic patch and cleanly applies them onto your templates in `src/`, avoiding placeholder collisions.
    *   If you choose to **Skip** a file, it is selectively unstaged (`git restore --staged`) to remain as uncommitted local drift, preserving unresolved issues.

### Scenario C: Midway Deployment Recovery (Rollback Flow)
*When a deployment template compilation, file permission error, or hook script crashes midway, leaving your host system in an inconsistent, half-written state.*

1.  **Rollback**: Run `drift rollback nvim`.
    *   Drift resets the package's local state database directory to the last committed clean HEAD, purges half-written/untracked files via `git checkout HEAD` and `git clean -fd`.
    *   It then performs a full redeploy fallback to safely restore system files to the last committed stable state.

### Scenario D: Bootstrapping a New Machine or Migrating Legacy Dotfiles (Clone Flow)
*When you set up a fresh machine and want to replicate your existing Drift configuration repository in one command, or migrate an old plain dotfiles repository.*

1.  **Clone Drift Workspace**: Run `drift clone git@github.com:username/dotfiles.git`.
    *   Drift fetches the remote repository and automatically executes a non-destructive repair (`drift repair`) to reconstruct the excluded `render/` and `install/` databases, `.gitignore` rules, and machine-specific local configuration templates (`config/drift_workspace.local.toml`, `config/secrets.env`).
2.  **Migrate Legacy Plain Dotfiles Repo**: Run `drift clone https://github.com/username/legacy-dotfiles.git`.
    *   Drift detects that the repository is a traditional plain dotfiles repository, isolates existing dotfiles into `src/legacy-dotfiles/`, generates `drift_package.toml` and `.drift_ignore`, and scaffolds the full Drift workspace infrastructure automatically.
3.  **Review & Deploy**: Navigate into the directory (`cd dotfiles`), configure machine-specific overrides in `config/drift_workspace.local.toml` / `config/secrets.env`, and run `drift deploy`.
