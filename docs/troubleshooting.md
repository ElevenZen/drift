# 💡 Troubleshooting & Practical FAQ

Quick answers to the most common operational questions. For flag details see [cli.md](cli.md); for the underlying model see [features.md](features.md).

## 1. Discarding System Drift Pollution & Overwriting Host Files
* **Q**: How do I discard all host system modifications and force-reset host files back to the declarative templates?
* **A**: Run **`drift deploy --force`** (or `drift deploy <pkg> -f`). Drift bypasses system drift sentinel safeguards and forces the target files to match the freshly compiled templates from `src/`.

## 2. Resetting a Stuck or Problematic Package
* **Q**: What should I do if a package's installation state is corrupted, out of sync, or encountering unexpected file collisions?
* **A**: **Uninstall and redeploy**:
  ```bash
  drift uninstall <pkg> --force   # Remove host mappings and reset state tracking
  # (Optional: verify and clean any unmanaged leftovers in target directory)
  drift deploy <pkg>              # Cleanly compile, stage, and redeploy
  ```

## 3. Recovering from Corrupted Files in `render/` or `install/`
* **Q**: What if a file inside internal sandbox `render/` or local state database `install/` was accidentally deleted or broken?
* **A**: In Drift, **`src/` is the single source of truth**. Simply run **`drift render <pkg>`** or **`drift deploy <pkg>`**; Drift will automatically re-compile and re-stage clean files from `src/`.

## 4. Self-Healing Missing Workspace Databases & Templates
* **Q**: My Git databases (`render/.git`, `install/.git`), `.gitignore` rules, or local config templates (`config/drift_workspace.local.toml`, `config/secrets.env`) are missing.
* **A**: Run **`drift repair`** to audit and reconstruct missing workspace databases, `.gitignore` rules, and template files non-destructively.

## 5. Recovering from Midway Deployment Failures
* **Q**: A deployment script or hook crashed midway and left files in a half-written state.
* **A**: Run **`drift rollback <pkg>`** to revert `install/` to the last committed clean HEAD and redeploy the last stable state.

## 6. Deploying Files Without Hooks & Debugging Broken Scripts
* **Q**: How do I deploy a package's configuration files without executing its lifecycle hooks, or debug a broken hook script?
* **A**: Deploy with **`--no-hooks`** to install the package's configuration files directly to disk without running any lifecycle hooks (`pre_install`, `post_update`, etc.). You can then test and debug any hook script in isolation using **`drift hook --from src <pkg> <hook> -v`**:
  ```bash
  # 1. Deploy the package's configuration files safely without running any hooks:
  drift deploy <pkg> --no-hooks

  # 2. Iterate and debug the hook script live from source with all injected variables & verbose output:
  drift hook <pkg> <hook> --from src -v
  ```
  This allows you to safely place your configuration files on disk while iterating on hook scripts in `src/<pkg>/` with full 6-tier environment variables and host facts injected.

## 7. Forcing Full Reinstallation (`-r` / `--reinstall`)
* **Q**: Why was package deployment skipped, and how do I force reinstallation of all packages and lifecycle hooks?
* **A**: Drift optimizes deploy cycles by inspecting staging outputs (`installed_files`, hook scripts, and `drift_package.toml`). If a package has no stage changes, Drift skips deploying it. To force a full reinstallation of all packages and execute their lifecycle hooks regardless of changes, pass **`-r`** or **`--reinstall`**:
  ```bash
  drift deploy -r
  ```

## 8. Inspecting Full Exception Stack Traces (`--trace` vs `--raw-errors`)
* **Q**: How do I see raw Python tracebacks and full stack traces when troubleshooting an error?
* **A**: Use **`--trace`** for full diagnostic mode (debug logs + stack traces) or **`--raw-errors`** to only unmask exceptions without debug log output:
  ```bash
  # Full debug logs + unmasked traceback on error:
  drift --trace deploy
  # Or as a movable flag after the subcommand:
  drift deploy --trace

  # Raw exception traceback only (standard logging):
  drift deploy --raw-errors
  ```

## 9. Deploy Reports System Drift but `drift diff` Outputs Nothing (`install-commit`)
* **Q**: What should I do when `drift deploy` halts with a "System drift detected" warning, but `drift diff` (or `drift diff -s`) outputs no differences?
* **A**: This indicates that the `install/` local state database repository contains uncommitted Git changes that match the compiled `render/` output (most commonly caused by an interrupted deployment or a failed commit step). Because the working tree files in `install/` already match `render/` and the host system, `drift diff` detects no delta, but the underlying `install/.git` repository is dirty.
  To resolve this cleanly, manually commit the staged changes in `install/`:
  ```bash
  # Commit uncommitted changes in the install state database:
  drift install-commit -m "chore: sync uncommitted install state" [packages...]

  # Then resume normal deployment:
  drift deploy
  ```

## 10. Launching Visual Side-by-Side Diffs in Your Editor
* **Q**: How do I review diffs side-by-side in my terminal editor or GUI editor (like Neovim, Vim, VS Code, or GNU Emacs)?
* **A**: Pass **`-y`** or **`--side-by-side`** to `drift diff`:
  ```bash
  # Side-by-side visual diff of pending deployments:
  drift diff -y

  # Visual diff of active system drift:
  drift diff -s -y nvim
  ```
  Drift automatically detects `$VISUAL` / `$EDITOR` (or finds `nvim`, `vim`, `code`, `emacs`) and opens changed file pairs in side-by-side split viewports across multiple tabs/windows.

## 11. Configuring Git User Identity Without Global Git Config
* **Q**: What should I do if `drift deploy` fails with "Git configuration error: 'user.name' is not configured", and I don't want to set global Git config (e.g. on CI runners or headless servers)?
* **A**: Define `git_user_name` and `git_user_email` under `[settings]` in `config/drift_workspace.toml` (or machine-specific `config/drift_workspace.local.toml`):
  ```toml
  [settings]
  git_user_name = "Drift Bot"
  git_user_email = "drift@localhost"
  ```
  Then run **`drift repair`** to apply it locally to the `render/` and `install/` repositories.

👉 Run `drift help faq` for more tips and topic manuals.
