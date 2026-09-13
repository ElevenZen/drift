# 🧪 Sandbox Compilation & Render Directory: `render/`

The `render/` folder is the "Tier 2" database in Drift's architecture, serving as an 
isolated, sandbox-backed compilation zone.

## 🛡️ Sandbox Isolation
Template parsing and compiling should never put your active host paths or state database 
at risk. During execution, Drift:
1.  Copies all source files and compiles templates dynamically into `render/`.
2.  Tracks compilation history by initializing `render/` as a dedicated local Git repository.
3.  Commit-lock: If any template rendering or dependency parsing fails, **compilation halts instantly with zero impact on your system**, leaving your system completely pristine.

## 🔗 Directed Acyclic Graph (DAG) Template Pipelines
Drift constructs a template dependency graph using engine configurations in `drift_workspace.toml` 
(e.g., `envsubst` or `mustache`).
*   **Template Dependencies**: The inputs to one engine can be templates compiled by another.
*   **Cycle Detection**: Drift runs topological cycle-validation, throwing `CyclicDependencyError` 
    to stop build loops.
*   **Deferred Compilation**: Missing dependencies trigger warnings rather than failures 
    unless a compiled file actually relies on them.

## ⚡ Native TOML Variable Self-Referencing & External Render Engines
*   **Built-in TOML Variable Self-Referencing**: Drift natively resolves inter-variable references (`$VAR`, `${VAR}`) across all standard `.toml` fields using Kahn's topological sort algorithm with cycle detection, requiring zero subprocesses or external dependencies.
*   **External Render Engines**: Registered template engines (`[render.envsubst]`, `[render.mustache]`, `[render.jinja2]`) compile templated dotfiles (e.g., `.bashrc.envst`, `config.j2`) and can also compile dynamic TOML templates (e.g., `drift_workspace.local.envst.toml`, `drift_package.envst.toml`) when complex templating logic is desired.

## 🎨 Package-Level Render Engines & 2-Stage Compilation Pipeline
Drift executes rendering across two modular stages with dedicated `.drift/` internal sandboxes:
1.  **Stage 1: Workspace Global Compilation Pipeline**:
    *   Evaluates global `[render.*]` engines defined in `drift_workspace.toml`.
    *   Compiles workspace-level input dependencies into `render/.drift/`.
    *   Compiles templated package configurations (`drift_package.envst.toml` $\rightarrow$ `render/<pkg>/drift_package.toml`).
2.  **Stage 2: Package-Scoped Compilation Pipeline**:
    *   Loads package configuration from `render/<pkg>/drift_package.toml`.
    *   Overlays package `[render.*]` engines onto workspace engines with **field-level inheritance** (overriding `input_file` relative to `src/<pkg>/` while inheriting unspecified `suffix` and `render_command`).
    *   Renders package-level input dependencies into `render/<pkg>/.drift/`.
    *   Compiles dedicated lifecycle hook scripts from `src/<pkg>/drift_hooks/` into `render/<pkg>/.drift/hooks/` (with template compilation support), keeping hooks sandboxed away from deployable dotfiles.
    *   Compiles package template files with the combined, effective render engines.


