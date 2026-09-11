# 📦 The 'Package' Concept in Drift

In Drift, a **Package** is a self-contained, modular unit of configuration. It represents 
a logical group of files, templates, ignore rules, and lifecycle scripts that manage a 
particular software or aspect of your system (e.g., `nvim`, `shell`, `qbittorrent`).

## 📁 Package Directory Structure
Every package resides within your workspace source directory (by default, `src/<package_name>/`):
```
src/nvim/
├── drift_package.toml       <-- Package configuration metadata
├── drift_package.py         <-- Optional dynamic Python configuration hook
├── .drift_ignore            <-- PCRE patterns for files to exclude from deployment
├── init.lua                 <-- Static dotfile
└── lua/
    └── config/
        └── options.lua      <-- Static dotfile
```

## 📝 Package Configurations
Each package is controlled by a dedicated configuration file named either `drift_package.toml` 
or `drift_package.local.toml`, with optional dynamic Python hooks (`drift_package.py` or configured via `[package] hook_file`). This dictates:
1.  **`install_method`**: How configurations are written to the host system:
    *   `stow`: Symmetric symlinking from `install/` state DB (uses GNU Stow logic).
    *   `copy`: Secure, physical file copying.
2.  **`target_directory`**: The physical destination where this package belongs on the host system 
    (e.g., `~/.config/nvim`).
3.  **`fully_controlled_dirs`**: Directories where Drift has total control, meaning Drift will 
    automatically synchronize and prune deleted files inside them (FCDs).
4.  **`Dynamic Python Hook`**: A `drift_package.py` (or custom `hook_file`) defining `configure_package(context)` to dynamically transform package settings based on host facts, workspace context, and environment.
5.  **`Lifecycle Hooks`**: Shell command hooks executed atomically during source generation, render, installation, update, uninstallation, and health probe sequences 
    (`probe`, `pre_source`, `pre_install`, `post_install`, `pre_update`, `post_update`, `pre_uninstall`, `post_uninstall`, `post_render`, `health`). 
    All lifecycle hooks always execute in user space without `sudo`, preserving all injected environment variables.

---

## 🧩 3. Variable Stitching & Fact Injections

Package configurations natively participate in Drift's 7-tier variable stitching system:
*   **Package Fact Injections**: Automatically interpolate dynamic facts:
    *   `${drift_package_name}`: Active package name (e.g. `nvim`).
    *   `${drift_package_target_dir}`: Resolved destination target directory path.
    *   `${drift_package_source_dir}`: Path to source templates in `src/`.
    *   `${drift_package_render_dir}`: Path to rendered files in `render/`.
    *   `${drift_package_install_dir}`: Path to local state in `install/`.
*   **Dual-Tier Package Scopes**:
    *   `[env.fallback]`: Baseline default values used only when unset across higher tiers.
    *   `[env.override]`: Highest-priority package values (overwrites workspace defaults and system facts).
*   **Topological Evaluation**: Package variables seamlessly reference and stitch with workspace `[env]`, secret vault keys, and host facts.
*   **Values-Only Scope**: Variable stitching operates **strictly within configuration values** (e.g. `target_directory`, hook commands, environment variable strings). TOML keys, table names, and section headers are not expanded.

> [!TIP]
> **Lifecycle Hooks Matrix**: For the complete lifecycle hooks execution table (trigger stages, working directories, privilege model, and default environment variables), see `drift help drift_package.toml`. You can test and execute any hook individually with `drift hook <package> <hook-name>`.

👉 Run `drift help drift_package.toml` to view the comprehensive configuration reference.

