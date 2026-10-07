# 🧪 Sandbox Compilation & Render Directory: `render/`

Drift decouples dotfile authoring from system deployment through a dedicated compilation stage: **Render**.
This document introduces both the **Render Verb (`drift render`)** and the **Render Repository (`render/` zone)**.

---

## 🚀 Part 1: The Render Verb (`drift render`)

The `render` verb transforms raw templates, static configuration files, and lifecycle hooks from **Zone 1 (`src/`)** into concrete, deployment-ready files in the **Zone 2 sandbox (`render/`)**. It executes with strict **zero-impact safety**: no changes touch active host destinations during rendering.

### 1. Pre-Flight Requirement Verification (`[package.requirements]`)
Before executing any template engine or allocating disk resources, Drift evaluates declarative platform requirements:
* **Host Facts Auditing**: Evaluates operating system (`os`), CPU architecture (`arch`), Linux distribution (`distribution`), required executables in `$PATH` (`binaries`), and required environment variables (`env`).
* **Dynamic Probe Hook**: Optionally executes `drift_hooks/probe` to run custom environment detection (e.g. verifying GPU drivers or GUI session types).
* **Zero-Cost Skipping**: If requirements are unmet, the package is cleanly skipped with an informative log message without running template compilers.

### 2. Template Engine Settings & Configuration
Drift supports both global and package-level engine definitions:
* **Workspace Engine Settings (`drift_workspace.toml`)**:
  * Registered under `[render.<engine_name>]` (e.g. `[render.envsubst]`, `[render.mustache]`, `[render.jinja2]`).
  * `suffix`: File extension pattern triggering the engine (e.g. `".drift.envst"`, `".drift.j2"`).
  * `render_command`: Execution command string supporting dynamic placeholders (`%s` template source, `%o` rendered destination, `%i` input file).
  * `input_file`: Optional external dependency file providing structured input data (e.g. `"config/theme.json"`).
* **Package-Level Engine Configuration (`drift_package.toml`)**:
  * Defined under `[render.<engine_name>]` within individual packages.
  * **Field-Level Inheritance**: Overlays onto workspace engines, inheriting unspecified `suffix` or `render_command` while allowing package-specific customizations.
  * **Scoped Input Files**: Resolves `input_file` relative to `src/<pkg>/` (or the workspace root), allowing packages to provide localized data sources.
  * **Exclusive Package Scope**: Package-level engines apply strictly to that package's source templates and hooks.
* **Selective Render Bypassing**:
  * Setting `[package] enable_render = false` completely bypasses template compilation for packages containing purely static dotfiles.

### 3. Multi-Engine Pipeline & 2-Phase Compilation
Drift executes template compilation through an orchestrated two-phase pipeline:
* **Built-in vs. External Engines**:
  * **Built-in `python_envsubst`**: Native environment variable substitution requiring zero external subprocesses or external dependencies.
  * **External Engines**: Executes third-party CLI tools (`envsubst`, `mustache`, `jinja2`, `tera-cli`) with automatic fallback to built-in `python_envsubst` if external `envsubst` command encounters issues.
* **Phase 1: Workspace Global Compilation Pipeline**:
  * Compiles templated package configuration files (`drift_package.*.toml` $\rightarrow$ intermediate `render/<pkg>/.drift/render/package/drift_package.toml` $\rightarrow$ final evaluated `render/<pkg>/.drift/drift_package.toml`). Only global workspace engines can compile package configs.
* **Phase 2: Package-Scoped Compilation Pipeline**:
  * Loads the compiled package configuration from `render/<pkg>/.drift/drift_package.toml`.
  * Evaluates package requirements and overlays package-level render engines.
  * Compiles engine input dependencies into `render/<pkg>/.drift/render/workspace/` (for workspace inputs) and `render/<pkg>/.drift/render/package/` (for package inputs).
  * Triggers the `pre_source` lifecycle hook.
  * Copies `.drift_ignore` into `render/<pkg>/.drift/.drift_ignore`.
  * **Pass 1 (Payload Pass)**: Compiles deployable dotfiles from `src/<pkg>/` into `render/<pkg>/`, stripping engine suffixes (e.g. `dot-zshrc.drift.envst` $\rightarrow$ `dot-zshrc`). Skips hidden files not using the required `dot-` prefix and excludes `drift_hooks/`.
  * **Pass 2 (Control Plane Hooks Pass)**: Compiles lifecycle hook scripts from `src/<pkg>/drift_hooks/` into `render/<pkg>/.drift/hooks/` (with full template compilation support) and ensures POSIX executable permissions (`chmod 0o755`).
  * Triggers the `post_render` lifecycle hook.

### 4. Variable Stitching & Environment Resolution
Template engines receive a rich, cohesive variable context stitched together across multiple layers. The help page `drift help package_config` provides detailed configuration.  

* **Unified 6-Tier Environment Precedence**:
  1. Package & Workspace Overrides (`[env.override]`)
  2. Authoritative Package Facts (`drift_package_*`) & System Facts (`drift_*`)
  3. Ambient Process Environment & CLI Variables (`os.environ`)
  4. Secret Vaults (`[env.secrets]`, `config/secrets.env`)
  5. Package & Workspace Defaults (`[env.default]` / `[env]`)
  6. Package & Workspace Fallbacks (`[env.fallback]`)
* **Secret Vault Isolation (`config/secrets.env`)**:
  * Loads sensitive credentials into environment scope using transient clean-room isolation.
  * Automatically masks secret values in debug logs (`KEY=****`).
* **Dynamic Python Preprocessing (`drift_package.py`)**:
  * Runs programmatic pre-render scripts to dynamically fetch tokens or compute host-specific settings.
* **Native Variable Self-Referencing**:
  * Resolves inter-variable references (`$VAR`, `${VAR}`) across all TOML tables using Kahn's topological sort algorithm with cycle detection.
* **Execution Scoping**:
  * Pre-resolved environment variables are automatically injected into `os.environ` during compilation via `with pkg_config.package_envs():`.

### 5. Render Collision Guard & Safety Checks
* **Render Collision Guard**: Detects when two different source templates would compile into the identical destination path (e.g. `config.j2` and `config.envst` both producing `config`) and halts with `RenderCollisionError` before writing.
* **Static Ignore Guard**: Rejects attempts to dynamically generate `.drift_ignore` from templates or nested files, ensuring ignore filtering remains strictly deterministic.

---

## 🏛️ Part 2: The Render Repository (`render/` Zone)

The `render/` directory is the **Zone 2 sandbox repository** in Drift's architecture. It serves as an isolated intermediate database tracking the output of all template compilation.

### 1. Package Structure in `render/`
Rendered packages cleanly separate deployable files from internal metadata:

```
render/
├── <pkg>/
│   ├── dot-config/             # Deployable dotfile payload (suffixes stripped, dot- prefix intact)
│   │   └── nvim/
│   │       └── init.lua
│   ├── dot-zshrc
│   └── .drift/                 # Internal control plane (NEVER deployed to host)
│       ├── drift_package.toml  # Compiled package configuration
│       ├── .drift_ignore       # Staged ignore rules
│       ├── hooks/              # Compiled lifecycle hook scripts (chmod +x)
│       └── render/             # Compiled engine input dependencies & assets
└── .git/                       # Dedicated sandbox Git repository
```

* **Deployable Payload**: Located at the package root (`render/<pkg>/...`). Contains compiled dotfiles with template suffixes stripped, ready for staging and installation.
* **Internal Control Plane (`.drift/`)**: Houses package configuration, lifecycle hooks, staged input dependencies, and ignore files. Hardcoded as ignored by deployment handlers so it is never copied or symlinked to host destinations.

### 2. Render Sandbox Functionality
* **Dedicated Git Repository**: The `render/` zone is initialized as an independent Git repository, recording every compilation pass in version control.
* **Pure Sandbox Isolation**: Compilation occurs entirely within `render/`. If a template fails to compile, an engine encounters an error, or a dependency is missing, **compilation halts immediately with zero side effects on your live system**.
* **Inspectability & Diffing**: Users can inspect compiled files directly or diff changes between compilation runs (`drift diff --zone render`) before staging them to the state database.
* **Sandbox Commit Checkpointing (`drift render-commit`)**: Commits all compiled outputs into the `render/` Git repository history (automatically executed as step 2 of `drift deploy`).
