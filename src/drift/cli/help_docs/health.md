# 🩺 Drift Package Runtime Health Checks (`drift health`)

The `drift health` command runs runtime health check probes on installed packages to verify if deployed services, background daemons, terminal environments, and host tools are functioning and healthy on the host machine.

---

## 🚀 Usage

```bash
# Check health of all installed packages (default: from install/ directory)
drift health

# Check health of specific packages
drift health nvim tmux myservice

# Check health directly from source directory (compiling templates on-the-fly)
drift health --from source

# Output in machine-readable JSON format
drift health --json

# Set a custom timeout per probe (in seconds)
drift health --timeout 30

# Enable verbose logging with stdout/stderr preview
drift health --verbose
```

---

## ⚙️ Configuring a Health Hook

Define the `health` hook inside your package's `drift_package.toml`:

```toml
[package]
install_method = "stow"
target_directory = "~/.config/tmux"

[hooks]
# Path to executable probe script (relative to package root)
health = "scripts/health_check.sh"

# Timeout in seconds for the probe execution (optional, default 120)
timeout = 15
```

---

## ⚡ Execution Invariants

1. **Script Source Base**: The probe script is read from either `install/<pkg>/` (`--from install`, default) or `src/<pkg>/` (`--from source`). Templates are rendered dynamically into `render/<pkg>/`.
2. **Working Directory (CWD)**: Executed with the **package's host target directory** as the working directory (`cwd = target_directory`).
3. **Environment Injection**: Complete 7-tier package variables (`$drift_package_name`, `$drift_package_target_dir`, `$drift_package_install_method`, `$drift_*`, `[env.override]`, `[env.fallback]`, secrets) are automatically injected.
4. **Sudo Privileges**: Health probes always execute in user space without `sudo` elevation for safety and predictability.
5. **Exit Code Evaluation**:
   - `Exit 0`: Evaluated as **`HEALTHY`**.
   - `Non-zero exit code`: Evaluated as **`UNHEALTHY`**.
   - The overall `drift health` command exits with `0` if all evaluated packages pass, and `1` if any fail.
