# "drift": Decoupled Two-Stage Git-Backed Dotfiles Manager
## Product Design & Interactive CLI Specification

---

## 1. Introduction & Product Vision

**"drift"** is a modern, developer-centric dotfiles manager written in Python. It is designed to solve the age-old conflict of dotfiles management: **how to maintain a pristine, declarative template-based configuration source while gracefully embracing, auditing, and synchronizing runtime system/GUI changes (drifts).**

Unlike traditional one-stage dotfiles managers (like GNU Stow, Chezmoi, or Dotbot) which either blindly overwrite local changes or hide system state in obscure binary databases, **drift** relies on a **Decoupled Two-Stage Git-Backed local database architecture**. By turning both the sandbox rendering zone (`render/`) and the applied configuration state (`install/`) into fully operational, local-only Git repositories, drift grants developers absolute visibility, safety, and bidirectional synchronization.

### The Core Philosophies of drift:
1. **Embrace the Drift**: Systems change. GUI tools write runtime adjustments, themes update, and hot-edits happen. Instead of fighting them, drift treats system drift as uncommitted Git edits in the local state database.
2. **Double Git-Database Integrity**: By leveraging standard Git indices inside `render/` and `install/`, drift delegates version control, differential tracking, and rollbacks to Git itself, keeping the CLI engine incredibly lightweight, auditable, and transparent.
3. **Sandbox Isolation**: Template compiling and rendering never touch active host paths directly. Edits are isolated inside a clean `render/` repository before staging.

---

## 2. Competitive Edge & Market Positioning

Comparing **drift** to popular dotfiles managers listed on `dotfiles.github.io/utilities`:

| Feature | **drift** (Python) | **Chezmoi** (Go) | **Dotbot** (Python) | **GNU Stow** (Perl) | **VCSH** (Shell) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Engine** | **Dual Local Git Repos** | Single Repo + BoltDB | None (YAML links) | Symlink Farm | Bare Git on `$HOME` |
| **Pipeline Stages** | **2-Stage (Render $\rightarrow$ DB $\rightarrow$ System)** | 1-Stage (Compile $\rightarrow$ System) | 1-Stage (Link) | 1-Stage (Link) | 1-Stage (Direct Git) |
| **Active Drift Audit**| **Yes (Automatic Reverse Sync)** | Yes (Manual `re-add/merge`)| No | No | Rely on Git status |
| **Dry-Run Fidelity** | **Absolute (Diff C comparison)** | Dry-run on templates | No | Stow `-n` simulation | No |
| **Mid-Fail Rollback** | **Yes (Dedicated Function)** | Manual cleanup | No | Stow `-D` unlink | No |

### Why drift beats the alternatives:
* **Over Chezmoi**: Chezmoi hides state in an opaque BoltDB binary database. Under drift, your `install/` state database is a **pure Git repository**. You can walk into `install/`, run `git log`, `git checkout`, or hook up git GUI clients (like Lazygit or GitKraken) to review your deployment history.
* **Over Dotbot/Stow**: They are strictly one-way bootstrap scripts. They do not comprehend drift, leaving you susceptible to silently lost runtime configurations.

---

## 3. High-Level Architectural Pipeline

```
                     [ 1. DECLARATIVE SOURCE ]
                     src/ (Templates & drift.toml)
                                 │
                                 ▼ (drift render / Stage 2 sandbox compiler)
                    [ 2. SANDBOX RENDER ZONE ]
                      render/ (Git repo tracking template compile history)
                                 │
                        Diff A   ▼ (Stage 2 incremental sync)
                        (Dry)   [ 3. LOCAL STATE DATABASE ]
                      install/ (Git repo tracking live configuration state)
                                 ▲
                        Diff B   │ (Stage 1 System -> install/ reverse-sync)
                       (Live)    ▼ (Symlinks or Physical Copy)
                    [ 4. SYSTEM ACTIVE CONFIGS ]
                       ~/* or /etc/* (Active system configuration files)
```

---

## 4. User-Facing CLI Command Design

By replacing complex Makefiles, drift exposes a rich, intuitive, and colored CLI interface powered by Python's `Typer` and `rich`.

```
drift [--global-flags] [command] [package] [--command-flags]
```

Global Flags:  
`-C, --directory` Run as if drift is started in `<directory>` instead of current working directory.  
`--no-git-root` Stop resolving git root of cwd or -C directory, using the literal path instead.  
`-v, --verbose` Enable verbose (DEBUG) logging output.


### A. Initialization: `drift init`
Initializes the active repository as a drift workspace.  
Only works if the directory is empty or tracked by git.  
*   **Actions**:
    1.  If the directory is empty and not tracked by git, then init an empty git repo.  
    2.  Verifies the main repository is tracked by Git, if not, raise an error.  
    3.  Change to git root, and check if it's already inited with necessary files.  
    4.  Creates `.gitignore` entries to isolate `render/` and `install/` folders.
    5.  Initializes `render/` and `install/` as independent, untracked local Git repositories.
    6.  Creates default directory templates (`src/`, `config/drift.toml`, `config/envsubst.bash`, `config/mustache.envst.json`, `install/state.toml`).
*   **Terminal Output**:
    ```bash
    ✨ Initialized drift workspace!
    📁 Created render/ sandbox Git database.
    📁 Created install/ local state Git database.
    📝 Generated drift.toml template.
    📝 Generated config/envsubst.bash and config/mustache.envst.json.
    ```

### B. Package Creation: `drift new <package> [config_filename] [--force] [--target <dir>] [--method <stow|copy>]` (Implemented)  
#### **Common Usage**
Create a new package directory with the default `package.toml` configuration file:
```bash
$ drift new nvim --target ~/.config/nvim --method copy
✨ Package 'nvim' created successfully!
📝 Generated package.toml at src/nvim/package.toml.
```

#### **Details & Deep Probing Logic**
*   **Command Signature**: `drift new <package> [config_filename] [--force / -f] [--target / -t <target_directory>] [--method / -m <install_method>]`
*   **Optional Arguments & Flags**:
    - `<config_filename>`: Explicitly name the config file as `drift_package.toml` or `package.toml` (defaults to `package.toml`).
    - `--force / -f`: Forcefully overwrites any existing config file inside the package.
    - `--target / -t <target_directory>`: Explicitly configures the deployment target directory field inside the generated `package.toml`. Defaults to `default_target_directory` in `drift.toml`.
    - `--method / -m <install_method>`: Explicitly configures the deployment installation method (`stow` or `copy`) field inside the generated `package.toml`. Defaults to `default_install_method` in `drift.toml`.
*   **Probing Guard**:
    - The CLI first checks if *any* configuration file already exists inside the package directory `src/<package>/`.
    - Specifically, it probes following files if `<config_filename>` is not present:
      1. `drift_package.toml`
      2. `package.toml`
      3. Any engine-templated configuration such as `package.<engine>.toml` or `drift_package.<engine>.toml`.  

    - If any configuration file exists:  
      - If `--force` is **not** supplied: The command halts and prints an error, preventing you from accidentally losing an existing configuration.
      - If `--force` **is** supplied: Overwrites the configuration with the default template.
*   **Default Configuration Output**:
    ```toml
    # src/<package>/package.toml
    [package]
    name = "<package>"
    install_method = "stow"  # Options: "stow" (symlink) or "copy" (physical)
    target_directory = "~"   # Destination for this package

    # Lifecycle Hooks (Optional)
    # pre_install  = ""
    # post_install = ""
    # pre_update   = ""
    # post_update  = ""
    # post_render  = ""
    # hook_timeout = 120

    # Advanced Flags
    # sudo = false
    # fully_controlled_dirs = []  # Sync deletions inside these directories
    # enable_render = true
    # enable_install = true
    ```

---

### C. Resource Import: `drift add <package> <paths...> [--dry-run]` (Implemented)
#### **Common Usage**
Import physical active system configuration files or directories (like `~/.config/nvim/init.lua`) into your declarative source folder:
```bash
$ drift add nvim ~/.config/nvim/init.lua
🚀 Imported ~/.config/nvim/init.lua into nvim package!
📁 Copied contents to src/nvim/dot-config/nvim/init.lua (Translated dot-prefix).
```

#### **Details & Deep Logic**
*   **Command Signature**: `drift add <package> <paths...> [--dry-run]`
*   **Dry-Run Mode**: Passing `--dry-run` performs all symlink resolutions, prefix translations, and path resolutions, logging the previewed import operations without writing any files to disk.
*   **Link State Safeguard**:
    - For each input path, the command checks if it is a symlink pointing into the `install/` state database repository. If it is, the CLI aborts because the file is already under drift's active governance.
*   **Symlink Resolution Policy**:
    - If an input path is a symlink pointing *elsewhere* (not to the state repository), or if it's a directory containing symlink entries, drift **recursively resolves all symlinks and copies their actual physical contents** rather than copying the links. This ensures complete package self-containment, portability, and reproducibility across fresh machines.
*   **Path Resolution & prefix Translation**:
    - Computes `package_config.target_directory` (with a fallback to `workspace_config.default_target_directory`).
    - Resolves paths absolutely and computes the relative path: `rel_path = os.path.relpath(abs_path, abs_target_dir)`.
    - **Dot-Prefix Translation (Symmetric Symmetry)**:
      - Directories and files starting with standard dots `.` must be translated to a `dot-` prefix to maintain Git-friendly name handling in the repository.
      - *Example*: `.config/nvim/init.lua` is translated to `dot-config/nvim/init.lua`.
*   **Automatic Overwrites**:
    - Unlike adopt which has a cautious interactive prompt, `drift add` automatically overwrites the destination in `src/` to represent importing the live configuration copy as the new source of truth. Use `--dry-run` to audit this.

---  

### D. Status Inspection: `drift status [packages...]` (Implemented)
Analyzes and aggregates the current system alignment. It computes three independent vectors:
*   **Template Status (A)**: Did template files under `src/` evolve compared to `render/`?
*   **System Drift Status (B)**: Has the host system drifted from `install/` due to runtime changes?
*   **Pending Delta (C)**: Are there differences waiting to be deployed from `render/` to `install/`?

#### Terminal Visual Representation:
```bash
$ drift status
🔍 Auditing configuration status across active packages...

📦 nvim (stow-method)
   ├── 💻 Template Source   [CHANGED] ── 2 files modified in src/nvim/
   ├── 🖥️ System Drift       [CLEAN]   ── Active files match state database
   └── 🚀 Deployment Pending [PENDING] ── 2 files to render and link

📦 qbittorrent (copy-method)
   ├── 💻 Template Source   [CLEAN]   ── Up to date with sandboxed render
   ├── 🖥️ System Drift       [DRIFTED] ── dot-config/qBittorrent/qBittorrent.conf modified on host
   └── 🚀 Deployment Pending [BLOCK]   ── Blocked by system drift (Run 'drift diff -s')
```

### E. Change Visualization: `drift diff [packages...] [options]` (Implemented)
Provides deep comparisons between configuration layers, outputting side-by-side terminal color diffs.

*   **`drift diff [packages...]` (Default, Pending Delta / Diff C)**:
    Compares what is waiting in `render/` with what is deployed on the host system.
*   **`drift diff [packages...] --template` (or `-t`, Diff A)**:
    Shows how your source edits changed the compiled sandbox configurations.
*   **`drift diff [packages...] --system` (or `-s`, Diff B)**:
    Shows system drift—precisely what the host system or GUI apps have altered.
*   **`drift diff [packages...] --stat`**:
    Shows a concise summary of changes (diffstat style) representing file modification counts.
*   **`drift diff [packages...] --side-by-side` (or `-y`)**:
    Shows side-by-side vertical layout terminal diff comparison. (Note: Relies on system git diffing/terminal capabilities).

---

### F. Safe Deployment: `drift deploy [packages...] [--force]` (Implemented)
Deploys configurations using a robust, atomic two-stage deployment engine.

#### Stage 0: Pre-flight checks
1.  **Git Config Check**: Verifies that Git `user.name` and `user.email` are fully configured on both the `render/` sandbox and `install/` state repositories using `check_repo_can_commit()`, preventing midway commit aborts.
2.  **Discovered Packages**: Determines the targeted packages to deploy (or defaults to all enabled packages in `drift.toml`).

#### Stage 1: Safety Guard (Sentinel)
1.  Triggers a silent **Reverse Sync** (Function F1) pulling the current host configuration state into `install/` for target packages.
    *   *First-time packages*: If a package subdirectory does not yet exist inside `install/`, it skips reverse-syncing it since no baseline tracking is established yet.
2.  Inspects the `install/` Git tree for changes:
    *   *If changes exist (Drift is detected)*: **Aborts immediately**. It prints a warning showing the active drift (Diff B) and instructs the user:
        ```bash
        ❌ [DEPLOY ABORTED] System drift detected in package 'qbittorrent'!
        Host configurations have drifted from the state database.
        
        👉 Run 'drift diff -s qbittorrent' to view the active system modifications.
        👉 Run 'drift adopt qbittorrent' to incorporate these modifications into your template.
        ```

#### Stage 2: Sequential Compile & Apply
If no drift is detected (or `--force` is supplied):
1.  **Render**: Compiles `src/` templates into `render/` (`Function F2`).
2.  **Commit Render**: Automatically commits compiled sandbox history (`Function F3`).
3.  **Stage Render to Install**: Merges changes from `render/` to `install/`, isolating deleted, added, and modified files (`Function F4`).
4.  **Install Deployment**: Operates manual file-by-file copy/linking with collision checks and infinite stow loop prevention (`Function F5`).
5.  **Commit Install**: Scope commits the deployed configurations and `state.toml` inside the `install/` database (`Function F6`).

#### Stage 3: Post-deploy Garbage Collection
*   *Global Deploy*: If a global deployment was run (omitting package names), it automatically calls `drift gc` (`Function F9`) to clean up, purge, and commit any unreferenced/disabled package folders.

#### 🚨 Tailored Midway Fail-Fast Guard & Recovery Options:
If a stage fails, **drift halts execution instantly** and guides the user according to the severity of the failure:

*   **Step 1 (Rendering) or Step 2 (Commit Sandbox)**: It reports the failure and instructs the user to simply resolve the configuration issue and try `drift deploy` again.
*   **Step 3 (Staging) or Step 4 (Physical Deploy/Install)**: Since files may be in a midway inconsistent or half-written state, it displays a prominent emergency recovery card:
    ```bash
    💥 [CRITICAL FAILURE] deployment failed during Step 4 (Physical Deploy/Install)!
       PermissionError: [Errno 13] Permission denied: '/home/user/.config/nvim/init.lua'

    ================================================================================
                               EMERGENCY RECOVERY REQUIRED                          
    ================================================================================
    The deployment has failed midway, leaving your host system in an inconsistent 
    and half-written state.

    👉 Please fix the error above and run: 'drift rollback nvim'

    This command will restore the state database, delete any half-written files, 
    and execute a full deployment fallback to restore your system to the last
    successfully committed configurations.

    ⚠️  WARNING: Do not run rollback under normal circumstances. It bypasses
       system drift checking and will discard uncommitted local system adjustments.
    ================================================================================
    ```
*   **Step 5 (Commit Install)**: Since target host files are fully and successfully written, it instructs the user to manually run the commit operation:
    ```bash
    The deployment succeeded on your host, but committing to the state database failed.
    👉 Please resolve the Git state manually by running:
        drift install-commit -m "Deploy Install: Automatically commit deployed changes for nvim"
    ```

---

### G. Recovery: `drift rollback [packages...] [--force]` (Implemented)
*   **Mechanism (Primitive 8)**:
    1.  Resets the local state database `install/` for target packages to the last clean HEAD commit (removing uncommitted failed staging/deploy edits via `git checkout HEAD` and purging untracked files with `git clean -fd` inside those package subdirectories).
    2.  Resets the `install/state.toml` registry file back to `HEAD`.
    3.  Performs a high-level **Full Redeploy** using `run_primitive_5_install_deployment` with `force=True` to redeploy and overwrite physical target system files, bringing the active host system back into complete alignment with the reset state database.
    4.  Restores state registry entries for target packages back to `"installed"`.
*   **Operational Protection**: If run under healthy system conditions (no package is recorded in a conflict state like `staging` or `deploying` in `install/state.toml`), `drift rollback` will abort immediately with a `RuntimeError` to prevent accidentally overwriting system files. Pass the `--force` option to ignore this safeguard.

---

### H. Synchronization & Bidirectional Drift Adoption: `drift adopt [packages...] [--interactive]` (Implemented)
The `drift adopt` command provides an elegant, bidirectional synchronization workflow. When GUI preferences panels, desktop utilities, or manual edits modify active system configuration files, `drift adopt` allows the developer to cleanly incorporate (adopt) those system drifts back into their declarative templates under `src/`.

#### 1. Pre-adoption Git Safeguard Check
To prevent blending concurrent modifications or causing accidental data loss, `drift adopt` strictly enforces a scoped, pre-adoption cleanliness guard:
*   Before modifying any file inside the declarative repository, the command checks if the specific target package source folder (`src/<package>/`) is **Git clean** (contains no uncommitted, staged, or untracked changes).
*   **Scoped Usability**: Uncommitted modifications in *other* package folders do not block adoption. This allows developers to work on multiple packages concurrently and adopt system edits for one package in isolation.
*   If the target package source directory (`src/<package>/`) is dirty, the command aborts immediately:
    ```bash
    ❌ [ADOPT ABORTED] The source directory of package 'nvim' has uncommitted modifications!
    Adopting system configurations into a dirty package directory is unsafe.

    👉 Please commit or stash your changes in 'src/nvim/' before running 'drift adopt nvim'.
    ```

#### 2. Staging and Selective Unstaging Workflow
To accurately track rename events and ensure database/staging transactional integrity, `drift adopt` integrates natively with Git indexing:
1.  **Pre-Staging Scoped to Package**: At the very beginning of the package adoption, the engine runs `git -C install add --all <pkg>` to stage all uncommitted local modifications, deletions, additions, and renames under the package. This ensures that Git's native rename detection (`R ` status) triggers perfectly.
2.  **Selective Unstaging via `git restore --staged`**: For any file that the user chooses to "Skip" or where programmatic patch application fails, the engine runs `git restore --staged` for its relative path, reverting it to unstaged/uncommitted local drift (representing unresolved issues). All successfully resolved (adopted or discarded) changes are kept staged in the Git index, ready to be committed at the end.

#### 3. Non-Interactive Mode (Targeted File Adoptions)
*   **Syntax**: `drift adopt <package> [file_paths...]`
*   **Description**: Symmetrically adopts specific configuration files directly into the package's declarative source folder (`src/<package>/`).
    *   **File Additions**: Symmetrically copied into the package source folder, automatically converting target-system dotfile prefixes (such as `.bashrc` or `.config/`) back to repository-safe prefixes (`dot-bashrc` or `dot-config/`). If an addition conflicts with an existing file under `src/`, non-interactive mode skips it with an error.
    *   **File Deletions**: Symmetrically deleted from the package source folder. If the file is already missing in `src/`, it skips cleanly and keeps the deletion staged.
    *   **File Modifications**:
        *   *Static Source File / Symmetrically Handled Additions*: If the modified file does not exist in `src/`, it is symmetrically processed as an addition (delegating to the addition loop handler) so the user has full choice in interactive mode. Otherwise, if static, it directly overwrites the source file in `src/` with the reverse-synced file.
        *   *Templated Source File*: Bypasses blind overwriting to protect placeholders, and attempts a **Symmetric Patch Application** (detailed below). If the patch application fails, the command skips the file (unstaging it) and logs an error instructing the user to run interactive mode to resolve the conflict.

#### 4. Interactive Command Mode (`drift adopt <package> --interactive` or `-i`)
Walks the developer through every single modified, added, or deleted system file detected in active system drift (Diff B), prompting them with clear reconciliation choices.

##### A. Reconciling File Additions & Deletions
File additions (which only originate inside Fully-Controlled Directories or from type promotions) and file deletions are distinct events and are handled via dedicated prompts:

###### Scenario 1: Interactive File Additions (FCD)
When a brand-new file is created on the host inside an FCD, `reverse-sync` pulls it into the `install/` base. During `drift adopt -i`, the developer is prompted:
```bash
Found untracked file addition inside Fully-Controlled Directory: ~/.config/qBittorrent/themes/dark.qbtheme
------------------------------------------------------------
New physical file created on host system.
------------------------------------------------------------

Reconciliation options:
[1] Adopt and copy into source package (Creates src/qbittorrent/dot-config/qBittorrent/themes/dark.qbtheme)
[2] Ignore file (Unlinks from install/, appends 'dark.qbtheme' to .drift_ignore to prevent future reverse-sync)
[3] Discard file (Stages file to install/ database so it is deleted from host on next deployment)
[4] Skip file

Select option [1-4]: 
```

###### Scenario 2: Interactive File Deletions
When a tracked file is deleted on the host system, `reverse-sync` records its deletion in `install/` as an uncommitted change. During `drift adopt -i`, the developer is prompted:
```bash
Found host file deletion: ~/.config/nvim/init.lua
------------------------------------------------------------
Tracked file deleted on host system.
------------------------------------------------------------

Reconciliation options:
[1] Adopt deletion (Deletes src/nvim/dot-config/nvim/init.lua from source package)
[2] Discard deletion / Restore (Stages deletion to install/ database, the file will be re-rendered & redeployed in next deploy)
[3] Skip file

Select option [1-3]: 
```

##### Under-the-Hood FCD Ignore & Discard Git Mechanics
When a developer makes choices on FCD untracked files, the underlying database states and ignore engines coordinate as follows to achieve mathematical alignment:
*   **Adopting (Option [1])**: Drift copies the file from `install/<package>/` to `src/<package>/` (translating dot-prefixes) and commits the change, aligning the source with the active host.
*   **Ignoring (Option [2])**:
    1.  *State Unlink*: Drift removes the file from `install/<package>/` (restoring database cleanliness).
    2.  *Pattern Append*: Drift appends the relative file path pattern to the package's `.drift_ignore` file.
    3.  *The Result*: In all future `reverse-sync` runs, the ignore engine sees that the host file matches `.drift_ignore` and skips reverse-syncing it. Since the file is deleted from `install/` and ignored in `.drift_ignore`, it won't be part of any next render/deploy comparisons, safely leaving the untracked file alone on the active system.
*   **Discarding (Option [3])**: Keep the staged changes staged. On the subsequent `drift deploy` (Stage 2) run, because the file is now actively tracked in `install/` but is absent from the newly compiled `render/` sandbox output, the compiler's delta-staging pipeline (`drift stage`) automatically classifies it as an orphaned deletion and generates a delete instruction for it. During `drift apply`, it is safely deleted from the host system, perfectly restoring baseline system alignment!

##### B. Reconciling File Modifications: Resolving the Template Override Challenge
*   **The Difficulty**: The reverse-synced file in `install/` is static (has variables expanded), but the source file in `src/` is a template (has placeholders like `${ENV_VAR}` or `%i`). Directly overwriting the template with the static file would destroy the templating logic.
*   **The Drift Solution**:
    1.  **Symmetric Patch Application**: Drift extracts the active system modifications as a unified patch from the `install/` state repository (`git diff install/<pkg>/file`). It then attempts to programmatically apply this patch onto the template file inside `src/` (re-aligning paths). If the user edits do not overlap with placeholder lines, the patch completes successfully!
        *   **Missing Old Source Template Fallback**: If the original source template does not exist (the template was deleted/missing from `src/`), the engine warns, treats it as a new file addition, touches a new empty file at the destination target path under `src/`, and applies a patch containing the full content of the file.
    2.  **Conflict Fallback Pipeline**: If the patch application fails or rejects (due to overlaps with placeholder fields), the interactive terminal displays a patch conflict card:
        ```bash
        ⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto template file 'init.envst.lua'!
           The system changes overlap with existing template placeholders (${ENV_VAR}).

        Choose a fallback resolution strategy:
        [1] Over-render & Freeze (Overwrites template with static content, saving original template to init.envst.lua.bak)
        [2] Open Merge Conflict Editor (Opens $EDITOR with conflict markers <<<<<< SYSTEM CHANGE ======= >>>>>>)
        [3] Open Side-by-Side Reference (Opens $EDITOR with template, while printing host diff to terminal side-by-side)
        [4] Discard modifications / Restore
        [5] Skip file
        
        Select option [1-5]: 
        ```

##### Fallback Options Explained:
*   **Option [1] Over-render & Freeze**: Converts the template into a static file. It overwrites `src/<package>/file.envst.lua` with the new static file, while saving a backup of the original template as `file.envst.lua.bak` and logging a warning so the developer can manually restore placeholders later if they wish.
*   **Option [2] Open Merge Conflict Editor**: Generates a temporary file containing standard three-way git conflict markers (`<<<<<<< SYSTEM CHANGE`, `=======`, `>>>>>>> TEMPLATE PLACEHOLDERS`). It launches the developer's default editor (configured via `$EDITOR`, falling back to `vim` or `nano`). Once edited and saved, the resolved file is validated and committed back to `src/`.
*   **Option [3] Open Side-by-Side Reference**: Launches the template file inside the developer's editor, while printing the active system diff on the side-by-side terminal screen as reference, letting the developer manually apply settings directly inside their editor without destroying adjacent templates.
*   **Option [4] Discard modifications / Restore**: Keep changes staged to force-restore original settings on host next deployment.
*   **Option [5] Skip file**: Skip modifying templates, unstage files in `install/` base.

---

### I. Uninstallation & Detachment: `drift uninstall [packages...] [--force] [--detach] [--dry-run]` (Implemented)
The uninstall command supports two distinct operational modes:

#### Dry-Run Mode (`--dry-run`)
*   Passing `--dry-run` performs standard path calculations and target mappings, reporting the files that would be removed, copies that would be detached, or backups that would be restored without writing or deleting any files on disk.

#### 1. Standard Uninstall Mode (Default)
*   **Mechanism (Primitive 7)**:
    1.  Identifies files belonging to the package.
    2.  Removes active symlinks (stow method) or deletes target files (copy method).
    3.  **Collision Guard Rollback**: Restores original physical host files that were backed up inside `backup/<package>/overwritten/` to their original locations.
    4.  Updates the `install/state.toml` database registry.
    5.  Performs a Git stage, directory removal, and auto-commit inside the `install/` Git repository:
        ```bash
        git -C install add state.toml
        git -C install rm -r --ignore-unmatch <package>/
        git -C install commit -m "Uninstall: Removed package <package>"
        ```

#### 2. Detach/Eject Mode (`--detach`)
*   **Mechanism (Primitive 7 with `--detach`)**:
    1.  **Keep Configuration**: Keeps the current configuration files active on your host system, fully decoupling them from Drift's future management.
    2.  **Symlink to Copy Conversion**: If the package was stowed via symlinks, recursively iterates through the deployed files. For every active symlink on the system, the engine unlinks it and copies the physical file counterpart from `install/<package>/` to the active host target path.
    3.  **Backups Kept Intact**: Leaves historical original backups inside `backup/<package>/overwritten/` completely untouched (does not restore them).
    4.  **Database Decouple**: Safely deletes the local `install/<package>/` directory and unregisters the package from `install/state.toml`, committing the decoupling to the local database with a `Detach:` prefix:
        ```bash
        git -C install add state.toml
        git -C install rm -r --ignore-unmatch <package>/
        git -C install commit -m "Detach: Removed package <package>"
        ```

*   **Safeguard**: Aborts with an error if the package is still declared as active/enabled in `drift.toml`, preventing accidental uninstalls/detachments of packages scheduled to run in bulk deploys, unless `--force` is supplied.

---  

### I_h. Mini User Manual: `drift help [topic]` (Implemented)
Provides a rich, interactive, built-in mini user manual for Drift using pager-fallback support.
*   **Syntax**: `drift help [topic]`
*   **Available Topics**:
    - *(no topic)*: Shows the overall loop-oriented architecture, data-flow diagram, and high-level vs low-level command registries.
    - `package`: Explains modular packages, folder layouts, and deployment configurations.
    - `src`: Explains the declarative source directory (`src/`), custom scaffolding, and `dot-` prefix translation rules.
    - `render`: Explains the sandbox render directory compilation, build isolation, and DAG dependency mapping.
    - `install`: Explains the state database (`install/`), differential delta calculations, and proactive collision safeguards.
    - `fcd`: Explains Fully-Controlled Directories (FCDs), dynamic file creation, and interactive tracking mechanics.
    - `ignore`: Explains the Syntax of `.drift_ignore` PCRE patterns, install-ignore skipping logic, and interactive Fully-Controlled Directory (FCD) auto-ignoring mechanics.
    - `package.toml` (or `drift_package.toml`): A complete, fully documented template with package option explanations.
    - `drift.toml`: A complete, fully documented template for global workspace settings.
*   **Pager Mechanics**: If run inside an interactive terminal (TTY), help text is dynamically piped to the system `PAGER` utility (like `less` or `more`) via Python's built-in `pydoc.pager` library. If output is piped or redirected, it falls back cleanly to a raw print to `stdout`.

---

### J. Low-Level Control Commands
These commands are intended for advanced troubleshooting, continuous integration, or automation scripts.

#### **1. Low-Level Render: `drift render [packages...]`**
##### **Common Usage**
```bash
$ drift render
🚀 Rendering all active package templates to sandbox render/
```
##### **Details**
*   If `packages` are provided: Recursively compiles templates and copies files for *only* those packages.
*   If `packages` are omitted: Recursively processes *all* enabled packages.

#### **2. Low-Level Commit: `drift render-commit [packages...] -m "message"`**
##### **Common Usage**
```bash
$ drift render-commit -m "Render: Update Neovim templates"
✨ Committed render sandbox changes.
```
##### **Details**
*   Stages compiled/copied configurations of selected packages or all packages inside the sandbox using `git add` and commits them under the `render/` repository with the specified message. Returns gracefully if the repository is already clean.  

#### **3. Low-Level Reverse Sync: `drift reverse-sync [packages...]`**
##### **Common Usage**
```bash
$ drift reverse-sync nvim
🔍 Pulling live configuration overrides from host system for 'nvim' into install state...
```
##### **Details**
*   **Mechanism (Primitive 1)**: Traverses the live target directories on the system. It checks for files deleted by the user/system, files with modifications (pulling their overrides), and scans Fully-Controlled Directories (FCD) for untracked/new files, copy-syncing them back to `install/`.
*   If `packages` are provided: Scopes the reverse synchronization strictly to those packages.
*   If `packages` are omitted: Bulk reverse-syncs all enabled packages.

#### **4. Low-Level Sandbox Staging: `drift stage [packages...] [--force]`**
##### **Common Usage**
```bash
$ drift stage nvim
🚀 Staging compiled sandbox templates from render/ to install/ state database...
```
##### **Details**
*   **Mechanism (Primitive 4)**: Reconciles the sandbox `render/` folder into the `install/` state database folder. It computes added, modified, or deleted files between the directories and moves deprecated configs to `backup/`.
*   **Guard**: If the `install/` repository has uncommitted local modifications, this command will abort **unless** the `--force / -f` flag is supplied, in which case it overwrites them.

#### **5. Low-Level State Application: `drift apply [packages...] [--force]`**
##### **Common Usage**
```bash
$ drift apply nvim
🚀 Applying 'nvim' configurations from state database to active host system...
```
##### **Details**
*   **Mechanism (Primitive 5)**: Applies files inside the `install/` state database to the live target paths on the host system.
*   **Collision Guard**: Backs up colliding physical files to `backup/<package>/overwritten/` if they are not already managed by drift.
*   **Force**: Bypasses certain safety checks (e.g. `enable_install` flag).

#### **6. Low-Level State Commit: `drift install-commit [packages...] -m "message"`**
##### **Common Usage**
```bash
$ drift install-commit -m "Deploy: Manual update of Neovim configs"
✨ Committed state database changes.
```
##### **Details**
*   **Mechanism (Primitive 6)**: Stages and locks the deployed configurations inside the `install/` local state tracking repository with an automated commit.
*   **Parameters**:
    - `-m "message"` (Required): Specifying the commit message.
    - If specific `packages` are provided, it scopes the `git add` actions strictly to those package folders (e.g. `install/<package>/`) and `state.toml`. Otherwise, it commits the entire repository.

## 5. Global Configuration: `drift.toml`

A centralized configuration file located at the repository root controls global environments and registers active packages.

```toml
# =====================================================================
# drift.toml Configuration
# =====================================================================

[workspace]
# Source directory for packages, default value is "src"
source_directory = "src"

# Sandbox rendering output path
render_directory = "render"

# Deployment database tracking folder
install_directory = "install"

# Backup archive folder for collisions & deletions
backup_directory = "backup"

# Global default target directory
# Supports home expansion (~ at the beginning).
default_target_directory = "~"

[render.envsubst]
# Shell script providing env variables for envsubst
# If it's a relative path, it's always relative to the 'config' folder under of working directory.
# The file is located at "config/envsubst.bash" .
input_file = "envsubst.bash"

# Files with name "file.envst.suffix" or "file.envst" will be rendered using envsubst.
suffix = "envst"

# The output of render_command will be written as render result.
# %i means engine input, %s means source template.
render_command = "bash -c 'source %i && envsubst < %s'"

[render.mustache]
# Json file as the input to mustache template render engine.
# This filename ends with "envst.json", so it need to be rendered with envsubst first to get the actual json file.
input_file = "mustache.envst.json"

# Files with name "file.mustache.suffix" or "file.mustache" will be rendered using mustache.
suffix = "mustache"
render_command = "mustache %i %s"

# ---------------------------------------------------------------------
# Enabled Packages Registry
# ---------------------------------------------------------------------
# Key: package folder name under src/
# Value: True/False to enable or disable the package globally
# Entry "DEFAULT = true | false" will set the default value for unlisted packages.
# "DEFAULT = false" is the default setting.
[packages.enable]
shell = true
nvim = true
qbittorrent = true
proxychains = false
```

---

## 6. Blueprint for Python Implementation

### A. Dependency Stack
1.  **`typer[all]`**: High-performance, declarative Python CLI framework. Uses `click` under the hood.
2.  **`rich`**: Beautiful terminal formatting, syntax highlighting, markdown rendering, and interactive confirmation menus.
3.  **`GitPython`**: Robust Python API wrapping the Git binary. Handles indexing, diffing, resetting, staging, and committing.
4.  **`jinja2` / `chevron`**: Fast, flexible template rendering engines to support advanced `.mustache` or `.jinja` template structures natively.

### B. High-Signal Console Aesthetics (Rich styling)
To maintain the polished feeling of a modern tool, console feedback utilizes strict semantic colors:
*   `✨` **Gold/Yellow**: Primary action success / Initiation.
*   `🔍` **Cyan**: Analysis, search, and status checks.
*   `🚀` **Green**: Deployments, additions, and successful updates.
*   `❌` **Bold Red**: Sentinel-blocked operations, aborts, and configuration errors.
*   `💥` **Inverted Bold Red**: Critical execution midway crashes.
*   `⚠️` **Orange/Yellow**: Collision guard warnings, safety backup prompts.
