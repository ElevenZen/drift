# Drift Development Guidelines & Agent Rules

## 1. Programming Paradigm & Code Style
* **Functional Programming Preference**: Always prefer functional-style constructs (`filter()`, `map()`, comprehensions, generator expressions, and pure function composition) over procedural `for`-loops across all codebase implementations and refactorings.
* **Modular Pipeline Extraction**: Extract filtering, transformation, and validation predicates into clean, single-responsibility helper functions to keep data pipelines declarative and readable.
* **Separation of Gathering & Manipulation**: Decouple data gathering/resolution from state manipulation and execution. When performing operations across objects (e.g. calling functions, transforming states), gather and resolve all necessary target objects or data representations first before operating on them.
* **Pure & Predictable Flow**: Strive for pure helpers with explicit inputs and outputs, isolating state mutations and file I/O to designated operational handlers.
* **Structured & Typed Return Boundaries**: Use dataclasses or typed result containers (`PackageRenderResult`, `HookResult`, etc.) rather than raw dictionaries or arbitrary tuples across module interfaces to ensure strong type safety and explicit inspection.
* **Boundary Normalization & Validation**: Normalize and validate inputs (e.g., resolving paths to absolute `Path` objects, validating enum keys) at ingestion entry points (`from_dict`, config loaders) so core primitives operate strictly on canonical, validated domain structures without defensive guessing.
* **No Backward Compatibility Burden**: Backward compatibility is not considered at this early development stage. Obsolete arguments, dead functions, legacy aliases, and transitional optional inputs should be removed cleanly and directly rather than retaining compatibility layers or shims.
* **`ensure_` vs `assert_` vs `check_` Naming Convention**:
  * Use **`ensure_`** for functions that **create or modify** state to guarantee a postcondition (e.g., `ensure_dir` creates a directory if missing, `ensure_rendered_file_hook_permissions` chmods hook files).
  * Use **`assert_`** for **read-only validation** guards that **raise an exception** on failure without modifying state (e.g., `assert_writable` checks permissions, `assert_can_escalate` verifies sudo availability, `assert_hooks_exist` validates hook files, `assert_no_cyclic_dependencies` validates dependency graphs).
  * Use **`check_`** for **read-only inspection** functions that **return a result value** (e.g., `bool`, `CheckResult`, or status object) without raising exceptions or modifying state (e.g., `check_existing_workspace_status` returns a report, `check_patch_conflicts` returns a `bool`). Never use `check_` for guards that throw.

## 2. Architecture & Documentation Standards
* **Layered Call Chain Overviews**: Preserve and maintain the `Architecture & Call Chain Overview` docstrings at the top of primitive modules (ordered by dependency layers).
* **Decomposed Stage Pipelines**: Deconstruct complex multi-step workflows (e.g., rendering, lifecycle hooks, reverse sync, drift adoption) into discrete, self-contained sub-stages (e.g., config resolution -> engine/env preparation -> candidate filtering -> file transformation -> result assembly) that can each be reasoned about and unit-tested in isolation.
* **Explicit Dependency Injection**: Pass configurations, registries, and path resolvers explicitly through function signatures; avoid relying on implicit globals, mutable default parameters, or ambient filesystem state.
* **Side-Effect Isolation & Inspectable Operations**: Keep pure computation and transformation logic decoupled from filesystem I/O, subprocess execution, and git operations. Stateful operations should support structured dry-run execution or inspectable result summaries where applicable.
* **Preserve Git Index & Status**: Never run git commands that alter git index, staging status, or workspace state (e.g. `git add`, `git restore --staged`, `git reset`, `git commit`) unless explicitly instructed by the user.
* **Stage Structural Fidelity**: `stage_repo` preserves the directory structure and contents of `render/` inside `install/` with complete 1:1 fidelity. All package files and internal control plane metadata (`.drift_ignore`, `drift_package.toml`, `.drift/hooks/`, `.drift/render/`) are mirrored strictly 1:1.
* **Privacy & User Information Protection**: Never hardcode or leak user-specific host paths (e.g., `/home/<username>`), usernames, personal IPs, or credentials in documentation, code comments, commit messages, or tests. Always use relative repository paths (e.g., `docs/ai_reference.md`, `src/drift/...`) for markdown links and generic placeholders (e.g., `~`, `$HOME`, `test_user`) in examples and test fixtures.
* **Clickable File References**: In conversation summaries and documentation, provide clickable Markdown links to files and symbols using relative repository paths.
* **No ASCII Art Box Tables**: Avoid decorative ASCII/Unicode box-drawing tables or containers (e.g. `┌───┐`). Use standard Markdown lists, Markdown tables, or clean Mermaid diagrams instead to ensure clean rendering in Markdown viewers, Obsidian, and documentation sites.
* **Minimal Mermaid Usage**: Avoid using Mermaid diagrams for simple linear sequences or tree structures (which often render with small, hard-to-read fonts in previewers). Use standard Markdown lists for linear flows and indented Markdown lists for hierarchical/tree structures; reserve Mermaid diagrams strictly for complex, non-linear multi-node networks.
* **Architecture & Reference Documentation**:
  * Consult [`docs/ai_reference.md`](docs/ai_reference.md) as the primary, concise cheat-sheet for primitives, core helper functions, directory topology, and domain invariants.
  * Consult [`docs/design.md`](docs/design.md) for full deep-dive architectural rationales, edge cases, and design specifications.

## 3. Testing & Validation
* Always back new features and refactors with corresponding unit/integration tests in `tests/`.
* **Granular Stage Testing**: Complement end-to-end integration tests with targeted unit tests for extracted sub-stages, predicates, and transformation helpers to isolate regressions quickly without heavy filesystem or subprocess scaffolding.
* **Targeted Test Execution**: When modifying or fixing a specific test file or localized logic, run only that single test file (e.g., `python3 -m unittest tests/test_config.py`) to maintain fast development feedback loops.
* **Whole Suite Execution Rules**:
  * Run the full test suite (`python3 -m unittest discover -s tests`) only after completing cross-cutting changes or finalizing a multi-file feature.
  * If other test files have previously passed and only test files were modified without altering source code, running the targeted test file is sufficient to assure overall suite correctness.
* **CLI & Integration Test Patterns (Reference: [`tests/test_cli.py`](tests/test_cli.py))**:
  * **Isolated Workspace & Git Topology Scaffolding**:
    * Create a temporary directory via `tempfile.TemporaryDirectory()` in `setUp()` and clean it up in `tearDown()`.
    * Build canonical workspace directory layouts (`src/`, `config/drift_workspace.toml`, `render/`, `install/`, target directories).
    * Initialize necessary Git repositories in temporary folders with `subprocess.run(["git", "init"], cwd=..., check=True, capture_output=True)` and set dummy git credentials (`user.name`, `user.email`). Use `repair_drift_workspace(Path(self.drift_root))` or `main(["init", "--force"])` to establish initial repo invariants.
  * **Stdout & Stderr Stream Patching**:
    * **Zero Terminal Leakage**: Test runner output (`python3 -m unittest discover -s tests`) must remain completely silent and pristine (dots only). Never let commands or logging leak raw strings into stdout/stderr.
    * **Setup / Intermediate Command Suppression**: When running prerequisite CLI commands during setup (e.g. `init`, `render`, `stage`), silence them using `with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):`.
    * **Output Capture & Verification**: Intercept streams using `with patch("sys.stdout", StringIO()) as mock_out:` (or explicit `try...finally` redirection with `sys.stdout = stdout`) to assert on command outputs without printing to the console.
  * **ANSI Escape & Whitespace Normalization (`TestCaseUtilityMixin`)**:
    * For tests verifying styled terminal output (Rich colors, emojis, tables, word-wrapping), inherit `TestCaseUtilityMixin` from [`tests/test_utils.py`](tests/test_utils.py).
    * Use `self.assertIn_stripped(expected, actual)` to perform resilient substring matching that strips ANSI escape codes and normalizes variable whitespace.
  * **Dual CLI Backend Testing**:
    * Drift supports both Typer and Argparse backends. When adding or testing CLI commands and flags, test both backends: `main(...)` (Typer entry point) and `run_argparse_cli(...)` (Argparse entry point).
    * For handler routing tests, use `unittest.mock.patch` on `drift.cli.cli_handlers.<handler>` to verify that flags (e.g. `dry_run`, `no_hooks`, `no_deps`, `reinstall`, `no_cache`) propagate correctly via `mock_action.call_args`.
  * **Exit Code Assertions**:
    * Test expected failures or help flags with `with self.assertRaises(SystemExit) as cm:` and assert explicit status codes: `cm.exception.code == 0` for help/success, `cm.exception.code == 1` for execution failures/domain errors, `cm.exception.code == 2` for syntax/argument errors.
  * **Scoped Test Mode & Logging Hygiene**:
    * By default, Drift tests run in test mode with logging disabled (`set_test_mode(True, enable_logging=False)`).
    * If a test verifies log emission via `self.assertLogs(...)`, enable logging strictly within the test scope using `set_test_mode(True, enable_logging=True)` and always restore `set_test_mode(True, enable_logging=False)` in a `finally` block or `tearDown()`.
    * Because enabling logging activates handlers on the root logger, always wrap logging tests or the asserted execution in `with patch("sys.stdout", StringIO()):` (or patch `sys.stderr`) to prevent non-asserted logger records from leaking to the terminal.

