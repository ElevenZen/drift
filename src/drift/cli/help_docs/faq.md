# 💡 Drift Frequently Asked Questions & Troubleshooting (FAQ)

Quick solutions and battle-tested recipes for common day-to-day scenarios and troubleshooting tasks.

---

### Q1: How do I discard all host system modifications / "pollution" and force-reset back to templates?
**Situation**: You experimented with system settings, an editor wrote bad defaults, or unwanted edits occurred on your active host files that you do **not** want to keep.  
**Solution**: Run **`drift deploy --force`** (or `drift deploy <pkg> -f` for a specific package).
*   Drift bypasses system drift sentinel checks and cleanly overwrites active host configuration files with the freshly compiled templates from `src/`.

---

### Q2: What should I do if a package's installation state is stuck, out of sync, or encountering unexpected file collisions?
**Situation**: A package was manually tampered with, uncommitted file conflicts exist on the host, or you want to restart its deployment from a clean slate.  
**Solution**: **Uninstall and redeploy**:
```bash
# 1. Cleanly remove host mappings and state tracking (use --no-hooks if hooks fail)
drift uninstall <pkg> --force [--no-hooks]

# 2. Inspect/clean host destination folder if any leftover unmanaged files remain
# e.g., rm -rf ~/.config/<pkg>

# 3. Redeploy freshly from declarative templates
drift deploy <pkg>
```

---

### Q3: What if files inside `render/` or `install/` are corrupted, broken, or accidentally edited?
**Situation**: A file inside the internal sandbox `render/` or local state database `install/` was accidentally modified, deleted, or corrupted.  
**Solution**: Remember that **`src/` is the single source of truth**:
*   Internal directories (`render/` and `install/`) are purely derived/compiled from your declarative templates in `src/`.
*   Simply run **`drift render <pkg>`** (or **`drift deploy <pkg>`** / **`drift deploy --force`**).
*   Drift will re-compile clean templates from `src/` into `render/`, re-stage into `install/`, and redeploy them to your host.

---

### Q4: How do I recover from missing or broken workspace infrastructure (e.g. after cloning or accidental deletion)?
**Situation**: State Git repositories (`render/.git`, `install/.git`), root `.gitignore`, or local configuration templates (`config/drift.local.toml`, `config/secrets.env`) are missing.  
**Solution**: Run **`drift repair`** (or `drift repair --dry-run` to preview).
*   Drift non-destructively audits and self-heals workspace structure, reconstructs missing Git databases, and scaffolds missing local config templates without touching your custom dotfiles.

---

### Q5: A deployment or hook script crashed midway. How do I restore stability?
**Situation**: A template syntax error, permission failure, or hook script crash stopped `drift deploy` halfway through, leaving files in a transitional state.  
**Solution**: Run **`drift rollback <pkg>`** (or `drift rollback`).
*   Drift resets `install/` to the last committed clean HEAD, removes half-written untracked files, and redeploys the last known stable state to your active host.
*   If hook scripts themselves are broken or crashing during rollback, append **`--no-hooks`** (or **`--no-hook`**) to perform pure file-level recovery.

---

### Q6: Host tools keep generating cache files or runtime logs that trigger system drift warnings. How do I ignore them permanently?
**Situation**: An application (e.g., Neovim shada/undo files, Python `__pycache__`, GUI app logs) writes runtime cache files inside a managed configuration directory.  
**Solution**:
1.  Add PCRE ignore patterns into `src/<pkg>/.drift_ignore` (e.g., `\.swp$`, `\.bak$`, `/cache/`, `/logs/`).
2.  For Fully-Controlled Directories (FCDs), run `drift adopt <pkg> --interactive` (`-i`) and choose **Option [2] Ignore** on the detected untracked file to automatically append the ignore rule.
*   👉 Run `drift help ignore` for complete pattern syntax and GNU Stow matching rules.

---

### Q7: What if `rollback` or `uninstall` doesn't run well in a broken install due to failing hook scripts?
**Situation**: In a damaged, broken, or half-configured install, lifecycle hooks (such as `pre_uninstall`, `post_uninstall`, `pre_update`, or `post_update`) may fail because of missing system dependencies, broken interpreters, or invalid script syntax, blocking you from rolling back or uninstalling the package.  
**Solution**: Pass the **`--no-hooks`** (or **`--no-hook`**) flag to bypass hook executions and perform only the essential file and database operations:
```bash
# Safely restore clean state without executing broken hooks:
drift rollback <pkg> --no-hooks

# Forcefully remove a broken package and restore backups without executing hooks:
drift uninstall <pkg> --force --no-hooks
```
*   This skips all hook scripts and directly performs physical file operations (symlink unlinking, file removals, backup restorations, and state database cleanups).

---

### Q8: When should I choose `install_method = "copy"` instead of `"stow"`? (Event Ordering & Daemons)
**Situation**: You have a daemon or system service (e.g., `systemd` user service, application with active `inotify` file watchers) that monitors a config file. You need your `pre_update` hook script to shut down the service *before* the configuration file content changes.  
**Solution**: Use **`install_method = "copy"`** in `drift_package.toml`:
*   **Why**: With `install_method = "stow"`, the host file is a symlink directly pointing into `install/<pkg>/`. When Drift stages compiled templates to `install/` during Primitive 4, the host system immediately sees the updated file contents **before** `pre_update` runs in Primitive 5.
*   **With `copy`**: The host target file remains completely untouched at the old version while `pre_update` runs. Physical files are updated only during host delivery in Primitive 5, providing **strict and predictable event ordering** (`staging` $\rightarrow$ `pre_update` $\rightarrow$ `file copy` $\rightarrow$ `post_update`).
*   **Rule of Thumb**:
    *   Use **`stow`** for interactive user configs (shell, tmux, vim, git) for instant reflection and lightweight symlinks.
    *   Use **`copy`** for services, daemons, or when exact lifecycle hook timing is essential.

---

### Q9: What if a broken hook script hurdles me from writing or deploying a package correctly?
**Situation**: You are writing a new package or updating existing lifecycle hooks, but hook script errors (syntax errors, missing runtime variables, or broken exit codes) prevent successful `drift deploy` or `drift apply`.  
**Solution**: Deploy with **`--no-hooks`** first, then test and debug your hook directly from source via **`drift hook --from src <pkg> <hook> -v`**:
```bash
# 1. Deploy files safely while bypassing failing hook executions:
drift deploy <pkg> --no-hooks

# 2. Iteratively edit hook scripts in src/<pkg>/ and execute directly in isolation:
drift hook <pkg> <hook> --from src -v
```
*   Running `drift hook --from src <pkg> <hook> -v` executes the debug version of the script in your `src/` directory with full 7-tier environment variables and host facts injected, showing real-time stdout, stderr, and return codes.

---

👉 Run `drift help workspace` to learn more about workspace architecture and dual-layer configuration overrides.  
👉 Run `drift help [topic]` for topic-specific manuals.
