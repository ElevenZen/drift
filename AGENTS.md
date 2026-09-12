# Drift Development Guidelines & Agent Rules

## 1. Programming Paradigm & Code Style
* **Functional Programming Preference**: Always prefer functional-style constructs (`filter()`, `map()`, comprehensions, generator expressions, and pure function composition) over procedural `for`-loops across all codebase implementations and refactorings.
* **Modular Pipeline Extraction**: Extract filtering, transformation, and validation predicates into clean, single-responsibility helper functions to keep data pipelines declarative and readable.
* **Separation of Gathering & Manipulation**: Decouple data gathering/resolution from state manipulation and execution. When performing operations across objects (e.g. calling functions, transforming states), gather and resolve all necessary target objects or data representations first before operating on them.
* **Pure & Predictable Flow**: Strive for pure helpers with explicit inputs and outputs, isolating state mutations and file I/O to designated operational handlers.
* **Structured & Typed Return Boundaries**: Use dataclasses or typed result containers (`PackageRenderResult`, `HookResult`, etc.) rather than raw dictionaries or arbitrary tuples across module interfaces to ensure strong type safety and explicit inspection.
* **Boundary Normalization & Validation**: Normalize and validate inputs (e.g., resolving paths to absolute `Path` objects, validating enum keys) at ingestion entry points (`from_dict`, config loaders) so core primitives operate strictly on canonical, validated domain structures without defensive guessing.

## 2. Architecture & Documentation Standards
* **Layered Call Chain Overviews**: Preserve and maintain the `Architecture & Call Chain Overview` docstrings at the top of primitive modules (ordered by dependency layers).
* **Decomposed Stage Pipelines**: Deconstruct complex multi-step workflows (e.g., rendering, lifecycle hooks, reverse sync, drift adoption) into discrete, self-contained sub-stages (e.g., config resolution -> engine/env preparation -> candidate filtering -> file transformation -> result assembly) that can each be reasoned about and unit-tested in isolation.
* **Explicit Dependency Injection**: Pass configurations, registries, and path resolvers explicitly through function signatures; avoid relying on implicit globals, mutable default parameters, or ambient filesystem state.
* **Side-Effect Isolation & Inspectable Operations**: Keep pure computation and transformation logic decoupled from filesystem I/O, subprocess execution, and git operations. Stateful operations should support structured dry-run execution or inspectable result summaries where applicable.
* **Clickable File References**: In conversation summaries and documentation, provide clickable Markdown links to files and symbols.

## 3. Testing & Validation
* Always back new features and refactors with corresponding unit/integration tests in `tests/`.
* **Granular Stage Testing**: Complement end-to-end integration tests with targeted unit tests for extracted sub-stages, predicates, and transformation helpers to isolate regressions quickly without heavy filesystem or subprocess scaffolding.
* **Targeted Test Execution**: When modifying or fixing a specific test file or localized logic, run only that single test file (e.g., `python3 -m unittest tests/test_config.py`) to maintain fast development feedback loops.
* **Whole Suite Execution Rules**:
  * Run the full test suite (`python3 -m unittest discover -s tests`) only after completing cross-cutting changes or finalizing a multi-file feature.
  * If other test files have previously passed and only test files were modified without altering source code, running the targeted test file is sufficient to assure overall suite correctness.
