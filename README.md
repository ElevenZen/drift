# 🌀 Drift: Next-Gen Transactional Dotfile Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org)
[![Build Status](https://img.shields.io/badge/tests-480%20passed-brightgreen)](tests/)

**Drift** is a declarative, modular configuration and dotfile deployment engine designed for power users who demand system safety, predictability, and complete visibility.  

Unlike traditional dotfile managers that directly symlink mutable directories or run opaque installation scripts, Drift implements a **two-stage, Git-backed compilation and deployment pipeline**. It isolates templates, compiles them in a secure sandbox, audits active system drifts, and executes deployments using atomic, transactional workflows.  

> **WARNING: Drift is still very glitchy and in early development stage, it's for experimental usage only. Backward compatibility is not considered. File syntax, deployment rules, function signatures may change at any time.** 

---

## ⚡ TL;DR

> **Drift is a transactional, two-stage Git-backed dotfile engine that isolates template compilation in a sandbox and seamlessly audits, protects, and bidirectionally synchronizes live system edits without lost updates.**

* 🛡️ **Zero Risk / Dual-Git Sandbox**: Templates compile in an isolated `render/` Git sandbox. If a render fails, your host system remains 100% untouched.
* 💻 **Config-as-a-Package (Servers to Laptops)**: Select and toggle packages per machine via `drift.local.toml` or dynamically compute package rosters via `envsubst` (`drift.local.envst.toml`). One unified repo scales from minimal cloud servers to high-end workstations.
* 🔄 **Embraces System Drift**: Never lose GUI tweaks or hot-edits. Audit runtime changes (`drift diff -s`) and adopt them into templates (`drift adopt`) instead of suffering blind overwrites.
* 💥 **Mid-Fail Rollback**: If a deployment crashes midway, `drift rollback` safely restores your state database and host files to the last clean committed state.
* 🐚 **Interactive Tab-Completions**: Zero-latency native tab-completion for **Bash, Zsh, and Fish** with rich inline documentation hints and dynamic workspace package discovery.
* 📦 **Modular & Pluggable**: Pure standard-library core with DAG template pipelines, structured machine-readable `--json` output, and zero mandatory external Python dependencies.

---

## 📦 Installation & Packaging

Drift requires **Python 3.9+** and has **zero mandatory third-party dependencies** in its core mode (with optional `[rich]` terminal UI support).

### 1. ⚡ Shell Wrapper Installer (**Strongly Recommended**)

> [!TIP]
> **Why the Shell Wrapper is the Best Way to Install Drift**:  
> Drift is currently under rapid active development. Because development builds do not constantly bump the version number in `pyproject.toml`, package managers like `pipx` or `uv tool` will cache previous builds and refuse to update or re-install when pointing to an unbumped version number.  
>  
> The **Shell Wrapper** sets up a lightweight executable launcher in `~/.local/bin/drift` that directly delegates to your local repository source code (`src/`). Any `git pull` updates, bug fixes, or new features are **immediately active without requiring reinstallation or manual version bumps!**

To install the wrapper, clone the repository and run:
```bash
git clone https://github.com/ElevenZen/drift.git
cd drift
./script/shell_wrapper_installer.bash
```
*   Pass `--force` (`-f`) to overwrite an existing wrapper.
*   Pass `--dir <path>` (`-d`) to install into a custom directory.

---

### 2. 📦 Standalone Executable (`zipapp`) & Release Artifacts
Because Drift is self-contained with pure standard library support, it can be packaged into a single portable binary file with Python's built-in `zipapp`:

#### Automated Build & Verification Pipeline
Use the built-in pipeline script to build both the standalone Zipapp and Python Wheel (`.whl`), with automated isolated verification tests:
```bash
# Build all release artifacts (Wheel + Zipapp) and execute verification tests
./script/build_artifacts.bash

# Clean previous build artifacts and exit
./script/build_artifacts.bash --clean

# Clean first, then rebuild all artifacts
./script/build_artifacts.bash --rebuild

# Or build standalone zipapp only:
./script/build_artifacts.bash --zipapp-only
```
Generated artifacts are placed in `dist/`:
*   `dist/drift`: Standalone zero-dependency executable binary (ready to copy to `~/.local/bin` or remote servers).
*   `dist/drift-0.1.0-py3-none-any.whl`: Standard Python distribution wheel.

#### Manual Zipapp Build
```bash
# Build standalone executable manually
python3 -m zipapp src -m "drift.cli:main" -o drift -p "/usr/bin/env python3"
chmod +x drift

# Move to your PATH or copy to remote servers
mv drift ~/.local/bin/
```

---

### 3. 🐍 Python Package Managers (`pipx`, `uv`, or `pip`)

> [!NOTE]
> When using `pipx`, note that `pipx upgrade` or reinstalling will skip processing if the package version has not changed. Use `pipx install --force ...` to force updates if needed.

#### A. Isolated Global CLI via `pipx` or `uv tool`
Install Drift into an isolated environment and place it directly into your `$PATH`:
```bash
# Core standard-library mode (zero external dependencies)
pipx install --force git+https://github.com/ElevenZen/drift.git

# Or with uv:
uv tool install --force git+https://github.com/ElevenZen/drift.git

# With enhanced Rich console UI:
pipx install --force "git+https://github.com/ElevenZen/drift.git#egg=drift[rich]"
```

#### B. Standard `pip install`
Install locally or from a Git repository via `pip`:
```bash
pip install --user git+https://github.com/ElevenZen/drift.git

# Or from a cloned local repository:
pip install --user .
```

---

### 4. 🐚 Interactive Shell Tab-Completion (`drift complete`)

Drift features built-in, zero-latency tab-completion generators for **Bash**, **Zsh**, and **Fish** with rich inline documentation hints and dynamic package discovery.

#### Automatic Installation (Standard User Directories)
Run `drift complete --install` to automatically detect your active shells and place completion scripts in standard user directories (`~/.config/fish/completions/drift.fish`, `~/.local/share/bash-completion/completions/drift`, `~/.local/share/zsh/site-functions/_drift`):
```bash
drift complete --install
```
*(Note: If you used `./script/shell_wrapper_installer.bash`, tab-completions are installed automatically during setup!)*

#### Dynamic Startup Evaluation (Shell RC)
Alternatively, evaluate completions on-the-fly in your shell profile:
*   **Bash** (`~/.bashrc`):
    ```bash
    eval "$(drift complete bash)"
    ```
*   **Zsh** (`~/.zshrc`):
    ```zsh
    eval "$(drift complete zsh)"
    ```
*   **Fish** (`~/.config/fish/config.fish`):
    ```fish
    drift complete fish | source
    ```

---

## ✨ Why Drift? (The Killer Selling Points)

### 🛡️ 1. Absolute Sandbox Isolation (Dual-Git Architecture)
Dotfile templating shouldn't put your live home directory at risk. Drift operates with **four decoupled tiers**:
*   **Tier 1: Declarative Source (`src/`)**: Your raw, templated package dotfiles.
*   **Tier 2: Compilation Sandbox (`render/`)**: An isolated Git database. Templates are compiled here on-the-fly. If a template render fails, **compilation halts instantly with zero impact on your system**.
*   **Tier 3: Local State Database (`install/`)**: A dedicated Git repository tracking your "last known good" state, backed by an explicit metadata database (`state.toml`).
*   **Tier 4: Active System Host (e.g. `~/`)**: The physical target paths.

Because Tier 2 and Tier 3 are isolated Git repositories, Drift can diff, stage, and transactionalize your configurations before a single symlink is modified on your host.

### 🔄 2. Capture & Adopt GUI/System Tool Settings (Intelligent Reverse-Sync)
Modern desktop environments, IDEs, and system utilities frequently write configuration modifications directly to your active files (e.g., when you adjust settings in a GUI panel, alter theme colors in a control center, or customize keybindings through a preferences UI). 

In traditional dotfile managers, **these GUI-driven updates are silently lost—either completely ignored or blindly wiped away and overwritten during your next template deployment pass**.

Drift fundamentally resolves this mismatch by recognizing configuration as a **continuous, two-way loop**:
*   **Active Host Monitoring**: Drift's **Reverse-Sync** automatically scans active host paths, detects these silent GUI-driven or system-tool updates, and mirrors them back into the `install/` state base.
*   **Interactive Review & Adoption**: You can run a diff to inspect the changes written by your system's GUI utilities. If you want to keep them, run `drift adopt <pkg>` to extract the changes as a patch and cleanly backport them into your source templates under `src/`.
*   **Safely Discard & Force-Restore**: If you want to reject the GUI changes and force-restore your original templated configs, simply run `drift deploy <pkg> --force`. Drift's engine will overwrite the active drift and restore your files exactly as defined in `src/`.

### 💻 3. Modular "Config-as-a-Package" (One Unified Repo from Servers to Laptops)
Unlike monolithic dotfile managers that force you to deploy entire configurations wholesale or maintain separate git branches per machine, Drift structures every tool as an **independent, self-contained package** under `src/<pkg>/` (with its own `drift_package.toml`, target paths, deployment methods, and lifecycle hooks).

A **single, unified dotfiles repository** can effortlessly power everything from minimal cloud servers to high-performance GPU workstations and personal laptops:

*   **Granular Machine Enablement (`config/drift.local.toml`)**:
    You can selectively enable or disable packages on each machine using the gitignored `config/drift.local.toml` override without modifying version-controlled source files:
    ```toml
    # config/drift.local.toml (Machine-specific override)
    [packages.enable]
    DEFAULT = false
    shell = true            # Enabled on all machines
    nvim = true             # Enabled on all machines
    cuda_toolkit = true     # Enabled only on high-performance GPU compute nodes
    desktop_hyprland = false# Disabled on headless servers, enabled on laptops
    ```
*   **Dynamic Host Profiling via Meta-Config Templating (`config/drift.local.envst.toml`)**:
    For automated fleet deployments across servers and laptops, you can write a host-detection script that computes and exports environment variables such as `DRIFT_PACKAGES` (e.g. `export DRIFT_PACKAGES="cuda_toolkit = true\ndesktop_hyprland = false\n"`), and author a local configuration template:
    ```toml
    # config/drift.local.envst.toml (Template rendered on-the-fly)
    [packages.enable]
    DEFAULT = false
    ${DRIFT_PACKAGES}
    ```
    When Drift runs, `render_envst_load_toml` automatically evaluates `${DRIFT_PACKAGES}` into valid TOML key-value pairs, giving you dynamic, zero-touch machine provisioning!

### 🔗 4. Directed Acyclic Graph (DAG) Template Pipelines
Drift supports declaring arbitrary, nested render engine pipelines in `drift.toml` (e.g., matching `.envst` or `.mustache`). 
*   **Template Input Dependencies**: A render engine's input variables can itself be a template compiled by another engine (e.g., `mustache` needing a static JSON config generated from environment variables).
*   **Cycle Detection**: Drift constructs a compiler dependency graph and executes cycle-detection validation, throwing `CyclicDependencyError` to prevent compilation loops.
*   **Deferred Render Compilation**: If variables or templates are missing during boot, Drift gracefully logs a warning. Compilation is only blocked if a file in the active workspace *actually* relies on the disabled engine, preventing unrelated package bottlenecks.

### 🛑 5. Proactive Collision Guard & Safeguards
Drift values your data integrity. Before any physical stage or deployment execution, the **Collision Guard** runs a multi-category safety audit:
*   **Zero Overwrite of Manual Files**: Any conflicting manual file on the host system is safely backed up to `backup/<package>/overwritten/` before deployment.
*   **Pruned Files Swept**: Deleted files are cleanly swept to `backup/<package>/deleted_files/`.
*   **Symlink Nesting Block**: Drift blocks deployments if the target directory written in configurations is equal to or nested inside the Drift workspace root directory.

> [!IMPORTANT]
> **Transient `backup/` Policy & User Responsibility**: The `backup/` folder stores displaced original files and pruned artifacts created during deployment collisions. **`backup/` is a local, unversioned directory that is neither tracked nor saved in Git by Drift.** Users are responsible for inspecting `backup/`, preserving critical historical assets, or committing them to private archival storage as needed.

### 🕵️ 6. PCRE-Based Ignorance & Stow Compatibility
Drift uses standard Perl-Compatible Regular Expressions (PCRE) for its package ignore files (`.drift_ignore`), matching the exact parsing rules of GNU Stow's `.stow-local-ignore`.
*   **Single Ignore File Restriction**: Drift strictly enforces exactly one `.drift_ignore` per package root, preventing fragmented and hard-to-audit nested ignore rules.
*   **Match Timing Guard**: Patterns are matched against native repository filenames *before* prefix expansion (e.g., matching `dot-bashrc` instead of `.bashrc`), eliminating translation bypasses.

### 🧹 7. Autonomous Garbage Collection (Self-Cleaning)  
Garbage collection is triggered automatically at the end of a bulk `drift deploy` (when deploying all packages across the workspace) or executed on demand using the explicit `drift gc` command (with optional `--dry-run` inspection).

When you toggle packages to `false` in `drift.toml` or delete package source folders, Drift's **Garbage Collection** automatically uninstalls the orphaned host files, purges untracked "zombie" folders inside `render/` and `install/`, and **commits the purges inside the database Git repositories**. 
*   **Automatic Trigger on Global Deploy**: Running `drift deploy` without package arguments automatically sweeps and purges stale database packages at the end of the deployment cycle.
*   **Manual Trigger**: Run `drift gc` anytime to clean orphaned state or `drift gc --dry-run` to preview purges safely.
*   **Isolated Commit Scoping**: The GC process only commits the specific directories it purges, ensuring unrelated system modifications are left untouched and auditable.

### 🔌 8. Decouple & Eject Packages on Demand (Detach Mode)
Sometimes, you want to stop managing a configuration through a dotfile manager but keep the configurations permanently active on your host system. 
*   **Keep Active Configurations**: Drift supports a dedicated **Detach Mode (`drift uninstall <pkg> --detach`)** that unregisters the package without deleting any files on your system.
*   **Symlink to Copy Conversion**: If the package was stowed via symlinks, the detach engine automatically replaces every system-level symlink with its actual, physical file copy. Your configuration is "frozen" as an independent file on your host target.
*   **Backups Untouched**: Your historical original system backups inside `backup/<package>/overwritten/` are kept completely intact (not restored or deleted).
*   **Decoupled Registry**: Cleanly deletes database directories and unregisters the package from `state.toml`, safely letting you "eject" a package on demand.

---

## 🔄 The Drift Data-Flow Loop

Rather than running isolated commands, Drift operates as a continuous, closed-loop state machine. 

```
                     [ 1. DECLARATIVE SOURCE ]
                     src/ (Templates & drift.toml)
                                 │
                                 ▼ (drift deploy)
                    [ 2. SANDBOX RENDER ZONE ]
                      render/ (Git sandbox compile base)
                                 │
                                 ▼ (Stage render to install)
                    [ 3. LOCAL STATE DATABASE ]
                      install/ (Git local state database)
                                 ▲
                        Diff     │ (drift status / drift diff -s)
                       (Live)    ▼ (Symmetric path translation)
                     [ 4. SYSTEM ACTIVE HOST ]
                        ~/* or /etc/* (Active system configurations)
```

---

## 🚀 High-Level Usage Scenarios

### Scenario A: Declaring & Deploying a New Configuration (One-Way Forward Flow)
*When you want to manage a new configuration file (e.g., Neovim config) with declarative templates.*

1.  **Scaffold**: Run `drift new nvim -t ~/.config/nvim` to create a package directory.
    *   **Folder Structure Relieved**: By setting `target_directory = "~/.config/nvim"` inside `src/nvim/drift_package.toml`, you no longer need nested directories like `dot-config/nvim/` on disk. Files are put directly inside `src/nvim/`.
2.  **Author**: Add template or files into `src/nvim/` (e.g., `src/nvim/init.envst.lua` containing `${ENV_VAR}`).
3.  **Deploy**: Run `drift deploy` (which triggers the functions: Render $\rightarrow$ Commit Render $\rightarrow$ Stage $\rightarrow$ Install Deployment $\rightarrow$ Commit Install).
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
    *   Drift fetches the remote repository and automatically executes a non-destructive repair (`drift repair`) to reconstruct the excluded `render/` and `install/` databases, `.gitignore` rules, and machine-specific local configuration templates (`config/drift.local.toml`, `config/secrets.env`).
2.  **Migrate Legacy Plain Dotfiles Repo**: Run `drift clone https://github.com/username/legacy-dotfiles.git`.
    *   Drift detects that the repository is a traditional plain dotfiles repository, isolates existing dotfiles into `src/legacy-dotfiles/`, generates `drift_package.toml` and `.drift_ignore`, and scaffolds the full Drift workspace infrastructure automatically.
3.  **Review & Deploy**: Navigate into the directory (`cd dotfiles`), configure machine-specific overrides in `config/drift.local.toml` / `config/secrets.env`, and run `drift deploy`.

---

## 🛠️ Operational Commands & Functions

Drift's actions are cleanly categorized into **High-Level User Commands** (frequently used workflows) and **Low-Level Control Commands** (under-the-hood troubleshooting & continuous integration). All commands support `--json` for machine-readable automation.

### 🚀 High-Level User Commands (Frequently Used)

| Command | Description |
| :--- | :--- |
| `drift clone <url> [dir]` | Clones a Git repo and auto-bootstraps/repairs the Drift workspace (or converts legacy dotfiles). |
| `drift init` | Initializes a new Git-backed Drift workspace, databases, templates, and `secrets.env`. |
| `drift new <pkg>` | Scaffolds a new package directory with `drift_package.toml` metadata config. |
| `drift add <pkg> <paths>` | Imports external target-system configurations into the package source directory. |
| `drift adopt [pkgs]` | Backports uncommitted system drifts safely into package source templates. |
| `drift deploy [pkgs]` | Sandbox-compiles, stages, and deploys declarative files to target active hosts. |
| `drift health [pkgs]` | Probes live runtime health check hooks on installed packages. |
| `drift uninstall <pkgs>` | Removes stowed/copied mappings on host target paths, reverting backups (or `--detach`). |
| `drift rollback [pkgs]` | Resets staging/deploy midway transaction failures to restore stable state. |
| `drift status [pkgs]` | Audits and inspects current workspace template, staging, and system-drift status. |
| `drift diff [pkgs]` | Compares and visualizes template (`-t`), system (`-s`), or pending (`Diff Δ`) layers. |
| `drift gc` | Purges orphan packages and zombie database directories in `render/` and `install/`. |
| `drift repair` | Audits and self-heals workspace structure, repositories, config templates, and secrets. |
| `drift complete [<shell>]` | Generates or installs native interactive shell tab-completion scripts (Bash, Zsh, Fish). |
| `drift help [topic]` | Interactive mini user manual with pager fallback support. |

### 🔧 Low-Level Control Commands (Troubleshooting & Automation)

| Command | Description |
| :--- | :--- |
| `drift reverse-sync` | Force-syncs active host system changes back into the `install/` state base. |
| `drift render` | Sandbox-compiles raw source package templates into `render/`. |
| `drift render-commit` | Manually commits compiled sandbox changes to the `render/` repository. |
| `drift stage` | Stages compiled files from sandbox `render/` to `install/` state base. |
| `drift apply` | Installs files from `install/` to package target directories. |
| `drift install-commit` | Manually commits deployment state database changes inside `install/`. |
| `drift hook <pkg> <hook>` | Directly triggers a specific lifecycle hook script for a single package. |

---

## 🪝 Robust Lifecycle Hook Integration
Packages can declare automated hook scripts inside `drift_package.toml` to integrate with external packages:
```toml
[package]
install_method = "stow"

[hooks]
pre_source = "scripts/generate_dynamic_templates.sh"
pre_install = "scripts/bootstrap.sh"
post_update = "scripts/reload_plugins.sh"
timeout = 60
```
*   **Mandatory Directories**: Drift executes hooks with strict, mandatory `hook_base_dir` and working directory (`cwd`) arguments, ensuring your hook runs with predictable paths (e.g. `src/<pkg>` for `pre_source`, `install/<pkg>` for `pre_install`, and `render/<pkg>` for `post_render`).
*   **Privilege Model**: When `sudo = true`, installation and update hooks (`pre/post_install`, `pre/post_update`) run with `sudo` elevation, while source and render hooks (`pre_source`, `post_render`) always execute in user space without `sudo`.

---

## 💡 Troubleshooting & Practical FAQ

### 1. Discarding System Drift Pollution & Overwriting Host Files
* **Q**: How do I discard all host system modifications and force-reset host files back to the declarative templates?
* **A**: Run **`drift deploy --force`** (or `drift deploy <pkg> -f`). Drift bypasses system drift sentinel safeguards and forces the target files to match the freshly compiled templates from `src/`.

### 2. Resetting a Stuck or Problematic Package
* **Q**: What should I do if a package's installation state is corrupted, out of sync, or encountering unexpected file collisions?
* **A**: **Uninstall and redeploy**:
  ```bash
  drift uninstall <pkg> --force   # Remove host mappings and reset state tracking
  # (Optional: verify and clean any unmanaged leftovers in target directory)
  drift deploy <pkg>              # Cleanly compile, stage, and redeploy
  ```

### 3. Recovering from Corrupted Files in `render/` or `install/`
* **Q**: What if a file inside internal sandbox `render/` or local state database `install/` was accidentally deleted or broken?
* **A**: In Drift, **`src/` is the single source of truth**. Simply run **`drift render <pkg>`** or **`drift deploy <pkg>`**; Drift will automatically re-compile and re-stage clean files from `src/`.

### 4. Self-Healing Missing Workspace Databases & Templates
* **Q**: My Git databases (`render/.git`, `install/.git`), `.gitignore` rules, or local config templates (`config/drift.local.toml`, `config/secrets.env`) are missing.
* **A**: Run **`drift repair`** to audit and reconstruct missing workspace databases, `.gitignore` rules, and template files non-destructively.

### 5. Recovering from Midway Deployment Failures
* **Q**: A deployment script or hook crashed midway and left files in a half-written state.
* **A**: Run **`drift rollback <pkg>`** to revert `install/` to the last committed clean HEAD and redeploy the last stable state.

👉 Run `drift help faq` for more tips and topic manuals.

---

## 📚 Further Reading & In-Depth Documentation

*   **Interactive Built-in Manual (`drift help`)**: After reading this README, explore Drift's built-in manual pages directly from your terminal by running `drift help` or `drift help <topic>` (topics: `package`, `src`, `render`, `install`, `fcd`, `ignore`, `drift_package.toml`, `drift.toml`, `workspace`, `health`, `clone`, `faq`).
*   **Deep Architectural Design (`docs/design.md`)**: If you want to understand the complete architectural design, atomic execution primitives, transaction state machines, safety guarantees, and low-level engine internals, read [docs/design.md](docs/design.md).

---

## 📜 License
Drift is released under the **MIT License**. See [LICENSE](LICENSE) for details.
