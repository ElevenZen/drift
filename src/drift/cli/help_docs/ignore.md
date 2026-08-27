# 🚫 Drift Ignore Engine: Syntax and Integration Reference

Drift includes a robust, Perl-Compatible Regular Expression (PCRE) file ignore engine matching the exact matching logic of GNU Stow's `.stow-local-ignore`. It prevents transient files, backup dumps, system caches, build artifacts, and non-dotfile repository assets from being deployed or tracked.

---

## 1. Syntax & Core Matching Rules (GNU Stow Algorithm)

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

## 3. Enforcement & Default Stow Ignore Compatibility

### 🛡️ Default Ignore List (When No `.drift_ignore` is Provided)
If a package does not contain a `.drift_ignore` file, Drift automatically applies GNU Stow's built-in default ignore list to ensure full backward compatibility:
```pcre
RCS
\.+,v
CVS
\.\#.+=
\.cvsignore
\.svn
_darcs
\.hg
\.git
\.gitignore
.+~
\#.*\#
^/README.*
^/LICENSE.*
^/COPYING.*
```

### 🔒 Single Source of Truth
Drift strictly enforces that **only one `.drift_ignore` file** exists per package root:
*   Nested ignore files in subdirectories (e.g., `src/<pkg>/subfolder/.drift_ignore`) are prohibited to maintain a clear, single source of ignore truth.
*   Managed metadata files (`drift_package.toml`, `.drift_ignore`, `.stow-local-ignore`, `drift_package.local.toml`) are automatically protected and ignored from host linking.

### 📝 Automated `.stow-local-ignore` Generation
During staging (`drift stage`) and deployment (`drift deploy`), Drift exports all active `DriftIgnore` patterns together with `MANAGED_CONFIG_FILES` into `install/<pkg>/.stow-local-ignore`. This guarantees that GNU Stow respects both custom and default ignore rules without polluting host target directories.

---

## 4. Lifecycle & Integration Behaviors

### 📦 Compilation & Deployment Pipelines (`drift deploy` / `drift stage` / `drift apply`)
*   Files matching `.drift_ignore` are **skipped during sandbox compilation** (`render/`).
*   They are never copied or symlinked onto your active host system.
*   They are excluded from staging diffs and state database tracking.

### 🔄 Fully-Controlled Directory (FCD) Reverse-Sync
Inside Fully-Controlled Directories (FCDs), Drift monitors for untracked host files:
*   **Automatic Skip**: If an untracked host file matches `.drift_ignore`, it is completely skipped during `reverse-sync` and never pulled into `install/`.
*   **Interactive Adoption (`drift adopt -i`)**: When you choose **Option [2] Ignore** on an untracked FCD file, Drift automatically unlinks it from the `install/` state base and appends its relative pattern to `.drift_ignore`.

---

👉 Run `drift help fcd` to learn more about Fully-Controlled Directories.  
👉 Run `drift help drift_package.toml` for complete package configuration syntax.
