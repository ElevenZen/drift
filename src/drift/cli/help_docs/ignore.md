# 🚫 Drift Ignore Engine: Syntax and Integration Reference

Drift includes a robust, Perl-Compatible Regular Expression (PCRE) file ignore engine whose syntax is derived from GNU Stow's ignore file specification (matching both relative path prefixes and basenames), with the sole architectural exception that Drift's internal control plane (`.drift/`) is always automatically ignored and never deployed to the host. It prevents transient files, backup dumps, system caches, build artifacts, and non-dotfile repository assets from being deployed to host targets.

> [!IMPORTANT]
> **Stage-Specific Ignore Invariant: Installation Only, Not Rendering**:
> In Drift's pipeline, **rendering is NOT skipped for ignored files**. All source files and templates under `src/<pkg>/` are fully compiled and rendered into `render/<pkg>/` and staged into `install/<pkg>/`.
> 
> File ignore evaluation happens **strictly at the installation stage** (when projecting or copying files from `install/<pkg>/` to the host target directory). Ignored files exist in the local state database but are **never symlinked or copied to your host system**.

---

## 1. Syntax & Core Matching Rules (Two-Group PCRE Algorithm)

Each package can have **exactly one** `.drift_ignore` file placed directly in its root (`src/<pkg>/.drift_ignore`).

### 📌 Comment & Line Rules
*   **Comments**: Lines starting with `#` are ignored.
*   **Empty Lines**: Blank lines and whitespace-only lines are ignored.
*   **Inline Escaped Comments**: To include a literal `#` in a regex pattern, escape it with a backslash (`\#`).

---

### 🔍 Two-Group Pattern Evaluation
Drift splits all ignore regex patterns into two distinct groups based on whether the pattern contains a forward slash (`/`):

#### Group 1: Patterns Containing `/` (Relative Path Matching)
When a pattern contains at least one `/`, it is matched against the file's **full relative path inside the package, prefixed with `/`** (e.g., `/app.log`, `/subfolder/file.txt`, `/dot-config/nvim/coc-settings.json`).

*   **Anchoring to Package Root**:
    To match a file or directory strictly at the package root, anchor with `^/`:
    ```pcre
    ^/README(\..*)?$
    ^/LICENSE(\..*)?$
    ^/install.*\.sh$
    ^/sample\.txt$
    ```
    > ⚠️ **Important**: Do **not** use `./` (e.g. `./sample.txt` will fail to match). Always use `^/`.

*   **Matching Subdirectories Anywhere**:
    ```pcre
    /cache/
    /build/
    /logs/
    ```

*   **Matching Specific Nested Paths**:
    ```pcre
    ^/dot-config/nvim/undodir/.*$
    ^/dot-config/gh/hosts\.yml$
    ```

---

#### Group 2: Patterns WITHOUT `/` (Basename Matching)
When a pattern contains no `/`, it is matched against the **isolated file or directory basename** anywhere in the package hierarchy:

*   **File Extension / Suffix Matching**:
    ```pcre
    \.bak$        # Matches foo.bak, sub/bar.bak
    \.tmp$        # Matches data.tmp, sub/dir/temp.tmp
    \.sw[p-z]$    # Matches Vim/Neovim swap files (.swp, .swo, .swx)
    ~$            # Matches editor backup files ending with ~
    \.un~$         # Matches Vim undo history files
    ```

*   **Prefix Matching**:
    ```pcre
    ^~            # Matches temporary files starting with ~
    ^\.git        # Matches .git, .gitignore, .gitmodules
    ```

*   **Exact Basename Matching**:
    ```pcre
    ^Thumbs\.db$  # Matches Windows thumbnail cache
    ^\.DS_Store$  # Matches macOS Finder metadata
    ```

---

## 2. Source Naming Convention & Prefix Translation (`dot-`)

In Drift source packages (`src/<pkg>/`), hidden files and directories can be represented using the `dot-` prefix convention (e.g. `dot-config/` instead of `.config/`, `dot-bashrc` instead of `.bashrc`).

*   **Match Timing Guard**: Drift evaluates `.drift_ignore` patterns against the native repository filenames **before** prefix expansion is applied.
*   **Writing Patterns for Hidden Files**:
    ```pcre
    ^/dot-bash_history$
    ^/dot-config/qBittorrent/logs/
    ```

---

## 3. Enforcement & Default Ignore Rules

### 🛡️ Default Ignore List (When No `.drift_ignore` is Provided)
If a package does not contain a `.drift_ignore` file, Drift automatically applies the comprehensive built-in default ignore list (`DEFAULT_DRIFT_IGNORE_CONTENT`):

```pcre
# Python bytecode and cache files
__pycache__
/__pycache__/
\.py[cod]$
\$py\.class$
\.pytest_cache
/\.pytest_cache/
\.mypy_cache
/\.mypy_cache/
\.ruff_cache
/\.ruff_cache/
\.venv
/\.venv/
^venv$
/venv/

# Version control systems & ignore metadata
^/\.gitignore
\.gitignore
\.git
\.hg
\.svn
_darcs
CVS
\.cvsignore
RCS
\.+,v
\.\#.+

# Editor temporary and backup files
.+~
\#.*\#
.*\.sw[a-p]$
.*\.swp$
.*\.swo$
.*\.un~$

# OS metadata
^\.DS_Store$
^Thumbs\.db$

# Package documentation and licenses
^/README.*
^/LICENSE.*
^/COPYING.*

# Drift internal control plane and sandbox
^/\.drift/
^/\.drift$
```

### 🔒 Single Source of Truth & Internal Metadata Isolation
Drift strictly enforces that **only one `.drift_ignore` file** exists per package root:
*   Nested ignore files in subdirectories (e.g., `src/<pkg>/subfolder/.drift_ignore`) are prohibited to maintain a clear, single source of ignore truth.
*   **Internal `.drift/` Directory**: The internal Drift directory (`.drift/`, containing package configuration `.drift/drift_package.toml`, ignore rules `.drift/.drift_ignore`, staged render engine inputs `.drift/render/`, and compiled lifecycle hooks in `.drift/hooks/`) is hardcoded as permanently ignored and never deployed to the active host target.

### 📝 Native Linker Evaluation & Sub-Repo `.gitignore` Generation
*   **Native Linker Evaluation**: During deployment (`drift deploy` or `drift apply`), Drift's native linker directly evaluates active `DriftIgnore` patterns (from `install/<pkg>/.drift/.drift_ignore` or default rules) when gathering deployable files. No synthetic `.stow-local-ignore` files are generated, maintaining 100% 1:1 structural fidelity between `render/` and `install/`.
*   **Sub-Repo `.gitignore`**: Drift automatically generates and maintains `.gitignore` files inside `render/` and `install/` databases to exclude synthetic files (`.gitignore*`) and editor/OS temporary files (`TEMPORARY_FILE_PATTERNS`: `*~`, `*#*#`, `*.swp`, `*.DS_Store`, etc.), keeping database Git repositories clean.

---

## 4. Lifecycle & Integration Behaviors

### 📦 Stage Separation: Render vs. Install

#### 1. Rendering Stage (`src/` ➔ `render/`)
* **Rendering is NOT skipped**: All source files and templates in `src/<pkg>/` are compiled and written into `render/<pkg>/`, even if they match ignore patterns.
* This guarantees that documentation, licenses, build helper templates, and internal artifacts are rendered and staged consistently in the sandbox repository.

#### 2. Installation Stage (`install/` ➔ Host Target)
Ignore pattern filtering occurs **exclusively during the installation / deployment stage**:
* **Symlink Deployment (`install_method = "symlink"`)**: Drift's native symlink engine evaluates `DriftIgnore` patterns and skips creating symlinks for any matching files on the host target.
* **Copy Deployment (`install_method = "copy"`)**: Drift's copy engine evaluates the ignore patterns against each staged file and skips copying matching files to the host target.
* Ignored files exist safely within the `install/<pkg>/` state database but **never reach or pollute the host system**.

### 🔄 Fully-Controlled Directory (FCD) Reverse-Sync
Inside Fully-Controlled Directories (FCDs), Drift monitors for untracked host files:
*   **Automatic Skip**: If an untracked host file matches `.drift_ignore`, it is completely skipped during `reverse-sync` and never pulled into `install/`.
*   **Interactive Adoption (`drift adopt -i`)**: When you choose **Option [2] Ignore** on an untracked FCD file, Drift automatically unlinks it from the `install/` state base and appends its relative pattern to `.drift_ignore`.

---

👉 Run `drift help fcd` to learn more about Fully-Controlled Directories.  
👉 Run `drift help drift_package.toml` for complete package configuration syntax.
