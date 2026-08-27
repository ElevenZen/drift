# 📥 Drift Repository Cloning & Bootstrapping (`drift clone`)

The `drift clone` command clones a remote or local Git repository and immediately prepares and heals the workspace for deployment in a single operation.

---

## 🚀 Usage

```bash
# Clone a Drift workspace repository into a default folder
drift clone git@github.com:username/dotfiles.git

# Clone into a specific destination directory
drift clone https://github.com/username/dotfiles.git ~/config/my-dotfiles

# Clone a specific branch
drift clone -b staging git@github.com:username/dotfiles.git

# Create a shallow clone
drift clone --depth 1 https://github.com/username/dotfiles.git

# Output machine-readable structured JSON
drift clone https://github.com/username/dotfiles.git --json
```

---

## ⚙️ How It Works

### Case A: Cloning an Existing Drift Workspace
When cloning an existing Drift repository:
1. Drift runs `git clone` to fetch your declarative source repository.
2. Drift automatically executes a non-destructive repair (`repair_drift_workspace`) to reconstruct runtime state databases that are intentionally omitted from version control:
   - Initializes the isolated `render/` sandbox Git repository.
   - Initializes the `install/` local state Git repository and `install/state.toml`.
   - Restores `install/.stow-local-ignore` and root `.gitignore` isolation rules.
   - Generates local configuration templates (`config/drift.local.toml`, `config/secrets.env`).
3. Drift outputs next-step guidance for configuring machine-specific overrides before deploying.

### Case B: Migrating a Plain / Legacy Dotfiles Repository
When cloning an old-style plain dotfiles repository (where dotfiles like `.bashrc` or `.config/nvim` reside in the root of the repository without Drift's `src/` hierarchy):
1. Drift clones the repository.
2. Drift isolates the existing dotfiles into a package source directory `src/<pkg_name>/`.
3. Drift initializes the Drift workspace infrastructure (`config/drift.toml`, `render/`, `install/`, `.gitignore`).
4. Drift generates `src/<pkg_name>/drift_package.toml` (defaulting to `install_method = "stow"` and `target_directory = "~"`) and `src/<pkg_name>/.drift_ignore`.
5. Drift enables the converted package in `config/drift.toml`.

---

## 🧭 Post-Clone Next Steps

After running `drift clone`:
1. `cd <directory>`
2. **Review Package Settings**: Inspect `src/<pkg>/drift_package.toml` (choose between `stow` or `copy`; see `drift help drift_package.toml`) and `src/<pkg>/.drift_ignore` (see `drift help ignore`).
3. **Configure Local Overrides**: Adjust machine-specific overrides in `config/drift.local.toml` (this overrides `config/drift.toml`; see `drift help drift.toml`).
4. **Configure Secrets**: Set tokens and environment variables in `config/secrets.env` (see `drift help workspace`).
5. **Deploy**: Run `drift diff` or `drift status` to preview, then run `drift deploy` to apply configurations to your host.
