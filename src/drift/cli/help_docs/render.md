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
Drift constructs a template dependency graph using engine configurations in `drift.toml` 
(e.g., `envsubst` or `mustache`).
*   **Template Dependencies**: The inputs to one engine can be templates compiled by another.
*   **Cycle Detection**: Drift runs topological cycle-validation, throwing `CyclicDependencyError` 
    to stop build loops.
*   **Deferred Compilation**: Missing dependencies trigger warnings rather than failures 
    unless a compiled file actually relies on them.
