# Drift Development Guidelines & Agent Rules

## 1. Programming Paradigm & Code Style
* **Functional Programming Preference**: Always prefer functional-style constructs (`filter()`, `map()`, comprehensions, generator expressions, and pure function composition) over procedural `for`-loops across all codebase implementations and refactorings.
* **Modular Pipeline Extraction**: Extract filtering, transformation, and validation predicates into clean, single-responsibility helper functions to keep data pipelines declarative and readable.
* **Pure & Predictable Flow**: Strive for pure helpers with explicit inputs and outputs, isolating state mutations and file I/O to designated operational handlers.

## 2. Architecture & Documentation Standards
* **Layered Call Chain Overviews**: Preserve and maintain the `Architecture & Call Chain Overview` docstrings at the top of primitive modules (ordered by dependency layers).
* **Clickable File References**: In conversation summaries and documentation, provide clickable Markdown links to files and symbols.

## 3. Testing & Validation
* Always back new features and refactors with corresponding unit/integration tests in `tests/`.
* Run the test suite (`python3 -m unittest discover -s tests`) to guarantee zero regressions.
