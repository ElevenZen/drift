# 💻 Declarative Source Directory: `src/`

The `src/` directory is the declarative "Tier 1" database of your Drift workspace. It holds 
your pristine, version-controlled source files, templates, and package metadata.

## ✍️ Writing a Basic Drift Package

To add a new configuration under Drift's governance:

1.  **Scaffold**: Use `drift new <package>` to create the directory and config:
    ```bash
    drift new nvim --target ~/.config/nvim --method copy
    ```
2.  **Add Dotfiles**: Place files directly into `src/nvim/`.
    *   **Folder Structure Relieved**: By setting `target_directory = "~/.config/nvim"`, any file inside `src/nvim/` will deploy relative to that target (e.g. `src/nvim/init.lua` will write to `~/.config/nvim/init.lua`).
    *   **Prefix Conversion**: If you are deploying files directly to your home directory, files/folders starting with standard dots (e.g., `.bashrc`) must be named with a `dot-` prefix inside the repository (e.g., `dot-bashrc`) to keep them visible and Git-friendly.
3.  **Write Templates**: Append a registered render engine suffix (like `.envst`) to dynamically compile variables:
    *   Example: `init.envst.lua` containing `${EDITOR_THEME}`.

## 🚫 PCRE-Based Package Ignore Files (`.drift_ignore`)
You can place a single `.drift_ignore` at each package's root to exclude matching files 
(like backup files or project readmes) using Perl-Compatible Regular Expressions (PCRE). 
Patterns are evaluated against relative paths *before* `dot-` prefix translation.
