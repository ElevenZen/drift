"""3-Way Orthogonal Classification, Envelope Closure, and Two-Step De-Rendering (Layer 1a-2).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Purpose:
    Given the three file versions of a templated configuration—Template (`T`), Rendered
    Baseline (`R`), and Host Drift (`H`)—this module aligns the fine-grained `T -> R` (`TR`)
    and `R -> H` (`RH`) `DiffUnit` sequences along their shared `R` line coordinate axis.
    It classifies every `TR <-> RH` interaction as either a clean de-renderable edit or a
    3-way conflict (`Cases 0.1–4.2`), closes adjacent conflicts into minimal self-contained
    envelopes, and produces a `TwoStepDerenderPlan` + `AdoptPatch` so clean edits can be
    applied in Step 1 (`T -> T_clean`) and conflict clusters spliced in Step 2
    (`T_clean -> T_merged`).

Pipeline Architecture:
    1. Boundary-Aligned RH_DELETE Normalization:
        split_rh_deletes_at_tr_boundaries(tr_units, rh_units) [Layer 1]
            - Case 2.5b Tagging: marks any 0-R-length `TR` `DELETE` unit (e.g. a false-branch
              `{% if %}` block that produced 0 lines in `R` at point `r_1`) as `is_conflict=True`
              if it lies strictly inside an `RH` `DELETE` span (`d_1 < r_1 < d_2`), whereas a
              single-sided boundary delete (`r_1 == d_1` or `r_1 == d_2`, Case 2.5a) stays clean
            - Boundary Splitting (_split_single_rh_delete): slices each `RH` `DELETE` span `[d_1, d_2)`
              at every interior `TR` unit boundary `pt` (`d_1 < pt < d_2`) so each resulting
              `RH` sub-delete falls within a single `TR` unit's `R`-span
            -> Returns Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]

    2. Orthogonal Matrix Classification Along the Shared `R` Axis:
        _classify_orthogonal_units(tr_units, rh_units) [Layer 2]
            - Evaluates every non-`EQUAL` `RH` unit against `tr_units` via _eval_rh_unit_conflicts:
                * Positive-R-Span RH Edit or Delete (`d_2 > d_1`: `SINGLE_SKEL_EDIT`, `SINGLE_EDIT`,
                  `BLOCK_EDIT`, `DELETE`):
                    - Slices `overlap_idx_range` (`u.r_end > d_1` and `u.r_start < d_2`) and
                      `interior_idx_range` (`u.r_start > d_1` and `u.r_end < d_2`) via `bisect`,
                      then maps to `tr_units` and filters into `overlapping_pos_tr` and
                      `enclosed_tr_dels`
                    - _is_clean_rh_edit_or_delete checks if the interaction is clean:
                        * Cases 1.1, 2.1 (on `TR_EQUAL`), & 4.1: all overlapped `TR` units are
                          `TR_EQUAL` (static lines) with no enclosed `TR_DELETE` -> Clean
                        * Case 1.2: 1-to-1 exact match between `TR_SINGLE_EDIT` and
                          `RH_SINGLE_SKEL_EDIT` -> Clean
                        * Case 2.1 (on `TR_SINGLE_EDIT`) & Case 2.2: complete deletion (`RH_DELETE`
                          with exact `[r_start, r_end)` match) of a single `TR_SINGLE_EDIT` or
                          `TR_BLOCK_EDIT` -> Clean
                        * All other overlaps (Case 1.3 token collision, Case 1.4 single-line edit
                          inside block/insert, Case 2.3 partial block deletion, Case 2.4 deletion of
                          `TR_INSERT`, Case 4.2 `RH_BLOCK_EDIT` touching non-`TR_EQUAL` or enclosing
                          `TR_DELETE`) -> Conflict
                 * Zero-R-Span RH Pure Insertion (`d_1 == d_2 = d`: `RH_INSERT`):
                    - Delegates to _eval_rh_insert_conflicts:
                        * Cases 3.2 & 3.4: `inside_tr_idx` (`u.r_start < d < u.r_end`) is non-empty -> Conflict
                        * Case 3.3: `tr_del_idx` (`u.r_start == d == u.r_end`, i.e. `TR_DELETE` at `d`)
                          is non-empty -> Conflict
                        * Cases 3.1 & 3.5: exterior boundary of `TR_EQUAL` / `TR_SINGLE_EDIT` /
                          `TR_BLOCK_EDIT` (Case 3.1) or `TR_INSERT` (Case 3.5) with no `TR_DELETE`
                          at `d` -> Clean
            - Propagates `is_conflict=True` onto all participating `TR` and `RH` units
            -> Returns Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]

    3. Partitioned `R`-Range Conflict Envelope Closure & Span Resolution:
        _resolve_derender_spans(classified_tr, classified_rh, t_tokens, h_tokens) [Layer 3]
            - Positive/Zero `R`-Span Partitioning:
                * Splits `classified_tr` into `(tr_pos, tr_zero)` (`u.unit_type != DELETE`) and
                  `classified_rh` into `(rh_pos, rh_zero)` (`e.unit_type != INSERT`) via `partition`
            - Positive-Length Conflict Envelopes (_merge_positive_conflict_r_ranges -> _build_positive_conflict_spans):
                * Merges strictly overlapping positive conflict `R`-intervals (`cur_s < prev_e`), keeping
                  merely touching intervals (`cur_s == prev_e`) split as independent smaller envelopes
                * Selects `tr_pos` and `rh_pos` sub-slices via `bisect` for each merged `[r_start, r_end)`
                  range, automatically enclosing any strictly interior (`r_start < pt < r_end`)
                  zero-length units
            - Zero-Length Conflict Envelopes (_build_zero_conflict_spans):
                * Collects conflicting `pt` coordinates (`conflict_zero_r_points`) from `tr_zero` and
                  `rh_zero` not strictly inside any positive conflict range
                  (`bisect_right(pos_conf_ends, pt) == bisect_left(pos_conf_starts, pt)`)
                * Selects all `tr_zero` and `rh_zero` units at `pt` via `bisect` and groups them into
                  a single zero-length conflict `_DerenderSpan` at `R[pt:pt]`
            - Clean Edit Span Resolution (_build_clean_edit_spans):
                * Maps non-conflicting positive `rh_pos` edits and clean `rh_zero` insertions to
                  their `[t_start, t_end)` bounds via `bisect` on `tr_pos`, storing newline-stripped
                  `replacement` lines and explicit `old_eof_newline` / `new_eof_newline` flags
            - Returns all active `_DerenderSpan`s sorted canonically by `(r_start, r_end)`

    4. Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`):
        build_two_step_derender_plan(t_lines, r_lines, h_lines) [Layer 4]
            - Tokenizes `T, R, H` (`tokenize_line`), runs hunk fission (`fission_diff_units`),
              refines `1 <-> 1 <-> 1` lines (`refine_single_line_units`), splits `RH` deletes
              (`split_rh_deletes_at_tr_boundaries`), classifies conflicts (`_classify_orthogonal_units`),
              and resolves sorted `_DerenderSpan`s (`_resolve_derender_spans`)
            - Step 1 Clean Patch & `T_clean` Synthesis:
                * Maps non-conflicting spans to `CleanPatchHunk`s via `span.to_clean_hunk(t_tokens)`
                * Applies clean spans onto `t_lines` via `_apply_clean_spans_to_template` to produce
                  `t_clean_lines` while leaving conflicting template regions untouched
            - Step 2 Conflict Cluster Anchoring (_anchor_conflict_clusters_to_t_clean):
                * Computes cumulative Step 1 line-offset shifts via `itertools.accumulate`
                * Anchors each conflicting `_DerenderSpan` to its shifted `[t_clean_start, t_clean_end)`
                  coordinates in `t_clean_lines`
            -> Returns TwoStepDerenderPlan(t_lines, t_clean_lines, clean_hunks, conflict_clusters)

    5. Display Unified Diff Synthesis & Patch Entry Point:
        generate_derendered_patch(target_file, patch_from, render_pkg_dir, ...) [Layer 5]
            - Reads `T` from `target_file`, `H` from `patch_from`, and `R` via
              read_rendered_baseline_lines (raising `SafeguardError` if `R` is missing)
            - Builds `TwoStepDerenderPlan` via `build_two_step_derender_plan`
            - Synthesizes human-readable preview diff via build_display_unified_diff:
                * Splices Step 2 `ConflictCluster` `diff3` marker blocks onto `t_clean_lines` via
                  `splice_conflict_clusters_diff3` (`adopt_patch.py`) to form `t_merged_lines`
                * Runs standard `difflib.unified_diff(t_lines, t_merged_lines)` anchored directly
                  on the raw template `T`
            -> Returns AdoptPatch

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Boundary-Aligned RH_DELETE Normalization
        _get_interior_cut_points
        _split_single_rh_delete
        split_rh_deletes_at_tr_boundaries
    Layer 2: Orthogonal Matrix Classification Along the Shared `R` Axis
        _is_clean_rh_edit_or_delete
        _eval_rh_edit_or_delete_conflicts
        _eval_rh_insert_conflicts
        _eval_rh_unit_conflicts
        _classify_orthogonal_units
    Layer 3: Partitioned `R`-Range Conflict Envelope Closure & Span Resolution
        _DerenderSpan
        _resolve_t_point_at_r
        _resolve_h_point_at_r
        _merge_positive_conflict_r_ranges
        _build_positive_conflict_spans
        _build_zero_conflict_spans
        _extract_clean_pos_replacement
        _build_clean_edit_spans
        _resolve_derender_spans
    Layer 4: Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`)
        _apply_clean_spans_to_template
        _anchor_conflict_clusters_to_t_clean
        build_two_step_derender_plan
    Layer 5: Display Unified Diff Synthesis & Patch Entry Point
        build_display_unified_diff
        read_rendered_baseline_lines
        generate_derendered_patch
===============================================================================
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
import difflib
from functools import reduce
from itertools import accumulate, chain, starmap
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from ..core.exceptions import SafeguardError
from ..models.adopt_models import (
    AdoptPatch,
    CleanPatchHunk,
    ConflictCluster,
    DiffRelation,
    DiffUnit,
    DiffUnitType,
    TokenizedLine,
    TwoStepDerenderPlan,
)
from ..utils.func_utils import branch, choose, partition
from .adopt_fission import (
    fission_diff_units,
    refine_single_line_units,
    tokenize_line,
)
from .adopt_patch import splice_conflict_clusters_diff3

if TYPE_CHECKING:
    from ..config.render_engine_config import RenderSourceMatch


# ==============================================================================
# Layer 1: Boundary-Aligned RH_DELETE Normalization
# ==============================================================================


def _get_interior_cut_points(
    d1: int,
    d2: int,
    tr_starts: Sequence[int],
) -> Sequence[int]:
    """Returns the sorted slice of `tr_starts` strictly inside `(d1, d2)` in O(log M) time."""
    return tr_starts[bisect_right(tr_starts, d1) : bisect_left(tr_starts, d2)]


def _split_single_rh_delete(
    rh_unit: DiffUnit,
    tr_starts: Sequence[int],
) -> List[DiffUnit]:
    """Splits a single `RH` `DELETE` unit `[d_1, d_2)` at every interior `R`-axis cut point.

    When a host deletion in `H` spans multiple adjacent `TR` units in `R` (for example, deleting
    both a static `TR_EQUAL` line and an adjacent `TR_SINGLE_EDIT` line in one sweep), splitting
    the `RH_DELETE` at `TR` boundaries lets each sub-delete be classified independently against
    the single `TR` unit it covers.
    """
    if rh_unit.unit_type != DiffUnitType.DELETE:
        return [rh_unit]
    d1, d2 = rh_unit.r_start, rh_unit.r_end
    interior = _get_interior_cut_points(d1, d2, tr_starts)
    if not interior:
        return [rh_unit]
    endpoints = [d1, *interior, d2]
    return list(map(
            lambda span: DiffUnit(
                relation=DiffRelation.RH,
                unit_type=DiffUnitType.DELETE,
                src_start=span[0],
                src_end=span[1],
                dst_start=rh_unit.dst_start,
                dst_end=rh_unit.dst_end,
                is_conflict=rh_unit.is_conflict,
            ),
            zip(endpoints[:-1], endpoints[1:]),
    ))


def split_rh_deletes_at_tr_boundaries(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]:
    """Normalizes `RH` `DELETE` spans along `TR` unit boundaries and marks Case 2.5b conflicts.

    Why Case 2.5b must be tagged before splitting:
        A `TR` `DELETE` at `r_1` represents template lines in `T` that produced 0 lines in `R`
        (such as an inactive `{% if false %}...{% endif %}` block).
        - **Case 2.5b (Conflict)**: If the host deletes a span `[d_1, d_2)` that *strictly encloses*
          `r_1` (`d_1 < r_1 < d_2`), the host deleted the lines on both sides of the invisible
          template block; silently deleting or keeping the hidden `{% if %}` block is ambiguous,
          so the `TR` `DELETE` is marked `is_conflict=True` before `[d_1, d_2)` is split at `r_1`.
        - **Case 2.5a (Clean)**: If `r_1 == d_1` or `r_1 == d_2` (the host only deleted lines on
          one side of the inactive block), the inactive template block remains untouched and
          `is_conflict` stays `False`.
    """
    tr_uniq_starts = tuple(dict.fromkeys(u.r_start for u in tr_units))
    rh_deletes = filter(lambda e: e.unit_type == DiffUnitType.DELETE, rh_units)
    enclosed_cut_points = set(chain.from_iterable(map(
            lambda e: _get_interior_cut_points(e.r_start, e.r_end, tr_uniq_starts),
            rh_deletes,
    )))
    # Case 2.5b has to be tagged before splitting RH_DELETE units
    updated_tr = tuple(map(
            lambda u: choose(
                u.unit_type == DiffUnitType.DELETE and u.r_start in enclosed_cut_points,
                replace(u, is_conflict=True),
                u,
            ),
            tr_units,
    ))
    split_rh = tuple(chain.from_iterable(map(
            lambda e: _split_single_rh_delete(e, tr_uniq_starts),
            rh_units
    )))
    return updated_tr, split_rh


# ==============================================================================
# Layer 2: Orthogonal Matrix Classification Along the Shared `R` Axis
# ==============================================================================


def _is_clean_rh_edit_or_delete(
    rh_unit: DiffUnit,
    overlapping_pos_tr: Sequence[DiffUnit],
    enclosed_tr_deletes: Sequence[DiffUnit],
) -> bool:
    """Returns True if a positive-`R`-span `RH` edit or delete (`d_2 > d_1`) is clean under the Orthogonal Matrix.

    Clean Positive-Span Cases (reference: `docs/features/template_adopt_derendering_draft.md` §3.4):
        - **Cases 1.1, 2.1 (on `TR_EQUAL`), & 4.1**: All overlapped positive-length `TR` units are
          `TR_EQUAL` (static lines in `T`), and no `TR_DELETE` is strictly enclosed inside `[d_1, d_2)`.
        - **Case 1.2**: `rh_unit` is a `SINGLE_SKEL_EDIT` whose `R`-span matches a single
          `TR_SINGLE_EDIT` unit `1 <-> 1`.
        - **Case 2.1 (on `TR_SINGLE_EDIT`) & Case 2.2**: `rh_unit` is an `RH_DELETE` whose `R`-span
          exactly equals the entire `[r_start, r_end)` span of a single `TR_SINGLE_EDIT` (Case 2.1)
          or `TR_BLOCK_EDIT` (Case 2.2), meaning the entire templated line or block can be cleanly
          deleted from `T`.

    Conflict Positive-Span Cases (returns `False`):
        - **Case 1.3**: `rh_unit.is_conflict` is already `True` (token collision on `1 <-> 1 <-> 1`
          flagged by `refine_single_line_units`).
        - **Case 1.4**: `RH_SINGLE_EDIT` / `RH_SINGLE_SKEL_EDIT` overlapping a `TR_BLOCK_EDIT` or `TR_INSERT`.
        - **Case 2.3**: `RH_DELETE` partially covering a multi-line `TR_BLOCK_EDIT` (`not exact_r_match`).
        - **Case 2.4**: `RH_DELETE` (complete or partial) over a `TR_INSERT` ($0 \to N$).
        - **Case 4.2**: `RH_BLOCK_EDIT` touching any non-`TR_EQUAL` unit (`TR_SINGLE_EDIT`,
          `TR_BLOCK_EDIT`, `TR_INSERT`) or enclosing a `TR_DELETE` (`enclosed_tr_deletes`).
    """
    # rh_unit.is_conflict catches Case 1.3 (1 <-> 1 <-> 1 token collision from Layer 1a-1);
    # enclosed_tr_deletes catches Case 4.2 when an RH_BLOCK_EDIT encloses a 0-R-length TR_DELETE.
    if rh_unit.is_conflict or enclosed_tr_deletes:
        return False
    if not overlapping_pos_tr:
        return True

    # Cases 1.1 (RH_SINGLE_EDIT), 2.1 (RH_DELETE on TR_EQUAL), & 4.1 (RH_BLOCK_EDIT):
    # All overlapped positive TR units are TR_EQUAL
    if all(u.unit_type == DiffUnitType.EQUAL for u in overlapping_pos_tr):
        return True

    # Case 4.2 (multi-unit): RH_BLOCK_EDIT crosses the boundary of multiple TR units
    # where at least one unit is non-TR_EQUAL.
    if len(overlapping_pos_tr) != 1:
        return False

    tr_u = overlapping_pos_tr[0]
    exact_r_match = rh_unit.r_start == tr_u.r_start and rh_unit.r_end == tr_u.r_end
    # Partial overlap inside a larger indivisible TR_BLOCK_EDIT or TR_INSERT:
    # catches Case 1.4 (multi-line), Case 2.3 (partial RH_DELETE on TR_BLOCK_EDIT),
    # Case 2.4 (partial RH_DELETE on TR_INSERT), and Case 4.2 (partial RH_BLOCK_EDIT).
    if not exact_r_match:
        return False

    # Case 1.2: 1 <-> 1 <-> 1 clean skeleton edit
    if (
        tr_u.unit_type == DiffUnitType.SINGLE_EDIT
        and rh_unit.unit_type == DiffUnitType.SINGLE_SKEL_EDIT
    ):
        return True

    # Case 2.1 (on TR_SINGLE_EDIT) & Case 2.2 (complete deletion of TR_BLOCK_EDIT)
    if rh_unit.unit_type == DiffUnitType.DELETE and tr_u.unit_type in (
        DiffUnitType.SINGLE_EDIT,
        DiffUnitType.BLOCK_EDIT,
    ):
        return True

    # Remaining exact-R-match conflict cases:
    # - Case 1.3: RH_SINGLE_EDIT on TR_SINGLE_EDIT (also caught above by rh_unit.is_conflict)
    # - Case 1.4 (1-line): RH_SINGLE_EDIT / RH_SINGLE_SKEL_EDIT on 1-line TR_BLOCK_EDIT or TR_INSERT
    # - Case 2.4 (complete): RH_DELETE over TR_INSERT (0 -> N block has 0 lines in T)
    # - Case 4.2 (single-unit): RH_BLOCK_EDIT over TR_SINGLE_EDIT, TR_BLOCK_EDIT, or TR_INSERT
    return False


def _eval_rh_edit_or_delete_conflicts(
    rh_idx: int,
    rh_unit: DiffUnit,
    tr_units: Sequence[DiffUnit],
    tr_r_starts: Sequence[int],
    tr_r_ends: Sequence[int],
) -> Tuple[Set[int], Set[int]]:
    """Evaluates positive-R-span `RH` edits and deletes (`d_2 > d_1`) against `tr_units`.

    - `overlap_idx_range = range(bisect_right(tr_r_ends, d_1), bisect_left(tr_r_starts, d_2))`
      bounds all `TR` units with `u.r_end > d_1` and `u.r_start < d_2`, including any `TR`
      units strictly enclosed inside `(d_1, d_2)`.
    - `interior_idx_range = range(bisect_right(tr_r_starts, d_1), bisect_left(tr_r_ends, d_2))`
      bounds all `TR` units strictly inside `(d_1, d_2)` (`d_1 < u.r_start <= u.r_end < d_2`).
    """
    d1, d2 = rh_unit.r_start, rh_unit.r_end
    overlap_idx_range = range(bisect_right(tr_r_ends, d1), bisect_left(tr_r_starts, d2))
    interior_idx_range = range(bisect_right(tr_r_starts, d1), bisect_left(tr_r_ends, d2))
    overlapping_pos_tr = tuple(filter(
        lambda u: u.unit_type != DiffUnitType.DELETE,
        map(lambda i: tr_units[i], overlap_idx_range),
    ))
    enclosed_tr_dels = tuple(filter(
        lambda u: u.unit_type == DiffUnitType.DELETE,
        map(lambda i: tr_units[i], interior_idx_range),
    ))
    return branch(
        _is_clean_rh_edit_or_delete(rh_unit, overlapping_pos_tr, enclosed_tr_dels),
        lambda: (set(), set()),
        lambda: (set(overlap_idx_range), {rh_idx}),
    )


def _eval_rh_insert_conflicts(
    rh_idx: int,
    r_insert_point: int,
    tr_r_starts: Sequence[int],
    tr_r_ends: Sequence[int],
) -> Tuple[Set[int], Set[int]]:
    """Evaluates Cases 3.1–3.5 for a pure `RH_INSERT` at `r_insert_point` on the `R` axis.
    Returns empty sets if `rh_unit` is clean, otherwise returns the index sets of all conflicting
    `tr_units` and `rh_units` (the latter always has length 0 or 1).

    - **Cases 3.2 & 3.4 (Conflict)**: `inside_tr_idx` (`u.r_start < r_insert_point < u.r_end`)
      contains 1 item when `r_insert_point` falls strictly inside a multi-line `TR_BLOCK_EDIT`
      (Case 3.2) or `TR_INSERT` (Case 3.4). Because `r_insert_point` is strictly interior, no
      `TR` unit can have `r_start == r_insert_point == r_end`, so we return immediately.
    - **Case 3.3 (Conflict)**: `tr_del_idx`
      (`range(bisect_left(tr_r_starts, r_insert_point), bisect_right(tr_r_ends, r_insert_point))`)
      bounds all `TR` units satisfying `u.r_start == r_insert_point == u.r_end` (i.e. 0-R-length
      `TR_DELETE` units at `r_insert_point`). Touching a `TR_DELETE` induces a 1-to-many
      loss-of-anchor conflict.
    - **Cases 3.1 & 3.5 (Clean)**: Otherwise `r_insert_point` lies on an exterior boundary of
      `TR_EQUAL` / `TR_SINGLE_EDIT` / `TR_BLOCK_EDIT` (Case 3.1) or `TR_INSERT` (Case 3.5) with
      no `TR_DELETE` at `r_insert_point`.
    """
    inside_tr_idx = range(
        bisect_right(tr_r_ends, r_insert_point),
        bisect_left(tr_r_starts, r_insert_point),
    )
    if inside_tr_idx:
        return (set(inside_tr_idx), {rh_idx})

    tr_del_idx = range(
        bisect_left(tr_r_starts, r_insert_point),
        bisect_right(tr_r_ends, r_insert_point),
    )
    return choose(
        bool(tr_del_idx),
        (set(tr_del_idx), {rh_idx}),
        (set(), set()),
    )


def _eval_rh_unit_conflicts(
    rh_idx: int,
    rh_unit: DiffUnit,
    tr_units: Sequence[DiffUnit],
    tr_r_starts: Sequence[int],
    tr_r_ends: Sequence[int],
) -> Tuple[Set[int], Set[int]]:
    """Computes `(conflicting_tr_indices, conflicting_rh_indices)` induced by `rh_units[rh_idx]`.
    Returns empty sets if `rh_unit` is clean, otherwise returns the index sets of all conflicting
    `tr_units` and `rh_units` (the latter always has length 0 or 1).

    Uses `bisect` over the monotonically non-decreasing `tr_r_starts` and `tr_r_ends` sequences
    to locate overlapping and enclosed `TR` units in O(log M) time across two geometric regimes:
        1. **Positive-R-Span Edit or Delete (`d_2 > d_1`)**: Delegates to `_eval_rh_edit_or_delete_conflicts`.
        2. **Zero-R-Span Pure Insertion (`d_1 == d_2 = d`, `RH_INSERT`)**: Delegates to
           `_eval_rh_insert_conflicts` for Cases 3.1–3.5.
    """
    if rh_unit.unit_type == DiffUnitType.EQUAL:
        return (set(), set())

    return branch(
        rh_unit.r_end > rh_unit.r_start,
        lambda: _eval_rh_edit_or_delete_conflicts(
            rh_idx, rh_unit, tr_units, tr_r_starts, tr_r_ends
        ),
        lambda: _eval_rh_insert_conflicts(
            rh_idx, rh_unit.r_start, tr_r_starts, tr_r_ends
        ),
    )


def _classify_orthogonal_units(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]:
    """Classifies all `TR` and `RH` units along the `R` axis according to Orthogonal Matrix Cases `0.1–4.2`.
    Returning updated `(tr_units, rh_units)` tuples with `is_conflict=True` on all conflicting units.

    Unions pre-existing conflict flags (from Layer 1a-1 Case 1.3 token collisions and Stage 1
    Case 2.5b enclosed `TR_DELETE`s) with the conflict index sets computed by `_eval_rh_unit_conflicts`,
    """
    tr_r_starts = [u.r_start for u in tr_units]
    tr_r_ends = [u.r_end for u in tr_units]
    # per_rh_sets: list of tuples of sets (conflicting_tr_indices, rh_indices(with len 0 or 1)) for each rh_unit
    # the item can be empty sets.
    per_rh_sets = [
        _eval_rh_unit_conflicts(rh_idx, rh_unit, tr_units, tr_r_starts, tr_r_ends)
        for rh_idx, rh_unit in enumerate(rh_units)
    ]
    init_tr = {idx for idx, u in enumerate(tr_units) if u.is_conflict}
    init_rh = {idx for idx, e in enumerate(rh_units) if e.is_conflict}
    conflict_tuples = [*per_rh_sets, (init_tr, init_rh)]
    conflict_tr_indices = set(chain.from_iterable(map(lambda pair: pair[0], conflict_tuples)))
    conflict_rh_indices = set(chain.from_iterable(map(lambda pair: pair[1], conflict_tuples)))
    return (
        tuple(starmap(
            lambda tr_idx, u: choose(tr_idx in conflict_tr_indices, replace(u, is_conflict=True), u),
            enumerate(tr_units)
        )),
        tuple(starmap(
            lambda rh_idx, e: choose(rh_idx in conflict_rh_indices, replace(e, is_conflict=True), e),
            enumerate(rh_units)
        )),
    )


# ==============================================================================
# Layer 3: Partitioned `R`-Range Conflict Envelope Closure & Span Resolution
# ==============================================================================


@dataclass(frozen=True)
class _DerenderSpan:
    """Resolved `(T, R, H)` span representing either a Step 1 clean edit or a Step 2 conflict envelope.

    Ordered canonically by `(r_start, r_end)` along the shared `R` coordinate axis:
        - Point spans `(d, d)` at boundary `d` naturally sort before `[d, r_2)` (`(d, d) < (d, r_2)`)
          and after `[r_1, d)` (`(r_1, d) < (d, d)`).
        - Across any sorted sequence of disjoint `_DerenderSpan`s, `span_k.t_end <= span_{k+1}.t_start`
          holds monotonically on `T`.
        - For clean spans (`is_conflict == False`), `replacement` stores newline-stripped lines
          alongside explicit `old_eof_newline` and `new_eof_newline` flags.
    """
    t_start: int
    t_end: int
    r_start: int
    r_end: int
    h_start: int
    h_end: int
    is_conflict: bool
    replacement: Tuple[str, ...] = ()
    old_eof_newline: bool = True
    new_eof_newline: bool = True

    def to_clean_hunk(self, t_tokens: Sequence[TokenizedLine]) -> CleanPatchHunk:
        """Converts a non-conflicting span directly into a Step 1 `CleanPatchHunk`."""
        return CleanPatchHunk(
            t_start=self.t_start,
            t_end=self.t_end,
            old_lines=tuple(t.stripped_line for t in t_tokens[self.t_start : self.t_end]),
            new_lines=self.replacement,
            old_eof_newline=self.old_eof_newline,
            new_eof_newline=self.new_eof_newline,
        )


def _resolve_t_point_at_r(
    r_point: int,
    tr_pos: Sequence[DiffUnit],
    tr_pos_starts: Sequence[int],
) -> int:
    """Resolves an exterior `R`-boundary coordinate `r_point` (with no `TR_DELETE` at `r_point`)
    to its corresponding 0-based line coordinate in `T` in O(log M) time.
    """
    idx = bisect_left(tr_pos_starts, r_point)
    return branch(idx > 0, lambda: tr_pos[idx - 1].src_end, lambda: 0)


def _resolve_h_point_at_r(
    r_point: int,
    rh_pos: Sequence[DiffUnit],
    rh_pos_starts: Sequence[int],
) -> int:
    """Resolves an exterior `R`-boundary coordinate `r_point` (with no `RH_INSERT` at `r_point`)
    to its corresponding 0-based line coordinate in `H` in O(log N) time.
    """
    idx = bisect_left(rh_pos_starts, r_point)
    return branch(idx > 0, lambda: rh_pos[idx - 1].dst_end, lambda: 0)


def _merge_positive_conflict_r_ranges(
    tr_pos: Sequence[DiffUnit],
    rh_pos: Sequence[DiffUnit],
) -> Tuple[Tuple[int, int], ...]:
    """Computes merged positive-length conflict `[r_start, r_end)` intervals on the `R` axis.

    Two sorted positive conflict intervals `(prev_s, prev_e)` and `(cur_s, cur_e)` merge only when
    they strictly overlap (`cur_s < prev_e`). Intervals that merely touch (`cur_s == prev_e`) have
    a synchronized `(T, R, H)` boundary at `prev_e` and remain split as independent, finer-grained
    conflict blocks.
    """
    raw_ranges = sorted(map(
        lambda u: (u.r_start, u.r_end),
        filter(lambda u: u.is_conflict, chain(tr_pos, rh_pos)),
    ))

    def _step(
        acc: List[Tuple[int, int]],
        span: Tuple[int, int],
    ) -> List[Tuple[int, int]]:
        if acc and span[0] < acc[-1][1]:
            return [*acc[:-1], (acc[-1][0], max(acc[-1][1], span[1]))]
        return [*acc, span]

    return tuple(reduce(_step, raw_ranges, []))


def _build_positive_conflict_spans(
    positive_conflict_ranges: Sequence[Tuple[int, int]],
    tr_pos: Sequence[DiffUnit],
    tr_pos_starts: Sequence[int],
    tr_pos_ends: Sequence[int],
    rh_pos: Sequence[DiffUnit],
    rh_pos_starts: Sequence[int],
    rh_pos_ends: Sequence[int],
) -> Tuple[_DerenderSpan, ...]:
    """Selects `tr_pos` and `rh_pos` slices via `bisect` for each merged positive conflict `R`-range.

    Because `tr_sub[0].src_start .. tr_sub[-1].src_end` and `rh_sub[0].dst_start .. rh_sub[-1].dst_end`
    span the entire `(T, R, H)` interval between the first and last positive units in `[r_start, r_end)`,
    any zero-length units strictly inside `(r_start, r_end)` (`r_start < pt < r_end`) are enclosed
    automatically.
    """
    tr_subs = map(
        lambda r: tr_pos[bisect_right(tr_pos_ends, r[0]) : bisect_left(tr_pos_starts, r[1])],
        positive_conflict_ranges,
    )
    rh_subs = map(
        lambda r: rh_pos[bisect_right(rh_pos_ends, r[0]) : bisect_left(rh_pos_starts, r[1])],
        positive_conflict_ranges,
    )
    return tuple(starmap(
        lambda r_span, tr_sub, rh_sub: _DerenderSpan(
            t_start=tr_sub[0].src_start,
            t_end=tr_sub[-1].src_end,
            r_start=r_span[0],
            r_end=r_span[1],
            h_start=rh_sub[0].dst_start,
            h_end=rh_sub[-1].dst_end,
            is_conflict=True,
        ),
        zip(positive_conflict_ranges, tr_subs, rh_subs),
    ))


def _build_zero_conflict_spans(
    tr_zero: Sequence[DiffUnit],
    rh_zero: Sequence[DiffUnit],
    pos_conf_starts: Sequence[int],
    pos_conf_ends: Sequence[int],
    tr_pos: Sequence[DiffUnit],
    tr_pos_starts: Sequence[int],
    rh_pos: Sequence[DiffUnit],
    rh_pos_starts: Sequence[int],
) -> Tuple[_DerenderSpan, ...]:
    """Groups conflicting zero-length units outside `(r_start, r_end)` by their `R`-coordinate `pt`.

    At any boundary point `pt` not strictly inside a positive conflict envelope
    (`bisect_right(pos_conf_ends, pt) == bisect_left(pos_conf_starts, pt)`), if any zero-length
    unit at `pt` is conflicting, all zero-length units at `R[pt]` are conflicting (Case 3.3 / 2.5b)
    and are grouped into a single zero-length conflict envelope at `R[pt:pt]`.
    """
    def _is_uncovered_zero_conflict(z: DiffUnit) -> bool:
        return (
            z.is_conflict
            and bisect_right(pos_conf_ends, z.r_start) == bisect_left(pos_conf_starts, z.r_start)
        )

    conflict_zero_r_points = sorted(set(map(
        lambda z: z.r_start,
        filter(_is_uncovered_zero_conflict, chain(tr_zero, rh_zero)),
    )))

    tr_zero_pts = [u.r_start for u in tr_zero]
    rh_zero_pts = [e.r_start for e in rh_zero]

    def build_span_at_r_point(pt: int) -> _DerenderSpan:
        tr_z_sub = tr_zero[bisect_left(tr_zero_pts, pt) : bisect_right(tr_zero_pts, pt)]
        rh_z_sub = rh_zero[bisect_left(rh_zero_pts, pt) : bisect_right(rh_zero_pts, pt)]
        t_start, t_end = branch(
            bool(tr_z_sub),
            lambda: (tr_z_sub[0].src_start, tr_z_sub[-1].src_end),
            lambda: (_resolve_t_point_at_r(pt, tr_pos, tr_pos_starts),) * 2,
        )
        h_start, h_end = branch(
            bool(rh_z_sub),
            lambda: (rh_z_sub[0].dst_start, rh_z_sub[-1].dst_end),
            lambda: (_resolve_h_point_at_r(pt, rh_pos, rh_pos_starts),) * 2,
        )
        return _DerenderSpan(
            t_start=t_start,
            t_end=t_end,
            r_start=pt,
            r_end=pt,
            h_start=h_start,
            h_end=h_end,
            is_conflict=True,
        )

    return tuple(map(build_span_at_r_point, conflict_zero_r_points))


def _extract_clean_pos_replacement(
    rh_unit: DiffUnit,
    t_start: int,
    t_end: int,
    t_tokens: Sequence[TokenizedLine],
    h_tokens: Sequence[TokenizedLine],
) -> Optional[Tuple[str, ...]]:
    """Computes the newline-stripped replacement lines for a clean positive-R-span `rh_unit`, or `None` if unchanged.

    Handles three clean replacement modes:
        1. **EOF-Newline-Only Drift on `RH_EQUAL`**: When `rh_unit` is at EOF and `H`'s final line
           gained/lost a trailing newline relative to `T`, returns newline-stripped `t_tokens[t_start:t_end]`
           (the EOF newline change itself is carried by `_DerenderSpan.new_eof_newline`); otherwise `None`.
        2. **De-Rendered Skeleton Edit (`SINGLE_SKEL_EDIT`, Case 1.2)**: Returns newline-stripped
           `derendered_lines` (`H'`) computed by Layer 1a-1 token calculus.
        3. **Static Edit / Full Block Deletion (Cases 1.1, 2.1, 2.2, 4.1)**: Returns
           `tuple(h.stripped_line for h in h_tokens[rh_unit.dst_start : rh_unit.dst_end])` (`()` for deletions).
    """
    if rh_unit.unit_type == DiffUnitType.EQUAL:
        is_eof_nl_diff = (
            # touches file ends
            t_end == len(t_tokens) and rh_unit.dst_end == len(h_tokens)
            # non-empty T span (guards against Case 4.1 TR_INSERT + RH_EQUAL at EOF where t_start == t_end)
            and t_end > t_start
            # eof nl difference
            and t_tokens[t_end - 1].has_newline != h_tokens[rh_unit.dst_end - 1].has_newline
        )
        if not is_eof_nl_diff:
            return None
        return tuple(t.stripped_line for t in t_tokens[t_start:t_end])

    if (
        rh_unit.unit_type == DiffUnitType.SINGLE_SKEL_EDIT
        and rh_unit.derendered_lines
    ):
        return rh_unit.derendered_lines

    return tuple(h.stripped_line for h in h_tokens[rh_unit.dst_start : rh_unit.dst_end])


def _build_clean_edit_spans(
    rh_pos: Sequence[DiffUnit],
    rh_zero: Sequence[DiffUnit],
    pos_conf_starts: Sequence[int],
    pos_conf_ends: Sequence[int],
    tr_pos: Sequence[DiffUnit],
    tr_pos_starts: Sequence[int],
    tr_pos_ends: Sequence[int],
    t_tokens: Sequence[TokenizedLine],
    h_tokens: Sequence[TokenizedLine],
) -> Tuple[_DerenderSpan, ...]:
    """Resolves all non-conflicting `rh_pos` and `rh_zero` edits into clean `_DerenderSpan`s via `bisect`.

    Pipeline Stages:
        1. **Positive-`R`-Length Clean Edits (`pos_clean_spans` — Cases 1.1, 1.2, 2.1, 2.3, 2.4, and EOF-newline drift)**:
           - **Candidate Filtering (`candidate_pos_rh`)**: Keeps `rh_pos` units `e` that are either
             non-`EQUAL` or touch the end of `H` (`e.dst_end == len(h_tokens)`, to detect EOF-newline-only
             drift), AND are strictly disjoint from every merged positive conflict interval in
             `[pos_conf_starts, pos_conf_ends)` (`bisect_right(pos_conf_ends, e.r_start) == bisect_left(pos_conf_starts, e.r_end)`).
           - **`R -> T` Range Projection (`pos_t_ranges`)**: Slices the overlapping `tr_pos` units via
             `bisect` to project `[e.r_start, e.r_end)` onto `[t_start, t_end) = (tr_sub[0].src_start, tr_sub[-1].src_end)`.
             Any boundary `TR_DELETE` (`tr_zero`) outside `tr_sub` is naturally excluded and preserved in `T`.
           - **Replacement Extraction (`pos_replacements` / `valid_pos_clean`)**: Calls
             `_extract_clean_pos_replacement` to obtain the newline-stripped replacement lines (`H'` for
             `SINGLE_SKEL_EDIT`, `H` slice for static edits/deletions, or `T` slice for EOF-newline-only
             drift), dropping `None` (unchanged `RH_EQUAL` at EOF).
        2. **Zero-`R`-Length Clean Insertions (`zero_clean_spans` — Cases 3.1 and 3.5)**:
           - Non-conflicting `tr_zero` (`TR_DELETE`) units require no span because unrendered template
             blocks remain untouched in `T`. If a `TR_DELETE` and `RH_INSERT` ever share the same `R`
             point `pt` (Case 3.3), Layer 2 marks both as conflicts.
           - Thus, every non-conflicting unit in `rh_zero` is a pure `RH_INSERT` at an `R`-boundary `pt`
             with no `TR_DELETE`. Each unique `pt` is projected to a single insertion point
             `t_pt = _resolve_t_point_at_r(pt, tr_pos, tr_pos_starts)` in `T` and paired with the
             inserted host lines `h_tokens[rh_z_sub[0].dst_start : rh_z_sub[-1].dst_end]`.
    """
    def is_disjoint_from_all_conflicts(e: DiffUnit) -> bool:
        return bisect_right(pos_conf_ends, e.r_start) == bisect_left(pos_conf_starts, e.r_end)

    candidate_pos_rh = tuple(filter(
        lambda e: (
            (e.unit_type != DiffUnitType.EQUAL or e.dst_end == len(h_tokens))
            and is_disjoint_from_all_conflicts(e)
        ),
        rh_pos,
    ))
    pos_t_ranges = tuple(map(
        lambda tr_sub: (tr_sub[0].src_start, tr_sub[-1].src_end),
        map(
            lambda e: tr_pos[bisect_right(tr_pos_ends, e.r_start) : bisect_left(tr_pos_starts, e.r_end)],
            candidate_pos_rh,
        ),
    ))
    pos_replacements = starmap(
        lambda pos_rh, t_range: _extract_clean_pos_replacement(
            pos_rh, t_range[0], t_range[1], t_tokens, h_tokens
        ),
        zip(candidate_pos_rh, pos_t_ranges),
    )
    valid_pos_clean = filter(
        # None indicates unchanged RH_EQUAL at EOF, which is not a changed span
        lambda item: item[2] is not None,
        zip(candidate_pos_rh, pos_t_ranges, pos_replacements),
    )
    pos_clean_spans = tuple(starmap(
        lambda e, t_rng, repl: _DerenderSpan(
            t_start=t_rng[0],
            t_end=t_rng[1],
            r_start=e.r_start,
            r_end=e.r_end,
            h_start=e.dst_start,
            h_end=e.dst_end,
            is_conflict=False,
            replacement=repl,
            old_eof_newline=(
                t_rng[1] < len(t_tokens)
                or t_rng[0] == t_rng[1]
                or t_tokens[t_rng[1] - 1].has_newline
            ),
            new_eof_newline=(
                e.dst_end < len(h_tokens)
                or e.dst_start == e.dst_end
                or h_tokens[e.dst_end - 1].has_newline
            ),
        ),
        valid_pos_clean,
    ))

    # here we assume rh_zero won't meet tr_zero,
    # otherwise it's a conflict because RH_INSERT and TR_DELETE are touching at the same R point
    # so all non-conflicting tr_zeros are handled above.
    # rh_zero are only RH_INSERT, so we can just find the corresponding T point for each R point.
    rh_zero_pts = [e.r_start for e in rh_zero]
    clean_zero_r_points = sorted(set(map(
        lambda e: e.r_start,
        filter(lambda e: not e.is_conflict, rh_zero),
    )))
    rh_zero_subs = map(
        lambda pt: rh_zero[bisect_left(rh_zero_pts, pt) : bisect_right(rh_zero_pts, pt)],
        clean_zero_r_points,
    )
    zero_t_points = map(
        lambda pt: _resolve_t_point_at_r(pt, tr_pos, tr_pos_starts),
        clean_zero_r_points,
    )
    zero_clean_spans = tuple(starmap(
        lambda pt, rh_z_sub, t_pt: _DerenderSpan(
            t_start=t_pt,
            t_end=t_pt,
            r_start=pt,
            r_end=pt,
            h_start=rh_z_sub[0].dst_start,
            h_end=rh_z_sub[-1].dst_end,
            is_conflict=False,
            replacement=tuple(
                # slice the host tokens from the first to the last RH_INSERT at this R point
                h.stripped_line for h in h_tokens[rh_z_sub[0].dst_start : rh_z_sub[-1].dst_end]
            ),
            old_eof_newline=True,
            new_eof_newline=(
                rh_z_sub[-1].dst_end < len(h_tokens)
                or rh_z_sub[0].dst_start == rh_z_sub[-1].dst_end
                or h_tokens[rh_z_sub[-1].dst_end - 1].has_newline
            ),
        ),
        zip(clean_zero_r_points, rh_zero_subs, zero_t_points),
    ))
    return (*pos_clean_spans, *zero_clean_spans)


def _resolve_derender_spans(
    classified_tr: Sequence[DiffUnit],
    classified_rh: Sequence[DiffUnit],
    t_tokens: Sequence[TokenizedLine],
    h_tokens: Sequence[TokenizedLine],
) -> List[_DerenderSpan]:
    """Partitions `classified_tr` and `classified_rh` into positive/zero `R`-length streams and
    computes all conflict envelopes and clean edit spans in canonical `(r_start, r_end)` order.
    """
    tr_pos, tr_zero = partition(
        lambda u: u.unit_type != DiffUnitType.DELETE,
        classified_tr,
    )
    rh_pos, rh_zero = partition(
        lambda e: e.unit_type != DiffUnitType.INSERT,
        classified_rh,
    )

    tr_pos_starts = [u.r_start for u in tr_pos]
    tr_pos_ends = [u.r_end for u in tr_pos]
    rh_pos_starts = [e.r_start for e in rh_pos]
    rh_pos_ends = [e.r_end for e in rh_pos]
    positive_conflict_ranges = _merge_positive_conflict_r_ranges(tr_pos, rh_pos)
    pos_conf_starts = [r_s for r_s, _ in positive_conflict_ranges]
    pos_conf_ends = [r_e for _, r_e in positive_conflict_ranges]

    pos_conflict_spans = _build_positive_conflict_spans(
        positive_conflict_ranges,
        tr_pos,
        tr_pos_starts,
        tr_pos_ends,
        rh_pos,
        rh_pos_starts,
        rh_pos_ends,
    )
    zero_conflict_spans = _build_zero_conflict_spans(
        tr_zero,
        rh_zero,
        pos_conf_starts,
        pos_conf_ends,
        tr_pos,
        tr_pos_starts,
        rh_pos,
        rh_pos_starts,
    )
    clean_edit_spans = _build_clean_edit_spans(
        rh_pos,
        rh_zero,
        pos_conf_starts,
        pos_conf_ends,
        tr_pos,
        tr_pos_starts,
        tr_pos_ends,
        t_tokens,
        h_tokens,
    )
    return sorted(
        chain(pos_conflict_spans, zero_conflict_spans, clean_edit_spans),
        key=lambda s: (s.r_start, s.r_end),
    )


# ==============================================================================
# Layer 4: Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`)
# ==============================================================================


def _apply_clean_spans_to_template(
    t_stripped_lines: Sequence[str],
    clean_spans: Sequence[_DerenderSpan],
) -> Tuple[str, ...]:
    """Synthesizes newline-stripped `T_clean` by splicing `span.replacement` at each clean `[t_start, t_end)`.

    Conflicting regions in `T` are absent from `clean_spans`, so they fall naturally into the
    unchanged template gaps between consecutive clean spans and remain preserved in `T_clean`.
    """
    if not clean_spans:
        return tuple(t_stripped_lines)
    prev_ends = (0, *(s.t_end for s in clean_spans))
    next_starts = (*(s.t_start for s in clean_spans), len(t_stripped_lines))
    gaps = tuple(
        starmap(lambda start, end: t_stripped_lines[start:end], zip(prev_ends, next_starts))
    )
    interleaved = chain.from_iterable(
        starmap(
            lambda gap, span: (*gap, *span.replacement),
            zip(gaps[:-1], clean_spans),
        )
    )
    return (*interleaved, *gaps[-1])


def _anchor_conflict_clusters_to_t_clean(
    active_spans: Sequence[_DerenderSpan],
    t_stripped_lines: Sequence[str],
    r_stripped_lines: Sequence[str],
    h_stripped_lines: Sequence[str],
) -> Tuple[ConflictCluster, ...]:
    """Anchors each conflicting `_DerenderSpan` to its shifted `[t_clean_start, t_clean_end)` in `T_clean`.

    Each preceding clean span `s` shifts subsequent line indices in `T_clean` by
    `len(s.replacement) - (s.t_end - s.t_start)`, whereas conflict spans remain untouched in
    Step 1 (`delta = 0`). Using `itertools.accumulate` computes the exact cumulative shift
    `shift` for every conflict span in a single declarative pass.
    """
    span_deltas = map(
        lambda s: choose(
            s.is_conflict,
            0,
            len(s.replacement) - (s.t_end - s.t_start),
        ),
        active_spans,
    )
    cumulative_shifts = accumulate(span_deltas, initial=0)
    conflict_spans_with_shifts = filter(
        lambda pair: pair[0].is_conflict,
        zip(active_spans, cumulative_shifts),
    )
    return tuple(starmap(
        lambda span, shift: ConflictCluster(
            t_start=span.t_start,
            t_end=span.t_end,
            t_clean_start=span.t_start + shift,
            t_clean_end=span.t_end + shift,
            t_lines=tuple(t_stripped_lines[span.t_start : span.t_end]),
            r_lines=tuple(r_stripped_lines[span.r_start : span.r_end]),
            h_lines=tuple(h_stripped_lines[span.h_start : span.h_end]),
        ),
        conflict_spans_with_shifts,
    ))


def build_two_step_derender_plan(
    t_lines: Sequence[str],
    r_lines: Sequence[str],
    h_lines: Sequence[str],
) -> TwoStepDerenderPlan:
    """Executes the end-to-end 3-way template de-rendering pipeline across `(t_lines, r_lines, h_lines)`.

    Why De-Rendering Uses a Two-Step Plan (`T -> T_clean -> T_merged`):
        1. **Step 1 (`T -> T_clean`)**: Applies all non-conflicting `CleanPatchHunk`s (static edits,
           clean `1 <-> 1 <-> 1` de-rendered lines `H'`, and complete block deletions) while leaving
           conflicting regions in `T` untouched (`_apply_clean_spans_to_template`).
        2. **Step 2 (`T_clean -> T_merged`)**: Anchors every `ConflictCluster` to its shifted
           `[t_clean_start, t_clean_end)` slice in `T_clean` (`_anchor_conflict_clusters_to_t_clean`)
           so Step 2 can splice `diff3` conflict markers (`T` vs `R` vs `H`) at exact 0-based coordinates.

    Args:
        t_lines: Line sequence of the source template `T` (`splitlines(keepends=True)`).
        r_lines: Line sequence of the rendered baseline `R` (`splitlines(keepends=True)`).
        h_lines: Line sequence of the host-modified file `H` (`splitlines(keepends=True)`).

    Returns:
        `TwoStepDerenderPlan(t_lines, t_clean_lines, clean_hunks, conflict_clusters)` with
        newline-stripped line tuples.
    """
    t_tokens = tuple(map(tokenize_line, t_lines))
    r_tokens = tuple(map(tokenize_line, r_lines))
    h_tokens = tuple(map(tokenize_line, h_lines))

    raw_tr_units = fission_diff_units(t_tokens, r_tokens, DiffRelation.TR)
    raw_rh_units = fission_diff_units(r_tokens, h_tokens, DiffRelation.RH)

    refined_tr, refined_rh = refine_single_line_units(
        raw_tr_units, raw_rh_units, t_tokens, r_tokens, h_tokens
    )
    split_tr, split_rh = split_rh_deletes_at_tr_boundaries(refined_tr, refined_rh)
    classified_tr, classified_rh = _classify_orthogonal_units(split_tr, split_rh)
    active_spans = _resolve_derender_spans(
        classified_tr, classified_rh, t_tokens, h_tokens
    )

    t_stripped = tuple(t.stripped_line for t in t_tokens)
    r_stripped = tuple(r.stripped_line for r in r_tokens)
    h_stripped = tuple(h.stripped_line for h in h_tokens)

    clean_spans = tuple(filter(lambda s: not s.is_conflict, active_spans))
    clean_hunks = tuple(map(lambda s: s.to_clean_hunk(t_tokens), clean_spans))
    t_clean_lines = _apply_clean_spans_to_template(t_stripped, clean_spans)
    conflict_clusters = _anchor_conflict_clusters_to_t_clean(
        active_spans, t_stripped, r_stripped, h_stripped
    )
    return TwoStepDerenderPlan(
        t_lines=t_stripped,
        t_clean_lines=t_clean_lines,
        clean_hunks=clean_hunks,
        conflict_clusters=conflict_clusters,
    )


# ==============================================================================
# Layer 5: Display Unified Diff Synthesis & Patch Entry Point
# ==============================================================================


def build_display_unified_diff(
    plan: TwoStepDerenderPlan,
    fromfile: str,
    tofile: str,
    n: int = 3,
) -> str:
    """Synthesizes a standard unified diff preview from raw template `T` to `T_merged`.

    Display Semantics:
        - Splices Step 2 `ConflictCluster` `diff3` blocks (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`)
          onto `plan.t_clean_lines` via `splice_conflict_clusters_diff3` to form `t_merged_lines`
          (when conflict-free, `t_merged_lines` equals `plan.t_clean_lines`).
        - Runs standard `difflib.unified_diff(plan.t_lines, t_merged_lines, lineterm="")` so the
          preview is 100% anchored on the raw template `T` and displays both Step 1 clean
          de-rendered edits and Step 2 3-way conflict blocks in context.
    """
    t_merged_lines = splice_conflict_clusters_diff3(
        plan.t_clean_lines,
        plan.conflict_clusters,
    )
    if plan.t_lines == t_merged_lines:
        return ""
    diff_lines = tuple(
        difflib.unified_diff(
            list(plan.t_lines),
            list(t_merged_lines),
            fromfile=fromfile,
            tofile=tofile,
            n=n,
            lineterm="",
        )
    )
    return choose(bool(diff_lines), "\n".join(diff_lines) + "\n", "")


def read_rendered_baseline_lines(
    render_pkg_dir: Path,
    rel_rendered_path: Path,
) -> List[str]:
    """Reads the rendered baseline `R` from `render/<pkg>/<rel_rendered_path>`.

    Raises:
        SafeguardError: If the rendered baseline file does not exist or is a symlink in `render_pkg_dir`.
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
    src_match: Optional[RenderSourceMatch] = None,
) -> AdoptPatch:
    """Reads `(T, R, H)` from disk, runs 3-way de-rendering, and returns a structured `AdoptPatch`.

    Args:
        target_file: Absolute path to the template source file `T` in `src/<pkg>/`.
        patch_from: Absolute path to the drifted host/install file `H` in `install/<pkg>/`.
        render_pkg_dir: Absolute path to the package's rendered baseline directory `render/<pkg>/`.
        rel_rendered_path: Package-relative path of the rendered baseline file `R` inside `render/<pkg>/`.
        src_rel_path: Package-relative path of the source template `T` (used for `--- a/...` / `+++ b/...` headers).
        src_match: Resolved `RenderSourceMatch` metadata for the template source file.

    Returns:
        An `AdoptPatch` populated with `clean_hunks`, `conflict_clusters`, `has_conflict`, and `display_diff`.
    """
    t_lines = target_file.read_text(encoding="utf-8").splitlines(keepends=True)
    h_lines = patch_from.read_text(encoding="utf-8").splitlines(keepends=True)
    r_lines = read_rendered_baseline_lines(
        render_pkg_dir=render_pkg_dir,
        rel_rendered_path=rel_rendered_path,
    )

    plan = build_two_step_derender_plan(t_lines, r_lines, h_lines)
    posix_target = src_rel_path.as_posix()
    fromfile = f"a/{posix_target}"
    tofile = f"b/{posix_target}"

    display_diff = build_display_unified_diff(plan, fromfile=fromfile, tofile=tofile)
    return AdoptPatch(
        src_match=src_match,
        patch_from=patch_from,
        has_conflict=plan.has_conflicts,
        clean_hunks=plan.clean_hunks,
        conflict_clusters=plan.conflict_clusters,
        display_diff=display_diff,
    )
