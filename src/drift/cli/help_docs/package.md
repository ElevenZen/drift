# 📦 The 'Package' Concept in Drift

In Drift, a **Package** is a self-contained, modular unit of configuration. It represents 
a logical group of files, templates, ignore rules, and lifecycle scripts that manage a 
particular software or aspect of your system (e.g., `nvim`, `shell`, `qbittorrent`).

## 📁 Package Directory Structure
Every package resides within your workspace source directory (by default, `src/<package_name>/`):
```
src/nvim/
├── drift_package.toml       <-- Package configuration metadata
├── .drift_ignore            <-- PCRE patterns for files to exclude from deployment
├── init.lua                 <-- Static dotfile
└── lua/
    └── config/
        └── options.lua      <-- Static dotfile
```

## 📝 Package Configurations
Each package is controlled by a dedicated configuration file named either `drift_package.toml` 
or `drift_package.local.toml`. This file dictates:
1.  **`install_method`**: How configurations are written to the host system:
    *   `stow`: Symmetric symlinking from `install/` state DB (uses GNU Stow logic).
    *   `copy`: Secure, physical file copying.
2.  **`target_directory`**: The physical destination where this package belongs on the host system 
    (e.g., `~/.config/nvim`).
3.  **`fully_controlled_dirs`**: Directories where Drift has total control, meaning Drift will 
    automatically synchronize and prune deleted files inside them (FCDs).
4.  **`Lifecycle Hooks`**: Shell command hooks executed atomically during source generation, render, installation, update, uninstallation, and health probe sequences 
    (`pre_source`, `pre_install`, `post_install`, `pre_update`, `post_update`, `pre_uninstall`, `post_uninstall`, `post_render`, `health`). 
    If `sudo = true`, installation, update, uninstallation, and health probe hooks (`pre/post_install`, `pre/post_update`, `pre/post_uninstall`, `health`) run with `sudo` elevation, while source and render hooks (`pre_source`, `post_render`) always run in user space without `sudo`.

> [!TIP]
> **Lifecycle Hooks Matrix**: For the complete lifecycle hooks execution table (trigger stages, working directories, privilege elevation, and default environment variables), see `drift help drift_package.toml`.

👉 Run `drift help drift_package.toml` to view the comprehensive configuration reference.
