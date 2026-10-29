"""Word-Level Tokenization, Hunk Fission, and Single-Line Calculus for Drift Adoption (Layer 1a-1).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Purpose:
    Standard line-level diff (`SequenceMatcher` on raw lines) treats any modified line as
    completely opaque—lumping adjacent modified, inserted, and loop-expanded lines into one
    monolithic `"replace"` block. When adopting host drift (`H`) back into a template (`T`)
    via the rendered baseline (`R`), coarse `"replace"` blocks hide which specific template
    lines correspond to which rendered lines and whether a host edit on a templated line
    actually touched a template variable (`{{ ... }}` / `${VAR}`) or only edited static text.

    This module decomposes coarse `T -> R` and `R -> H` diffs into fine-grained atomic
    `DiffUnit`s and performs token-level closed-interval calculus on aligned `1 <-> 1 <-> 1`
    single-line edits.

Pipeline Architecture:
    1. Lossless Word-Level Line Tokenization:
        tokenize_line(line) [Layer 1]
            - Strips trailing line ending (`\r\n` / `\n`) while preserving `raw_line` and `line_ending`
            - Splits line into contiguous `TokenKind.WORD` (`\w+`), `TokenKind.PUNCT` (`[^\w\s]`),
              and `TokenKind.SPACE` (`\s+`) tokens via `_classify_token`
            - Caches `word_set` (`frozenset` of `WORD` tokens) for O(1) disjoint-line pruning
            -> Returns TokenizedLine

    2. Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching:
        find_monotonic_line_matches(a_lines, b_lines) [Layer 2]
            - Evaluates pairwise line similarity via compute_skeleton_match(line_a, line_b):
                * O(1) Word-Set Pruning: rejects pairs with disjoint `word_set` (unless both lack words)
                * Space-Bridge Coalescing (_coalesce_space_bridged_opcodes): merges non-equal token
                  opcodes separated only by `SPACE` tokens (_is_space_only_equal_opcode) so internal
                  spaces inside template tags (`{{ var }}` -> `hello world`) form a single slot
                * Skeleton Gate: requires >= 1 shared `WORD` token (or >= 2 `PUNCT` tokens if wordless)
                * Slot-Normalized Score: computes `S = E_skel / (E_skel + N_slots)` where
                  `E_skel = 1.0 * n_word + 0.25 * n_punct` and `N_slots` counts non-space replacement
                  spans (_has_non_space_tokens); rejects pairs with `S < 0.5`
                * Skeleton Signature: builds a tuple of static tokens with `"\x00"` placeholders for
                  each variable slot
                -> Returns SkeletonMatch(is_valid, score, signature)
            - Bidirectional Uniqueness Filtering (_filter_unique_skeleton_matrix):
                * Invalidates any candidate `(i, j)` whose `signature` appears more than once in row `i`
                  or column `j`, preventing false 1-to-1 pairings across loop-unrolled lines (`{% for %}`)
            - 2D Dynamic Programming (_dp_row / _dp_cell):
                * Selects the highest-scoring strictly monotonic (`i_1 < i_2` and `j_1 < j_2`) set of
                  valid 1-to-1 line pairs within a `"replace"` block
            -> Returns List[Tuple[int, int]] of matched relative `(rel_i, rel_j)` line indices

    3. Second-Pass Sentinel Hunk Fission (`T -> R` and `R -> H`):
        fission_diff_units(src_tokens, dst_tokens, relation) [Layer 3]
            - Runs first-pass line-level `SequenceMatcher` on `raw_line` sequences:
                * `"equal"` spans -> expands into 1-to-1 `DiffUnitType.EQUAL` units (1 line each)
                * `"insert"` / `"delete"` spans -> wraps directly as `INSERT` / `DELETE` `DiffUnit`s
                * `"replace"` spans -> delegates to _fission_replace_block:
                    - Fast-path `1 <-> 1`: classifies as `SINGLE_EDIT` if `compute_skeleton_match` is
                      valid, else `BLOCK_EDIT`
                    - Multi-line `M <-> N`: finds 1-to-1 anchors via `find_monotonic_line_matches`,
                      replaces matched line pairs with unique `"\x00__DRIFT_MATCH_k__\n"` sentinels,
                      and runs a second-pass `SequenceMatcher` so unmatched gaps between anchors split
                      cleanly into minimal `SINGLE_EDIT`, `BLOCK_EDIT`, `INSERT`, and `DELETE` units
            -> Returns Tuple[DiffUnit, ...]

    4. Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus:
        refine_single_line_units(tr_units, rh_units, t_tokens, r_tokens, h_tokens) [Layer 4]
            - Finds all `R` line indices where both `TR` and `RH` have a 1-to-1 `SINGLE_EDIT` unit
            - For each shared `1 <-> 1 <-> 1` line triple `(T_i, R_r, H_j)`, runs
              try_derender_single_line(t_line, r_line, h_line):
                * Extracts template variable intervals `[c_1, c_2]` on `R_r` (with space-bridge coalescing)
                  and host edit intervals `[p_1, p_2]` on `R_r`
                * Closed-Interval Overlap Check: tests `max(p_1, c_1) <= min(p_2, c_2)` on token boundaries
                  so even zero-width insertions immediately adjacent to a variable slot are caught
                * Clean Disjoint Edit (Case 1.2): splices both template variable tokens and host static
                  edits onto `R_r` in a single left-to-right pass -> returns synthesized line `H'`
                * Overlapping Edit (Case 1.3): returns `None`
            - Upgrades clean `RH` units to `DiffUnitType.SINGLE_SKEL_EDIT` with `derendered_lines=(H',)`
              and marks colliding `TR` and `RH` units with `is_conflict=True`
            -> Returns Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Lossless Word-Level Line Tokenization
        _classify_token
        tokenize_line
    Layer 2: Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching
        _is_space_only_equal_opcode
        _coalesce_space_bridged_opcodes
        _has_non_space_tokens
        compute_skeleton_match
        _filter_unique_skeleton_matrix
        find_monotonic_line_matches
    Layer 3: Second-Pass Sentinel Hunk Fission
        _fission_replace_block
        fission_diff_units
    Layer 4: Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus
        try_derender_single_line
        refine_single_line_units
===============================================================================
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import difflib
from functools import reduce
import re
from typing import Dict, List, Optional, Sequence, Tuple

from ..models.adopt_models import (
    DiffRelation,
    DiffUnit,
    DiffUnitType,
    SkeletonMatch,
    TokenizedLine,
    TokenKind,
)
from ..utils.func_utils import branch, choose

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
_WORD_Weight = 1.0
_PUNCT_WEIGHT = 0.25
_SLOT_PENALTY = 1.0
_SIMILARITY_THRESHOLD = 0.5
_INVALID_SKELETON_MATCH = SkeletonMatch(is_valid=False, score=0.0, signature=())

_Opcode = Tuple[str, int, int, int, int]


# ==============================================================================
# Layer 1: Lossless Word-Level Line Tokenization
# ==============================================================================


def _classify_token(token: str) -> TokenKind:
    """Classifies a non-empty regex token into `WORD` (`\\w+`), `SPACE` (`\\s+`), or `PUNCT` (`[^\\w\\s]`)."""
    first = token[0]
    if first.isspace():
        return TokenKind.SPACE
    return choose(first.isalnum() or first == "_", TokenKind.WORD, TokenKind.PUNCT)


def tokenize_line(line: str) -> TokenizedLine:
    """Losslessly partitions a single line into word, whitespace, and punctuation tokens.

    Separates the trailing line ending (`\\r\\n` or `\\n`) from the line body, splits the body
    into contiguous `WORD`, `SPACE`, and `PUNCT` tokens so `"".join(tokens) + line_ending == line`,
    and precomputes `word_set` for fast O(1) word-overlap pruning during skeleton matching.

    Args:
        line: Raw line string (with or without trailing newline characters).

    Returns:
        A `TokenizedLine` containing `raw_line`, `stripped_line`, `line_ending`, `tokens`,
        `token_kinds`, and `word_set`.
    """
    stripped = line.rstrip("\r\n")
    line_ending = line[len(stripped) :]
    tokens = tuple(_TOKEN_RE.findall(stripped))
    token_kinds = tuple(map(_classify_token, tokens))
    word_set = frozenset(
        tok for tok, kind in zip(tokens, token_kinds) if kind == TokenKind.WORD
    )
    return TokenizedLine(
        raw_line=line,
        stripped_line=stripped,
        line_ending=line_ending,
        tokens=tokens,
        token_kinds=token_kinds,
        word_set=word_set,
    )


# ==============================================================================
# Layer 2: Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching
# ==============================================================================


def _is_space_only_equal_opcode(op: _Opcode, line_a: TokenizedLine) -> bool:
    """Returns True if `op` is an `'equal'` token opcode consisting exclusively of `SPACE` tokens."""
    tag, i1, i2, _, _ = op
    return tag == "equal" and all(
        kind == TokenKind.SPACE for kind in line_a.token_kinds[i1:i2]
    )


def _coalesce_space_bridged_opcodes(
    opcodes: Sequence[_Opcode],
    line_a: TokenizedLine,
    line_b: TokenizedLine,
) -> List[_Opcode]:
    """Merges non-equal token opcodes that are separated only by whitespace-only `'equal'` spans.

    Why this is needed:
        When a template expression like `{{ value1 }}` renders to a multi-word string like
        `hello world`, `SequenceMatcher` often aligns the internal space inside `{{ value1 }}`
        with the space between `hello` and `world`, splitting a single logical template slot
        into two replacement spans (`"{{" + "value1" -> "hello"` and `"}}" -> "world"`).
        Coalescing across space-only bridges guarantees the entire template expression is
        counted and bounded as one contiguous slot.
    """
    if not opcodes:
        return []

    def _step(acc: List[_Opcode], op: _Opcode) -> List[_Opcode]:
        if not acc:
            return [op]
        tag, i1, i2, j1, j2 = op
        if tag != "equal":
            if (
                len(acc) >= 2
                and acc[-2][0] != "equal"
                and _is_space_only_equal_opcode(acc[-1], line_a)
            ):
                prev_non_eq = acc[-2]
                merged: _Opcode = ("replace", prev_non_eq[1], i2, prev_non_eq[3], j2)
                return [*acc[:-2], merged]
            if acc[-1][0] != "equal":
                prev = acc[-1]
                return [*acc[:-1], ("replace", prev[1], i2, prev[3], j2)]
        return [*acc, op]

    return reduce(_step, opcodes, [])


def _has_non_space_tokens(
    kinds_a: Sequence[TokenKind],
    kinds_b: Sequence[TokenKind],
) -> bool:
    """Returns True if either token-kind slice contains at least one non-whitespace (`WORD` or `PUNCT`) token."""
    return any(k != TokenKind.SPACE for k in kinds_a) or any(
        k != TokenKind.SPACE for k in kinds_b
    )


def compute_skeleton_match(
    line_a: TokenizedLine,
    line_b: TokenizedLine,
) -> SkeletonMatch:
    """Evaluates whether two lines share the same structural skeleton and computes their similarity.

    Algorithm:
        1. O(1) Disjoint-Word Prune: If either line has words and their `word_set`s are disjoint,
           reject immediately.
        2. Token Diff & Space-Bridge Coalescing: Aligns token sequences via `SequenceMatcher` and
           merges non-equal spans bridged only by whitespace (`_coalesce_space_bridged_opcodes`).
        3. Structural Gate: Requires at least 1 shared `WORD` token (or at least 2 shared `PUNCT`
           tokens when both lines contain zero words).
        4. Slot-Normalized Score: Computes `S = E_skel / (E_skel + N_slots)`, where
           `E_skel = 1.0 * n_word + 0.25 * n_punct` and `N_slots` is the count of non-whitespace
           replacement slots (so expanding one `{{ var }}` into a 10-token value still incurs
           only 1 slot penalty). Requires `S >= 0.5`.
        5. Skeleton Signature: Emits the tuple of matched non-space tokens interspersed with
           `"\\x00"` slot markers, used downstream to detect ambiguous repeated loop lines.

    Returns:
        `SkeletonMatch(is_valid, score, signature)`.
    """
    if not line_a.tokens or not line_b.tokens:
        return _INVALID_SKELETON_MATCH

    both_lack_words = not line_a.word_set and not line_b.word_set
    if not both_lack_words and (
        not line_a.word_set
        or not line_b.word_set
        or line_a.word_set.isdisjoint(line_b.word_set)
    ):
        return _INVALID_SKELETON_MATCH

    raw_opcodes = difflib.SequenceMatcher(
        None, line_a.tokens, line_b.tokens, autojunk=False
    ).get_opcodes()
    opcodes = _coalesce_space_bridged_opcodes(raw_opcodes, line_a, line_b)

    n_word = sum(
        1
        for tag, i1, i2, _, _ in opcodes
        if tag == "equal"
        for kind in line_a.token_kinds[i1:i2]
        if kind == TokenKind.WORD
    )
    n_punct = sum(
        1
        for tag, i1, i2, _, _ in opcodes
        if tag == "equal"
        for kind in line_a.token_kinds[i1:i2]
        if kind == TokenKind.PUNCT
    )

    gate_passed = choose(both_lack_words, n_punct >= 2, n_word >= 1)
    if not gate_passed:
        return _INVALID_SKELETON_MATCH

    n_slots = sum(
        1
        for tag, i1, i2, j1, j2 in opcodes
        if tag != "equal"
        and _has_non_space_tokens(line_a.token_kinds[i1:i2], line_b.token_kinds[j1:j2])
    )
    e_skel = _WORD_Weight * n_word + _PUNCT_WEIGHT * n_punct
    denom = e_skel + _SLOT_PENALTY * n_slots
    score = choose(denom > 0.0, e_skel / denom, 0.0)
    if score < _SIMILARITY_THRESHOLD:
        return _INVALID_SKELETON_MATCH

    signature = tuple(
        sig_tok
        for tag, i1, i2, j1, j2 in opcodes
        for sig_tok in branch(
            tag == "equal",
            lambda i1=i1, i2=i2: [
                tok
                for tok, kind in zip(line_a.tokens[i1:i2], line_a.token_kinds[i1:i2])
                if kind != TokenKind.SPACE
            ],
            lambda i1=i1, i2=i2, j1=j1, j2=j2: choose(
                _has_non_space_tokens(
                    line_a.token_kinds[i1:i2], line_b.token_kinds[j1:j2]
                ),
                ["\x00"],
                [],
            ),
        )
    )
    return SkeletonMatch(is_valid=True, score=score, signature=signature)


def _filter_unique_skeleton_matrix(
    raw_matrix: Sequence[Sequence[SkeletonMatch]],
    m_len: int,
    n_len: int,
) -> List[List[SkeletonMatch]]:
    """Invalidates candidate matches whose skeleton signature appears multiple times in their row or column.

    Why this is needed:
        A Jinja2 `{% for item in items %}` block in `T` (e.g., `server {{ item }}`) expands into
        multiple rendered lines in `R` (`server alpha`, `server beta`) that all share the exact
        same skeleton signature `("server", "\\x00")`. Enforcing bidirectional signature uniqueness
        across row `i` and column `j` prevents a loop template line from arbitrarily anchoring to
        one of its unrolled output lines, keeping the entire loop body intact as a `BLOCK_EDIT`.
    """
    row_sig_counts: List[Counter[Tuple[str, ...]]] = [
        Counter(
            raw_matrix[i][j].signature
            for j in range(n_len)
            if raw_matrix[i][j].is_valid
        )
        for i in range(m_len)
    ]
    col_sig_counts: List[Counter[Tuple[str, ...]]] = [
        Counter(
            raw_matrix[i][j].signature
            for i in range(m_len)
            if raw_matrix[i][j].is_valid
        )
        for j in range(n_len)
    ]
    return [
        [
            choose(
                raw_matrix[i][j].is_valid
                and row_sig_counts[i][raw_matrix[i][j].signature] == 1
                and col_sig_counts[j][raw_matrix[i][j].signature] == 1,
                raw_matrix[i][j],
                _INVALID_SKELETON_MATCH,
            )
            for j in range(n_len)
        ]
        for i in range(m_len)
    ]


def find_monotonic_line_matches(
    a_lines: Sequence[TokenizedLine],
    b_lines: Sequence[TokenizedLine],
) -> List[Tuple[int, int]]:
    """Finds the maximum-weight strictly monotonic 1-to-1 line matching inside a `"replace"` block.

    Builds the `M x N` pairwise `SkeletonMatch` matrix, prunes ambiguous loop-unrolled candidates
    via `_filter_unique_skeleton_matrix`, and solves a 2D dynamic program (`_dp_row` / `_dp_cell`)
    that maximizes total skeleton score over non-crossing pairs `(i_1 < i_2, j_1 < j_2)`.

    Args:
        a_lines: Tokenized source slice `src_tokens[i1:i2]`.
        b_lines: Tokenized destination slice `dst_tokens[j1:j2]`.

    Returns:
        Ordered list of 0-based relative index pairs `(rel_i, rel_j)` within `a_lines` and `b_lines`.
    """
    m_len = len(a_lines)
    n_len = len(b_lines)
    if m_len == 0 or n_len == 0:
        return []

    raw_matrix = [
        [compute_skeleton_match(a_lines[i], b_lines[j]) for j in range(n_len)]
        for i in range(m_len)
    ]
    matrix = _filter_unique_skeleton_matrix(raw_matrix, m_len, n_len)

    def _dp_row(
        prev_row: List[Tuple[float, Tuple[Tuple[int, int], ...]]],
        i: int,
    ) -> List[Tuple[float, Tuple[Tuple[int, int], ...]]]:
        def _dp_cell(
            cur_row: List[Tuple[float, Tuple[Tuple[int, int], ...]]],
            j: int,
        ) -> List[Tuple[float, Tuple[Tuple[int, int], ...]]]:
            best = choose(
                prev_row[j][0] >= cur_row[-1][0],
                prev_row[j],
                cur_row[-1],
            )
            cell = matrix[i - 1][j - 1]
            diag_score = prev_row[j - 1][0] + cell.score
            next_best = choose(
                cell.is_valid and diag_score > best[0],
                (diag_score, (*prev_row[j - 1][1], (i - 1, j - 1))),
                best,
            )
            return [*cur_row, next_best]

        return reduce(_dp_cell, range(1, n_len + 1), [(0.0, ())])

    final_row = reduce(
        _dp_row,
        range(1, m_len + 1),
        [(0.0, ()) for _ in range(n_len + 1)],
    )
    return list(final_row[n_len][1])


# ==============================================================================
# Layer 3: Second-Pass Sentinel Hunk Fission
# ==============================================================================


def _fission_replace_block(
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    relation: DiffRelation,
) -> List[DiffUnit]:
    """Decomposes a coarse `"replace"` block `src[i1:i2] <-> dst[j1:j2]` into atomic `DiffUnit`s.

    How Sentinel Fission Works:
        1. If the block is already `1 <-> 1`, checks `compute_skeleton_match` directly to emit
           either a `SINGLE_EDIT` (same skeleton) or a `BLOCK_EDIT` (unrelated replacement).
        2. For multi-line `M <-> N` blocks, runs `find_monotonic_line_matches` to discover
           high-confidence 1-to-1 skeleton anchor pairs `(rel_i, rel_j)`.
        3. Replaces each matched pair `(rel_i, rel_j)` in temporary string lists `sub_src` and
           `sub_dst` with an identical unique sentinel `"\\x00__DRIFT_MATCH_k__\\n"` and runs a
           second-pass `SequenceMatcher` over `sub_src` vs `sub_dst`:
           - `"equal"` spans on sentinels (or raw lines) become 1-to-1 `SINGLE_EDIT` (or `EQUAL`) units.
           - Unmatched intervals between sentinels automatically partition into minimal `INSERT`,
             `DELETE`, or `BLOCK_EDIT` units.
    """
    if i2 - i1 == 1 and j2 - j1 == 1:
        match = compute_skeleton_match(src_tokens[i1], dst_tokens[j1])
        u_type = choose(match.is_valid, DiffUnitType.SINGLE_EDIT, DiffUnitType.BLOCK_EDIT)
        return [DiffUnit(relation, u_type, i1, i2, j1, j2)]

    matches = find_monotonic_line_matches(src_tokens[i1:i2], dst_tokens[j1:j2])
    if not matches:
        return [DiffUnit(relation, DiffUnitType.BLOCK_EDIT, i1, i2, j1, j2)]

    src_match_map: Dict[int, int] = {rel_i: k for k, (rel_i, _) in enumerate(matches)}
    dst_match_map: Dict[int, int] = {rel_j: k for k, (_, rel_j) in enumerate(matches)}

    sub_src = [
        choose(
            rel_i in src_match_map,
            f"\x00__DRIFT_MATCH_{src_match_map.get(rel_i)}__\n",
            src_tokens[i1 + rel_i].raw_line,
        )
        for rel_i in range(i2 - i1)
    ]
    sub_dst = [
        choose(
            rel_j in dst_match_map,
            f"\x00__DRIFT_MATCH_{dst_match_map.get(rel_j)}__\n",
            dst_tokens[j1 + rel_j].raw_line,
        )
        for rel_j in range(j2 - j1)
    ]

    sub_opcodes = difflib.SequenceMatcher(
        None, sub_src, sub_dst, autojunk=False
    ).get_opcodes()

    return [
        unit
        for sub_tag, si1, si2, sj1, sj2 in sub_opcodes
        for unit in branch(
            sub_tag == "equal",
            lambda si1=si1, si2=si2, sj1=sj1: [
                DiffUnit(
                    relation=relation,
                    unit_type=choose(
                        src_tokens[i1 + si1 + offset].raw_line
                        == dst_tokens[j1 + sj1 + offset].raw_line,
                        DiffUnitType.EQUAL,
                        DiffUnitType.SINGLE_EDIT,
                    ),
                    src_start=i1 + si1 + offset,
                    src_end=i1 + si1 + offset + 1,
                    dst_start=j1 + sj1 + offset,
                    dst_end=j1 + sj1 + offset + 1,
                )
                for offset in range(si2 - si1)
            ],
            lambda sub_tag=sub_tag, si1=si1, si2=si2, sj1=sj1, sj2=sj2: [
                DiffUnit(
                    relation=relation,
                    unit_type=choose(
                        sub_tag == "insert",
                        DiffUnitType.INSERT,
                        choose(
                            sub_tag == "delete",
                            DiffUnitType.DELETE,
                            DiffUnitType.BLOCK_EDIT,
                        ),
                    ),
                    src_start=i1 + si1,
                    src_end=i1 + si2,
                    dst_start=j1 + sj1,
                    dst_end=j1 + sj2,
                )
            ],
        )
    ]


def fission_diff_units(
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    relation: DiffRelation,
) -> Tuple[DiffUnit, ...]:
    """Computes the complete ordered sequence of fine-grained `DiffUnit`s between two tokenized files.

    Expands `"equal"` spans into individual 1-line `EQUAL` units, preserves pure `"insert"` and
    `"delete"` spans, and splits `"replace"` spans via `_fission_replace_block` so every line in
    `src_tokens` and `dst_tokens` is covered by a contiguous partition of `DiffUnit`s.

    Args:
        src_tokens: Tokenized lines of the source side (`T` for `TR`, `R` for `RH`).
        dst_tokens: Tokenized lines of the destination side (`R` for `TR`, `H` for `RH`).
        relation: `DiffRelation.TR` (`T -> R`) or `DiffRelation.RH` (`R -> H`).

    Returns:
        Ordered tuple of `DiffUnit` objects covering `[0, len(src_tokens))` and `[0, len(dst_tokens))`.
    """
    src_raw = [t.raw_line for t in src_tokens]
    dst_raw = [t.raw_line for t in dst_tokens]
    raw_opcodes = difflib.SequenceMatcher(
        None, src_raw, dst_raw, autojunk=False
    ).get_opcodes()

    return tuple(
        unit
        for tag, i1, i2, j1, j2 in raw_opcodes
        for unit in branch(
            tag == "equal",
            lambda i1=i1, i2=i2, j1=j1: [
                DiffUnit(
                    relation=relation,
                    unit_type=DiffUnitType.EQUAL,
                    src_start=i1 + offset,
                    src_end=i1 + offset + 1,
                    dst_start=j1 + offset,
                    dst_end=j1 + offset + 1,
                )
                for offset in range(i2 - i1)
            ],
            lambda tag=tag, i1=i1, i2=i2, j1=j1, j2=j2: branch(
                tag == "replace",
                lambda: _fission_replace_block(
                    src_tokens, dst_tokens, i1, i2, j1, j2, relation
                ),
                lambda: [
                    DiffUnit(
                        relation=relation,
                        unit_type=choose(
                            tag == "insert",
                            DiffUnitType.INSERT,
                            DiffUnitType.DELETE,
                        ),
                        src_start=i1,
                        src_end=i2,
                        dst_start=j1,
                        dst_end=j2,
                    )
                ],
            ),
        )
    )


# ==============================================================================
# Layer 4: Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus
# ==============================================================================


def try_derender_single_line(
    t_line: TokenizedLine,
    r_line: TokenizedLine,
    h_line: TokenizedLine,
) -> Optional[str]:
    """Performs `1 <-> 1 <-> 1` closed token-interval calculus on an aligned `(T, R, H)` line triple.

    Algorithm:
        1. Projects both `T <-> R` variable slots `[c_1, c_2]` (coalesced across internal spaces)
           and `R <-> H` host edits `[p_1, p_2]` onto the token index axis of `r_line`.
        2. Closed-Interval Overlap Check (`max(p_1, c_1) <= min(p_2, c_2)`):
           Treats both intervals as closed `[start, end]` in token-boundary space so that a host
           edit touching or inserting immediately adjacent to a variable boundary (`p_2 == c_1` or
           `p_1 == c_2`) is conservatively classified as a conflict (Case 1.3 -> returns `None`).
        3. Synthesis (Case 1.2):
           When all host edits are strictly disjoint from all variable slots, merges both lists
           of `R`-axis token replacements in sorted order and reconstructs the de-rendered template
           line `H'` (preserving `t_line`'s variable expressions alongside `h_line`'s static token
           edits and `h_line.line_ending`).

    Args:
        t_line: Tokenized template line from `T`.
        r_line: Tokenized rendered baseline line from `R`.
        h_line: Tokenized host-modified line from `H`.

    Returns:
        The synthesized de-rendered template line `H'` (including trailing line ending) if all
        edits are disjoint (Case 1.2), or `None` if any host edit touches a variable slot (Case 1.3).
    """
    raw_tr_ops = difflib.SequenceMatcher(
        None, t_line.tokens, r_line.tokens, autojunk=False
    ).get_opcodes()
    tr_ops = _coalesce_space_bridged_opcodes(raw_tr_ops, t_line, r_line)
    rh_ops = difflib.SequenceMatcher(
        None, r_line.tokens, h_line.tokens, autojunk=False
    ).get_opcodes()

    var_intervals: List[Tuple[int, int, Tuple[str, ...]]] = [
        (r1, r2, t_line.tokens[t1:t2])
        for tag, t1, t2, r1, r2 in tr_ops
        if tag != "equal"
    ]
    host_edits: List[Tuple[int, int, Tuple[str, ...]]] = [
        (p1, p2, h_line.tokens[h1:h2])
        for tag, p1, p2, h1, h2 in rh_ops
        if tag != "equal"
    ]

    if not var_intervals:
        return h_line.raw_line
    if not host_edits:
        return t_line.raw_line

    has_closed_overlap = any(
        max(p1, c1) <= min(p2, c2)
        for p1, p2, _ in host_edits
        for c1, c2, _ in var_intervals
    )
    if has_closed_overlap:
        return None

    combined_replacements = sorted(
        [*var_intervals, *host_edits],
        key=lambda item: (item[0], item[1]),
    )

    def _step_apply(
        state: Tuple[int, List[str]],
        rep: Tuple[int, int, Tuple[str, ...]],
    ) -> Tuple[int, List[str]]:
        cursor, acc = state
        start_r, end_r, new_tokens = rep
        prefix = list(r_line.tokens[cursor:start_r])
        return (end_r, [*acc, *prefix, *new_tokens])

    final_cursor, out_tokens = reduce(_step_apply, combined_replacements, (0, []))
    trailing = list(r_line.tokens[final_cursor:])
    return "".join([*out_tokens, *trailing, h_line.line_ending])


def refine_single_line_units(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
    t_tokens: Sequence[TokenizedLine],
    r_tokens: Sequence[TokenizedLine],
    h_tokens: Sequence[TokenizedLine],
) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]:
    """Refines all `1 <-> 1 <-> 1` aligned `(TR SINGLE_EDIT, RH SINGLE_EDIT)` pairs via token calculus.

    Locates every `R` line index `r_idx` where both `tr_units` and `rh_units` have a 1-line
    `SINGLE_EDIT` unit, then runs `try_derender_single_line`:
    - If de-rendering succeeds (Case 1.2): upgrades the `RH` unit to `DiffUnitType.SINGLE_SKEL_EDIT`
      with `derendered_lines=(res,)`.
    - If de-rendering fails (Case 1.3 overlap): marks both the `TR` unit and the `RH` unit with
      `is_conflict=True`.

    Returns:
        Updated `(tr_units, rh_units)` tuples.
    """
    tr_single_by_r: Dict[int, int] = {
        u.r_start: idx
        for idx, u in enumerate(tr_units)
        if u.unit_type == DiffUnitType.SINGLE_EDIT and u.r_end - u.r_start == 1
    }
    rh_single_by_r: Dict[int, int] = {
        e.r_start: idx
        for idx, e in enumerate(rh_units)
        if e.unit_type == DiffUnitType.SINGLE_EDIT and e.r_end - e.r_start == 1
    }

    shared_r_indices = sorted(tr_single_by_r.keys() & rh_single_by_r.keys())
    if not shared_r_indices:
        return tuple(tr_units), tuple(rh_units)

    derendered_by_r: Dict[int, Optional[str]] = {
        r_idx: try_derender_single_line(
            t_tokens[tr_units[tr_single_by_r[r_idx]].src_start],
            r_tokens[r_idx],
            h_tokens[rh_units[rh_single_by_r[r_idx]].dst_start],
        )
        for r_idx in shared_r_indices
    }
    tr_updates: Dict[int, DiffUnit] = {
        tr_single_by_r[r_idx]: replace(
            tr_units[tr_single_by_r[r_idx]], is_conflict=True
        )
        for r_idx, res in derendered_by_r.items()
        if res is None
    }
    rh_updates: Dict[int, DiffUnit] = {
        rh_single_by_r[r_idx]: branch(
            res is not None,
            lambda r_idx=r_idx, res=res: replace(
                rh_units[rh_single_by_r[r_idx]],
                unit_type=DiffUnitType.SINGLE_SKEL_EDIT,
                derendered_lines=(res or "",),
            ),
            lambda r_idx=r_idx: replace(
                rh_units[rh_single_by_r[r_idx]],
                is_conflict=True,
            ),
        )
        for r_idx, res in derendered_by_r.items()
    }
    return (
        tuple(tr_updates.get(idx, u) for idx, u in enumerate(tr_units)),
        tuple(rh_updates.get(idx, e) for idx, e in enumerate(rh_units)),
    )
