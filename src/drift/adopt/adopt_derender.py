"""3-Way Template Hunk-Map De-Rendering Engine for Drift Adoption (Layer 1a).

Architecture & Call Chain Overview:
===================================
Translates host-modified rendered files (H) back into template-compatible patches
against source templates (T) using the rendered baseline (R):

1. Template Hunk Mapping (`T <-> R`):
   - `build_template_hunk_map`: Extracts non-equal SequenceMatcher opcodes between
     Template lines (T) and Rendered baseline lines (R) as `TemplateHunk` spans.
2. 3-Way Hunk Classification & De-Rendering (`T <-> R <-> H`):
   - `_classify_template_hunk`: Classifies each `TemplateHunk` against the host diff
     opcodes (`R <-> H`) into Case 1 (`TemplateHunkClassification.EQUAL`), Case 2
     (`TemplateHunkClassification.EDIT`), or Case 3 (`TemplateHunkClassification.CONFLICT`).
   - `_apply_subline_containment`: Handles Case 2a (sub-line edits where the rendered
     value still appears uniquely inside the host edit block) vs Case 2b (full replacement).
   - `_format_conflict_base_lines`: Preserves template line-count alignment on Case 3
     border-crossing conflicts so `R_derendered` stays aligned 1-to-1 with `T`.
   - `derender_host_lines`: Produces `DerenderedPair(r_derendered, h_derendered)`.
3. Baseline Resolution & Unified Patch Synthesis:
   - `read_rendered_baseline_lines`: Reads `R` from `render/<pkg>/<rel_path>`, raising
     `SafeguardError` if the rendered baseline does not exist.
   - `generate_derendered_patch`: Synthesizes a unified diff (`R_derendered -> H_derendered`)
     with headers targeting `src_rel_path`.
"""

from __future__ import annotations

import difflib
from functools import reduce
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from ..core.exceptions import SafeguardError
from ..models.adopt_models import (
    DerenderedPair,
    TemplateHunk,
    TemplateHunkClassification,
)
from ..utils.func_utils import branch, choose


def _strip_line_endings(text: str) -> str:
    """Strips trailing CR/LF characters while preserving internal line breaks."""
    return text.rstrip("\r\n")


def build_template_hunk_map(t_lines: List[str], r_lines: List[str]) -> List[TemplateHunk]:
    """Builds the ordered list of non-equal template-to-rendered hunks (`T <-> R`)."""
    opcodes = difflib.SequenceMatcher(None, t_lines, r_lines).get_opcodes()
    return [
        TemplateHunk(t_start=t1, t_end=t2, r_start=r1, r_end=r2)
        for tag, t1, t2, r1, r2 in opcodes
        if tag != "equal"
    ]


def _classify_template_hunk(
    hunk: TemplateHunk,
    r_h_opcodes: List[Tuple[str, int, int, int, int]],
) -> Tuple[TemplateHunkClassification, Optional[int]]:
    """Classifies a TemplateHunk against `R <-> H` opcodes into (TemplateHunkClassification, idx).

    - Case 1 (TemplateHunkClassification.EQUAL, eq_idx): The template hunk is untouched by the host.
    - Case 2 (TemplateHunkClassification.EDIT, edit_idx): A single host edit opcode completely encloses the template hunk.
    - Case 3 (TemplateHunkClassification.CONFLICT, None): A multi-line template hunk is partially sliced by a host edit.
    """
    r1, r2 = hunk.r_start, hunk.r_end

    if r1 == r2:
        enclosing_edit = next(
            (
                idx
                for idx, (tag, d1, d2, _, _) in enumerate(r_h_opcodes)
                if tag != "equal" and d1 <= r1 <= d2
            ),
            None,
        )
        if enclosing_edit is not None:
            return (TemplateHunkClassification.EDIT, enclosing_edit)
        enclosing_eq = next(
            (
                idx
                for idx, (tag, d1, d2, _, _) in enumerate(r_h_opcodes)
                if tag == "equal" and d1 <= r1 <= d2
            ),
            0,
        )
        return (TemplateHunkClassification.EQUAL, enclosing_eq)

    enclosing_eq = next(
        (
            idx
            for idx, (tag, d1, d2, _, _) in enumerate(r_h_opcodes)
            if tag == "equal" and d1 <= r1 and r2 <= d2
        ),
        None,
    )
    if enclosing_eq is not None:
        return (TemplateHunkClassification.EQUAL, enclosing_eq)

    enclosing_edit = next(
        (
            idx
            for idx, (tag, d1, d2, _, _) in enumerate(r_h_opcodes)
            if tag != "equal" and d1 <= r1 and r2 <= d2
        ),
        None,
    )
    if enclosing_edit is not None:
        return (TemplateHunkClassification.EDIT, enclosing_edit)

    return (TemplateHunkClassification.CONFLICT, None)


def _format_conflict_base_lines(t_slice: List[str], r_slice: List[str]) -> List[str]:
    """Formats Case 3 conflict baseline lines to match `len(t_slice)` while keeping rendered content.

    When `len(t_slice) == len(r_slice)` (or `t_slice` is empty), returns `r_slice` directly.
    When a template span `t_slice` expands to a different number of rendered lines `r_slice`,
    folds or pads `r_slice` into `len(t_slice)` lines so `R_derendered` maintains 1-to-1 line
    number alignment with `T` for all subsequent hunks in the file.
    """
    t_len = len(t_slice)
    r_len = len(r_slice)
    if t_len == 0 or t_len == r_len:
        return list(r_slice)

    if r_len == 0:
        return [f"__DRIFT_CONFLICT_EMPTY_RENDER_{i}__\n" for i in range(t_len)]

    if t_len == 1:
        joined = " / ".join(_strip_line_endings(line) for line in r_slice)
        return [f"{joined}\n"]

    if r_len > t_len:
        head = list(r_slice[: t_len - 1])
        tail_joined = " / ".join(_strip_line_endings(line) for line in r_slice[t_len - 1 :])
        return head + [f"{tail_joined}\n"]

    padding = [f"__DRIFT_CONFLICT_PAD_{i}__\n" for i in range(t_len - r_len)]
    return list(r_slice) + padding


def _substitute_line_pair_in_text(
    block_text: str,
    t_line: str,
    r_line: str,
) -> str:
    """Substitutes a single stripped rendered line with its template line if uniquely contained."""
    r_stripped = _strip_line_endings(r_line)
    t_stripped = _strip_line_endings(t_line)
    is_unique_match = bool(r_stripped.strip()) and block_text.count(r_stripped) == 1
    return branch(
        is_unique_match,
        lambda: block_text.replace(r_stripped, t_stripped, 1),
        lambda: block_text,
    )


def _substitute_single_hunk_in_text(
    block_text: str,
    hunk: TemplateHunk,
    t_lines: List[str],
    r_lines: List[str],
) -> str:
    """Substitutes `R[r1:r2]` with `T[t1:t2]` inside `block_text` if uniquely contained (Case 2a).

    When a multi-line hunk has 1-to-1 line correspondence (`t_len == r_len > 1`) and the
    entire multi-line block does not match as a unit (e.g. one line had a sub-line edit while
    an adjacent line had its value replaced), falls back to per-line containment across the
    paired lines.
    """
    t_slice = t_lines[hunk.t_start : hunk.t_end]
    r_slice = r_lines[hunk.r_start : hunk.r_end]
    r_stripped = _strip_line_endings("".join(r_slice))
    t_stripped = _strip_line_endings("".join(t_slice))
    if bool(r_stripped.strip()) and block_text.count(r_stripped) == 1:
        return block_text.replace(r_stripped, t_stripped, 1)

    if len(t_slice) == len(r_slice) and len(t_slice) > 1:
        return reduce(
            lambda acc, pair: _substitute_line_pair_in_text(acc, pair[0], pair[1]),
            zip(t_slice, r_slice),
            block_text,
        )

    return block_text


def _apply_subline_containment(
    h_block: List[str],
    edit_hunks: List[TemplateHunk],
    t_lines: List[str],
    r_lines: List[str],
) -> List[str]:
    """Applies Case 2a sub-line de-rendering or Case 2b full replacement to a host edit block."""
    if not edit_hunks or not h_block:
        return list(h_block)

    initial_text = "".join(h_block)
    replaced_text = reduce(
        lambda acc, hunk: _substitute_single_hunk_in_text(acc, hunk, t_lines, r_lines),
        edit_hunks,
        initial_text,
    )
    return replaced_text.splitlines(keepends=True)


def _does_opcode_overlap_conflict(
    d1: int,
    d2: int,
    conflict_hunks: List[TemplateHunk],
) -> bool:
    """Checks whether an `R <-> H` edit span `[d1, d2)` overlaps any Case 3 conflict hunk."""
    return any(
        choose(
            d1 == d2,
            hk.r_start <= d1 <= hk.r_end,
            max(d1, hk.r_start) < min(d2, hk.r_end),
        )
        for hk in conflict_hunks
    )


def _de_render_equal_block(
    d1: int,
    d2: int,
    h1: int,
    h2: int,
    eq_hunks: List[TemplateHunk],
    t_lines: List[str],
    r_lines: List[str],
    h_lines: List[str],
    conflict_r_indices: FrozenSet[int] = frozenset(),
) -> List[Tuple[str, bool]]:
    """Replaces untouched Case 1 template hunks inside an 'equal' `R <-> H` span with `T[t1:t2]`,
    returning `(line, is_conflict)` pairs where untouched lines inside Case 3 conflict hunks are
    tagged `is_conflict=True` so they never act as false context anchors in `T`.
    """
    if not eq_hunks:
        return [(r_lines[r_idx], r_idx in conflict_r_indices) for r_idx in range(d1, d2)]

    ordered_hunks = sorted(eq_hunks, key=lambda hk: (hk.r_start, hk.t_start))

    def _step(
        state: Tuple[int, List[Tuple[str, bool]]],
        hunk: TemplateHunk,
    ) -> Tuple[int, List[Tuple[str, bool]]]:
        cursor, acc = state
        prefix = choose(
            hunk.r_start > cursor,
            [(r_lines[r_idx], r_idx in conflict_r_indices) for r_idx in range(cursor, hunk.r_start)],
            [],
        )
        t_chunk = [(line, False) for line in t_lines[hunk.t_start : hunk.t_end]]
        return (
            max(cursor, hunk.r_end),
            acc + prefix + t_chunk,
        )

    final_cursor, accumulated = reduce(_step, ordered_hunks, (d1, []))
    trailing = choose(
        final_cursor < d2,
        [(r_lines[r_idx], r_idx in conflict_r_indices) for r_idx in range(final_cursor, d2)],
        [],
    )
    return accumulated + trailing


def derender_host_lines(
    t_lines: List[str],
    r_lines: List[str],
    h_lines: List[str],
) -> DerenderedPair:
    """De-renders Rendered baseline (`R`) and Host (`H`) lines against Template (`T`).

    Returns `DerenderedPair(r_derendered, h_derendered, r_conflict_indices, h_conflict_indices)` where:
    - Case 1 (Untouched template hunk): Replaced with `T[t1:t2]` in both `R'` and `H'`.
    - Case 2a (Sub-line edit containing rendered value): Replaced with `T[t1:t2]` in `R'`
      and sub-line substituted in `H'`.
    - Case 2b (Full replacement/deletion of template hunk): Replaced with `T[t1:t2]` in `R'`
      and kept as host's new lines in `H'`.
    - Case 3 (Partial/border-crossing edit across multi-line template hunk): Kept as rendered
      conflict baseline in `R'` and host lines in `H'` with `r_conflict_indices` and
      `h_conflict_indices` populated so unified diff generation anchors strictly on the
      outer template lines of `T` and `git merge-file --diff3` displays all three versions
      (`<<<<<<< T ||||||| R ======= H >>>>>>>`).
    """
    t_r_opcodes = difflib.SequenceMatcher(None, t_lines, r_lines).get_opcodes()
    raw_r_h_opcodes = difflib.SequenceMatcher(None, r_lines, h_lines).get_opcodes()
    r_h_opcodes: List[Tuple[str, int, int, int, int]] = choose(
        bool(raw_r_h_opcodes),
        raw_r_h_opcodes,
        [("equal", 0, 0, 0, 0)],
    )
    non_equal_hunks = build_template_hunk_map(t_lines, r_lines)
    classifications: Dict[TemplateHunk, Tuple[TemplateHunkClassification, Optional[int]]] = {
        hunk: _classify_template_hunk(hunk, r_h_opcodes) for hunk in non_equal_hunks
    }
    conflict_hunks = [
        hk
        for hk in non_equal_hunks
        if classifications[hk][0] == TemplateHunkClassification.CONFLICT
    ]
    conflict_r_indices: FrozenSet[int] = frozenset(
        r_idx
        for hk in conflict_hunks
        for r_idx in range(hk.r_start, hk.r_end)
    ) | frozenset(
        r_idx
        for tag, d1, d2, _, _ in r_h_opcodes
        if tag != "equal" and _does_opcode_overlap_conflict(d1, d2, conflict_hunks)
        for r_idx in range(d1, d2)
    )

    r_tagged: List[Tuple[str, bool]] = [
        item
        for tag, t1, t2, r1, r2 in t_r_opcodes
        for item in branch(
            tag == "equal",
            lambda r1=r1, r2=r2: [
                (r_lines[r_idx], r_idx in conflict_r_indices) for r_idx in range(r1, r2)
            ],
            lambda t1=t1, t2=t2, r1=r1, r2=r2: branch(
                classifications[TemplateHunk(t1, t2, r1, r2)][0] == TemplateHunkClassification.CONFLICT,
                lambda t1=t1, t2=t2, r1=r1, r2=r2: [
                    (line, True)
                    for line in _format_conflict_base_lines(t_lines[t1:t2], r_lines[r1:r2])
                ],
                lambda t1=t1, t2=t2: [(line, False) for line in t_lines[t1:t2]],
            ),
        )
    ]

    eq_hunks_by_idx: Dict[int, List[TemplateHunk]] = {
        idx: [
            hk
            for hk in non_equal_hunks
            if classifications[hk] == (TemplateHunkClassification.EQUAL, idx)
        ]
        for idx in range(len(r_h_opcodes))
    }
    edit_hunks_by_idx: Dict[int, List[TemplateHunk]] = {
        idx: [
            hk
            for hk in non_equal_hunks
            if classifications[hk] == (TemplateHunkClassification.EDIT, idx)
        ]
        for idx in range(len(r_h_opcodes))
    }

    h_tagged: List[Tuple[str, bool]] = [
        item
        for idx, (tag, d1, d2, h1, h2) in enumerate(r_h_opcodes)
        for item in branch(
            tag == "equal",
            lambda idx=idx, d1=d1, d2=d2, h1=h1, h2=h2: _de_render_equal_block(
                d1,
                d2,
                h1,
                h2,
                eq_hunks_by_idx.get(idx, []),
                t_lines,
                r_lines,
                h_lines,
                conflict_r_indices,
            ),
            lambda idx=idx, d1=d1, d2=d2, h1=h1, h2=h2: [
                (line, _does_opcode_overlap_conflict(d1, d2, conflict_hunks))
                for line in _apply_subline_containment(
                    h_lines[h1:h2], edit_hunks_by_idx.get(idx, []), t_lines, r_lines
                )
            ],
        )
    ]

    return DerenderedPair(
        r_derendered=[line for line, _ in r_tagged],
        h_derendered=[line for line, _ in h_tagged],
        r_conflict_indices=frozenset(i for i, (_, is_conf) in enumerate(r_tagged) if is_conf),
        h_conflict_indices=frozenset(j for j, (_, is_conf) in enumerate(h_tagged) if is_conf),
    )


def _format_range_unified(start: int, stop: int) -> str:
    """Formats a 0-based `[start, stop)` slice as a 1-based unified diff range."""
    length = stop - start
    if length == 1:
        return f"{start + 1}"
    if length == 0:
        return f"{start},0"
    return f"{start + 1},{length}"


def _format_opcode_lines(
    tag: str,
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    r_lines: List[str],
    h_lines: List[str],
) -> List[str]:
    """Formats a single SequenceMatcher opcode into unified diff body lines."""
    if tag == "equal":
        return [f" {line}" for line in r_lines[i1:i2]]
    removed = choose(tag in ("replace", "delete"), [f"-{line}" for line in r_lines[i1:i2]], [])
    added = choose(tag in ("replace", "insert"), [f"+{line}" for line in h_lines[j1:j2]], [])
    return [*removed, *added]


def _build_anchored_unified_diff(
    pair: DerenderedPair,
    fromfile: str,
    tofile: str,
    n: int = 3,
) -> str:
    """Builds a unified diff where Case 3 conflict spans never match as equal context lines.

    By replacing conflict-span indices in the SequenceMatcher key lists with distinct sentinels,
    any untouched rendered lines inside a Case 3 conflict hunk are prevented from matching as
    `' '` context lines. This guarantees that every diff hunk is anchored strictly on the
    outer template lines of `T`.
    """
    r_lines = pair.r_derendered
    h_lines = pair.h_derendered
    if r_lines == h_lines and not pair.r_conflict_indices and not pair.h_conflict_indices:
        return ""

    r_keys = [
        choose(i in pair.r_conflict_indices, f"\x00__DRIFT_CONFLICT_R_{i}__\n", line)
        for i, line in enumerate(r_lines)
    ]
    h_keys = [
        choose(j in pair.h_conflict_indices, f"\x00__DRIFT_CONFLICT_H_{j}__\n", line)
        for j, line in enumerate(h_lines)
    ]
    if r_keys == h_keys:
        return ""

    groups = list(difflib.SequenceMatcher(None, r_keys, h_keys).get_grouped_opcodes(n))
    if not groups:
        return ""

    header_lines = [f"--- {fromfile}\n", f"+++ {tofile}\n"]
    body_lines = [
        line
        for group in groups
        for line in [
            f"@@ -{_format_range_unified(group[0][1], group[-1][2])} "
            f"+{_format_range_unified(group[0][3], group[-1][4])} @@\n",
            *(
                op_line
                for tag, i1, i2, j1, j2 in group
                for op_line in _format_opcode_lines(tag, i1, i2, j1, j2, r_lines, h_lines)
            ),
        ]
    ]
    return "".join([*header_lines, *body_lines])


def read_rendered_baseline_lines(
    render_pkg_dir: Path,
    rel_rendered_path: Path,
) -> List[str]:
    """Reads the rendered baseline `R` from `render/<pkg>/<rel_rendered_path>`.

    Raises:
        SafeguardError: If the rendered baseline file does not exist in `render_pkg_dir`.
    """
    render_candidate = render_pkg_dir / rel_rendered_path
    if not render_candidate.is_file() or render_candidate.is_symlink():
        raise SafeguardError(
            f"Cannot plan adoption when the changed file '{rel_rendered_path}' is a template and its "
            f"rendered baseline '{render_candidate}' does not exist. "
            f"Please run 'drift adopt' with '--with-render'."
        )
    return render_candidate.read_text(encoding="utf-8").splitlines(keepends=True)


def generate_derendered_patch(
    target_file: Path,
    patch_from: Path,
    render_pkg_dir: Path,
    rel_rendered_path: Path,
    src_rel_path: Path,
) -> str:
    """Generates a unified diff (`R_derendered -> H_derendered`) targeting `src_rel_path`.

    Returns `""` only when `R_derendered == H_derendered` (e.g. permission-only change).
    Raises `SafeguardError` via `read_rendered_baseline_lines` if the rendered baseline in
    `render_pkg_dir / rel_rendered_path` does not exist, and lets `OSError` / `UnicodeDecodeError`
    propagate if `target_file` or `patch_from` cannot be read.
    """
    t_lines = target_file.read_text(encoding="utf-8").splitlines(keepends=True)
    h_lines = patch_from.read_text(encoding="utf-8").splitlines(keepends=True)
    r_lines = read_rendered_baseline_lines(
        render_pkg_dir=render_pkg_dir,
        rel_rendered_path=rel_rendered_path,
    )

    pair = derender_host_lines(t_lines, r_lines, h_lines)
    posix_target = src_rel_path.as_posix()
    return _build_anchored_unified_diff(
        pair,
        fromfile=f"a/{posix_target}",
        tofile=f"b/{posix_target}",
    )
