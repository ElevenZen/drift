# 🌀 Drift: Next-Gen Transactional Dotfile Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org)
[![Build Status](https://img.shields.io/badge/tests-186%20passed-brightgreen)](tests/)

**Drift** is a declarative, modular configuration and dotfile deployment engine designed for power users who demand system safety, predictability, and complete visibility. 

Unlike traditional dotfile managers that directly symlink mutable directories or run opaque installation scripts, Drift implements a **two-stage, Git-backed compilation and deployment pipeline**. It isolates templates, compiles them in a secure sandbox, audits active system drifts, and executes deployments using atomic, transactional primitives.

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
*   **Active Host Monitoring**: Drift's **Reverse-Sync (Primitive 1)** automatically scans active host paths, detects these silent GUI-driven or system-tool updates, and mirrors them back into the `install/` state base.
*   **Interactive Review & Adoption**: You can run a diff to inspect the changes written by your system's GUI utilities. If you want to keep them, simply commit the changes in `install/` and backport the settings to your source templates in `src/`.
*   **Safely Discard & Force-Restore**: If you want to reject the GUI changes and force-restore your original templated configs, simply commit the deletion or modifications in the `install/` base and redeploy. Drift's engine will recognize the clean baseline and restore your files exactly as defined in `src/`.

### 🔗 3. Directed Acyclic Graph (DAG) Template Pipelines
Drift supports declaring arbitrary, nested render engine pipelines in `drift.toml` (e.g., matching `.envst.sh` or `.mustache`). 
*   **Template Input Dependencies**: A render engine's input variables can itself be a template compiled by another engine (e.g., `mustache` needing a static JSON config generated from environment variables).
*   **Cycle Detection**: Drift constructs a compiler dependency graph and executes cycle-detection validation, throwing `CyclicDependencyError` to prevent compilation loops.
*   **Deferred Render Compilation**: If variables or templates are missing during boot, Drift gracefully logs a warning. Compilation is only blocked if a file in the active workspace *actually* relies on the disabled engine, preventing unrelated package bottlenecks.

### 🛑 4. Proactive Collision Guard & Safeguards
Drift values your data integrity. Before any physical stage or deployment execution, the **Collision Guard** runs a multi-category safety audit:
*   **Zero Overwrite of Manual Files**: Any conflicting manual file on the host system is safely backed up to `backup/<package>/overwritten/` before deployment.
*   **Pruned Files Swept**: Deleted files are cleanly swept to `backup/<package>/deleted_files/`.
*   **Symlink Nesting Block**: Drift blocks deployments if the target directory written in configurations is equal to or nested inside the Drift workspace root directory.

### 🕵️ 5. PCRE-Based Ignorance & Stow Compatibility
Drift uses standard Perl-Compatible Regular Expressions (PCRE) for its package ignore files (`.drift_ignore`), matching the exact parsing rules of GNU Stow's `.stow-local-ignore`.
*   **Single Ignore File Restriction**: Drift strictly enforces exactly one `.drift_ignore` per package root, preventing fragmented and hard-to-audit nested ignore rules.
*   **Match Timing Guard**: Patterns are matched against native repository filenames *before* prefix expansion (e.g., matching `dot-bashrc` instead of `.bashrc`), eliminating translation bypasses.

### 🧹 6. Autonomous Garbage Collection (Self-Cleaning)
When you toggle packages to `false` in `drift.toml`, or delete source files, Drift's **Garbage Collection (Primitive 9)** automatically uninstalls them, purges untracked "zombie" folders inside `render/` and `install/`, and **commits the purges inside the database Git repositories**. 
*   **Isolated Commit Scoping**: The GC process only commits the specific directories it purges, ensuring unrelated system modifications are left untouched and auditable.

### 🔌 7. Decouple & Eject Packages on Demand (Detach Mode)
Sometimes, you want to stop managing a configuration through a dotfile manager but keep the configurations permanently active on your host system. 
*   **Keep Active Configurations**: Drift supports a dedicated **Detach Mode (`drift uninstall <pkg> --detach`)** that unregisters the package without deleting any files on your system.
*   **Symlink to Copy Conversion**: If the package was stowed via symlinks, the detach engine automatically replaces every system-level symlink with its actual, physical file copy. Your configuration is "frozen" as an independent file on your host target.
*   **Backups Untouched**: Your historical original system backups inside `backup/<package>/overwritten/` are kept completely intact (not restored or deleted).
*   **Decoupled Registry**: Cleanly deletes database directories and unregisters the package from `state.toml`, safely letting you "eject" a package on demand.

---

## 🚀 Getting Started

### 1. Initialize Your Workspace
Run the initialization command to scaffold a clean, Git-backed Drift workspace:
```bash
drift init
```
This generates:
*   `config/drift.toml` (Global settings, package activations, and engine configurations).
*   `render/` and `install/` databases initialized as clean, independent local Git repositories.

### 2. Scaffold a New Package
```bash
drift new shell
```
This scaffolds a standard package subdirectory structure in `src/shell/` including:
*   `src/shell/package.toml` (Package metadata, hook commands, target home path, and deployment options).
*   `src/shell/.drift_ignore` (Package PCRE ignore configuration).

### 3. Add Your Configurations
Place files or templates inside the package folder. For example, create `src/shell/dot-bashrc` with:
```bash
export ALIAS_VAR="my_alias"
```

### 4. Deploy Your Workspace
Compile, sandbox-stage, and deploy your configurations to the host system in one command:
```bash
drift deploy
```

---

## 🛠️ The 11 Core Primitives

Drift maps all operational workflows to **11 atomic primitives**:

| Primitive | Command | Description |
| :--- | :--- | :--- |
| **P1** | `drift reverse-sync` | Scans system, reverse-syncs host edits to the state database. |
| **P2** | `drift render` | Compiles source templates into the sandbox. |
| **P3** | `drift render-commit` | Commits compiled sandbox changes to the `render/` repository. |
| **P4** | `drift stage` | Stages sandbox modifications into the state database. |
| **P5** | `drift deploy` | Symmetrically translates paths and deploys files to the host. |
| **P6** | `drift install-commit` | Commits deployment states in the `install/` state database. |
| **P7** | `drift uninstall` | Tears down host-side file mappings, restoring original backups. |
| **P8** | `drift rollback` | Resets transaction failures, rolling back database states. |
| **P9** | `drift gc` | Decommissions disabled packages and purges zombie folders. |
| **P10** | `drift new <pkg>` | Scaffolds a new package configuration. |
| **P11** | `drift add <pkg>` | Imports external system configurations into the source repository. |

---

## 🪝 Robust Lifecycle Hook Integration
Packages can declare automated hook scripts inside `package.toml` to integrate with external packages:
```toml
[package]
name = "nvim"
install_method = "stow"
pre_install = "scripts/bootstrap.sh"
post_update = "scripts/reload_plugins.sh"
hook_timeout = 60
```
*   **Mandatory Directories**: Drift executes hooks with strict, mandatory `hook_dir` and working directory (`cwd`) arguments, ensuring your hook runs with predictable paths.

---

## 📜 License
Drift is released under the **MIT License**. See [LICENSE](LICENSE) for details.
