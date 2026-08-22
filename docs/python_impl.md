# Python Implementation Blueprint: Dotfiles Orchestration Engine

This folder is dedicated to the development of the Python-based Dotfiles Orchestration Engine. 
The implementation of the 7 core primitives and workflows outlined in `@design.md` should be built inside this directory using Python 3, adhering to the technical specifications and guards detailed below.

---

## 1. Orchestration Language & Dependency Policy

*   **Choice**: **Python 3** (target compatibility: Python >= 3.8).
*   **Zero-Dependency Mandate**: The script must rely **strictly** on the Python Standard Library. No external pip packages (like `click`, `pyyaml`, or third-party `toml` libraries) are allowed. This ensures that the engine can run seamlessly on a fresh minimal OS installation.
*   **Key Modules**:
    *   `os`, `sys`, `shutil`: For core filesystem manipulation and paths management.
    *   `pathlib`: For robust object-oriented path traversals and comparisons.
    *   `subprocess`: For executing surgical command elevations (`sudo`) and external compiler/stow triggers.
    *   `json` or hand-rolled parse helpers: For configuration formats. *Note on Python < 3.11 compatibility*: Since Python 3.11 introduced `tomllib` natively, for compatibility with older Python 3.x installations on target machines, a lightweight, hand-rolled TOML reader (regular expression/line-by-line parser) should be implemented to read `drift_package.toml` and `drift.toml` without external pip dependencies.

---

## 2. Privilege Escalation & Isolation (Surgical Sudo)

*   **The Hazard**: Running the orchestration script as `sudo python deploy.py` is **forbidden**. It corrupts `$HOME` expansions (pointing to `/root` instead of the user home directory) and permissions of sandbox directories (`render/`, `install/`, and `backup/`).
*   **The Blueprint**:
    1.  The engine must always start and run under the **unprivileged active user account**.
    2.  User home directories must be resolved dynamically using `os.path.expanduser("~")` or `pathlib.Path.home()`, which guarantees resolving the correct unprivileged host paths.
    3.  When a file copy or link operation requires root access (`sudo = true` in `drift_package.toml`):
        *   Do not write files directly. Instead, write to a temp file in unprivileged space (e.g. `render/` or `/tmp/`), and then invoke a surgical sub-process:
            ```python
            subprocess.run(["sudo", "cp", temp_file, target_path], check=True)
            ```
        *   This ensures that file ownership is managed cleanly, and root credentials are only prompted at the exact moment of system write side-effects.

---

## 3. Platform Portability & Standard Library Enforcement

*   **The Hazard**: Calling shell commands (like GNU `readlink -f`, `stat -c`, or `cp -r`) introduces fragile platform discrepancies between Linux (GNU-based) and macOS (BSD-based), causing failures during cross-host reverse synchronizations.
*   **The Blueprint**:
    *   Never spawn shell subprocesses to query filesystem states. Use the **Python Standard Library** exclusively for auditing, as it abstracts system-specific C libraries under a unified cross-platform Python API.
    *   *Examples*:
        *   Determine if a file is a symbolic link: `os.path.islink(path)`
        *   Read target of symbolic link: `os.readlink(path)`
        *   Recursively copy directories: `shutil.copytree(src, dst)`
        *   Calculate file hash (for copy-mode drift checks): Use `hashlib.sha256()` instead of `md5sum` or `sha256sum` commands.

---

## 4. Prefix Translation Algorithm (Segment-Level Rewriting)

*   **The Hazard**: Converting prefix `dot-` to `.` via a simple string replace (e.g., `path.replace("dot-", ".")`) is prone to collisions. For instance, a regular file named `my-dot-file.conf` would be incorrectly modified to `my.-file.conf`.
*   **The Blueprint**:
    *   Path conversions must be calculated strictly on **Segment-Level** path fragments using a splitting algorithm.
    *   *Algorithm*:
        ```python
        from pathlib import Path

        def translate_dot_prefixes(relative_path: str) -> str:
            path_obj = Path(relative_path)
            parts = list(path_obj.parts)
            
            translated_parts = []
            for part in parts:
                # Only rewrite the part if it starts with the explicit prefix "dot-"
                if part.startswith("dot-"):
                    translated_parts.append("." + part[4:])
                else:
                    translated_parts.append(part)
                    
            return str(Path(*translated_parts))
        ```
    *   This ensures that segment-boundaries are respected symmetrically across both `stow` and `copy` deployment strategies.

---

## 5. Atomicity & Failure Recovery (Fail-Safe Strategy)

*   **The Hazard**: Network drops, power failures, or `Ctrl+C` interruptions during deployment (Primitive 5) can leave application directories in an inconsistent or corrupt state.
*   **The Blueprint**:
    *   **Atomic Copies**: When performing `copy` installations, copy the file to a temporary location on the target filesystem first, then run a surgical `mv` to overwrite. This prevents partial writes if the process is terminated mid-execution.
    *   **Incremental Checklist**: Primitive 4 must write a JSON file (`install/.changelist.json`) detailing files slated for deployment. If execution is interrupted, the next run can read this changelist to resume incrementally.
    *   **Fail-Fast Hook Isolation**: If a lifecycle hook (e.g. `pre_install` or `post_update` script) returns a non-zero exit status, the script must halt immediately. It must print the exact failure log and halt Stage 2 to prevent subsequent state corruption.

---

## 6. Template Suffix Parser Rules

*   During Sandbox Render (Primitive 2), the renderer must scan `src/<package_name>/` recursively and parse template names dynamically:
    1.  Files with suffix **`.envst.[ext]`**: Send through the `envsubst` pipeline, stripping `.envst` from the final filename (e.g., `init.envst.lua` renders to `init.lua` in `render/`).
    2.  Files with suffix **`.mustache.[ext]`**: Send through the `mustache` parser pipeline, stripping `.mustache` from the final filename.
    3.  Other files: Copy directly without templating.
