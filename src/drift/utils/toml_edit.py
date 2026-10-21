"""Format-preserving, line-surgical TOML text editing for Drift configuration files.

Edits raw TOML text without a parse-and-dump round trip, so user comments, blank lines,
key order, `${VAR}` template expressions, quoting style, and line endings survive
byte-for-byte outside of the edited lines. Lexical scanning reuses the tokenizer state of
`toml_utils` (`_ParserState`, `_strip_line_comment`) so that commented-out headers,
header-like text inside multiline strings, and multiline arrays are never mistaken for
section headers.

This module is pure (text in, text out) and raises `ValueError` on unsupported shapes;
callers in higher layers translate failures into domain errors.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 3: Public Editing Entry Point
    set_toml_table_bools(text, table_keys, updates, create_table_if_missing) -> TomlBoolEditResult
        - Applies each boolean update sequentially via _apply_single_bool_update
        - Appends a new table block at EOF when the table is absent (opt-in)

Layer 2: Structural Discovery & Single-Key Edits
    scan_toml_logical_lines(text) -> List[TomlLogicalLine]
        - Groups physical lines into logical lines (headers, assignments, other)
    find_toml_table_span(logical_lines, table_keys) -> Optional[TomlTableSpan]
        - Locates a `[table]` header and the assignments that belong to it
    _apply_single_bool_update(text, table_keys, key, value, create_table_if_missing)
        - Rewrites the value token of an existing key, or inserts a new key line

Layer 1: Lexical Helpers
    split_toml_dotted_key(raw) -> List[str]
        - Splits dotted keys / header names on unquoted dots and unquotes segments
    format_toml_key(key) -> str
        - Emits bare keys when possible, otherwise basic quoted keys
    _find_unquoted(raw, targets, start) -> int
        - Finds the first target character outside quoted strings
    _split_physical_lines(text) -> List[Tuple[str, str]]
        - Splits text into (body, line_ending) pairs preserving CRLF / missing final EOL
===============================================================================
"""

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .func_utils import branch, choose
from .toml_utils import (
    _ParserState,
    _escape_toml_string,
    _find_unquoted,
    _strip_line_comment,
    _unquote_simple_key,
    parse_toml_value,
)

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TomlEditAction(str, Enum):
    """Outcome of a single key edit."""
    UPDATED = "updated"
    INSERTED = "inserted"
    UNCHANGED = "unchanged"


class TomlLineKind(str, Enum):
    """Classification of a logical TOML line."""
    HEADER = "header"
    ARRAY_HEADER = "array_header"
    ASSIGNMENT = "assignment"
    OTHER = "other"


@dataclass(frozen=True)
class TomlLogicalLine:
    """A logical TOML line spanning physical lines [start, end] (inclusive)."""
    start: int
    end: int
    kind: TomlLineKind
    table_keys: Tuple[str, ...] = ()
    key_path: Tuple[str, ...] = ()

    @property
    def is_multiline(self) -> bool:
        return self.end != self.start


@dataclass(frozen=True)
class TomlTableSpan:
    """A located `[table]` header and the assignment logical lines belonging to it."""
    header: TomlLogicalLine
    assignments: Tuple[TomlLogicalLine, ...] = field(default_factory=tuple)

    @property
    def insert_after(self) -> int:
        """Physical line index after which new keys are inserted."""
        return branch(bool(self.assignments), lambda: self.assignments[-1].end, lambda: self.header.end)

    def find_assignment(self, key: str) -> Optional[TomlLogicalLine]:
        return next((a for a in self.assignments if a.key_path == (key,)), None)

    def find_dotted_conflict(self, key: str) -> Optional[TomlLogicalLine]:
        return next(
            (a for a in self.assignments if len(a.key_path) > 1 and a.key_path[0] == key),
            None,
        )


@dataclass(frozen=True)
class TomlBoolEditResult:
    """Edited text and per-key actions."""
    text: str
    actions: Dict[str, TomlEditAction]

    @property
    def changed(self) -> bool:
        return any(a != TomlEditAction.UNCHANGED for a in self.actions.values())


# =============================================================================
# Layer 1: Lexical Helpers
# =============================================================================


def _split_physical_lines(text: str) -> List[Tuple[str, str]]:
    """Splits text into (body, line_ending) pairs, preserving CRLF/LF and a missing final EOL."""
    return [_split_line_ending(raw) for raw in text.splitlines(keepends=True)]


def _split_line_ending(raw: str) -> Tuple[str, str]:
    """Splits a physical line into (body, line_ending)."""
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith(("\n", "\r")):
        return raw[:-1], raw[-1]
    return raw, ""


def _detect_line_ending(text: str) -> str:
    return choose("\r\n" in text, "\r\n", "\n")


def _unquote_key_segment(segment: str) -> str:
    return _unquote_simple_key(segment.strip())


def split_toml_dotted_key(raw: str) -> List[str]:
    """Splits a TOML key or table name on unquoted dots and unquotes each segment."""
    segments: List[str] = []
    rest = raw
    while True:
        idx = _find_unquoted(rest, ".")
        if idx < 0:
            segments.append(_unquote_key_segment(rest))
            return segments
        segments.append(_unquote_key_segment(rest[:idx]))
        rest = rest[idx + 1:]


def format_toml_key(key: str) -> str:
    """Formats a key as a bare key when possible, otherwise as a basic quoted string."""
    if not key:
        raise ValueError("TOML key must be a non-empty string.")
    return branch(bool(_BARE_KEY_RE.match(key)), lambda: key, lambda: f'"{_escape_toml_string(key)}"')


def _classify_logical_line(logical: str, start: int, end: int) -> TomlLogicalLine:
    if logical.startswith("[[") and logical.endswith("]]"):
        keys = tuple(split_toml_dotted_key(logical[2:-2]))
        return TomlLogicalLine(start, end, TomlLineKind.ARRAY_HEADER, table_keys=keys)
    if logical.startswith("[") and logical.endswith("]"):
        keys = tuple(split_toml_dotted_key(logical[1:-1]))
        return TomlLogicalLine(start, end, TomlLineKind.HEADER, table_keys=keys)
    eq = _find_unquoted(logical, "=")
    if eq > 0:
        key_path = tuple(split_toml_dotted_key(logical[:eq]))
        return TomlLogicalLine(start, end, TomlLineKind.ASSIGNMENT, key_path=key_path)
    return TomlLogicalLine(start, end, TomlLineKind.OTHER)


# =============================================================================
# Layer 2: Structural Discovery & Single-Key Edits
# =============================================================================


def scan_toml_logical_lines(text: str) -> List[TomlLogicalLine]:
    """Groups physical lines into classified logical lines.

    Comment-only and blank lines are omitted. Multi-line values (arrays, inline tables,
    multiline strings) are grouped into a single logical line spanning [start, end].

    Raises:
        ValueError: If the text ends with unclosed brackets or quotes.
    """
    physical = _split_physical_lines(text)
    state = _ParserState()
    result: List[TomlLogicalLine] = []
    buffer: List[str] = []
    start: Optional[int] = None

    for idx, (body, _eol) in enumerate(physical):
        cleaned = _strip_line_comment(body, state)
        if not cleaned and not buffer and not state.in_multiline:
            continue
        if start is None:
            start = idx
        buffer.append(cleaned)
        if not state.is_clean:
            continue
        logical = "\n".join(buffer).strip()
        buffer = []
        line_start, start = start, None
        if logical:
            result.append(_classify_logical_line(logical, line_start, idx))

    if not state.is_clean:
        raise ValueError("TOML format error: unclosed brackets or quotes at end of document.")
    return result


def find_toml_table_span(
    logical_lines: Sequence[TomlLogicalLine],
    table_keys: Sequence[str],
) -> Optional[TomlTableSpan]:
    """Locates the `[table_keys]` header and its assignments (up to the next header)."""
    target = tuple(table_keys)
    header_positions = [
        i for i, line in enumerate(logical_lines)
        if line.kind == TomlLineKind.HEADER and line.table_keys == target
    ]
    if not header_positions:
        return None
    pos = header_positions[0]
    following = logical_lines[pos + 1:]
    next_header = next(
        (i for i, line in enumerate(following)
         if line.kind in (TomlLineKind.HEADER, TomlLineKind.ARRAY_HEADER)),
        len(following),
    )
    assignments = tuple(
        line for line in following[:next_header] if line.kind == TomlLineKind.ASSIGNMENT
    )
    return TomlTableSpan(header=logical_lines[pos], assignments=assignments)


def _format_bool(value: bool) -> str:
    return choose(value, "true", "false")


def _rewrite_value_in_line(body: str, new_value: str) -> Tuple[str, str]:
    """Replaces the value token in a single-line `key = value  # comment` body.

    Returns:
        (new_body, old_value_text)
    """
    eq = _find_unquoted(body, "=")
    if eq < 0:
        raise ValueError(f"Cannot locate '=' in TOML assignment line: {body!r}")
    value_start = eq + 1
    while value_start < len(body) and body[value_start] in " \t":
        value_start += 1
    comment = _find_unquoted(body, "#", value_start)
    value_end = choose(comment < 0, len(body), comment)
    while value_end > value_start and body[value_end - 1] in " \t":
        value_end -= 1
    old_value = body[value_start:value_end]
    return body[:value_start] + new_value + body[value_end:], old_value


def _leading_whitespace(body: str) -> str:
    return body[: len(body) - len(body.lstrip(" \t"))]


def _append_table_block(text: str, table_keys: Sequence[str], key: str, value: bool) -> str:
    eol = _detect_line_ending(text)
    header = "[" + ".".join(format_toml_key(k) for k in table_keys) + "]"
    block = f"{header}{eol}{format_toml_key(key)} = {_format_bool(value)}{eol}"
    if not text:
        return block
    prefix = choose(text.endswith(("\n", "\r")), text, text + eol)
    return prefix + eol + block


def _apply_single_bool_update(
    text: str,
    table_keys: Sequence[str],
    key: str,
    value: bool,
    create_table_if_missing: bool,
) -> Tuple[str, TomlEditAction]:
    table_name = ".".join(table_keys)
    span = find_toml_table_span(scan_toml_logical_lines(text), table_keys)
    if span is None:
        if not create_table_if_missing:
            raise ValueError(
                f"Cannot locate a '[{table_name}]' section header. "
                "Please edit the configuration file manually."
            )
        return _append_table_block(text, table_keys, key, value), TomlEditAction.INSERTED

    dotted = span.find_dotted_conflict(key)
    if dotted is not None:
        raise ValueError(
            f"Key '{key}' under '[{table_name}]' uses dotted-key syntax (line {dotted.start + 1}); "
            "please edit the configuration file manually."
        )

    physical = _split_physical_lines(text)
    existing = span.find_assignment(key)
    if existing is not None:
        if existing.is_multiline:
            raise ValueError(
                f"Value of '{key}' under '[{table_name}]' spans multiple lines "
                f"(lines {existing.start + 1}-{existing.end + 1}); please edit the configuration file manually."
            )
        body, eol = physical[existing.start]
        new_body, old_value = _rewrite_value_in_line(body, _format_bool(value))
        try:
            parsed_old = parse_toml_value(old_value)
        except ValueError:
            parsed_old = None
        if isinstance(parsed_old, bool) and parsed_old == value:
            return text, TomlEditAction.UNCHANGED
        physical[existing.start] = (new_body, eol)
        return "".join(b + e for b, e in physical), TomlEditAction.UPDATED

    anchor = span.insert_after
    anchor_body, anchor_eol = physical[anchor]
    indent = _section_indent(physical, span)
    new_line = f"{indent}{format_toml_key(key)} = {_format_bool(value)}"
    eol = _detect_line_ending(text)
    if anchor_eol:
        physical.insert(anchor + 1, (new_line, anchor_eol))
    else:
        physical[anchor] = (anchor_body, eol)
        physical.insert(anchor + 1, (new_line, ""))
    return "".join(b + e for b, e in physical), TomlEditAction.INSERTED


def _section_indent(physical: Sequence[Tuple[str, str]], span: TomlTableSpan) -> str:
    """Returns the indentation of the last assignment in the span ('' when the table is empty)."""
    if not span.assignments:
        return ""
    return _leading_whitespace(physical[span.assignments[-1].start][0])


# =============================================================================
# Layer 3: Public Editing Entry Point
# =============================================================================


def set_toml_table_bools(
    text: str,
    table_keys: Sequence[str],
    updates: Mapping[str, bool],
    create_table_if_missing: bool = False,
) -> TomlBoolEditResult:
    """Sets boolean keys inside a `[table]` section while preserving all other text.

    Rules:
        - Existing key with the same boolean value: untouched (UNCHANGED).
        - Existing single-line key with another value: only the value token is rewritten,
          preserving indentation, key quoting, `=` spacing, and trailing comments (UPDATED).
        - Missing key: inserted after the last assignment of the section (or directly after
          the header if the section has no keys), copying its indentation (INSERTED).
        - Missing section: appended at EOF when `create_table_if_missing`, else ValueError.

    Raises:
        ValueError: On missing sections (unless opted in), multi-line existing values,
            dotted-key conflicts, or malformed delimiters.
    """
    if not table_keys:
        raise ValueError("table_keys must be non-empty.")
    current = text
    actions: Dict[str, TomlEditAction] = {}
    for key, value in updates.items():
        if not isinstance(value, bool):
            raise ValueError(f"Value for '{key}' must be a boolean, got {type(value).__name__}.")
        current, action = _apply_single_bool_update(
            current, table_keys, key, value, create_table_if_missing
        )
        actions[key] = action
    return TomlBoolEditResult(text=current, actions=actions)
