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
# 1. Cleanly remove host mappings and state tracking
drift uninstall <pkg> --force

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

---

### Q6: Host tools keep generating cache files or runtime logs that trigger system drift warnings. How do I ignore them permanently?
**Situation**: An application (e.g., Neovim shada/undo files, Python `__pycache__`, GUI app logs) writes runtime cache files inside a managed configuration directory.  
**Solution**:
1.  Add PCRE ignore patterns into `src/<pkg>/.drift_ignore` (e.g., `\.swp$`, `\.bak$`, `/cache/`, `/logs/`).
2.  For Fully-Controlled Directories (FCDs), run `drift adopt <pkg> --interactive` (`-i`) and choose **Option [2] Ignore** on the detected untracked file to automatically append the ignore rule.
*   👉 Run `drift help ignore` for complete pattern syntax and GNU Stow matching rules.

---

👉 Run `drift help workspace` to learn more about workspace architecture and dual-layer configuration overrides.  
👉 Run `drift help [topic]` for topic-specific manuals.
