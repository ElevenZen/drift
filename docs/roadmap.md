# 🗺️ Drift Project Review & Roadmap

This document provides a critical architectural review of Drift's current state (Part I) alongside its prioritized forward roadmap (Parts II & III).

For the complete history of completed engineering milestones and feature implementations, see **[`docs/changes.md`](changes.md)**.

* **Engineering Changelog & Completed Milestones**: [`docs/changes.md`](changes.md)
* **Architecture & Subsystem Index**: [`docs/architecture.md`](architecture.md)
* **User Feature Guide**: [`docs/features.md`](features.md)

---

## Part I: Project Review (Current Codebase State)

### Strengths

**1. The 4-tier transactional model (`src/ → render/ → install/ → host`) solves real state-management problems.**
Most dotfile managers are either "symlink farms" (GNU Stow) or single-stage template appliers (`chezmoi`). By maintaining independent Git repositories at both `render/` (compiled template output) and `install/` (deployed host baseline), Drift establishes a true 3-way merge anchor (`render/` vs. `install/` vs. live host). Rendering before reverse-sync allows Drift to automatically resolve **convergent drift** (when host edits and template updates arrive at identical content) while halting on **divergent drift** so GUI or manual host edits are never silently clobbered. Combined with pre-stage install conflict/permission auditing, append-only Write-Ahead Logging (`backup/<pkg>/wal/actions.json` + `wal/stash/`), and the 3-tier `RollbackPolicy` ladder (`CLI --rollback` $\rightarrow$ `[settings] rollback` $\rightarrow$ `[package] auto_rollback`), failed deployments or fatal installation hooks can be rolled back atomically across an entire multi-package batch.

**2. True 3-way template de-rendering (`T <-> R <-> H`) and pure-Python two-step patching.**
Unlike one-way template managers that overwrite `${VAR}` or `{{ expr }}` slots with literal host strings when syncing back, [`src/drift/adopt/`](../src/drift/adopt/) combines Neovim-style 3-class lossless tokenization, coarse-to-fine 2D dynamic-programming hunk fission, an 18-case orthogonal $(T \to R) \times (R \to H)$ intersection matrix, and single-line closed-interval token calculus (`try_derender_single_line`) to backport live host edits into source templates while preserving untouched template expressions. Clean edits apply in Step 1 (`CleanPatchHunk`), while genuine variable-slot or loop-interior collisions are anchored via prefix-sum coordinate shifting (`itertools.accumulate`) into 3-way `diff3` conflict markers (`<<<<<<<` / `|||||||` / `=======` / `>>>>>>>`) in Step 2—implemented in pure Python with zero external `patch` or `git merge-file` subprocess dependencies.

**3. Build-system-grade incremental rendering via a 3-Phase Merkle DAG.**
Instead of wiping and re-rendering package directories on every run, [`src/drift/render/`](../src/drift/render/) models compilation as a 3-phase Merkle DAG (`Phase 1: PackageConfigNode` $\rightarrow$ `Phase 2: PackageHooksNode` $\rightarrow$ `Phase 3: PackagePayloadNode`), backed by a 3-bucket `render_lock.json` and a nanosecond stat-fingerprinted in-memory [`RenderCache`](../src/drift/render/render_cache.py). Unchanged templates and static files incur zero disk writes, preserving file `mtime`s and keeping `render/.git` history clean.

**4. Strict `prepare_*` (read-only planning) vs. `execute_*` (mutation) separation across all 17 primitives.**
State-transition primitives (`reverse-sync`, `stage`, `apply`, `uninstall`, `add`, `adopt`, `enable`/`disable`, and full-cycle `plan`) decouple pure inspection and `FileAction` compilation (`StagePlan`, `InstallPlan`, `UninstallPlan`, `AdoptPlan`) from physical execution (`execute_delivery_actions`). This makes [`drift plan`](internals/plan_engine.md) (`drift deploy --dry-run`) a genuine, zero-side-effect simulation of the entire pipeline and guarantees that pre-flight assertion guards (missing hooks, target permission errors, cross-package path collisions, cyclic dependencies) abort before mutating host files or `state.toml`.

**5. Expressive 6-tier configuration, in-memory `[secrets]` decryption, and zero-dependency core.**
Drift runs on pure Python 3.9+ stdlib in core mode (including a built-in `internal` `envsubst` engine, comment-preserving `[packages.enable]` editor for `drift enable`/`disable`, stdlib fallback TOML parser, zipapp bundler, dual Typer/Argparse CLI backends, 4-shell tab completion for `bash`/`zsh`/`fish`/`nu`, and 15 built-in `drift help` manuals) while providing a 6-tier `Package > Workspace` environment hierarchy (`override`, `facts`, `ambient`, `secrets`, `default`, `fallback`), zero-disk-leakage `[secrets]` decryption providers (`age`, `gpg`, `sops`, `op`, `bw`, `pass`), `.envst.toml` and `.local.toml` config layering, Python preprocessor hooks (`drift_workspace.py` / `drift_package.py`), declarative host `[requirements]`, and topological inter-package `dependencies`.

**6. Disciplined functional architecture, static type safety, and 3-layer documentation.**
Strict bottom-up package layering (`utils` $\rightarrow$ `models`/`core` $\rightarrow$ `config`/`hooks`/`render`/`adopt` $\rightarrow$ `primitives` $\rightarrow$ `cli`), strongly-typed result dataclasses, functional pipeline helpers (`choose`, `branch`, `when_not_none`, `when_none`, `partition`, `star_partition`, `star_filter`, `SpliceSpan`, `splice_spans`), 3-state cross-platform path validation (`Strictly Relative` / `Partially Anchored` / `Strictly Absolute`), explicit `.as_posix()` path conversion discipline, and the 3-layer documentation hierarchy ([`docs/architecture.md`](architecture.md) $\rightarrow$ [`docs/internals/*.md`](internals/) $\rightarrow$ `src/drift/*/AGENT_MODULE.md`) make the codebase unusually verifiable and easy to reason about.

---

### Weaknesses & Current Gaps

**1. Cross-tier schema evolution & read-before-write coupling (`src/` vs. `render/` vs. `install/`).**
Because `render/<pkg>/.drift/drift_package.toml` and `install/<pkg>/.drift/drift_package.toml` persist rendered snapshots of package configs in their own Git repositories, evolving Drift's configuration schema (e.g., renaming or removing a key enforced by `validate_known_keys` in `PackageConfig.from_dict`) can create version skew between `src/` and older committed snapshots in `install/`. While lazy FIFO dependency expansion in [`resolve_target_package_order()`](../src/drift/primitives/package_assertions.py) prevents unrelated installed packages from blocking targeted `stage`/`apply` runs, operations that inspect already-installed packages (`drift status`, `drift gc`, `drift uninstall`, `drift health`, or dependent packages) still parse `install/<pkg>/.drift/drift_package.toml` under the current schema and require re-deploying the affected package to refresh its staged config.

**2. Missing ready-to-run `samples/` workspaces & unified `drift doctor` diagnostics.**
While [`README.md`](../README.md) (Five-Minute Tour & Pi Agent guide), [`docs/features.md`](features.md), and the 15 built-in `drift help` manuals (`docs/manual/`) now provide clear user-facing documentation separated from [`docs/internals/`](internals/), new users still lack a curated `samples/` directory of copy-pasteable real-world packages (shell setups, multi-machine networking like `frp`, package manager configs like `mise`/`nix`). In addition, workspace diagnostics remain split across `drift status`, `drift health`, `drift repair`, and `assert_workspace_healthy()` rather than a single unified **`drift doctor`** command.

**3. Subprocess coupling to the `git` CLI (`render/.git` and `install/.git`).**
Although patch parsing and 2-step `diff3` application in `adopt_patch.py` have been migrated to pure Python, Drift's state repositories still shell out directly to `git` (`status --porcelain`, `diff`, `add`, `commit`, `checkout`, `clean`) via scattered `subprocess.run` calls. Centralizing these into a **`GitRepo` abstraction class** will reduce subprocess overhead, standardize Git error translation, and simplify unit-test mocking.

**4. Hook ergonomics & repetitive per-package triggers.**
Currently, `[hooks]` entries that pass arguments or run one-liner commands require either authoring a wrapper script in `drift_hooks/` or invoking `sh -c '...'`, and shared post-deployment actions (such as `fc-cache -fv` or `systemctl --user daemon-reload`) must be duplicated across individual packages. Supporting **`shlex`-tokenized Command Hooks with arguments** and **Pacman-style Passive File Triggers** will eliminate this boilerplate.

**5. Lifecycle timing asymmetry between `symlink` and `copy` delivery modes.**
In `symlink` mode, live host paths point directly into `install/<pkg>/`. As a result, `drift stage` (`render/ → install/`) immediately updates the live file contents seen by the host *before* `drift apply` runs `pre_update` hooks, and in-place host edits immediately dirty the `install/` Git working tree without `reverse-sync`. In `copy` mode, host files only change during `drift apply` (between `pre_update` and `post_update`) and require `reverse-sync` to capture host drift. Hook authors must be aware of this semantic difference when writing `pre_update` scripts.

**6. Scanner-based fallback TOML parser on Python 3.9–3.10.**
Supporting Python 3.9 and 3.10 without external `tomli` requires maintaining a custom scanner-based TOML parser in [`toml_utils.py`](../src/drift/utils/toml_utils.py). Although it supports multiline strings, inline tables, and arrays of tables, complex edge cases in nested inline structures or escaping rules remain a maintenance surface compared to Python 3.11+'s standard library `tomllib` (motivating a token/lexer-based fallback parser).

---

## Part II: Strategic Tier Ranking

The roadmap is prioritized into four execution tiers based on **architectural ROI**, **system safety guarantees**, and **adoption impact** (completed items are detailed in [`docs/changes.md`](changes.md)):

### 🌟 Tier S: Game Changers & Core Value Proposition
* **[`drift plan`](internals/plan_engine.md)** [Completed]: Full dry-run visualization of render, stage, and install actions before modifying host files.
* **[3-Way Template De-Rendering (`drift adopt`)](internals/template_adopt_derendering.md)** [Completed]: Word-level token calculus, 2D dynamic-programming hunk fission, and two-step `diff3` patching to backport live host edits into source templates without clobbering `${VAR}` / `{{ expr }}` slots.
* **Asymmetric In-Repo Secret Encryption (Age / SSH-key)** [Completed]: Zero-disk-leakage in-memory secret decryption into the compilation sandbox via `[secrets]` (`decrypt_command`, `secrets_file`, `key_file`).
* **Smarter Rollback with WAL (Write-Ahead Log)** [Completed]: Guarantees 100% reversible rollbacks for physical host filesystem side effects and backup stores via `backup/<pkg>/wal/`.
* **`drift doctor`**: Single diagnostic command validating workspace structure, Git repositories, hook permissions, target writability, and dependencies.
* **Unified Layered Import System (`[[imports]]`)**: Layered overlay mounting for package inheritance, file remounting, and external assets.

### 🚀 Tier A: High Value & Ergonomic Wins
* **Package Dependency Declaration & Topological Install Ordering** [Completed]: Explicit `dependencies` in `drift_package.toml` with required/optional semantics, topological install ordering, and pre-flight validation.
* **Audit `check=False` & Error Stacking** [Completed]: Eliminates error masking and redundant multi-line error boxes.
* **`drift enable` & `drift disable` CLI Commands** [Completed]: Format-preserving `[packages.enable]` toggling (`--local`, `--dry-run`, `--all`) paired with `drift deploy --gc` and `--adhoc`.
* **Command Hooks & Arguments (`shlex`)**: Inline commands and arguments in `[hooks]` without creating wrapper scripts.
* **Sample Workspace Repository (`samples/`)**: Real-world reference packages (shell, multi-machine networking, `mise`/`nix` configs).
* **Native Package Distributions (PyPI, AUR, Homebrew, Nixpkgs)**: Standard package manager availability for release v1.0.
* **Globbing in `packages.enable`**: Wildcard pattern matching (`desktop_* = false`, `server_* = true`) across machine classes.
* **Passive File Triggers (Pacman-style Hooks)**: Directory-watching triggers executed once in a consolidated batch after deployment.
* **`drift migrate`**: Seamless one-command migration from GNU Stow / chezmoi / yadm.

### ⚖️ Tier B: Solid Improvements & Scale Enhancements
* **Dynamic Password Manager CLI Provider** [Completed]: Declarative `[secrets]` integration with 1Password (`op`), Bitwarden (`bw`), `pass`, and external decryption tools.
* **Incremental / Selective Rendering** [Completed]: 3-Phase Merkle DAG with `render_lock.json` and nanosecond stat-fingerprinted `RenderCache`.
* **Workspace-Wide Snippets & Partials (`config/partials/`)**: Boilerplate deduplication across Mustache, Jinja2, and shell templates.
* **Fleet & Server Orchestration Integration**: Non-interactive batch flags, JSON status objects, and Ansible roles for fleet automation.
* **Git Abstraction Layer (`GitRepo` class)**: Centralizes Git subprocess calls for clean mocking and unified error translation.
* **Rendered Output Format Validation**: Verifies syntax of rendered JSON/YAML/TOML before staging.
* **Progress Meters & Batch Worklist Summaries**: Step-by-step progress counters (`[3/10]`) and upfront target discovery logs across CLI subcommands.

### 🔧 Tier C: Advanced & Specialized Patterns
* **Auto-Rollback for Critical Services** [Completed]: Automatic revert on deployment failure across `deploy` and `apply` with 3-state `RollbackPolicy` (`auto`, `always`, `never`).
* **Reorder Render Before Reverse-Sync** [Completed]: Resolves converged template updates directly in git history and pre-plans installation before staging.
* **`drift revert`**: Fast undo of last deployment commit in the install repository.
* **Binary Deployment Strategy (drift + mise)**: Alternative binary management scheme with mise / pkgit / nix home-manager.
* **Compilation Package Pattern**: Source subfolder builds via `pre_source` hooks.
* **Structured Hook Protocol**: Rich JSON output protocol over fd/file beyond integer exit codes.
* **Windows File-Lock Probing**: File accessibility probing for Windows service updates.

---

## Part III: Open Task Breakdown by Category

*(Note: All completed `[x]` tasks have been archived in [`docs/changes.md`](changes.md).)*

### Category 1: Reliability & Parser Hardening

- [ ] **Enhance TOML fallback parser.** Use a lexer to parse tokens from TOML strings in [`toml_utils.py`](../src/drift/utils/toml_utils.py) instead of line scanning and splitting.
- [ ] **Windows file-lock probing.** On Windows, probe file accessibility (can append/open) before installation because of file locks. Needs careful placement relative to `pre_update` hook — if a user writes stop-service commands there, probing must happen after the hook. Design the failure semantics: does a failed probe count as deployment failure, and how does the user restore service state?

### Category 2: User Experience & Onboarding

- [ ] **`drift doctor` — unified diagnostic command.** Currently `drift status`, `drift health`, `drift repair`, and `assert_workspace_healthy()` each probe different aspects. A unified `drift doctor` that runs everything — workspace structure, Git repo integrity, config validation, hook script permissions, target directory accessibility, and package health probes — in a single pass with a structured report. Think `brew doctor` or `rustup check`.
- [ ] **Sample workspace repository (`samples/`).** Add a `samples/` directory with real-world examples: `frp` config (networks among machines), package manager configs (`mise` / `nix home-manager` / `linuxbrew`), and common shell/editor setups.
- [ ] **Granular progress tracking & step meters across CLI commands.** Add uniform step counters (e.g., `[3/10] Rendering package 'zsh'...`) and structured progress indicators to logging and CLI outputs across all multi-package subcommands (`render`, `stage`, `apply`, `deploy`, `status`, `gc`):
  - Print the resolved target package worklist upfront before executing operations so users have immediate visibility into batch scope.
  - Prepend step indexes `[i/N]` to starting and completion log messages for each package.
  - In Rich console mode, provide clean animated progress meters while preserving full detailed logs in `--debug` and non-interactive pipelines.
- [ ] **`drift env <package>` command.** Inspect or export resolved 6-tier package environment variables (similar to `dotenv` / `direnv` utilities).
- [ ] **Opt-in post-deploy health checks (`drift deploy --health`).** Allow `drift deploy` to automatically invoke `drift health` at the end of deployment as an opt-in verification step.
- [ ] **Backup files listing.** Print the paths written to `backup/<pkg>/overwritten/` and `backup/<pkg>/deleted/` after executing a `FileAction` list.
- [ ] **`drift revert` command.** Quickly revert an installed package to a previous Git commit in the `install/` repository without full midway WAL rollback semantics—a lightweight "undo last deploy."

### Category 3: Config & Rendering Enhancements

- [ ] **Command hooks and hooks with arguments (`shlex`).** Use `shlex` to parse hook values in `[hooks]`. When the first token contains `/` (or resolves to a package hook script), treat it as a file path (with arguments); otherwise treat it as an executable command in `$PATH` with argument passing.
- [ ] **Globbing in `packages.enable` match.** Support glob patterns in workspace config package enablement (e.g., `desktop_* = false`, `server_* = true`).
- [ ] **Passive File Triggers & Post-Deployment Hook Aggregation (Pacman-style Triggers).** Allow packages to declare passive triggers that observe target directory paths rather than requiring explicit per-package invocation. When any deployed package writes, modifies, or deletes files within a watched directory (e.g. `~/.local/share/fonts/`, `~/.config/fish/completions/`, or a plugin directory):
  - The trigger is automatically queued and activated for that deployment transaction.
  - All activated passive triggers execute once in a consolidated batch at the end of the deploy pipeline (after all active package hooks have completed).
  - Eliminates redundant, repetitive reload scripts across individual plugin packages (e.g., calling `fc-cache` once instead of 15 times when installing multiple font packages).
- [ ] **Rendered output validation.** Render engines can optionally call external validators on rendered output to verify format correctness (`json`, `yaml`, `toml`, etc.) before staging.
- [ ] **Unified Layered Import System (`[[imports]]`).** Replace ad-hoc cross-package imports and external asset fetching with a unified layered overlay file system. A package declares an ordered list of `[[imports]]` from internal packages or external sources. The effective package source is stacked in declaration order with the package's local `src/<pkg>/` directory acting as the top-most override layer:
  ```toml
  [package]
  name = "web_service_prod"

  # 1. Base package inheritance (Import All "*")
  [[imports]]
  source = "package:web_service_base"

  # 2. Selective file remount from another internal package
  [[imports]]
  source = "package:common_configs"
  from = "dot-config/shared/logging.conf"
  to = "dot-config/service/logging.conf"

  # 3. External Git repository (cached in .drift/cache/external/)
  [[imports]]
  source = "git:https://github.com/ohmyzsh/ohmyzsh.git"
  to = "dot-oh-my-zsh"
  ref = "v1.2.0"
  refresh = "weekly"

  # 4. External archive / release binary
  [[imports]]
  source = "archive:https://github.com/junegunn/fzf/releases/download/v0.50.0/fzf-0.50.0-linux_amd64.tar.gz"
  from = "fzf"
  to = "dot-local/bin/fzf"
  executable = true

  # Local environment variables override base package variables
  [env.override]
  service_port = "8080"
  ```
  *Key Properties*:
  - **1:1 Pipeline Fidelity**: Layered resolution happens purely during render input preparation; `render/`, `install/`, and host symlinks remain 100% standard 1:1 packages.
  - **Native Drift Adoption**: When host files drift, `drift adopt` writes patches directly into the top layer (`src/<pkg>/`), leaving base packages untouched.
  - **DAG & Cycle Detection**: Internal package dependencies form a DAG validated with `assert_no_cyclic_dependencies`.
- [ ] **Workspace-Wide Snippets & Template Partials Directory (`config/partials/`).** Standardize a workspace-level search path for template partials and reusable snippets across render engines:
  - **Mustache**: Automatically registers `config/partials/` so templates can use `{{> header}}` or `{{> systemd/service}}` with automatic indentation preservation.
  - **Jinja2**: Adds `config/partials/` to the template loader search path for `{% include "header.j2" %}` and macro libraries `{% from "macros.j2" import service_unit %}`.
  - **Envsubst / Shell**: Exposes `$drift_partials_dir` so hook scripts and wrappers can easily `source "$drift_partials_dir/common.sh"`.
- [ ] **Compilation package pattern.** Use package `[env.override]` / `[env.fallback]` to declare build flags (instead of scattering them across CLI `./configure` or `cmake -D...` invocations), render them into a `pre_source` hook that checks for existing build artifacts and compiles on-demand. Build artifacts output to a source subfolder, which the deploy pipeline installs to the target directory (e.g., `/opt` or `~/.local`).

### Category 4: Architecture & Performance

- [ ] **Git abstraction layer (`GitRepo` class).** Extract all `subprocess.run(["git", ...])` calls into a thin `GitRepo` class that encapsulates repo path, handles error translation, and supports dry-run inspection. Benefits:
  - Centralized error handling for Git failures
  - Testable without filesystem Git repos (mock the class)
  - Future pluggable backends (`libgit2`, `dulwich`) for environments without Git CLI
- [ ] **Parallel package processing.** Packages without mutual dependencies are independent by design (separate source dirs, configs, target dirs), so rendering, staging, and deployment could run in parallel per topological wave with `concurrent.futures.ThreadPoolExecutor`.
- [ ] **Structured hook protocol (beyond exit codes).** Hooks writing JSON to a well-known fd or file, reporting warnings, skip-reasons, or output variables. For example, a `probe` hook could report *what* dependency is missing, not just "failed."

### Category 5: Distribution & Ecosystem

- [ ] **Native package manager distributions.**
  - **PyPI**: Automated publishing of `drift-dotfiles` via `twine` / GitHub Actions Trusted Publisher.
  - **Homebrew**: Custom Homebrew Formula / Tap (`brew install <tap>/drift`).
  - **AUR**: `PKGBUILD` script for Arch Linux (`yay -S drift-bin`).
  - **Nixpkgs**: Nix derivation / flake to register Drift in the Nix ecosystem.
- [ ] **First-class `drift migrate` from chezmoi/stow/yadm.** A dedicated migration path from popular tools (`chezmoi` state $\rightarrow$ Drift packages, `stow` directory $\rightarrow$ Drift `src/`) to reduce adoption friction.
- [ ] **Fleet & server orchestration integration (Ansible, Salt, Cloud-Init, GitOps).** Design Drift to operate seamlessly under external server management and orchestration tools:
  - **Headless & CI/CD Flags**: Standardize `--batch` / `--yes` (bypass interactive prompts), `--quiet` (minimal stdout for logging pipelines), and deterministic exit codes.
  - **Idempotency & Check-Mode Auditing**: Support `drift plan --json` and `drift status --json` with structured status objects (`status: CLEAN | CHANGED | DRIFTED`) so Ansible (`changed_when`, `check_mode`) or Terraform `local-exec` can audit state without side effects.
  - **Official Integration Recipes & Roles**: Provide reference Ansible roles (`drift_deploy`), Cloud-Init bootstrap scripts, and systemd pull-mode timers (GitOps cron for automated workstation/server synchronization).
  - **Host Fact & Role Tagging**: Leverage Drift's multi-tier variable precedence and host fact matching to allow a single dotfile repository to configure diverse server fleets by hostname, distro, arch, or injected role environment variables.
- [ ] **Binary deployment strategy with drift + mise.** Establish a `drift + mise` binary deployment scheme as a lightweight `home-manager` alternative (or three-pronged `mise` + `nix` + `homebrew` pattern).
