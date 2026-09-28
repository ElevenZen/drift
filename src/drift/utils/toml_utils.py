"""TOML parsing, serialization, and table merging utilities for Drift.

===============================================================================
Fallback Parser Specification Boundaries & Unsupported Features
===============================================================================

For older Python versions (< 3.11) where standard library `tomllib` is absent,
`_parse_toml_fallback` implements a lightweight, self-contained subset tailored
strictly to Drift's configuration files (`drift_workspace.toml`, `drift_package.toml`).

Supported TOML Features:
    - Tables (`[table]`) and dotted table headers (`[parent.child]`).
    - Array of tables (`[[array.of.tables]]`) including nested subtables (`[arr.subtab]`).
    - Multiline strings: triple-quoted basic strings (\"\"\"...\"\"\") with line continuation
      and escape decoding, and triple-quoted literal strings ('''...''') with verbatim content.
    - Scalars: double-quoted strings (with standard escapes: \\n, \\t, \\r, \\", \\\\),
      single-quoted literal strings, integers, floats, booleans (true/false).
    - Arrays: single-line, multiline, nested arrays, trailing commas, and inline comments.
    - Inline tables: single and nested inline tables ({ a = { b = 1 } }), arrays of inline tables.

Explicit Specification Boundaries & Unsupported Features:
    - RFC 3339 Datetimes/Timestamps: parsed as unquoted strings.
    - Alternate number bases (hex 0x, octal 0o, binary 0b), scientific notation (1e5),
      and numeric underscores (1_000_000).
    - Special float literals (inf, -inf, nan).
    - Unicode escape sequences (\\uXXXX, \\UXXXXXXXX).
    - Left-hand side dotted assignment keys (a.b = 1 within a table stores "a.b" flatly
      rather than creating a nested dictionary; section headers [a.b] work normally).
    - Duplicate key collision guarding (later definitions overwrite earlier keys rather than raising errors).
    - Lenient Array-of-Tables Mutation: Unlike Python 3.11+ stdlib `tomllib` (which strictly
      enforces TOML v1.0.0 namespace immutability and raises `TOMLDecodeError` if an inline array
      is subsequently extended with `[[...]]`), `_parse_toml_fallback` permissively allows
      appending array-of-tables dictionaries to existing lists. Drift configuration files and tests
      should avoid relying on this mixed syntax to ensure cross-version TOML standard compatibility.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Public Ingestion & Serialization Boundaries
    parse_toml(content) -> dict
        - Dispatches to stdlib `tomllib.loads` on Python >= 3.11
        - Delegates to `_parse_toml_fallback` on Python < 3.11
    dump_toml(data) -> str
        - Serializes top-level key-values followed by table sections
    merge_toml(dict_a, dict_b) -> dict
        - Deep recursive dictionary merge for layered configuration tables

Layer 2: Decomposed Fallback Parsing & Serialization Pipelines
    Fallback Document Parser:
        _parse_toml_fallback(content) -> dict
            - Iterates raw lines, stripping comments via `_strip_line_comment`
            - Accumulates multi-line expressions until state is clean
            - Dispatches complete logical lines via `_apply_logical_line`
        _strip_line_comment(raw_line, state) -> str
            - Strips `#` comments outside strings while updating bracket/quote depth
        _apply_logical_line(logical_line, root, cursor) -> Optional[dict]
            - Dispatches section headers (`[[...]]`, `[...]`) and assignments (`key = val`)
        _navigate_toml_table(root, keys, is_array_entry) -> dict
            - Traverses or instantiates nested tables / array of tables along dotted paths

    Value Tokenizer & Scalar Parser:
        parse_toml_value(val_str) -> Any
            - Dispatches arrays `[...]`, inline tables `{...}`, multiline strings,
              single-line strings, and scalars
        split_array_elements(array_str) -> List[str]
            - Splits comma-separated elements respecting quotes, brackets, and braces
        _parse_multiline_basic_string(val_str) -> str
            - Parses \"\"\"...\"\"\" with newline trimming and line-continuation backslashes
        _parse_multiline_literal_string(val_str) -> str
            - Parses '''...''' with newline trimming and literal content
        _parse_inline_table(val_str) -> dict
            - Parses `{ key = "val", ... }` into a Python dictionary
        _unescape_string(inner) -> str
            - Decodes standard double-quoted string escape sequences (`\\n`, `\\t`, etc.)
        _parse_scalar_value(val_str) -> Any
            - Coerces booleans, integers, floats, and unquoted fallback strings

    Document Serializer Helpers:
        _dump_table(table_name, table_dict) -> List[str]
            - Formats section tables `[name]` and 1-level nested subtables `[name.sub]`
        _format_toml_value(val) -> Optional[str]
            - Formats scalar types, strings, and lists into valid TOML syntax
        _escape_toml_string(val) -> str
            - Escapes special characters, control codes, and line breaks for string literals

Layer 1: Lexical State & Delimiter Tracking Primitives
    _ParserState (Dataclass)
        - In-memory tracker for quote states (`in_double_quote`, `in_single_quote`,
          `in_triple_double`, `in_triple_single`) and nesting depths (`open_brackets`, `open_braces`)
        - `is_clean`: Invariant check for zero open delimiters
        - `in_multiline`: Checks if currently inside a triple-quoted multiline string
        - `in_string`: Checks if currently inside any quoted string literal
        - `in_brackets`: Checks if currently inside brackets or braces
        - `consume_quotes(text, i)`: Single/multiline quote transition trigger
        - `track_bracket(char, context_str)`: Nesting increment/decrement with
          immediate error validation on unmatched closing delimiters
===============================================================================
"""

from dataclasses import dataclass
import re
from typing import Any, List, Optional

try:
    import tomllib  # type: ignore[import-not-found, unused-ignore] # pyright: ignore[reportMissingImports]
    HAS_TOMLLIB = True
except ImportError:
    tomllib = None  # type: ignore[assignment]
    HAS_TOMLLIB = False


# =============================================================================
# Layer 1: Lexical State & Delimiter Tracking Primitives
# =============================================================================


@dataclass
class _ParserState:
    """Tracks parser quoting and bracket/brace nesting depth during tokenization.

    Maintains single-pass tokenizer state across characters and multiline buffers,
    ensuring that commas, comments, and delimiters inside quoted strings or nested
    brackets/braces are handled accurately.
    """
    in_double_quote: bool = False
    in_single_quote: bool = False
    in_triple_double: bool = False
    in_triple_single: bool = False
    open_brackets: int = 0
    open_braces: int = 0

    @property
    def is_clean(self) -> bool:
        """Returns True if no quotes, brackets, or braces are currently open."""
        return (
            not self.in_string
            and self.open_brackets == 0
            and self.open_braces == 0
        )

    @property
    def in_multiline(self) -> bool:
        """Returns True if currently inside a multiline string (triple double or single quotes)."""
        return self.in_triple_double or self.in_triple_single

    @property
    def in_string(self) -> bool:
        """Returns True if currently inside either a single or multiline quoted string."""
        return (
            self.in_double_quote
            or self.in_single_quote
            or self.in_triple_double
            or self.in_triple_single
        )

    @property
    def in_brackets(self) -> bool:
        """Returns True if currently inside brackets or braces."""
        return self.open_brackets != 0 or self.open_braces != 0

    def consume_quotes(self, text: str, i: int) -> Optional[int]:
        """Checks for and toggles single, double, or triple quote state at index i.

        Returns:
            Number of characters consumed (1 for ' or ", 3 for ''' or \"\"\"),
            or None if text[i] is not a valid quote delimiter.
        """
        # 1. Triple double quotes: """
        if not self.in_single_quote and not self.in_triple_single and text[i:i + 3] == '"""':
            self.in_triple_double = not self.in_triple_double
            return 3

        # 2. Triple single quotes: '''
        if not self.in_double_quote and not self.in_triple_double and text[i:i + 3] == "'''":
            self.in_triple_single = not self.in_triple_single
            return 3

        # 3. Single double quote: "
        if not self.in_triple_double and not self.in_triple_single and not self.in_single_quote:
            if text[i] == '"':
                self.in_double_quote = not self.in_double_quote
                return 1

        # 4. Single single quote: '
        if not self.in_triple_double and not self.in_triple_single and not self.in_double_quote:
            if text[i] == "'":
                self.in_single_quote = not self.in_single_quote
                return 1

        return None

    def track_bracket(self, char: str, context_str: str = "") -> bool:
        """Tracks nesting level for '[' / ']' and '{' / '}'.

        Returns:
            True if `char` was a recognized bracket/brace delimiter; False otherwise.

        Raises:
            ValueError: If an unmatched closing bracket ']' or brace '}' is encountered.
        """
        if char == '[':
            self.open_brackets += 1
            return True
        elif char == ']':
            if self.open_brackets == 0:
                ctx = f" in: {context_str}" if context_str else ""
                raise ValueError(f"Toml format error, unmatched closing bracket ']'{ctx}")
            self.open_brackets -= 1
            return True
        elif char == '{':
            self.open_braces += 1
            return True
        elif char == '}':
            if self.open_braces == 0:
                ctx = f" in: {context_str}" if context_str else ""
                raise ValueError(f"Toml format error, unmatched closing brace '}}'{ctx}")
            self.open_braces -= 1
            return True
        return False


# =============================================================================
# Layer 2: Decomposed Value Parsing Pipeline
# =============================================================================


def split_array_elements(array_str: str) -> List[str]:
    """Splits a TOML array or inline table string by commas, respecting quotes, brackets, and braces.

    Iterates through the raw content string and accumulates character tokens into elements.
    Commas encountered inside quoted strings or nested brackets/braces are treated as literal
    characters rather than element separators.

    Args:
        array_str: Inner content of an array `[...]` or inline table `{...}`.

    Returns:
        List of unparsed element substring tokens with outer whitespace trimmed.

    Raises:
        ValueError: If there are unclosed quotes, brackets, or braces, or unmatched closing delimiters.
    """
    elements: List[str] = []
    pending_element: List[str] = []
    state = _ParserState()
    escaped = False
    i = 0
    n = len(array_str)

    def _submit_pending() -> None:
        """Appends the accumulated pending characters to elements if non-empty, then resets."""
        nonlocal pending_element
        elem = "".join(pending_element).strip()
        pending_element = []
        if elem:
            elements.append(elem)

    while i < n:
        char = array_str[i]

        # Handle backslash escapes within double or triple-double quoted strings
        if escaped:
            pending_element.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\" and (state.in_double_quote or state.in_triple_double):
            pending_element.append(char)
            escaped = True
            i += 1
            continue

        # 1. Quote delimiters (single, double, or triple)
        consumed = state.consume_quotes(array_str, i)
        if consumed is not None:
            pending_element.append(array_str[i:i + consumed])
            i += consumed
            continue

        # 2. Inside strings: preserve all characters literally
        if state.in_string:
            pending_element.append(char)
            i += 1
            continue

        # 3. Nested brackets and braces: preserve commas inside sub-structures
        if state.track_bracket(char, array_str) or state.in_brackets:
            pending_element.append(char)
            i += 1
            continue

        # 4. Non-comma scalars: accumulate normal characters
        if char != ',':
            pending_element.append(char)
            i += 1
            continue

        # 5. Top-level comma delimiter: split current element
        _submit_pending()
        i += 1

    # Flush any remaining token after the last comma
    _submit_pending()

    if not state.is_clean:
        raise ValueError(f"Toml format error, unclosed brackets, braces, or quotes in: {array_str}")

    return elements


def _parse_inline_table(val_str: str) -> dict:
    """Parses a TOML inline table string like `{ a = 1, b = "val" }` into a Python dictionary.

    Splits key-value assignments using `split_array_elements` to safely handle commas inside
    nested strings, arrays, or sub-inline tables.

    Args:
        val_str: Raw inline table string enclosed in curly braces `{...}`.

    Returns:
        Dictionary mapping parsed keys to their recursively parsed values.
    """
    content = val_str[1:-1].strip()
    if not content:
        return {}

    elements = split_array_elements(content)
    table: dict = {}
    for elem in elements:
        if '=' in elem:
            k, v = elem.split('=', 1)
            key = k.strip()
            if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
                key = key[1:-1]
            table[key] = parse_toml_value(v.strip())
    return table


def _unescape_string(inner: str) -> str:
    """Unescapes double-quoted TOML string escape sequences.

    Supports standard TOML escape characters: quotes, newlines, tabs, carriage returns,
    backspaces, formfeeds, and backslashes.

    Args:
        inner: String content stripped of outer quotation marks.

    Returns:
        Unescaped Python string.
    """
    return (
        inner
        .replace('\\"', '"')
        .replace('\\n', '\n')
        .replace('\\r', '\r')
        .replace('\\t', '\t')
        .replace('\\b', '\b')
        .replace('\\f', '\f')
        .replace('\\\\', '\\')
    )


def _parse_multiline_basic_string(val_str: str) -> str:
    """Parses a multiline basic TOML string (\"\"\"...\"\"\").

    Trims an immediate first newline per TOML specification, resolves line-continuation
    backslashes (`\\` followed by newline and optional whitespace), and decodes escapes.

    Args:
        val_str: Raw multiline string token starting and ending with `\"\"\"`.

    Returns:
        Parsed Python string.
    """
    inner = val_str[3:-3]
    if inner.startswith('\r\n'):
        inner = inner[2:]
    elif inner.startswith('\n'):
        inner = inner[1:]

    # Line continuation: backslash followed by newline and following whitespace
    inner = re.sub(r'\\\r?\n[\t ]*', '', inner)
    return _unescape_string(inner)


def _parse_multiline_literal_string(val_str: str) -> str:
    """Parses a multiline literal TOML string ('''...''').

    Trims an immediate first newline per TOML specification, preserving all other
    characters verbatim without escape sequence evaluation.

    Args:
        val_str: Raw multiline string token starting and ending with `'''`.

    Returns:
        Parsed verbatim Python string.
    """
    inner = val_str[3:-3]
    if inner.startswith('\r\n'):
        inner = inner[2:]
    elif inner.startswith('\n'):
        inner = inner[1:]
    return inner


def _parse_scalar_value(val_str: str) -> Any:
    """Parses booleans, integers, floats, and unquoted fallback strings.

    Args:
        val_str: Raw scalar string token.

    Returns:
        Coerced Python bool, int, float, or original unquoted string.
    """
    val_lower = val_str.lower()
    if val_lower == "true":
        return True
    if val_lower == "false":
        return False

    # Integer match (e.g. 42, -12, +5)
    if re.match(r'^[-+]?\d+$', val_str):
        try:
            return int(val_str)
        except ValueError:
            pass

    # Float match (e.g. 3.14, -0.5, +2.0)
    if re.match(r'^[-+]?\d+\.\d+$', val_str):
        try:
            return float(val_str)
        except ValueError:
            pass

    # Fallback to unquoted string representation
    return val_str


def parse_toml_value(val_str: str) -> Any:
    """Parses a raw TOML value string into its appropriate Python type.

    Evaluates values across data type boundaries:
    1. Arrays (`[...]`) -> recursive list parsing
    2. Inline tables (`{...}`) -> recursive dictionary parsing
    3. Multiline basic strings (`\"\"\"...\"\"\"`) -> newline trimmed, line continuation, unescaped
    4. Multiline literal strings (`'''...'''`) -> newline trimmed, literal content
    5. Double-quoted strings (`"..."`) -> unescaped string
    6. Single-quoted literal strings (`'...'`) -> literal string
    7. Scalars (booleans, integers, floats, unquoted strings)

    Args:
        val_str: Raw TOML value representation.

    Returns:
        Parsed Python object (list, dict, str, int, float, or bool).
    """
    val_str = val_str.strip()
    if not val_str:
        return ""

    # 1. Array
    if val_str.startswith('[') and val_str.endswith(']'):
        content = val_str[1:-1].strip()
        if not content:
            return []
        elements = split_array_elements(content)
        return [parse_toml_value(elem) for elem in elements]

    # 2. Inline table
    if val_str.startswith('{') and val_str.endswith('}'):
        return _parse_inline_table(val_str)

    # 3. Multiline basic string: """..."""
    if val_str.startswith('"""') and val_str.endswith('"""') and len(val_str) >= 6:
        return _parse_multiline_basic_string(val_str)

    # 4. Multiline literal string: '''...'''
    if val_str.startswith("'''") and val_str.endswith("'''") and len(val_str) >= 6:
        return _parse_multiline_literal_string(val_str)

    # 5. Double-quoted string
    if val_str.startswith('"') and val_str.endswith('"'):
        return _unescape_string(val_str[1:-1])

    # 6. Single-quoted literal string
    if val_str.startswith("'") and val_str.endswith("'"):
        inner = val_str[1:-1]
        inner = inner.replace("\\'", "'")
        return inner.replace('\\\\', '\\')

    # 7. Scalars (bool, int, float, fallback string)
    return _parse_scalar_value(val_str)


# =============================================================================
# Layer 2: Decomposed Fallback Document Ingestion Pipeline
# =============================================================================


def _navigate_toml_table(data: dict, keys: List[str], is_array_entry: bool = False) -> dict:
    """Navigates to or creates a TOML table dictionary from a dot-path of keys.

    Handles standard nested tables (e.g., `[packages.enable]`), array of tables
    (e.g., `[[package.dependencies]]`), and subtables within arrays (e.g., `[arr.subtab]`).

    Args:
        data: The root dictionary being populated.
        keys: List of dotted key segments (e.g. `['package', 'dependencies']`).
        is_array_entry: If True, indicates an array-of-tables header `[[...]]`.

    Returns:
        The target dictionary for subsequent key-value assignments.
    """
    current = data

    # Traverse intermediate table path segments
    for k in keys[:-1]:
        # If intermediate key points to an array of tables, navigate into its most recent entry
        if k in current and isinstance(current[k], list) and current[k] and isinstance(current[k][-1], dict):
            current = current[k][-1]
        elif k not in current or not isinstance(current[k], dict):
            current[k] = {}
            current = current[k]
        else:
            current = current[k]

    leaf = keys[-1]

    # Array of tables (`[[table]]`): append a new table entry and return it
    if is_array_entry:
        if leaf not in current or not isinstance(current[leaf], list):
            current[leaf] = []
        new_table: dict = {}
        current[leaf].append(new_table)
        return new_table

    # Standard table (`[table]`): navigate into active table or create a new one
    if leaf in current and isinstance(current[leaf], list) and current[leaf] and isinstance(current[leaf][-1], dict):
        return current[leaf][-1]
    elif leaf not in current or not isinstance(current[leaf], dict):
        current[leaf] = {}
        return current[leaf]
    else:
        return current[leaf]


def _strip_line_comment(raw_line: str, state: _ParserState) -> str:
    """Strips comments from a TOML line outside quoted strings while updating nesting state.

    Iterates through characters in `raw_line`, respecting backslash escapes within double
    and triple-double quotes and tracking quote toggles. If an unquoted `#` is reached,
    character collection terminates immediately. Non-string bracket delimiters update the parser state.

    Args:
        raw_line: A single raw line from the TOML document.
        state: Shared `_ParserState` instance tracking multi-line delimiters.

    Returns:
        Line content with comments stripped. Preserves indentation and newlines if inside
        a multiline string.
    """
    clean_chars: List[str] = []
    escaped = False
    i = 0
    n = len(raw_line)

    while i < n:
        char = raw_line[i]

        # Handle backslash escapes inside double-quoted or triple-double-quoted strings
        if escaped:
            clean_chars.append(char)
            escaped = False
            i += 1
            continue

        if char == "\\" and (state.in_double_quote or state.in_triple_double):
            clean_chars.append(char)
            escaped = True
            i += 1
            continue

        # Quote delimiters (single, double, or triple)
        consumed = state.consume_quotes(raw_line, i)
        if consumed is not None:
            clean_chars.append(raw_line[i:i + consumed])
            i += consumed
            continue

        # Inside strings: preserve all characters literally (including '#' and quotes)
        if state.in_string:
            clean_chars.append(char)
            i += 1
            continue

        # Comment delimiter outside strings: stop processing this line
        if char == '#':
            break

        # Delimiter brackets/braces update state and append
        state.track_bracket(char, raw_line)
        clean_chars.append(char)
        i += 1

    result = "".join(clean_chars)
    # Retain indentation and trailing spaces when inside multiline strings
    return result if state.in_multiline else result.strip()


def _apply_logical_line(
    logical_line: str,
    root: dict,
    cursor: Optional[dict],
) -> Optional[dict]:
    """Applies a complete logical TOML line (table header or key-value pair) to root.

    Args:
        logical_line: A fully assembled logical line (comments stripped, multi-line joined).
        root: The root document dictionary.
        cursor: The active dictionary table context for assignments, or None for top-level.

    Returns:
        The updated active table dictionary for subsequent assignments.
    """
    # 1. Array of tables header: `[[name]]`
    if logical_line.startswith('[[') and logical_line.endswith(']]'):
        table_name = logical_line[2:-2].strip()
        keys = [k.strip() for k in table_name.split('.')]
        return _navigate_toml_table(root, keys, is_array_entry=True)

    # 2. Standard table header: `[name]`
    if logical_line.startswith('[') and logical_line.endswith(']'):
        table_name = logical_line[1:-1].strip()
        keys = [k.strip() for k in table_name.split('.')]
        return _navigate_toml_table(root, keys, is_array_entry=False)

    # 3. Key-value assignment: `key = val`
    if '=' in logical_line:
        key_part, val_part = logical_line.split('=', 1)
        key = key_part.strip()
        val = parse_toml_value(val_part.strip())
        target = root if cursor is None else cursor
        target[key] = val
        return cursor

    return cursor


def _parse_toml_fallback(content: str) -> dict:
    """Hand-rolled lightweight TOML parser for older Python versions (< 3.11).

    Parses single and multi-line TOML expressions, section headers (`[table]`),
    and array-of-tables (`[[arr]]`). See the module header for full supported
    features and explicit specification boundaries.

    Note on stdlib `tomllib` divergence:
        Unlike Python 3.11+ `tomllib.loads` (which strictly raises `TOMLDecodeError`
        if an inline array is extended via `[[...]]`), this fallback parser permissively
        permits appending array-of-tables elements to an existing list. Standard Drift
        configurations and tests must avoid this mixed syntax to ensure cross-version compatibility.

    Args:
        content: Raw TOML document string.

    Returns:
        Parsed configuration dictionary.

    Raises:
        ValueError: If unclosed brackets, braces, or quotes remain at end of content.
    """
    root: dict = {}
    state = _ParserState()
    buffer: List[str] = []
    cursor: Optional[dict] = None

    for raw_line in content.splitlines():
        line_clean = _strip_line_comment(raw_line, state)

        # Skip empty lines outside multiline strings when buffer is empty
        if not line_clean and not buffer and not state.in_multiline:
            continue

        buffer.append(line_clean)

        # Wait until all multiline brackets/braces/quotes are closed before executing line
        if not state.is_clean:
            continue

        # Join line buffer into a single logical line and apply it
        logical_line = "\n".join(buffer).strip()
        buffer = []
        if not logical_line:
            continue

        cursor = _apply_logical_line(logical_line, root, cursor)

    # Invariant: file must not terminate with unclosed delimiters
    if not state.is_clean:
        raise ValueError("Toml format error, unclosed brackets or quotes.")

    return root


# =============================================================================
# Layer 3: Public Ingestion & Serialization Boundaries
# =============================================================================


def parse_toml(content: str) -> dict:
    """Parses a TOML string into a dictionary.

    Uses native `tomllib` on Python 3.11+, and falls back to a custom
    fallback parser on older Python versions (< 3.11).

    Note:
        Standard library `tomllib` (Python 3.11+) strictly enforces namespace immutability
        and rejects extending inline arrays with array-of-tables syntax (`[[...]]`), whereas
        the fallback parser is lenient. TOML configuration files must avoid mixing these
        syntaxes to guarantee parser parity across all Python versions.

    Args:
        content: The raw TOML string to parse.

    Returns:
        Parsed configuration dictionary.
    """
    if HAS_TOMLLIB and tomllib is not None:
        return tomllib.loads(content)
    return _parse_toml_fallback(content)


def merge_toml(dict_a: dict, dict_b: dict) -> dict:
    """Recursively merges dictionary dict_b into dict_a, returning a new dictionary.

    Keys present only in `dict_a` or only in `dict_b` are preserved. When both dictionaries
    contain the same key, nested dictionaries are merged recursively; all other values in
    `dict_b` overwrite those in `dict_a`.

    Args:
        dict_a: Base dictionary.
        dict_b: Overriding dictionary.

    Returns:
        A new merged dictionary without mutating the inputs.
    """
    result = {}
    for key, value in dict_a.items():
        if key in dict_b:
            if isinstance(value, dict) and isinstance(dict_b[key], dict):
                result[key] = merge_toml(value, dict_b[key])
            else:
                result[key] = dict_b[key]
        else:
            result[key] = value

    for key, value in dict_b.items():
        if key not in result:
            result[key] = value

    return result


def _escape_toml_string(val: Any) -> str:
    """Escapes special characters in TOML string literals (including newlines and control characters)."""
    return (
        str(val)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _format_toml_value(val: Any) -> Optional[str]:
    """Formats a scalar Python value or list into a valid TOML value string representation."""
    if val is None:
        return None
    if isinstance(val, bool):
        return str(val).lower()
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        formatted_items = [_format_toml_value(item) for item in val]
        return f"[{', '.join(x for x in formatted_items if x is not None)}]"
    return f'"{_escape_toml_string(val)}"'


def _dump_table(table_name: str, table_dict: dict) -> List[str]:
    """Formats a TOML table and any 1-level nested subtables into lines."""
    table_lines = [f"[{table_name}]"]
    sub_table_blocks: List[str] = []

    for k, v in table_dict.items():
        if isinstance(v, dict):
            sub_lines = [f"[{table_name}.{k}]"]
            for sk, sv in v.items():
                formatted_sub = _format_toml_value(sv)
                if formatted_sub is not None:
                    sub_lines.append(f"{sk} = {formatted_sub}")
            sub_table_blocks.append("\n".join(sub_lines))
        else:
            formatted = _format_toml_value(v)
            if formatted is not None:
                table_lines.append(f"{k} = {formatted}")

    if sub_table_blocks:
        table_lines.extend(sub_table_blocks)

    return table_lines


def dump_toml(data: dict) -> str:
    """Serializes a dictionary of basic package/workspace settings back to TOML format.

    Serializes any top-level scalar values first, followed by section tables (`[name]`)
    and any 1-level nested subtables (`[name.sub]`).

    Args:
        data: Dictionary to serialize.

    Returns:
        Formatted TOML document string with a trailing newline.
    """
    lines: List[str] = []

    # 1. Top-level key-values (outside tables)
    for k, v in data.items():
        if not isinstance(v, dict):
            formatted = _format_toml_value(v)
            if formatted is not None:
                lines.append(f"{k} = {formatted}")

    # 2. Section tables and nested tables
    for table_name, table_dict in data.items():
        if isinstance(table_dict, dict):
            if lines:
                lines.append("")  # Empty line separator
            lines.extend(_dump_table(table_name, table_dict))

    return "\n".join(lines) + "\n"
