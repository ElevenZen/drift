# 📦 Installation & Packaging

Drift requires **Python 3.9+** and has **zero mandatory third-party dependencies** in its core mode (with optional `[rich]` terminal UI support).

## 1. ⚡ Shell Wrapper Installer (**Strongly Recommended**)

> [!TIP]
> **Why the Shell Wrapper is the Best Way to Install Drift**:  
> Drift is currently under rapid active development. Because development builds do not constantly bump the version number in `pyproject.toml`, package managers like `pipx` or `uv tool` will cache previous builds and refuse to update or re-install when pointing to an unbumped version number.  
>  
> The **Shell Wrapper** sets up a lightweight executable launcher in `~/.local/bin/drift` that directly delegates to your local repository source code (`src/`). Any `git pull` updates, bug fixes, or new features are **immediately active without requiring reinstallation or manual version bumps!**

To install the wrapper, clone the repository and run:
```bash
git clone https://github.com/ElevenZen/drift.git
cd drift
./script/shell_wrapper_installer.bash
```
*   Pass `--force` (`-f`) to overwrite an existing wrapper.
*   Pass `--dir <path>` (`-d`) to install into a custom directory.

---

## 2. 📦 Standalone Executable (`zipapp`) & Release Artifacts
Because Drift is self-contained with pure standard library support, it can be packaged into a single portable binary file with Python's built-in `zipapp`:

### Automated Build & Verification Pipeline
Use the built-in pipeline script to build both the standalone Zipapp and Python Wheel (`.whl`), with automated isolated verification tests:
```bash
# Build all release artifacts (Wheel + Zipapp) and execute verification tests
./script/build_artifacts.bash

# Clean previous build artifacts and exit
./script/build_artifacts.bash --clean

# Clean first, then rebuild all artifacts
./script/build_artifacts.bash --rebuild

# Or build standalone zipapp only:
./script/build_artifacts.bash --zipapp-only
```
Generated artifacts are placed in `dist/`:
*   `dist/drift`: Standalone zero-dependency executable binary (ready to copy to `~/.local/bin` or remote servers).
*   `dist/drift-0.1.0-py3-none-any.whl`: Standard Python distribution wheel.

### Manual Zipapp Build
```bash
# Build standalone executable manually
python3 -m zipapp src -m "drift.cli:main" -o drift -p "/usr/bin/env python3"
chmod +x drift

# Move to your PATH or copy to remote servers
mv drift ~/.local/bin/
```

---

## 3. 🐍 Python Package Managers (`pipx`, `uv`, or `pip`)

> [!NOTE]
> When using `pipx`, note that `pipx upgrade` or reinstalling will skip processing if the package version has not changed. Use `pipx install --force ...` to force updates if needed.

### A. Isolated Global CLI via `pipx` or `uv tool`
Install Drift into an isolated environment and place it directly into your `$PATH`:
```bash
# Core standard-library mode (zero external dependencies)
pipx install --force git+https://github.com/ElevenZen/drift.git

# Or with uv:
uv tool install --force git+https://github.com/ElevenZen/drift.git

# With enhanced Rich console UI:
pipx install --force "git+https://github.com/ElevenZen/drift.git#egg=drift[rich]"
```

### B. Standard `pip install`
Install locally or from a Git repository via `pip`:
```bash
pip install --user git+https://github.com/ElevenZen/drift.git

# Or from a cloned local repository:
pip install --user .
```

---

## 4. 🐚 Interactive Shell Tab-Completion (`drift complete`)
 
Drift features built-in, zero-latency tab-completion generators for **Bash**, **Zsh**, **Fish**, and **Nushell** with rich inline documentation hints and dynamic package discovery.

### Automatic Installation (Standard User Directories)
Run `drift complete --install` to automatically install completion scripts for all supported shells into standard user directories (`~/.local/share/bash-completion/completions/drift`, `~/.local/share/zsh/site-functions/_drift`, `~/.config/fish/completions/drift.fish`, `~/.config/nushell/completions/drift.nu`), or specify a single shell (e.g. `drift complete nu --install`):
```bash
drift complete --install
```
*(Note: If you used `./script/shell_wrapper_installer.bash`, tab-completions are installed automatically during setup!)*

### Dynamic Startup Evaluation (Shell RC / Config)
Alternatively, evaluate or source completions in your shell configuration:
*   **Bash** (`~/.bashrc`):
    ```bash
    eval "$(drift complete bash)"
    ```
*   **Zsh** (`~/.zshrc`):
    ```zsh
    eval "$(drift complete zsh)"
    ```
*   **Fish** (`~/.config/fish/config.fish`):
    ```fish
    drift complete fish | source
    ```
*   **Nushell** (`~/.config/nushell/config.nu`):
    ```nu
    use ~/.config/nushell/completions/drift.nu *
    ```
