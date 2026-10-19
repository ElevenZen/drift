# `src/drift/cli/` — Layer 5 CLI Presentation, Dual Backends & Tab-Completion Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 5 (Presentation, Command Schema, Dual Argparse/Typer Backends, Handlers & Shell Completion)**
* **Core Responsibility**: Defines the single-source-of-truth declarative CLI schema (`schema.py`), compiles both the `typer` (Rich) and `argparse` (stdlib fallback) CLI parsers from that schema, routes parsed commands through unified handlers (`cli_handlers.py`) and action executors (`actions.py`) wrapped in a standardized exit-code boundary (`error_boundary.py`), serves built-in help topics (`help_docs/`), and generates shell tab-completion scripts (`completion/`).
* **Dependency Rules**:
  * **Allowed Imports**: All lower layers ([`../utils/`](../utils/), [`../core/`](../core/), [`../models/`](../models/), [`../config/`](../config/), [`../hooks/`](../hooks/), [`../render/`](../render/), [`../primitives/`](../primitives/)).
  * **Forbidden Incoming Imports**: No module outside `src/drift/cli/` (except `src/drift/__main__.py`) may import from `src/drift/cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Declarative Schema & Help Assets**: [`schema.py`](schema.py), [`help_docs.py`](help_docs.py) (`help_docs/*.md`), [`error_boundary.py`](error_boundary.py)
2. **Completion Code Generators**: [`completion/bash.py`](completion/bash.py), [`completion/zsh.py`](completion/zsh.py), [`completion/fish.py`](completion/fish.py), [`completion/nushell.py`](completion/nushell.py) $\rightarrow$ [`completion/__init__.py`](completion/__init__.py)
3. **Action Executors & Unified Handlers**: [`actions.py`](actions.py) $\rightarrow$ [`cli_handlers.py`](cli_handlers.py)
4. **Dual Parser Backends & Entry Point**: [`argparse_backend.py`](argparse_backend.py), [`typer_backend.py`](typer_backend.py) $\rightarrow$ [`__init__.py`](__init__.py) (`main`)

---

## 3. Module & Public API Catalog

### [`schema.py`](schema.py) — Single Source of Truth CLI & Completion Schema
* `SourceType(Enum)`: `NONE`, `DYNAMIC_PACKAGES`, `FIXED_CHOICES`, `FILES`, `DIRECTORIES`.
* `Choice(value: str, description: str)`, `OptionSpec`, `PositionalSpec`, `CommandSpec`, `CompletionSchema`.
* `build_completion_schema() -> CompletionSchema`: Declarative registry of all global options, movable flags (`--json`, `-v/--verbose`), high-level commands, and low-level primitive commands.

### [`cli_handlers.py`](cli_handlers.py) & [`actions.py`](actions.py) — Unified Command Routing & Execution
* `DriftCLIContext(directory=None, verbose=False, json_mode=False, env_vars=())`: Normalizes global CLI flags (`-C/--directory`, `-v/--verbose`, `--json`, `-e/--env`), resolves `drift_root`, and scopes CLI `-e KEY=VAL` overrides.
* `handle_<command>(ctx: DriftCLIContext, ...)` in [`cli_handlers.py`](cli_handlers.py): Normalizes CLI arguments (e.g. `RollbackPolicy.from_value`, `PackageStage` tier parsing) and dispatches to `actions.execute_<command>(...)`.
* `check_sudo_and_root(drift_root: Path) -> None` in [`actions.py`](actions.py): Enforces the CLI Sudo Guard (blocks running `drift` under `sudo` on user-owned workspaces).
* `assert_workspace_healthy(drift_root: Path, json_mode: bool = False) -> WorkspaceHealthReport`: Runs pre-flight workspace health checks before executing commands.
* `execute_<command>(...)` in [`actions.py`](actions.py): Loads `WorkspaceConfig`, invokes the corresponding primitive in `src/drift/primitives/`, formats Rich/text or `--json` output, and exits with the appropriate [`ExitCode`](../core/constants.py).

### [`typer_backend.py`](typer_backend.py), [`argparse_backend.py`](argparse_backend.py) & [`__init__.py`](__init__.py) — Dual CLI Backends
* `generate_typer_app() -> typer.Typer` ([`typer_backend.py`](typer_backend.py)): Dynamically builds the Typer/Rich CLI application from `build_completion_schema()`.
* `generate_argparse_parser(schema: CompletionSchema) -> argparse.ArgumentParser` & `run_argparse_cli(argv=None) -> None` ([`argparse_backend.py`](argparse_backend.py)): Zero-dependency standard-library CLI backend compiled from the same `CompletionSchema`.
* `main(argv=None) -> None` ([`__init__.py`](__init__.py)): Entry point dispatching to Typer when installed or falling back seamlessly to `run_argparse_cli`.

### [`error_boundary.py`](error_boundary.py) & [`help_docs.py`](help_docs.py) — Error Handling & Built-in Manual
* `cli_error_boundary(json_mode: bool = False, use_rich: bool = False, raw_errors: bool = False)`: Context manager catching `DriftError` subclasses, formatting structured error panels or JSON payloads, and raising `SystemExit(exc.exit_code)`.
* `get_help_page(topic: Optional[str]) -> str` & `print_help_document(topic: Optional[str] = None) -> None`: Loads and renders markdown manuals from [`help_docs/`](help_docs/) (`overall.md`, `package.md`, `src.md`, `render.md`, `install.md`, `fcd.md`, `ignore.md`, `drift_package_toml.md`, `drift_workspace_toml.md`, `workspace.md`, `health.md`, `clone.md`, `faq.md`).

### [`completion/`](completion/) — Shell Tab-Completion Generators (`drift complete`)
* `generate_completion_script(shell: str, schema: Optional[CompletionSchema] = None) -> str`: Compiles native tab-completion scripts for `bash` (`BashGenerator`), `zsh` (`ZshGenerator`), `fish` (`FishGenerator`), and `nu` / `nushell` (`NushellGenerator`).

---

## 4. Local Invariants & Modification Rules
1. **Schema-First CLI Changes**: Whenever adding or modifying a CLI command, flag, or choice list, update [`schema.py`](schema.py) (`build_completion_schema`), [`cli_handlers.py`](cli_handlers.py), and [`actions.py`](actions.py) together—never hardcode divergent flags in only one backend (`typer_backend.py` or `argparse_backend.py`).
2. **Dual Backend Testing Requirement**: Every new CLI command or flag must be tested against both `main([...])` (Typer) and `run_argparse_cli([...])` (Argparse) in `tests/test_cli.py`.
3. **Sudo Workspace Guard**: All workspace-mutating or workspace-reading commands in `actions.py` must call `prepare_cli_environment(drift_root)` (which runs `check_sudo_and_root(drift_root)` and `init_system_facts()`).

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/cli.md`](../../../docs/cli.md), [`docs/features/tab_completion.md`](../../../docs/features/tab_completion.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_cli.py
  PYTHONPATH=src python3 -m unittest tests/test_completion.py
  PYTHONPATH=src python3 -m unittest tests/test_help_docs.py
  ```
