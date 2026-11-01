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
            - Splits line into contiguous `TokenKind.WORD` (`\\w+`), `TokenKind.PUNCT` (`[^\\w\\s]`),
              and `TokenKind.SPACE` (`\\s+`) tokens via `_classify_token`
            - Caches `word_set` (`frozenset` of `WORD` tokens) for O(1) disjoint-line pruning
            -> Returns TokenizedLine

    2. Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching:
        find_monotonic_line_matches(a_lines, b_lines) [Layer 2]
            - Evaluates pairwise line similarity via compute_skeleton_match(line_a, line_b):
                * O(1) Word-Set Pruning: rejects pairs with disjoint `word_set` (unless both lack words)
                * Space-Bridge Coalescing (_extract_token_opcodes + _coalesce_space_bridged_opcodes):
                  merges non-equal `_TokenOpcode`s separated only by `SPACE` tokens
                  (`_TokenOpcode.is_space_only_equal`) so internal spaces inside template tags
                  (`{{ var }}` -> `hello world`) form a single slot
                * Skeleton Gate & Slot-Normalized Score (_compute_skeleton_score): requires >= 1 shared
                  `WORD` token (or >= 2 `PUNCT` tokens if wordless) and computes
                  `S = E_skel / (E_skel + N_slots)` where `E_skel = 1.0 * n_word + 0.25 * n_punct` and
                  `N_slots` counts non-space replacement spans (`_TokenOpcode.has_non_space_tokens`);
                  rejects pairs with `S < 0.5` (`_SIMILARITY_THRESHOLD`)
                * Skeleton Signature (`_TokenOpcode.signature_tokens`): builds a tuple of static
                  non-space tokens with `"\x00"` placeholders for each variable slot
                -> Returns SkeletonMatch(is_valid, score, signature)
            - Bidirectional Uniqueness Filtering (_filter_unique_skeleton_matrix):
                * Invalidates any candidate `(i, j)` whose `signature` appears more than once in row `i`
                  (1-to-N `{% for %}` loop unrolling) or column `j` (M-to-1 `{% if %}`/`{% else %}`
                  branch ambiguity)
            - 2D Dynamic Programming (_DpCell / _dp_row / _dp_cell):
                * Selects the highest-scoring strictly monotonic (`i_1 < i_2` and `j_1 < j_2`) set of
                  valid 1-to-1 line pairs within a `"replace"` block
            -> Returns Tuple[Tuple[int, int], ...] of matched relative `(rel_i, rel_j)` line indices

    3. Second-Pass Sentinel Hunk Fission (`T -> R` and `R -> H`):
        fission_diff_units(src_tokens, dst_tokens, relation) [Layer 3]
            - Runs first-pass line-level `SequenceMatcher` on `raw_line` sequences via
              `_diff_sequences_to_units` and expands each opcode via `_expand_raw_opcode_to_units`:
                * `"equal"` spans -> expands into 1-to-1 `DiffUnitType.EQUAL` units (1 line each)
                * `"insert"` / `"delete"` spans -> wraps directly as `INSERT` / `DELETE` `DiffUnit`s
                * `"replace"` spans -> delegates to _fission_replace_block:
                    - Fast-path `1 <-> 1`: classifies as `SINGLE_EDIT` if `compute_skeleton_match` is
                      valid, else `BLOCK_EDIT`
                    - Multi-line `M <-> N` (_fission_multiline_replace_block): finds 1-to-1 anchors
                      via `find_monotonic_line_matches`, replaces matched line pairs with unique
                      `"\x00__DRIFT_MATCH_k__\n"` sentinels, and runs a second-pass `SequenceMatcher`
                      (`_diff_sequences_to_units` + `_expand_sub_opcode_to_units`) so unmatched gaps
                      between anchors split cleanly into minimal `SINGLE_EDIT`, `BLOCK_EDIT`,
                      `INSERT`, and `DELETE` units
            -> Returns Tuple[DiffUnit, ...]

    4. Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus:
        refine_single_line_units(tr_units, rh_units, t_tokens, r_tokens, h_tokens) [Layer 4]
            - Finds all `R` line indices where both `TR` and `RH` have a 1-to-1 `SINGLE_EDIT` unit
            - For each shared `1 <-> 1 <-> 1` line triple `(T_i, R_r, H_j)`, runs
              try_derender_single_line(t_line, r_line, h_line):
                * Extracts template variable `SpliceSpan`s `[c_1, c_2]` on `R_r` and host edit
                  `SpliceSpan`s `[p_1, p_2]` on `R_r` directly from `_extract_token_opcodes`
                * Closed-Interval Overlap Check (`SpliceSpan.overlaps_closed`): tests
                  `max(p_1, c_1) <= min(p_2, c_2)` on token boundaries so even zero-width insertions
                  immediately adjacent to a variable slot are caught
                * Clean Disjoint Edit (Case 1.2): splices both template variable tokens and host static
                  edits onto `R_r` via `splice_spans` -> returns newline-stripped line `H'`
                * Overlapping Edit (Case 1.3): returns `None`
            - Partitions results via `star_partition` to upgrade clean `RH` units to
              `DiffUnitType.SINGLE_SKEL_EDIT` with `derendered_lines=(H',)` and mark colliding `TR`
              and `RH` units with `is_conflict=True`
            -> Returns Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Lossless Word-Level Line Tokenization
        _classify_token
        tokenize_line
    Layer 2: Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching
        _TokenOpcode
        _DpCell
        _extract_token_opcodes
        _coalesce_space_bridged_opcodes
        _compute_skeleton_score
        compute_skeleton_match
        _filter_unique_skeleton_matrix
        find_monotonic_line_matches
    Layer 3: Second-Pass Sentinel Hunk Fission
        _diff_sequences_to_units
        _expand_sub_opcode_to_units
        _fission_multiline_replace_block
        _fission_replace_block
        _expand_raw_opcode_to_units
        fission_diff_units
    Layer 4: Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus
        try_derender_single_line
        refine_single_line_units
===============================================================================
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import difflib
from functools import reduce
from itertools import chain, compress, product, starmap
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..models.adopt_models import (
    DiffRelation,
    DiffUnit,
    DiffUnitType,
    SkeletonMatch,
    TokenizedLine,
    TokenKind,
)
from ..utils.func_utils import (
    SpliceSpan,
    branch,
    choose,
    partition,
    splice_spans,
    star_filter,
    star_partition,
    when_none,
)

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]")
_WORD_WEIGHT = 1.0
_PUNCT_WEIGHT = 0.25
_SLOT_PENALTY = 1.0
# Minimum slot-normalized skeleton similarity `S = E_skel / (E_skel + 1.0 * N_slots)`.
# `S >= 0.5` is the exact balance point `E_skel >= N_slots` (static skeleton weight must be at
# least as large as the number of variable/edit slots). This admits canonical 1-slot lines such as
# `return {{ val }}` (`E_skel = 1.0, N_slots = 1 -> S = 0.50`) and `key = {{ value }}`
# (`E_skel = 1.25, N_slots = 1 -> S = 0.556`) while rejecting lines where slots outweigh skeleton.
_SIMILARITY_THRESHOLD = 0.5
_INVALID_SKELETON_MATCH = SkeletonMatch(is_valid=False, score=0.0, signature=())


# ==============================================================================
# Layer 1: Lossless Word-Level Line Tokenization
# ==============================================================================


def _classify_token(token: str) -> TokenKind:
    """Classifies a non-empty regex token into `WORD` (`\\w+`), `SPACE` (`\\s+`), or `PUNCT` (`[^\\w\\s]`)."""
    first = token[0]
    if first.isspace():
        return TokenKind.SPACE
    # python `str.isalnum()` returns True for Unicode letters and digits
    if first.isalnum() or first == "_":
        return TokenKind.WORD
    return TokenKind.PUNCT


def tokenize_line(line: str) -> TokenizedLine:
    """Losslessly partitions a single line into word, whitespace, and punctuation tokens.

    Separates the trailing line ending (`\r\n` or `\n`) from the line body, splits the body
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
    word_set = frozenset(compress(tokens, map(lambda k: k == TokenKind.WORD, token_kinds)))
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


@dataclass(frozen=True)
class _TokenOpcode:
    """Structured token-level diff opcode aligning `line_a.tokens[a_start:a_end]` with `line_b.tokens[b_start:b_end]`."""
    tag: str
    a_start: int
    a_end: int
    b_start: int
    b_end: int
    line_a: TokenizedLine
    line_b: TokenizedLine

    @property
    def is_equal(self) -> bool:
        """True if this opcode represents an `'equal'` token slice."""
        return self.tag == "equal"

    @property
    def a_tokens(self) -> Tuple[str, ...]:
        """Slice of `line_a.tokens[a_start:a_end]`."""
        return self.line_a.tokens[self.a_start : self.a_end]

    @property
    def b_tokens(self) -> Tuple[str, ...]:
        """Slice of `line_b.tokens[b_start:b_end]`."""
        return self.line_b.tokens[self.b_start : self.b_end]

    @property
    def a_kinds(self) -> Tuple[TokenKind, ...]:
        """Slice of `line_a.token_kinds[a_start:a_end]`."""
        return self.line_a.token_kinds[self.a_start : self.a_end]

    @property
    def b_kinds(self) -> Tuple[TokenKind, ...]:
        """Slice of `line_b.token_kinds[b_start:b_end]`."""
        return self.line_b.token_kinds[self.b_start : self.b_end]

    @property
    def is_space_only_equal(self) -> bool:
        """True if this opcode is an `'equal'` span consisting exclusively of `SPACE` tokens."""
        return self.is_equal and all(
            kind == TokenKind.SPACE for kind in self.a_kinds
        )

    @property
    def has_non_space_tokens(self) -> bool:
        """True if either side's token slice contains at least one non-whitespace (`WORD` or `PUNCT`) token."""
        return any(k != TokenKind.SPACE for k in self.a_kinds) or any(
            k != TokenKind.SPACE for k in self.b_kinds
        )

    @property
    def signature_tokens(self) -> Tuple[str, ...]:
        """Skeleton signature contribution of this opcode (non-space static tokens or `'\\x00'` slot marker)."""
        if self.is_equal:
            return tuple(compress(
                    self.a_tokens,
                    map(lambda k: k != TokenKind.SPACE, self.a_kinds),
            ))
        return choose(self.has_non_space_tokens, ("\x00",), ())

    def merge_to(self, end_op: _TokenOpcode) -> _TokenOpcode:
        """Merges `self` through `end_op` into a single `'replace'` span."""
        return replace(
            self,
            tag="replace",
            a_end=end_op.a_end,
            b_end=end_op.b_end,
        )


@dataclass(frozen=True)
class _DpCell:
    """2D dynamic programming state storing cumulative skeleton score and matched `(rel_i, rel_j)` pairs."""
    score: float = 0.0
    pairs: Tuple[Tuple[int, int], ...] = ()


def _extract_token_opcodes(
    line_a: TokenizedLine,
    line_b: TokenizedLine,
) -> Tuple[_TokenOpcode, ...]:
    """Runs token-level `SequenceMatcher` on `(line_a, line_b)` and binds each opcode to both lines."""
    raw_opcodes = difflib.SequenceMatcher(None, line_a.tokens, line_b.tokens, autojunk=False).get_opcodes()
    return tuple(starmap(lambda tag, i1, i2, j1, j2:
            _TokenOpcode(
                tag=tag,
                a_start=i1, a_end=i2,
                b_start=j1, b_end=j2,
                line_a=line_a, line_b=line_b,
            ),
            raw_opcodes))


def _coalesce_space_bridged_opcodes(
    opcodes: Sequence[_TokenOpcode],
) -> Tuple[_TokenOpcode, ...]:
    """Merges non-equal token opcodes that are separated only by whitespace-only `'equal'` spans.

    Why this is needed:
        When a template expression like `{{ value1 }}` renders to a multi-word string like
        `hello world`, `SequenceMatcher` often aligns the internal space inside `{{ value1 }}`
        with the space between `hello` and `world`, splitting a single logical template slot
        into two replacement spans (`"{{" + "value1" -> "hello"` and `"}}" -> "world"`).
        Coalescing across space-only bridges guarantees the entire template expression is
        counted and bounded as one contiguous slot.
    """
    def _step(acc: List[_TokenOpcode], op: _TokenOpcode) -> List[_TokenOpcode]:
        merge_one = not op.is_equal and len(acc) >= 1 and not acc[-1].is_equal
        merge_two = (
            not op.is_equal
            and len(acc) >= 2
            and acc[-1].is_space_only_equal
            and not acc[-2].is_equal
        )
        return branch(
            merge_one, lambda: [*acc[:-1], acc[-1].merge_to(op)],
            merge_two, lambda: [*acc[:-2], acc[-2].merge_to(op)],
            lambda: [*acc, op],
        )

    return tuple(reduce(_step, opcodes, []))


def _compute_skeleton_score(
    opcodes: Sequence[_TokenOpcode],
    both_lack_words: bool,
) -> float:
    """Evaluates the structural token gate and computes the slot-normalized similarity score.

    Gate & Scoring Rules:
        - Structural Gate: Requires `n_word >= 1` when either line contains words, or
          `n_punct >= 2` when both lines contain zero words (`both_lack_words=True`).
          Returns `0.0` immediately if the gate fails.
        - Slot-Normalized Formula: `S = E_skel / (E_skel + 1.0 * N_slots)`, where
          `E_skel = 1.0 * n_word + 0.25 * n_punct` and `N_slots` counts non-whitespace
          replacement spans (`op.has_non_space_tokens`).
    """
    equal_ops, non_equal_ops = partition(lambda op: op.is_equal, opcodes)
    equal_kinds = tuple(chain.from_iterable(map(lambda op: op.a_kinds, equal_ops)))
    n_word = sum(1 for kind in equal_kinds if kind == TokenKind.WORD)
    n_punct = sum(1 for kind in equal_kinds if kind == TokenKind.PUNCT)
    gate_passed = choose(both_lack_words, n_punct >= 2, n_word >= 1)
    if not gate_passed:
        return 0.0
    n_slots = sum(1 for op in non_equal_ops if op.has_non_space_tokens)
    e_skel = _WORD_WEIGHT * n_word + _PUNCT_WEIGHT * n_punct
    denom = e_skel + _SLOT_PENALTY * n_slots
    return choose(denom > 0.0, e_skel / denom, 0.0)


def compute_skeleton_match(
    line_a: TokenizedLine,
    line_b: TokenizedLine,
) -> SkeletonMatch:
    """Evaluates whether two lines share the same structural skeleton and computes their similarity.

    Algorithm:
        1. O(1) Disjoint-Word Prune: If either line has words and their `word_set`s are disjoint,
           reject immediately.
        2. Token Diff & Space-Bridge Coalescing: Aligns token sequences via `_extract_token_opcodes`
           and merges non-equal spans bridged only by whitespace (`_coalesce_space_bridged_opcodes`).
        3. Structural Gate & Slot-Normalized Score (`_compute_skeleton_score`): Requires at least
           1 shared `WORD` token (or at least 2 shared `PUNCT` tokens when both lines contain zero
           words) and `S = E_skel / (E_skel + N_slots) >= 0.5` (`_SIMILARITY_THRESHOLD`).
        4. Skeleton Signature: Emits the tuple of matched non-space tokens interspersed with
           `"\\x00"` slot markers, used downstream to detect ambiguous repeated loop lines.

    Returns:
        `SkeletonMatch(is_valid, score, signature)`.
    """
    if not line_a.tokens or not line_b.tokens:
        return _INVALID_SKELETON_MATCH

    both_lack_words = not line_a.word_set and not line_b.word_set
    if not both_lack_words and line_a.word_set.isdisjoint(line_b.word_set):
        return _INVALID_SKELETON_MATCH

    opcodes = _coalesce_space_bridged_opcodes(_extract_token_opcodes(line_a, line_b))
    score = _compute_skeleton_score(opcodes, both_lack_words)
    if score < _SIMILARITY_THRESHOLD:
        return _INVALID_SKELETON_MATCH

    signature = tuple(chain.from_iterable(map(lambda op: op.signature_tokens, opcodes)))
    return SkeletonMatch(is_valid=True, score=score, signature=signature)


def _filter_unique_skeleton_matrix(
    raw_matrix: Sequence[Sequence[SkeletonMatch]],
) -> Tuple[Tuple[SkeletonMatch, ...], ...]:
    """Invalidates candidate matches whose skeleton signature appears multiple times in their row or column.

    Bidirectional Signature Uniqueness Rules (`raw_matrix[i][j]` aligns `a_lines[i]` with `b_lines[j]`):
        1. Candidate Scope: Only valid cells (`cell.is_valid == True`, i.e., passed the structural
           gate and `score >= 0.5`) are counted in `row_sig_counts[i]` and `col_sig_counts[j]`.
        2. Row Uniqueness (`row_sig_counts[i][sig] == 1` — `1 -> N` Loop-Unrolling Guard):
           Across row `i`, source line `a_lines[i]` must not match multiple destination lines
           `b_lines[0..n_len-1]` with the identical `signature`. For example, in `T -> R`, a single
           `{% for %}` template line `T[i]` (`server = {{ host }}`) matches multiple unrolled
           rendered lines `R[0], R[1], R[2]` (`server = node1`, `server = node2`, `server = node3`)
           across row `i` (`row_sig_counts[i][("server", "=", "\\x00")] == 3`), while each column
           `j` has only 1 match (`col_sig_counts[j] == 1`). Row uniqueness invalidates all 3 cells
           so the loop body stays intact as a `BLOCK_EDIT`.
        3. Column Uniqueness (`col_sig_counts[j][sig] == 1` — `M -> 1` Branch-Ambiguity Guard):
           Down column `j`, destination line `b_lines[j]` must not match multiple source lines
           `a_lines[0..m_len-1]` with the identical `signature`. For example, in `T -> R`, two
           branches of an `{% if %}` / `{% else %}` block (`T[1]`: `server = {{ prod }}` and
           `T[3]`: `server = {{ dev }}`) both match the single rendered output line `R[0]`
           (`server = 10.0.0.1`) down column `0` (`col_sig_counts[0][("server", "=", "\\x00")] == 2`),
           while each row has only 1 match (`row_sig_counts[i] == 1`). Column uniqueness prevents
           `R[0]` from arbitrarily anchoring to one branch and deleting the other.
        4. Per-Signature Counting Prevents False Positives on Shared Prefixes:
           Counts are grouped by exact `cell.signature` tuple rather than total valid matches in a
           row/column. If `a_lines[0]` (`set_header X_Real_IP {{ ip }};`) has a specific diagonal
           match `(0, 0)` with signature `("set_header", "X_Real_IP", "\\x00", ";")` (count `1`)
           alongside weaker off-diagonal matches `(0, 1), (0, 2)` against other `set_header` lines
           with generic signature `("set_header", "\\x00", ";")` (count `2`), only the repeated
           off-diagonal signature is invalidated while the unique diagonal match `(0, 0)` survives.
    """
    def _count_valid_signatures(cells: Sequence[SkeletonMatch]) -> Counter[Tuple[str, ...]]:
        return Counter(map(
                lambda cell: cell.signature,
                filter(lambda cell: cell.is_valid, cells),
        ))

    row_sig_counts = tuple(map(_count_valid_signatures, raw_matrix))
    col_sig_counts = tuple(map(_count_valid_signatures, zip(*raw_matrix)))
    return tuple(starmap(
            lambda row, row_counts: tuple(starmap(
                    lambda cell, col_counts: choose(
                            cell.is_valid
                            and row_counts[cell.signature] == 1
                            and col_counts[cell.signature] == 1,
                            cell,
                            _INVALID_SKELETON_MATCH,
                    ),
                    zip(row, col_sig_counts),
            )),
            zip(raw_matrix, row_sig_counts),
    ))


def find_monotonic_line_matches(
    a_lines: Sequence[TokenizedLine],
    b_lines: Sequence[TokenizedLine],
) -> Tuple[Tuple[int, int], ...]:
    """Finds the maximum-weight strictly monotonic 1-to-1 line matching inside a `"replace"` block.

    Builds the `M x N` pairwise `SkeletonMatch` matrix, prunes ambiguous loop-unrolled candidates
    via `_filter_unique_skeleton_matrix`, and solves a 2D dynamic program (`_dp_row` / `_dp_cell`)
    that maximizes total skeleton score over non-crossing pairs `(i_1 < i_2, j_1 < j_2)`:
        `dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1] + matrix[i - 1][j - 1].score if valid)`

    Args:
        a_lines: Tokenized source slice `src_tokens[i1:i2]`.
        b_lines: Tokenized destination slice `dst_tokens[j1:j2]`.

    Returns:
        Ordered tuple of 0-based relative index pairs `(rel_i, rel_j)` within `a_lines` and `b_lines`.
    """
    m_len = len(a_lines)
    n_len = len(b_lines)
    if m_len == 0 or n_len == 0:
        return ()

    # matrix is a 2D tuple of SkeletonMatch objects
    # where matrix[i][j] corresponds to the similarity between a_lines[i] and b_lines[j]
    raw_matrix = tuple(map(
            lambda a_line: tuple(map(
                    lambda b_line: compute_skeleton_match(a_line, b_line),
                    b_lines,
            )),
            a_lines,
    ))
    matrix = _filter_unique_skeleton_matrix(raw_matrix)
    empty_cell = _DpCell()

    # DP transition: dp[i][j] = max(dp[i-1][j], dp[i][j-1], dp[i-1][j-1] + matrix[i-1][j-1].score if valid)
    def _dp_row(
        prev_row: List[_DpCell],
        i: int,
    ) -> List[_DpCell]:
        def _dp_cell(
            cur_row: List[_DpCell],
            j: int,
        ) -> List[_DpCell]:
            prev_best = choose(
                prev_row[j].score >= cur_row[-1].score,
                prev_row[j],
                cur_row[-1],
            )
            cell = matrix[i - 1][j - 1]
            diag_score = prev_row[j - 1].score + cell.score
            next_best = choose(
                cell.is_valid and diag_score > prev_best.score,
                _DpCell(
                    score=diag_score,
                    pairs=(*prev_row[j - 1].pairs, (i - 1, j - 1)),
                ),
                prev_best,
            )
            return [*cur_row, next_best]

        return reduce(_dp_cell, range(1, n_len + 1), [empty_cell])

    # loop range is (M + 1) * (N + 1) to account for the initial empty row and column in the DP table
    final_row = reduce(_dp_row, range(1, m_len + 1), [empty_cell] * (n_len + 1))
    return final_row[n_len].pairs


# ==============================================================================
# Layer 3: Second-Pass Sentinel Hunk Fission
# ==============================================================================


def _diff_sequences_to_units(
    a_seq: Sequence[str],
    b_seq: Sequence[str],
    expand_opcode: Callable[[str, int, int, int, int], Iterable[DiffUnit]],
) -> Tuple[DiffUnit, ...]:
    """Runs `SequenceMatcher` on `(a_seq, b_seq)` and flattens expanded opcodes into `DiffUnit`s."""
    opcodes = difflib.SequenceMatcher(None, a_seq, b_seq, autojunk=False).get_opcodes()
    return tuple(chain.from_iterable(starmap(expand_opcode, opcodes)))


def _expand_sub_opcode_to_units(
    sub_tag: str,
    si1: int,
    si2: int,
    sj1: int,
    sj2: int,
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    i1: int,
    j1: int,
    relation: DiffRelation,
) -> Tuple[DiffUnit, ...]:
    """Converts a single second-pass sentinel `SequenceMatcher` opcode into atomic `DiffUnit`s."""
    if sub_tag == "equal":
        # Expand each line in the equal span into a 1-to-1 `DiffUnitType.EQUAL`
        # or `DiffUnitType.SINGLE_EDIT` unit depending on whether the raw lines are identical.
        return tuple(map(lambda offset: DiffUnit(
                    relation=relation,
                    unit_type=choose(
                        src_tokens[i1 + si1 + offset].raw_line == dst_tokens[j1 + sj1 + offset].raw_line,
                        DiffUnitType.EQUAL, DiffUnitType.SINGLE_EDIT,
                    ),
                    src_start=i1 + si1 + offset,
                    src_end=i1 + si1 + offset + 1,
                    dst_start=j1 + sj1 + offset,
                    dst_end=j1 + sj1 + offset + 1,
                ), range(si2 - si1)))
    unit_type = choose(
        sub_tag == "insert", DiffUnitType.INSERT,
        sub_tag == "delete", DiffUnitType.DELETE,
        DiffUnitType.BLOCK_EDIT,
    )
    return (
        DiffUnit(
            relation=relation,
            unit_type=unit_type,
            src_start=i1 + si1,
            src_end=i1 + si2,
            dst_start=j1 + sj1,
            dst_end=j1 + sj2,
        ),
    )


def _fission_multiline_replace_block(
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    relation: DiffRelation,
) -> Tuple[DiffUnit, ...]:
    """Runs second-pass sentinel fission on a multi-line `M <-> N` `"replace"` block."""
    matches = find_monotonic_line_matches(src_tokens[i1:i2], dst_tokens[j1:j2])
    if not matches:
        return (DiffUnit(relation, DiffUnitType.BLOCK_EDIT, i1, i2, j1, j2),)

    # Map each matched relative line index (rel_i or rel_j) to its shared sentinel ID `match_idx`
    src_match_map: Dict[int, int] = dict(
        starmap(lambda match_idx, pair: (pair[0], match_idx), enumerate(matches))
    )
    dst_match_map: Dict[int, int] = dict(
        starmap(lambda match_idx, pair: (pair[1], match_idx), enumerate(matches))
    )

    # Replace each matched line with a unique sentinel `"\x00__DRIFT_MATCH_k__\n"` for second-pass alignment
    sub_src = tuple(map(lambda rel_i: choose(
                rel_i in src_match_map,
                f"\x00__DRIFT_MATCH_{src_match_map.get(rel_i)}__\n",
                src_tokens[i1 + rel_i].raw_line,
            ),
            range(i2 - i1)))
    sub_dst = tuple(map(lambda rel_j: choose(
                rel_j in dst_match_map,
                f"\x00__DRIFT_MATCH_{dst_match_map.get(rel_j)}__\n",
                dst_tokens[j1 + rel_j].raw_line,
            ),
            range(j2 - j1)))

    return _diff_sequences_to_units(sub_src, sub_dst,
        lambda sub_tag, si1, si2, sj1, sj2: _expand_sub_opcode_to_units(
            sub_tag, si1, si2, sj1, sj2, src_tokens, dst_tokens, i1, j1, relation
        ),
    )


def _fission_replace_block(
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    relation: DiffRelation,
) -> Tuple[DiffUnit, ...]:
    """Decomposes a coarse `"replace"` block `src[i1:i2] <-> dst[j1:j2]` into atomic `DiffUnit`s.

    How Sentinel Fission Works:
        1. If the block is already `1 <-> 1`, checks `compute_skeleton_match` directly to emit
           either a `SINGLE_EDIT` (same skeleton) or a `BLOCK_EDIT` (unrelated replacement).
        2. For multi-line `M <-> N` blocks, delegates to `_fission_multiline_replace_block`, which
           discovers 1-to-1 skeleton anchor pairs via `find_monotonic_line_matches`, replaces each
           matched pair with an identical unique sentinel `"\\x00__DRIFT_MATCH_k__\\n"`, and runs a
           second-pass `SequenceMatcher` over `sub_src` vs `sub_dst`:
           - `"equal"` spans on sentinels (or raw lines) become 1-to-1 `SINGLE_EDIT` (or `EQUAL`) units.
           - Unmatched intervals between sentinels automatically partition into minimal `INSERT`,
             `DELETE`, or `BLOCK_EDIT` units.
    """
    if i2 - i1 == 1 and j2 - j1 == 1:
        match = compute_skeleton_match(src_tokens[i1], dst_tokens[j1])
        u_type = choose(match.is_valid, DiffUnitType.SINGLE_EDIT, DiffUnitType.BLOCK_EDIT)
        return (DiffUnit(relation, u_type, i1, i2, j1, j2),)

    return _fission_multiline_replace_block(
        src_tokens, dst_tokens, i1, i2, j1, j2, relation
    )


def _expand_raw_opcode_to_units(
    tag: str,
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    src_tokens: Sequence[TokenizedLine],
    dst_tokens: Sequence[TokenizedLine],
    relation: DiffRelation,
) -> Tuple[DiffUnit, ...]:
    """Converts a single first-pass line-level `SequenceMatcher` opcode into atomic `DiffUnit`s."""
    if tag == "equal":
        # Expand each line in the equal span into a 1-to-1 `DiffUnitType.EQUAL` unit
        return tuple(map(lambda offset: DiffUnit(
                    relation=relation,
                    unit_type=DiffUnitType.EQUAL,
                    src_start=i1 + offset, src_end=i1 + offset + 1,
                    dst_start=j1 + offset, dst_end=j1 + offset + 1,
                ),
                range(i2 - i1)))
    if tag == "replace":
        return _fission_replace_block(
            src_tokens, dst_tokens, i1, i2, j1, j2, relation
        )
    return (
        DiffUnit(
            relation=relation,
            unit_type=choose(tag == "insert", DiffUnitType.INSERT, DiffUnitType.DELETE),
            src_start=i1, src_end=i2,
            dst_start=j1, dst_end=j2,
        ),
    )


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
    src_raw = tuple(map(lambda t: t.raw_line, src_tokens))
    dst_raw = tuple(map(lambda t: t.raw_line, dst_tokens))
    return _diff_sequences_to_units(src_raw, dst_raw,
        lambda tag, i1, i2, j1, j2: _expand_raw_opcode_to_units(
            tag, i1, i2, j1, j2, src_tokens, dst_tokens, relation
        ),
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
        1. Projects both `T <-> R` variable slots `[c_1, c_2]` and `R <-> H` host edits
           `[p_1, p_2]` onto the token index axis of `r_line`.
        2. Closed-Interval Overlap Check (`max(p_1, c_1) <= min(p_2, c_2)`):
           Treats both intervals as closed `[start, end]` in token-boundary space so that a host
           edit touching or inserting immediately adjacent to a variable boundary (`p_2 == c_1` or
           `p_1 == c_2`) is conservatively classified as a conflict (Case 1.3 -> returns `None`).
        3. Synthesis (Case 1.2):
           When all host edits are strictly disjoint from all variable slots, splices both sets
           of `R`-axis token replacements via `splice_spans` and returns the newline-stripped
           de-rendered template line `H'`.

    Args:
        t_line: Tokenized template line from `T`.
        r_line: Tokenized rendered baseline line from `R`.
        h_line: Tokenized host-modified line from `H`.

    Returns:
        The synthesized newline-stripped de-rendered template line `H'` if all edits are disjoint
        (Case 1.2), or `None` if any host edit touches a variable slot (Case 1.3).
    """
    # Note: `_coalesce_space_bridged_opcodes` is not needed here because `_TOKEN_RE` greedy-matches
    # maximal `\s+` into a single `SPACE` token `[k, k+1]`. On the integer lattice, the closed-interval
    # union `[b_1, k] U [k+1, b_2]` equals `[b_1, b_2]`, so `overlaps_closed` catches any host edit
    # touching the space bridge, and `splice_spans` preserves `R[k:k+1] == T[a_k:a_k+1]` identically.
    tr_ops = _extract_token_opcodes(t_line, r_line)
    rh_ops = _extract_token_opcodes(r_line, h_line)

    # Both `var_intervals` (R -> T tag restoration) and `host_edits` (R -> H static token edits)
    # are projected onto the shared token index axis `[start, end)` of `r_line.tokens`.
    var_intervals = tuple(map(lambda op: SpliceSpan(
            start=op.b_start,
            end=op.b_end,
            replacement=op.a_tokens,
        ),
        filter(lambda op: not op.is_equal, tr_ops),
    ))
    host_edits = tuple(map(lambda op: SpliceSpan(
            start=op.a_start,
            end=op.a_end,
            replacement=op.b_tokens,
        ),
        filter(lambda op: not op.is_equal, rh_ops),
    ))

    if not var_intervals:
        return h_line.stripped_line
    if not host_edits:
        return t_line.stripped_line

    var_edited = any(starmap(
            lambda h_edit, v_slot: h_edit.overlaps_closed(v_slot),
            product(host_edits, var_intervals),
        ))
    if var_edited:
        return None

    combined_spans = sorted(
        (*var_intervals, *host_edits),
        key=lambda s: (s.start, s.end),
    )
    return "".join(splice_spans(r_line.tokens, combined_spans))


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
      with newline-stripped `derendered_lines=(derendered_line,)`.
    - If de-rendering fails (Case 1.3 overlap): marks both the `TR` unit and the `RH` unit with
      `is_conflict=True`.

    Returns:
        Updated `(tr_units, rh_units)` tuples.
    """
    def _index_single_edits_by_r(units: Sequence[DiffUnit]) -> Dict[int, int]:
        return dict(starmap(lambda idx, u: (u.r_start, idx),
                star_filter(lambda _, u: u.unit_type == DiffUnitType.SINGLE_EDIT,
                    enumerate(units),
                ),
            ))

    # tr_single_by_r maps each `r_idx` to its index in `tr_units` where `unit_type == SINGLE_EDIT`
    # rh_single_by_r maps each `r_idx` to its index in `rh_units` where `unit_type == SINGLE_EDIT`
    tr_single_by_r = _index_single_edits_by_r(tr_units)
    rh_single_by_r = _index_single_edits_by_r(rh_units)
    shared_r_indices = sorted(tr_single_by_r.keys() & rh_single_by_r.keys())
    # check if two have any common line edits in both TR and RH, if not return the original units
    if not shared_r_indices:
        return tuple(tr_units), tuple(rh_units)

    # derendered_row_map is a generator of tuples (r_idx, optional derendered_line)
    # where derendered_line is None if there is a conflict
    derendered_row_map = map(
        lambda r_idx: (
            r_idx,
            try_derender_single_line(
                t_tokens[tr_units[tr_single_by_r[r_idx]].src_start],
                r_tokens[r_idx],
                h_tokens[rh_units[rh_single_by_r[r_idx]].dst_start],
            ),
        ),
        shared_r_indices,
    )
    clean_pairs, conflict_pairs = star_partition(
        lambda _, derendered_line: derendered_line is not None,
        derendered_row_map,
    )

    tr_conflicts: Dict[int, DiffUnit] = dict(starmap(
            lambda r_idx, _: (
                tr_single_by_r[r_idx],
                replace(tr_units[tr_single_by_r[r_idx]], is_conflict=True),
            ),
            conflict_pairs,
        ))
    rh_skeletons: Dict[int, DiffUnit] = dict(starmap(
            lambda r_idx, derendered_line: (
                rh_single_by_r[r_idx],
                replace(
                    rh_units[rh_single_by_r[r_idx]],
                    unit_type=DiffUnitType.SINGLE_SKEL_EDIT,
                    derendered_lines=(derendered_line,),
                ),
            ),
            clean_pairs,
        ))
    rh_conflicts: Dict[int, DiffUnit] = dict(starmap(
            lambda r_idx, _: (
                rh_single_by_r[r_idx],
                replace(rh_units[rh_single_by_r[r_idx]], is_conflict=True),
            ),
            conflict_pairs,
        ))
    rh_updates: Dict[int, DiffUnit] = {**rh_skeletons, **rh_conflicts}
    return (
        tuple(starmap(lambda idx, u: when_none(tr_conflicts.get(idx), u), enumerate(tr_units))),
        tuple(starmap(lambda idx, e: when_none(rh_updates.get(idx), e), enumerate(rh_units))),
    )
