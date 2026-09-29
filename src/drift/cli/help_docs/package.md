# 📦 The 'Package' Concept in Drift

In Drift, a **Package** is a self-contained, modular unit of configuration. It represents 
a logical group of files, templates, ignore rules, dependencies, and lifecycle scripts that 
manage a particular tool, service, or aspect of your system (e.g. `nvim`, `zsh`, `git`, `sway`).

Rather than treating dotfiles as a flat, monolithic directory tree, Drift organizes configurations 
into isolated packages under `src/<package_name>/`. Each package encapsulates its own deployment strategy, 
prerequisites, variable scopes, template engines, and lifecycle hooks—allowing a single dotfiles repository 
to seamlessly power diverse machines from headless cloud servers to multi-monitor workstations.

---

## 📁 Package Directory Layout & Anatomy

A complete Drift package resides within `src/<package_name>/`:

```
src/nvim/
├── drift_package.toml       <-- Primary declarative configuration metadata
├── drift_package.local.toml <-- Optional machine-specific local overrides (gitignored)
├── drift_package.py         <-- Optional dynamic Python configuration hook
├── .drift_ignore            <-- PCRE patterns for files to exclude from deployment
├── drift_hooks/             <-- Dedicated lifecycle hook scripts (isolated from deploy)
│   ├── probe.sh             <-- Pre-flight verification script
│   ├── bootstrap.sh         <-- Pre-install or post-install script
│   └── lib/helper.sh        <-- Sibling helper library sourced via relative path
├── init.lua                 <-- Static dotfile payload
└── lua/
    └── config/
        └── options.lua.envst <-- Templated dotfile compiled by envsubst
```

* **`drift_package.toml`**: Declarative settings defining destination target paths, install method (`symlink` vs. `copy`), requirements, dependencies, and environment defaults.
* **`drift_package.local.toml`**: Local-only, uncommitted overrides merged over `drift_package.toml` for host-specific tuning.
* **`drift_package.py`**: Dynamic Python preprocessor hook for programmatic settings and remote vault secret injection.
* **`.drift_ignore`**: Perl-Compatible Regular Expressions (PCRE) to filter unwanted files before rendering or staging.
* **`drift_hooks/`**: Dedicated lifecycle shell scripts. Drift compiles them into `.drift/hooks/`, keeping them isolated so they are never deployed or symlinked to host destinations.
* **Payload Files & Templates**: Plain configuration files (e.g. `init.lua`) or template files (e.g. `options.lua.envst`) that compile and deploy to the target directory.

---

## 🧭 The Package Mental Framework: 10 Core Capabilities

Understanding what a package can do establishes the mental model before diving into TOML syntax:

### 1. 🎯 Target Mapping & Deployment Strategies (`symlink` vs. `copy`)
Every package declares where and how its files should be mapped to the host system:
*   **Symlink Projection (`install_method = "symlink"`)**: Projects relative symlinks from the local state database (`install/`) to your host destination (e.g. `~/.config/nvim`). Edits on the host are reflected immediately via symlinks. Ideal for user dotfiles.
*   **Discrete Physical Copies (`install_method = "copy"`)**: Physically delivers discrete files to host targets. Host files remain unchanged until explicitly updated during deploy passes. Strongly recommended for system services and background daemons (e.g. `systemd`).
*   **Subfolder Payload Isolation (`source_directory`)**: Compile only a subfolder (e.g. `src/<pkg>/dotfiles/`) to the host, keeping package-level documentation, build scripts, or tests isolated.
*   **Cross-Platform Paths**: Supports OS-specific destinations like `target_directory_windows` (`%APPDATA%`).
*   **Elevated File Operations (`sudo = true`)**: Elevate only physical file installations to root while keeping Git databases user-owned.

### 2. 🔗 Inter-Package Dependencies & Topological Ordering (`dependencies`)
Packages can declare explicit dependencies on other packages:
*   **Declarative Prerequisites**: Declare `dependencies = ["base", "git"]` or optional dependencies (`{ name = "fzf", optional = true }`).
*   **Topological DAG Staging & Deployment**: Drift constructs a dependency graph (DAG) across the workspace and sequences actions so prerequisite packages are always compiled, staged, and deployed *before* dependents.
*   **Reverse Topological Uninstallation**: During package uninstallation (`drift uninstall`), packages are dismantled in **reverse topological order**, ensuring dependent packages are cleanly detached before base dependencies.
*   **Downstream Safety Guards**: Drift prevents accidental uninstallation of a package if other active packages still depend on it, unless bypassed with `--ignore-missing-dependencies` or `--force`.

### 3. 🛡️ Declarative Host Requirements & Platform Filtering (`[requirements]`)
Packages can declare platform prerequisites evaluated **strictly before template rendering begins**:
*   **Zero-Cost Skipping**: Evaluates host OS (`os`), CPU architecture (`arch`), Linux distribution (`distro`), required executables in `$PATH` (`binaries`), non-empty environment variables (`env`), and host LAN IP addresses / CIDR subnets (`ip`).
*   **Graceful Skip**: If requirements are not satisfied on the current host, Drift skips the package immediately (`status = "SKIPPED"`). No template engines run, no intermediate files compile, and no sandbox files are written, allowing the rest of the workspace to deploy without error.

### 4. 🧩 Unified 6-Tier Environment & In-TOML Variable Stitching (`[env]`)
Packages participate in Drift's symmetrical, 6-tier precedence hierarchy:
*   **4 Symmetrical Sub-Tables**: `[env.override]` (Tier 2, forced overrides), `[env.secrets]` (Tier 4, private credentials), `[env.default]` (Tier 5, standard defaults), `[env.fallback]` (Tier 6, soft baseline).
*   **In-TOML Variable Stitching**: Define derived variables (`URL = "http://${HOST}:${PORT}"`) directly within TOML. Drift resolves references using Kahn's topological sort algorithm with cycle detection.
*   **Automatic Fact Injection**: Access auto-probed facts (`${drift_os}`, `${drift_arch}`, `${drift_package_name}`, `${drift_package_target_dir}`, `${drift_package_source_dir}`).
*   **Cross-Section References**: Any configuration field (such as `target_directory`) can reference resolved environment variables.

### 5. 🪝 Full-Spectrum Lifecycle Hooks (`[hooks]`)
Automate workflows at every milestone of package lifecycle with 10 event triggers:
*   `probe`: Dynamic pre-flight gatekeeper executed before rendering.
*   `pre_source` & `post_render`: Triggers before and after sandbox template compilation.
*   `pre_install` & `post_install`: Triggers before and after first-time host deployment.
*   `pre_update` & `post_update`: Triggers before and after updating an already installed package.
*   `pre_uninstall` & `post_uninstall`: Triggers before and after unlinking or removing files.
*   `health`: Runtime health probe hook executed during `drift health`.
*   **Dedicated `drift_hooks/` Isolation**: Hook scripts live in `src/<pkg>/drift_hooks/`, compile into `.drift/hooks/`, and are completely isolated from host target deployments.
*   **Unified Working Directory**: Scripts execute with `cwd = hook_path.parent`, allowing relative sourcing of sibling helper scripts.
*   **User Space Execution**: All hooks execute as the normal user without sudo, preserving all 6 tiers of environment variables.

### 6. 🐍 Dynamic Python Preprocessor Hook (`drift_package.py`)
For complex packages requiring programmatic adjustments:
*   Define `configure_package(context: PackageHookContext)` in `src/<pkg>/drift_package.py`.
*   Executes dynamically *before* variable stitching with **zero footprint on `os.environ`**.
*   The recommended, canonical place to securely fetch credentials or remote configurations from secret managers (1Password CLI, Bitwarden, HashiCorp Vault, AWS Secrets Manager) and inject them into `[env.secrets]`, `[env.default]`, or `[env.override]`.

### 7. 🎨 Self-Contained Render Engines (`[render.<name>]`)
Each package can encapsulate its own template compilation rules:
*   Define custom package-specific engines or selectively patch global workspace engines.
*   **Field-Level Inheritance**: Override only `input_file` while inheriting `suffix` and `render_command` from workspace defaults.
*   **Intermediate Sandboxing (`.drift/render/`)**: Package-specific input templates compile into `render/<pkg>/.drift/render/`, keeping control-plane artifacts strictly isolated from deployable dotfiles.

### 8. 🔍 Fully-Controlled Directories (`fully_controlled_dirs` / FCDs)
Designate directories under `target_directory` (e.g. `plugins/`, `themes/`) that are 100% owned by the package:
*   Drift audits these directories during reverse sync to detect untracked or wild files created by applications.
*   Developers can adopt them into source templates (`drift adopt`), ignore them (`.drift_ignore`), or discard them.

### 9. 🚫 PCRE Ignore Rules (`.drift_ignore`)
Filter out unwanted files before compilation and staging:
*   Single `.drift_ignore` per package root using standard Perl-Compatible Regular Expressions (PCRE), derived from GNU Stow ignore rules.
*   Patterns match repository paths before dot-prefix translation, preventing temporary files, caches, or private keys from leaking into the sandbox or host.

### 10. 🩺 Runtime Health Probes (`drift health`)
Verify runtime integrity on active hosts:
*   Execute package health check probes (`drift health <pkg>`) to confirm installed binaries, running systemd units, or active socket connections.
*   Integrates with automated test scripts and CI/CD pipelines with structured `--json` reporting.

---

## 🔄 How a Package Flows Through the Drift Pipeline

A package traverses four decoupled architectural layers:

```
[1. Declarative Source]      src/<pkg>/ (templates, static files, hooks, config)
  │
  ▼  Pre-Flight Evaluation: [requirements] + probe hook
  │  (If unmet: status = "SKIPPED", halts early with zero disk writes)
  │
  ▼  Primitive 2: Render (templates -> static compiled files)
[2. Sandbox Render Zone]     render/<pkg>/ (isolated Git database)
  │
  ▼  Primitive 4: Stage (delta computation & topological ordering)
[3. Local State Database]    install/<pkg>/ (Git database + state.toml tracking)
  │ ▲
  │ │  Primitive 1: Reverse-Sync (drift adopt backports live host edits to src/)
  ▼ │  Primitive 5: Apply (relative symlink projection or atomic physical copy)
[4. Active Host System]      Target destination (e.g. ~/.config/nvim)
```

1.  **Authoring**: You place static files, templates (`*.envst`), ignore rules, and `drift_package.toml` in `src/<pkg>/`.
2.  **Pre-Flight Verification**: Drift evaluates `[requirements]` and `probe` hook before rendering. Unmet packages are skipped gracefully.
3.  **Rendering (Sandbox)**: Templates compile into `render/<pkg>/` under active environment scope without touching host files.
4.  **Staging (State DB)**: File changes are staged into `install/<pkg>/` and ordered by topological dependency.
5.  **Deployment (Host)**: Files are symlinked (`symlink`) or copied (`copy`) to the target host directory with collision detection.
6.  **Audit & Adoption**: Any runtime tweaks made on the host can be inspected (`drift diff -s`) and safely adopted into templates (`drift adopt`).

---

## 📚 Where to Go Next

*   **Detailed Configuration Syntax**: Run `drift help drift_package.toml` (or `drift help package_config`) for a comprehensive line-by-line guide to all TOML configuration options, sub-tables, and code examples.
*   **Lifecycle Hooks Matrix**: For the complete Lifecycle Hooks Matrix, execution stages, and hook environment parameters, see `drift help drift_package.toml`.
*   **Workspace-Level Controls**: Run `drift help drift_workspace.toml` (or `drift help workspace_config`) to learn how to toggle packages per machine and manage global render engines.
*   **Runtime Health Probes**: Run `drift help health` to learn about health probe checks and verification commands.
