# 💾 Local State Database & Installation: `install/`

The `install/` directory represents the "Tier 3" database. It is a pure, independent Git 
repository that tracks the "last known good" deployed state of your host system, alongside 
an explicit registry database (`state.toml`).

## 🔄 Differential Tracking
Rather than blindly overwriting active directories, Drift computes the state differences 
between the sandbox `render/` and the last successfully applied `install/` baseline. This 
reconciliation:
*   Identifies exactly which files are **Added**, **Modified**, or **Deleted**.
*   Generates an incremental staging instruction list, allowing Drift to execute surgical 
    copying or symlinking.

## 🛑 Proactive Collision Guard
Before modifying any live file on your system, Drift runs a comprehensive collision audit:
*   **Zero Overwrite**: Any manual, untracked file on the host blocking deployment is 
    safely backed up to `backup/<package>/overwritten/` before the deploy.
*   **Clean Sweeping**: Orphaned and deleted files are pruned from the host and swept 
    safely to `backup/<package>/deleted_files/`.
*   **Nesting Prevention**: Drift prevents deployment if a target directory is equal to 
    or nested inside your Drift workspace root, avoiding infinite stow loops.

## 🛡️ Internal Metadata & Hook Isolation (`.drift/`)
The `install/<pkg>/.drift/` directory stores internal package metadata, staged render engine inputs, and lifecycle hooks (`.drift/hooks/`):
*   **Complete Host Isolation**: The `.drift/` directory is hardcoded as ignored in `DriftIgnore` and is **never deployed, copied, or symlinked to the active host target directory**.
*   **Stow & Copy Protection**: During deployment, GNU Stow ignores `.drift/` via generated `.stow-local-ignore`, and copy deployment skips `.drift/` entirely.

## 📦 Deployment Methods & Dot-Prefix Translation
Drift supports both `stow` (GNU Stow symlinks) and `copy` (physical file copies) deployment methods:
*   **`stow`**: Creates symmetric symlinks pointing into `install/<pkg>/`.
*   **`copy`**: Executes atomic, safe physical file delivery.
*   **Dot-Prefix Translation (`dot-`)**: In both `stow` and `copy` deployment modes, Drift automatically translates repository `dot-` prefixes (e.g. `dot-config/` $\rightarrow$ `.config/`, `dot-zshrc` $\rightarrow$ `.zshrc`) during deployment.

> ⚠️ **Note on `backup/`**: The `backup/` directory is untracked and unversioned. Users are responsible for archiving or saving critical files from `backup/` manually.
