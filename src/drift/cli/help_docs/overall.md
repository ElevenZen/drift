# 🌀 Drift: Next-Gen Transactional Dotfile Manager

Drift is a declarative, modular configuration and dotfile deployment engine designed 
for power users who demand system safety, predictability, and complete visibility.

Unlike traditional dotfile managers that directly symlink mutable directories or run 
opaque installation scripts, Drift implements a **decoupled, multi-zone compilation 
and transactional deployment pipeline**. It isolates templates, compiles them in a secure sandbox, 
audits live system drift, and executes deployments using atomic, reversible workflows.

---

## 🔄 The Drift Data-Flow Loop

```
[Zone 1: Declarative Source]     src/ & config/
  │
  ▼  Primitive 2: Render (drift render)
[Zone 2: Sandbox Render Zone]    render/ (isolated Git repo)
  │
  ▼  Primitive 4: Stage delta (drift stage)
[Zone 3: Local State Database]   install/ (tracking Git repo)
  │ ▲
  │ │  Primitive 1: Reverse-Sync (drift adopt)
  ▼ │  Primitive 5: Apply (drift apply - symlink / copy)
[Zone 4: Active Host System]     Target destinations (~, /etc, etc.)
```

> [!NOTE]
> **Composite Deployment**: The commonly used command `drift deploy` is a composite workflow that automatically executes **Render ➔ Stage ➔ Apply** in sequence.

---

## 🏛️ The Four Architectural Zones

Drift strictly decouples dotfile lifecycle across four distinct filesystem zones:

1. **Zone 1: Declarative Source (`src/` & `config/`)**:
   * Version-controlled dotfiles, templates (`*.envst`), ignore rules (`.drift_ignore`), lifecycle hooks (`drift_hooks/`), and configuration metadata (`drift_package.toml`, `drift_workspace.toml`).
   * Clean and decoupled from host paths—you never author files directly against host locations.
2. **Zone 2: Sandbox Render Zone (`render/`)**:
   * Isolated Git repository where templates compile under active environment variables and dynamic Python hooks (`drift_package.py`).
   * Operates as a pure sandbox: zero mutations touch the live host system during compilation.
3. **Zone 3: Local State Database (`install/`)**:
   * Git-backed state tracking database recording the "last known good" deployed state, file checksums, and package manifests (`state.toml`).
   * Preserves 1:1 structural fidelity with `render/`, enabling surgical delta computation and rollbacks.
4. **Zone 4: Active Host System (Target Destinations)**:
   * The live filesystem on the host machine (e.g. `~/.config`, `$HOME`, `/etc`).
   * Files are projected as symlinks or delivered as atomic physical copies.

---

## ⚙️ The Four Core Pipeline Stages (Primitives)

Drift's architecture is powered by four primary operational primitives:

### 1. 🔄 Reverse-Sync (Primitive 1: `drift adopt` / `drift status` / `drift diff`)
* **Live System Audit**: Compares active host files against the `install/` state database to detect uncommitted edits ("drifts") or newly created files in Fully-Controlled Directories (FCDs).
* **Two-Way Reconciliation**: Backports modifications made on the host directly back into source templates in `src/`, closing the configuration feedback loop.

### 2. 🎨 Render & Commit (Primitive 2 & 3: `drift render` & `drift render-commit`)
* **Pre-Flight Requirements**: Checks OS, CPU architecture, Linux distro, required binaries, and environment variables before compilation (zero-cost skipping).
* **Dynamic Preprocessing**: Runs `drift_package.py` to dynamically fetch credentials from secret vaults.
* **Variable Stitching & Compilation (`drift render`)**: Stitches variables across 6 tiers via Kahn's topological sort and compiles templates into `render/`.
* **Sandbox Git Commit (`drift render-commit`)**: Commits compiled template outputs into the isolated `render/` Git repository (automatically invoked during `drift deploy`).

### 3. 📦 Stage (Primitive 4: `drift stage`)
* **Inter-Package Dependency DAG**: Sequences packages in topological order using Kahn's algorithm so prerequisites stage before dependents.
* **Delta Computation**: Compares `render/` against `install/`, identifying added, modified, and deleted files.
* **Database Commit**: Commits changes into `install/` and writes staging manifests and `.stow-local-ignore` rules.

### 4. 🚀 Apply & Commit (Primitive 5 & 6: `drift apply` & `drift install-commit`)
* **Collision Audit & Backups**: Proactively checks for host collisions and archives untracked blocking files into `backup/`.
* **Deployment Execution (`drift apply`)**: Deploys staged files using **symlink projection** (`stow`) or **discrete physical copies** (`copy`).
* **Lifecycle Hooks**: Triggers `pre_install`, `post_install`, `pre_update`, or `post_update` scripts in user space.
* **State Database Commit (`drift install-commit`)**: Commits deployed file changes into the `install/` Git repository (automatically invoked during `drift deploy`).
* **Rollback Safety**: Automatically reverts changes if an installation hook or file operation fails mid-flight.

---

## 🧭 Main Drift Concepts

Understanding these core concepts establishes the mental model:

* **Modular Packages (`src/<pkg>/`)**: Self-contained units of configuration for individual tools or services (`nvim`, `zsh`, `sway`), each declaring target mapping, prerequisites, variables, and hooks.
* **Deployment Strategies (`stow` vs. `copy`)**: Choose symlink projection (`stow`) for instant reflection of dotfile edits, or physical copying (`copy`) for system daemons and services requiring strict pre-update stop triggers.
* **Inter-Package Dependencies**: Declare prerequisite packages (`dependencies = ["base", "git"]`). Drift sequences staging and deploy forward, and uninstallation in reverse topological order, with safety guards preventing broken dependencies.
* **Declarative Host Requirements (`[package.requirements]`)**: Pre-flight platform gates evaluated strictly *before* rendering. Incompatible packages are skipped with zero compile or disk overhead.
* **Unified 6-Tier Environment & Native Variable Stitching**: Symmetrical `[env.*]` tables supporting derived self-referencing (`$VAR`, `${VAR}`) resolved via Kahn's DAG algorithm.
* **Dedicated Hook Isolation (`drift_hooks/`)**: Lifecycle scripts compile into `.drift/hooks/`, ensuring helper scripts are never symlinked or deployed to the host target.
* **Fully-Controlled Directories (FCDs)**: Subdirectories (like `plugins/`, `themes/`) owned 100% by Drift, audited during reverse sync to detect untracked runtime files.
* **Two-Group PCRE Ignore Engine (`.drift_ignore`)**: Evaluated at the installation stage to filter temporary files, swap files, and caches from host deployment.
* **Secret Isolation (`config/secrets.env`)**: Git-ignored dotenv vaults loaded with transient clean-room isolation and automatic log masking (`KEY=****`).

---

## 🚀 Frequently Used CLI Commands

*   `drift clone <url> [dir]`       Clones a Git repo and auto-bootstraps/repairs the Drift workspace.
*   `drift init`                    Initializes a new Git-backed Drift workspace & databases.
*   `drift new <pkg>`               Scaffolds a new package directory with `drift_package.toml` metadata.
*   `drift add <pkg> <paths>`       Imports external target-system configurations into package source.
*   `drift adopt <pkg>`             Backports uncommitted system drifts back into package templates.
*   `drift deploy [pkgs]`           Composite command: compiles (render), stages, and deploys configs.
*   `drift render [pkgs]`           Sandbox-compiles templates and stitched environment into `render/`.
*   `drift render-commit [pkgs]`    Commits rendered changes in the isolated `render/` repository.
*   `drift stage [pkgs]`            Calculates deltas and stages changes from `render/` to `install/`.
*   `drift apply [pkgs]`            Deploys staged files from `install/` to active host targets.
*   `drift install-commit [pkgs]`  Commits deployed state changes in the `install/` database repository.
*   `drift health [pkgs]`           Runs runtime health check probes on installed packages.
*   `drift uninstall [pkgs]`        Removes deployed symlink/copy mappings on host paths, restoring backups.
*   `drift rollback [pkgs]`         Resets staging/deploy midway failures to restore a stable state.
*   `drift status`                  Audits and inspects current template, staging, and system-drift status.
*   `drift diff`                    Compares and visualizes template, deployment, or active system layers.
*   `drift gc`                      Purges orphan packages, ghost records, and zombie database directories.
*   `drift repair`                  Audits and self-heals workspace structure and Git databases.
*   `drift complete [shell]`        Generates or installs interactive shell tab-completions.

---

## 📚 Where to Go Next (Documentation Map)

*   `drift help package`               Understand the 'package' concept, anatomy, and 10 core capabilities.
*   `drift help workspace`             Learn about workspace directories, local overrides, and the secrets vault.
*   `drift help drift_package.toml`    Complete package configuration reference, hooks matrix, and dependencies.
*   `drift help drift_workspace.toml`  Complete workspace global configuration reference, merging, and engine DAGs.
*   `drift help src`                   Learn about the declarative source directory (`src/`).
*   `drift help render`                Understand sandbox template compilation (`render/`).
*   `drift help install`               Understand the state database and deployment (`install/`).
*   `drift help fcd`                   Understand Fully-Controlled Directories (FCDs) and untracked file auditing.
*   `drift help ignore`                Understand `.drift_ignore` syntax, install ignore logic, and FCD ignore mechanics.
*   `drift help health`                Learn about package runtime health check probes and lifecycle hooks.
*   `drift help clone`                 Learn about cloning Drift repositories and bootstrapping new machines.
*   `drift help faq`                   Troubleshooting recipes and frequently asked questions.
