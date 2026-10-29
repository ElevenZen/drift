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
                * Positive-R-Span RH Edit (`d_2 > d_1`: `SINGLE_SKEL_EDIT`, `SINGLE_EDIT`,
                  `BLOCK_EDIT`, `DELETE`):
                    - Collects `overlapping_pos_tr` (`max(d_1, u.r_start) < min(d_2, u.r_end)`)
                      and strictly enclosed `TR` `DELETE`s (`d_1 < u.r_start < d_2`)
                    - _is_clean_positive_rh_edit checks if the interaction is clean:
                        * Case 0.2: all overlapped `TR` units are `TR_EQUAL` (static lines) -> Clean
                        * Case 1.2: 1-to-1 exact match between `TR_SINGLE_EDIT` and
                          `RH_SINGLE_SKEL_EDIT` -> Clean
                        * Case 1.4 & Case 2.4: complete deletion (`RH_DELETE` with exact `[r_start, r_end)`
                          match) of a single `TR_SINGLE_EDIT` or `TR_BLOCK_EDIT` -> Clean
                        * All other overlaps (Case 1.3 token collision, Case 2.2 partial block
                          deletion, Case 2.3 block modification, Case 2.5b enclosed `TR_DELETE`,
                          or multi-unit span mismatch) -> Conflict
                * Zero-R-Span RH Pure Insertion (`d_1 == d_2 = d`: `RH_INSERT`):
                    - Clean (Case 3.1) unless `d` falls strictly inside a multi-line `TR` unit
                      (`u.r_start < d < u.r_end`, Case 3.2) or touches a degenerate 0-R-length
                      `TR_DELETE` / `TR_INSERT` at `d` (Case 3.3)
            - Propagates `is_conflict=True` onto all participating `TR` and `RH` units
            -> Returns Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]

    3. Synchronized Block Decomposition & Conflict Envelope Closure:
        _build_sync_blocks(tr_units, rh_units) -> _coalesce_sync_blocks(blocks) [Layer 3]
            - Lockstep Stream Partitioning (_build_sync_blocks):
                * Walks `tr_units` and `rh_units` simultaneously along `R`
                * Emits 0-R-length `TR` units (`TR_DELETE`) or 0-R-length `RH` units (`RH_INSERT`)
                  as standalone point `_SyncBlock`s
                * Advances across positive-R-length units via _advance_positive_span until both
                  streams reach a common `R` endpoint (`tr_r_end == rh_r_end`) with no interior
                  point units, grouping them into a minimal synchronized `_SyncBlock`
            - Conflict Envelope Closure (_coalesce_sync_blocks):
                * Merges any adjacent `_SyncBlock`s that both have `is_conflict=True` into a
                  single contiguous conflict envelope
            -> Returns List[_SyncBlock]

    4. Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`):
        build_two_step_derender_plan(t_lines, r_lines, h_lines) [Layer 4]
            - Tokenizes `T, R, H` (`tokenize_line`), runs hunk fission (`fission_diff_units`),
              refines `1 <-> 1 <-> 1` lines (`refine_single_line_units`), splits `RH` deletes
              (`split_rh_deletes_at_tr_boundaries`), classifies conflicts (`_classify_orthogonal_units`),
              and builds coalesced `_SyncBlock`s (`_build_sync_blocks` -> `_coalesce_sync_blocks`)
            - Single-Pass Plan Accumulation (_accumulate_plan):
                * Conflicting `_SyncBlock`:
                    - Preserves `t_lines[t_start:t_end]` unchanged inside `t_clean_lines` so Step 1
                      never mutates conflicting template regions
                    - Records shifted `[t_clean_start, t_clean_end)` coordinates and `(T, R, H)`
                      line slices into a Step 2 `ConflictCluster`
                * Clean `_SyncBlock`:
                    - Resolves replacement lines via _extract_clean_block_replacement (handling
                      EOF-newline-only changes on `RH_EQUAL`, `derendered_lines` on
                      `SINGLE_SKEL_EDIT`, or raw `h_lines[h_start:h_end]`)
                    - Emits a Step 1 `CleanPatchHunk` (with newline-stripped `old_lines`/`new_lines`
                      and `old_eof_newline`/`new_eof_newline` flags) and appends `replacement` to
                      `t_clean_lines`
            -> Returns TwoStepDerenderPlan(t_lines, t_clean_lines, clean_hunks, conflict_clusters)

    5. Display Unified Diff Synthesis & Patch Entry Point:
        generate_derendered_patch(target_file, patch_from, render_pkg_dir, ...) [Layer 5]
            - Reads `T` from `target_file`, `H` from `patch_from`, and `R` via
              read_rendered_baseline_lines (raising `SafeguardError` if `R` is missing)
            - Builds `TwoStepDerenderPlan` via `build_two_step_derender_plan`
            - Synthesizes human-readable preview diff via build_display_unified_diff:
                * Conflict-free fast path: standard `difflib.unified_diff(t_lines, t_clean_lines)`
                * Conflict path: substitutes each `ConflictCluster` with unique old/new sentinels
                  (`"\\x00__DRIFT_CONF_OLD_k__\\n"` / `"\\x00__DRIFT_CONF_NEW_k__\\n"`) carrying
                  `(-R, +H)` display lines so `SequenceMatcher.get_grouped_opcodes` anchors context
                  strictly on outer `T` lines and renders hunks via `_format_range_unified` and
                  `_format_display_opcode`
            -> Returns AdoptPatch

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Boundary-Aligned RH_DELETE Normalization
        _get_interior_cut_points
        _split_single_rh_delete
        split_rh_deletes_at_tr_boundaries
    Layer 2: Orthogonal Matrix Classification Along the Shared `R` Axis
        _is_clean_positive_rh_edit
        _eval_rh_unit_conflicts
        _classify_orthogonal_units
    Layer 3: Synchronized Block Decomposition & Conflict Envelope Closure
        _SyncBlock
        _advance_positive_span
        _build_sync_blocks
        _coalesce_sync_blocks
    Layer 4: Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`)
        _extract_clean_block_replacement
        build_two_step_derender_plan
    Layer 5: Display Unified Diff Synthesis & Patch Entry Point
        _format_range_unified
        _format_display_opcode
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
    TwoStepDerenderPlan,
)
from ..utils.func_utils import branch, choose
from .adopt_fission import (
    fission_diff_units,
    refine_single_line_units,
    tokenize_line,
)

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
    # TODO: can we use map here?
    return [
        DiffUnit(
            relation=DiffRelation.RH,
            unit_type=DiffUnitType.DELETE,
            src_start=endpoints[k],
            src_end=endpoints[k + 1],
            dst_start=rh_unit.dst_start,
            dst_end=rh_unit.dst_end,
            is_conflict=rh_unit.is_conflict,
        )
        for k in range(len(endpoints) - 1)
    ]


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
    tr_starts = tuple(dict.fromkeys(u.r_start for u in tr_units))
    # TODO: can we use filter->map->join here instead of the nested comprehension? I think it will be more readable
    enclosed_cut_points = {
        pt
        for e in rh_units
        if e.unit_type == DiffUnitType.DELETE
        for pt in _get_interior_cut_points(e.r_start, e.r_end, tr_starts)
    }
    # case 2.5b has to be tagged before the split of the RH_DELETE
    # TODO: can we use map here?
    updated_tr = tuple(
        choose(
            u.unit_type == DiffUnitType.DELETE and u.r_start in enclosed_cut_points,
            replace(u, is_conflict=True),
            u,
        )
        for u in tr_units
    )
    # TODO: can we use map and join here instead of the nested comprehension? I think it will be more readable
    split_rh = tuple(
        sub_e
        for e in rh_units
        for sub_e in _split_single_rh_delete(e, tr_starts)
    )
    return updated_tr, split_rh


# ==============================================================================
# Layer 2: Orthogonal Matrix Classification Along the Shared `R` Axis
# ==============================================================================


def _is_clean_positive_rh_edit(
    rh_unit: DiffUnit,
    overlapping_pos_tr: Sequence[DiffUnit],
    enclosed_tr_deletes: Sequence[DiffUnit],
) -> bool:
    """Returns True if a positive-`R`-span `RH` edit (`d_2 > d_1`) is clean under the Orthogonal Matrix.

    Clean Positive-Span Cases:
        # TODO: it's not 0.2, it's 1.1 and 2.1 and 4.1
        # please reference the doc template_adopt_derendering_draft.md for the case numbers

        - **Case 0.2**: All overlapped positive-length `TR` units are `TR_EQUAL` (static lines in `T`),
          and no `TR_DELETE` is strictly enclosed inside `[d_1, d_2)`.
        - **Case 1.2**: `rh_unit` is a `SINGLE_SKEL_EDIT` whose `R`-span matches a single
          `TR_SINGLE_EDIT` unit `1 <-> 1`.
        - **Case 1.4 & Case 2.4**: `rh_unit` is an `RH_DELETE` whose `R`-span exactly equals the
          entire `[r_start, r_end)` span of a single `TR_SINGLE_EDIT` (Case 1.4) or `TR_BLOCK_EDIT`
          (Case 2.4), meaning the entire templated line or block can be cleanly deleted from `T`.

    All other positive-span interactions (e.g., Case 1.3 token collision, Case 2.2 partial deletion
    of a `TR_BLOCK_EDIT`, Case 2.3 modification inside a `TR_BLOCK_EDIT`, or Case 2.5b enclosed
    `TR_DELETE`) return `False` (conflict).
    """

    # is_conflict is for 2.5b, enclosed_tr_deletes is for TR_DELETE in RH_SINGLE/BLOCK_EDIT
    # TODO: the doc template_adopt_derendering_draft.md doesn't cover TR_DELETE in RH_EDIT.
    if rh_unit.is_conflict or enclosed_tr_deletes:
        return False
    if not overlapping_pos_tr:
        return True

    # Case 0.2: All overlapped positive TR units are TR_EQUAL
    if all(u.unit_type == DiffUnitType.EQUAL for u in overlapping_pos_tr):
        return True

    # there will always be at least one overlapping TR unit for any given range in R.
    # more than one means the edit/delete hunk is crossing the border of indivisible TR units
    # which is a conflict.
    if len(overlapping_pos_tr) != 1:
        return False

    tr_u = overlapping_pos_tr[0]
    exact_r_match = (rh_unit.r_start == tr_u.r_start and rh_unit.r_end == tr_u.r_end)
    # not exact_r_match means the RH hunk is inside one larger indivisible TR unit.
    if not exact_r_match:
        return False

    # Case 1.2: 1 <-> 1 <-> 1 clean skeleton edit
    if (
        tr_u.unit_type == DiffUnitType.SINGLE_EDIT
        and rh_unit.unit_type == DiffUnitType.SINGLE_SKEL_EDIT
    ):
        return True

    # Case 1.4 & Case 2.4: Complete deletion of TR_SINGLE_EDIT or TR_BLOCK_EDIT
    if rh_unit.unit_type == DiffUnitType.DELETE and tr_u.unit_type in (
        DiffUnitType.SINGLE_EDIT,
        DiffUnitType.BLOCK_EDIT,
    ):
        return True
    
    # TODO: please list all the possible cases here, like RH_DELETE over TR_INSERT and so on
    return False


def _eval_rh_unit_conflicts(
    j: int,
    e: DiffUnit,
    tr_units: Sequence[DiffUnit],
    tr_r_starts: Sequence[int],
    tr_r_ends: Sequence[int],
) -> Tuple[Set[int], Set[int]]:
    """Computes `(conflicting_tr_indices, conflicting_rh_indices)` induced by `rh_units[j]`.

    Uses `bisect` over the monotonically non-decreasing `tr_r_starts` and `tr_r_ends` sequences
    to locate overlapping and enclosed `TR` units in O(log M) time across two geometric regimes:
        1. **Positive-R-Span Edit (`d_2 > d_1`)**:
           - `overlap_range = range(bisect_right(tr_r_ends, d_1), bisect_left(tr_r_starts, d_2))`
             bounds all `TR` units with `u.r_end > d_1` and `u.r_start < d_2`.
           - `interior_range = range(bisect_right(tr_r_starts, d_1), bisect_left(tr_r_starts, d_2))`
             bounds all `TR` units with `d_1 < u.r_start < d_2`.
        2. **Zero-R-Span Pure Insertion (`d_1 == d_2 = d`, `RH_INSERT`)**:
           - **Case 3.2 (Conflict)**: `range(bisect_right(tr_r_ends, d), bisect_left(tr_r_starts, d))`
             identifies all `TR` units strictly containing `d` (`u.r_start < d < u.r_end`).
           - **Case 3.3 (Conflict)**: `range(bisect_left(tr_r_ends, d), bisect_right(tr_r_starts, d))`
             bounds all `TR` units touching `d` (`u.r_start == d` or `u.r_end == d`).
    """
    if e.unit_type == DiffUnitType.EQUAL:
        return (set(), set())

    d1, d2 = e.r_start, e.r_end
    if d2 > d1:
        # Positive-R-Span Hunk Except Equal (Edit, Delete)
        overlap_range = range(bisect_right(tr_r_ends, d1), bisect_left(tr_r_starts, d2))
        # TODO: I think we should use tr_r_ends in the right index to find real interior
        # although it's the same to Delete units since they are 0-length
        interior_range = range(bisect_right(tr_r_starts, d1), bisect_left(tr_r_starts, d2))
        overlapping_pos_idx = [
            i
            for i in overlap_range
            if tr_units[i].r_end > tr_units[i].r_start
            # TODO: can we just use tr_units[i].unit_type != DiffUnitType.DELETE here instead of checking length?
            # and we can call filter()
        ]
        enclosed_del_idx = [
            i
            for i in interior_range
            if tr_units[i].unit_type == DiffUnitType.DELETE
            # TODO: we can call filter()
        ]
        # TODO: we can just move the filter call into following two expressions.
        # filter the tr_units with (i in overlap_range and type != DELETE) and (i in interior_range and type == DELETE)
        # and i think rename overlap_range -> overlap_idx_range, interior_range -> interior_idx_range
        overlapping_pos_tr = [tr_units[i] for i in overlapping_pos_idx]
        enclosed_tr_dels = [tr_units[i] for i in enclosed_del_idx]
        return branch(
            # TODO: don't call it positive edit , call it (edit or delete) may be better
            # _is_clean_rh_edit_or_delete
            _is_clean_positive_rh_edit(e, overlapping_pos_tr, enclosed_tr_dels),
            lambda: (set(), set()),
            lambda: ({*overlapping_pos_idx, *enclosed_del_idx}, {j}),
        )

    # TODO: please extract the following code into a separate function, like _eval_rh_insert_conflicts
    # Pure insertion (DiffUnitType.INSERT) at point d = d1 = d2 (Cases 3.1, 3.2, 3.3)
    d = d1
    # TODO: maybe rename to inside_tr_idx, there could only be 0 or 1 item. if this is not empty, then it's a
    # conflict. we can skip the touching_range checks and just set it to empty set
    interior_tr_idx = list(
        range(bisect_right(tr_r_ends, d), bisect_left(tr_r_starts, d))
    )
    # TODO: this can be many hunks, but only when inside_tr_idx is empty.
    # actually, the condition should be tr_r_ends == d or tr_r_starts == d, it's an accurate binary search?
    touching_range = range(
        bisect_left(tr_r_ends, d),
        bisect_right(tr_r_starts, d),
    )
    touching_degenerate_tr_idx = [
        i
        for i in touching_range
        if tr_units[i].unit_type in (DiffUnitType.DELETE, DiffUnitType.INSERT)
        and (tr_units[i].r_start == d or tr_units[i].r_end == d)
    ]
    return choose(
        bool(interior_tr_idx or touching_degenerate_tr_idx),
        ({*interior_tr_idx, *touching_degenerate_tr_idx}, {j}),
        (set(), set()),
    )


def _classify_orthogonal_units(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]:
    """Classifies all `TR` and `RH` units along the `R` axis according to Orthogonal Matrix Cases `0.1–4.2`.

    Unions pre-existing conflict flags (from Layer 1a-1 Case 1.3 token collisions and Stage 1
    Case 2.5b enclosed `TR_DELETE`s) with the conflict index sets computed by `_eval_rh_unit_conflicts`,
    returning updated `(tr_units, rh_units)` tuples with `is_conflict=True` on all conflicting units.
    """
    init_tr = {idx for idx, u in enumerate(tr_units) if u.is_conflict}
    init_rh = {idx for idx, e in enumerate(rh_units) if e.is_conflict}
    tr_r_starts = [u.r_start for u in tr_units]
    tr_r_ends = [u.r_end for u in tr_units]
    per_rh_sets = [
        _eval_rh_unit_conflicts(j, e, tr_units, tr_r_starts, tr_r_ends)
        for j, e in enumerate(rh_units)
    ]
    conflict_tr_indices = reduce(
        lambda acc, pair: acc | pair[0], per_rh_sets, init_tr
    )
    conflict_rh_indices = reduce(
        lambda acc, pair: acc | pair[1], per_rh_sets, init_rh
    )
    return (
        tuple(
            choose(i in conflict_tr_indices, replace(u, is_conflict=True), u)
            for i, u in enumerate(tr_units)
        ),
        tuple(
            choose(j in conflict_rh_indices, replace(e, is_conflict=True), e)
            for j, e in enumerate(rh_units)
        ),
    )


# ==============================================================================
# Layer 3: Synchronized Block Decomposition & Conflict Envelope Closure
# ==============================================================================


@dataclass(frozen=True)
class _SyncBlock:
    """Minimal synchronized span across `(T, R, H)` bounded by aligned `R`-axis unit endpoints.

    Attributes:
        t_start: 0-based start line index in `T`.
        t_end: 0-based end line index (exclusive) in `T`.
        r_start: 0-based start line index in `R`.
        r_end: 0-based end line index (exclusive) in `R`.
        h_start: 0-based start line index in `H`.
        h_end: 0-based end line index (exclusive) in `H`.
        tr_slice: Contiguous slice of `TR` `DiffUnit`s covering `[t_start, t_end) <-> [r_start, r_end)`.
        rh_slice: Contiguous slice of `RH` `DiffUnit`s covering `[r_start, r_end) <-> [h_start, h_end)`.
        is_conflict: True if any unit in `tr_slice` or `rh_slice` is marked `is_conflict=True`.
    """
    t_start: int
    t_end: int
    r_start: int
    r_end: int
    h_start: int
    h_end: int
    tr_slice: Tuple[DiffUnit, ...]
    rh_slice: Tuple[DiffUnit, ...]
    is_conflict: bool


def _advance_positive_span(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
    i_start: int,
    j_start: int,
) -> Tuple[int, int]:
    """Advances `(i_end, j_end)` from `(i_start, j_start)` until `TR` and `RH` reach a shared `R` endpoint.

    Whenever a multi-line `TR` unit (e.g. a 1-to-3 loop expansion `R[0:3]`) overlaps multiple `RH`
    units (or vice versa), this helper advances whichever stream lags behind on the `R` axis—and
    absorbs any 0-R-length point units (`r_start == r_end`) strictly inside the span—until
    `tr_r_end == rh_r_end`.
    """
    i_end = i_start + 1
    j_end = j_start + 1
    tr_r_end = tr_units[i_start].r_end
    rh_r_end = rh_units[j_start].r_end

    while tr_r_end != rh_r_end or (
        i_end < len(tr_units)
        and tr_units[i_end].r_start == tr_units[i_end].r_end
        and tr_units[i_end].r_start < max(tr_r_end, rh_r_end)
    ) or (
        j_end < len(rh_units)
        and rh_units[j_end].r_start == rh_units[j_end].r_end
        and rh_units[j_end].r_start < max(tr_r_end, rh_r_end)
    ):
        if (
            i_end < len(tr_units)
            and tr_units[i_end].r_start == tr_units[i_end].r_end
            and tr_units[i_end].r_start < max(tr_r_end, rh_r_end)
        ):
            i_end += 1
        elif (
            j_end < len(rh_units)
            and rh_units[j_end].r_start == rh_units[j_end].r_end
            and rh_units[j_end].r_start < max(tr_r_end, rh_r_end)
        ):
            j_end += 1
        elif tr_r_end < rh_r_end and i_end < len(tr_units):
            tr_r_end = max(tr_r_end, tr_units[i_end].r_end)
            i_end += 1
        elif rh_r_end < tr_r_end and j_end < len(rh_units):
            rh_r_end = max(rh_r_end, rh_units[j_end].r_end)
            j_end += 1
        else:
            break

    return i_end, j_end


def _build_sync_blocks(
    tr_units: Sequence[DiffUnit],
    rh_units: Sequence[DiffUnit],
) -> List[_SyncBlock]:
    """Partitions `(tr_units, rh_units)` into a sequence of minimal `R`-synchronized `_SyncBlock`s.

    Walks `tr_units` (index `i`) and `rh_units` (index `j`) in lockstep:
    1. Leading 0-R-length `TR` units (`TR_DELETE` where `r_start == r_end`) emit a `[t_start, t_end)`
       block with an empty `H` slice `[h_cur, h_cur)`.
    2. Leading 0-R-length `RH` units (`RH_INSERT` where `r_start == r_end`) emit an `[h_start, h_end)`
       block anchored at `[t_cur, t_cur)` in `T`.
    3. Positive-R-length units on both sides advance together via `_advance_positive_span` until
       their `R` endpoints coincide, forming a synchronized `(T, R, H)` block.
    """
    blocks: List[_SyncBlock] = []
    i = 0
    j = 0
    t_cur = 0
    r_cur = 0
    h_cur = 0
    n_tr = len(tr_units)
    n_rh = len(rh_units)

    while i < n_tr or j < n_rh:
        if i < n_tr and tr_units[i].r_start == tr_units[i].r_end:
            u = tr_units[i]
            blocks.append(
                _SyncBlock(
                    t_start=u.src_start,
                    t_end=u.src_end,
                    r_start=u.r_start,
                    r_end=u.r_end,
                    h_start=h_cur,
                    h_end=h_cur,
                    tr_slice=(u,),
                    rh_slice=(),
                    is_conflict=u.is_conflict,
                )
            )
            t_cur = u.src_end
            r_cur = u.r_end
            i += 1
        elif j < n_rh and rh_units[j].r_start == rh_units[j].r_end:
            e = rh_units[j]
            blocks.append(
                _SyncBlock(
                    t_start=t_cur,
                    t_end=t_cur,
                    r_start=e.r_start,
                    r_end=e.r_end,
                    h_start=e.dst_start,
                    h_end=e.dst_end,
                    tr_slice=(),
                    rh_slice=(e,),
                    is_conflict=e.is_conflict,
                )
            )
            r_cur = e.r_end
            h_cur = e.dst_end
            j += 1
        elif i < n_tr and j < n_rh:
            i_end, j_end = _advance_positive_span(tr_units, rh_units, i, j)
            tr_sub = tuple(tr_units[i:i_end])
            rh_sub = tuple(rh_units[j:j_end])
            t_s = tr_sub[0].src_start
            t_e = tr_sub[-1].src_end
            r_s = tr_sub[0].r_start
            r_e = tr_sub[-1].r_end
            h_s = rh_sub[0].dst_start
            h_e = rh_sub[-1].dst_end
            is_conf = any(u.is_conflict for u in tr_sub) or any(
                e.is_conflict for e in rh_sub
            )
            blocks.append(
                _SyncBlock(
                    t_start=t_s,
                    t_end=t_e,
                    r_start=r_s,
                    r_end=r_e,
                    h_start=h_s,
                    h_end=h_e,
                    tr_slice=tr_sub,
                    rh_slice=rh_sub,
                    is_conflict=is_conf,
                )
            )
            t_cur, r_cur, h_cur = t_e, r_e, h_e
            i, j = i_end, j_end
        elif i < n_tr:
            u = tr_units[i]
            blocks.append(
                _SyncBlock(
                    t_start=u.src_start,
                    t_end=u.src_end,
                    r_start=u.r_start,
                    r_end=u.r_end,
                    h_start=h_cur,
                    h_end=h_cur,
                    tr_slice=(u,),
                    rh_slice=(),
                    is_conflict=u.is_conflict,
                )
            )
            t_cur, r_cur = u.src_end, u.r_end
            i += 1
        else:
            e = rh_units[j]
            blocks.append(
                _SyncBlock(
                    t_start=t_cur,
                    t_end=t_cur,
                    r_start=e.r_start,
                    r_end=e.r_end,
                    h_start=e.dst_start,
                    h_end=e.dst_end,
                    tr_slice=(),
                    rh_slice=(e,),
                    is_conflict=e.is_conflict,
                )
            )
            r_cur, h_cur = e.r_end, e.dst_end
            j += 1

    return blocks


def _coalesce_sync_blocks(blocks: Sequence[_SyncBlock]) -> List[_SyncBlock]:
    """Merges adjacent conflicting `_SyncBlock`s into minimal contiguous conflict envelopes.

    For example, when an `RH_DELETE` splits across a `TR_DELETE` boundary (Case 2.5b), the left
    half of the deletion, the enclosed 0-R-length `TR_DELETE`, and the right half of the deletion
    initially form three adjacent conflicting `_SyncBlock`s; coalescing merges them into one
    unified `ConflictCluster` covering the full `(T, R, H)` span.
    """
    def _step(acc: List[_SyncBlock], blk: _SyncBlock) -> List[_SyncBlock]:
        if acc and acc[-1].is_conflict and blk.is_conflict:
            prev = acc[-1]
            merged = _SyncBlock(
                t_start=prev.t_start,
                t_end=blk.t_end,
                r_start=prev.r_start,
                r_end=blk.r_end,
                h_start=prev.h_start,
                h_end=blk.h_end,
                tr_slice=(*prev.tr_slice, *blk.tr_slice),
                rh_slice=(*prev.rh_slice, *blk.rh_slice),
                is_conflict=True,
            )
            return [*acc[:-1], merged]
        return [*acc, blk]

    return reduce(_step, blocks, [])


# ==============================================================================
# Layer 4: Two-Step De-Render Plan Construction (`T -> T_clean -> T_merged`)
# ==============================================================================


def _extract_clean_block_replacement(
    blk: _SyncBlock,
    t_lines: Sequence[str],
    h_lines: Sequence[str],
) -> Optional[Tuple[str, ...]]:
    """Computes the replacement template lines for a clean `_SyncBlock` if it modifies `T`, else `None`.

    Handles three clean replacement modes:
        1. **EOF-Newline-Only Drift on `RH_EQUAL`**: All `RH` units are `EQUAL` in body text, or
           the final line at EOF gained/lost a trailing newline in `H` relative to `T` -> returns
           `t_lines[t_start:t_end]` with `H`'s trailing line ending on the last line.
        2. **De-Rendered Skeleton Edit (`SINGLE_SKEL_EDIT`, Case 1.2)**: Returns the synthesized
           `derendered_lines` (`H'`) computed by Layer 1a-1 token calculus.
        3. **Static Edit / Insertion / Full Block Deletion (Cases 0.2, 1.4, 2.4, 3.1)**: Returns
           `tuple(h_lines[blk.h_start : blk.h_end])` (which is `()` for deletions).
    """
    if not blk.rh_slice:
        return None
    if all(e.unit_type == DiffUnitType.EQUAL for e in blk.rh_slice):
        is_eof_nl_diff = (
            blk.t_end == len(t_lines)
            and blk.h_end == len(h_lines)
            and blk.t_end > blk.t_start
            and blk.h_end > blk.h_start
            and t_lines[blk.t_end - 1].endswith(("\n", "\r"))
            != h_lines[blk.h_end - 1].endswith(("\n", "\r"))
        )
        if not is_eof_nl_diff:
            return None
        last_t_stripped = t_lines[blk.t_end - 1].rstrip("\r\n")
        h_last = h_lines[blk.h_end - 1]
        h_ending = h_last[len(h_last.rstrip("\r\n")) :]
        return (
            *t_lines[blk.t_start : blk.t_end - 1],
            f"{last_t_stripped}{h_ending}",
        )

    if (
        len(blk.rh_slice) == 1
        and blk.rh_slice[0].unit_type == DiffUnitType.SINGLE_SKEL_EDIT
        and blk.rh_slice[0].derendered_lines
    ):
        return blk.rh_slice[0].derendered_lines

    return tuple(h_lines[blk.h_start : blk.h_end])


def build_two_step_derender_plan(
    t_lines: Sequence[str],
    r_lines: Sequence[str],
    h_lines: Sequence[str],
) -> TwoStepDerenderPlan:
    """Executes the end-to-end 3-way template de-rendering pipeline across `(t_lines, r_lines, h_lines)`.

    Why De-Rendering Uses a Two-Step Plan (`T -> T_clean -> T_merged`):
        1. **Step 1 (`T -> T_clean`)**: Applies all non-conflicting `CleanPatchHunk`s (static edits,
           clean `1 <-> 1 <-> 1` de-rendered lines `H'`, and complete block deletions) while leaving
           conflicting regions in `T` untouched.
        2. **Step 2 (`T_clean -> T_merged`)**: Because Step 1 insertions/deletions shift line numbers
           in `T_clean`, `_accumulate_plan` tracks the exact shifted slice `[t_clean_start, t_clean_end)`
           for every `ConflictCluster` so Step 2 can splice `diff3` conflict markers (`T` vs `R` vs `H`)
           at exact 0-based coordinates in `T_clean`.

    Args:
        t_lines: Line sequence of the source template `T` (`splitlines(keepends=True)`).
        r_lines: Line sequence of the rendered baseline `R` (`splitlines(keepends=True)`).
        h_lines: Line sequence of the host-modified file `H` (`splitlines(keepends=True)`).

    Returns:
        `TwoStepDerenderPlan(t_lines, t_clean_lines, clean_hunks, conflict_clusters)`.
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

    sync_blocks = _build_sync_blocks(classified_tr, classified_rh)
    coalesced_blocks = _coalesce_sync_blocks(sync_blocks)

    def _accumulate_plan(
        state: Tuple[List[str], List[CleanPatchHunk], List[ConflictCluster]],
        blk: _SyncBlock,
    ) -> Tuple[List[str], List[CleanPatchHunk], List[ConflictCluster]]:
        t_clean_acc, hunks_acc, clusters_acc = state
        if blk.is_conflict:
            t_clean_start = len(t_clean_acc)
            t_slice = tuple(t_lines[blk.t_start : blk.t_end])
            next_t_clean = [*t_clean_acc, *t_slice]
            t_clean_end = len(next_t_clean)
            cluster = ConflictCluster(
                t_start=blk.t_start,
                t_end=blk.t_end,
                t_clean_start=t_clean_start,
                t_clean_end=t_clean_end,
                t_lines=t_slice,
                r_lines=tuple(r_lines[blk.r_start : blk.r_end]),
                h_lines=tuple(h_lines[blk.h_start : blk.h_end]),
            )
            return (next_t_clean, hunks_acc, [*clusters_acc, cluster])

        replacement = _extract_clean_block_replacement(blk, t_lines, h_lines)
        if replacement is not None:
            t_slice = t_lines[blk.t_start : blk.t_end]
            old_eof_nl = branch(
                blk.t_end == len(t_lines) and bool(t_slice),
                lambda: t_slice[-1].endswith(("\n", "\r")),
                lambda: True,
            )
            new_eof_nl = branch(
                blk.h_end == len(h_lines) and bool(replacement),
                lambda: replacement[-1].endswith(("\n", "\r")),
                lambda: True,
            )
            hunk = CleanPatchHunk(
                t_start=blk.t_start,
                t_end=blk.t_end,
                old_lines=tuple(ln.rstrip("\r\n") for ln in t_slice),
                new_lines=tuple(ln.rstrip("\r\n") for ln in replacement),
                old_eof_newline=old_eof_nl,
                new_eof_newline=new_eof_nl,
            )
            return ([*t_clean_acc, *replacement], [*hunks_acc, hunk], clusters_acc)

        return (
            [*t_clean_acc, *t_lines[blk.t_start : blk.t_end]],
            hunks_acc,
            clusters_acc,
        )

    t_clean_list, clean_hunks_list, clusters_list = reduce(
        _accumulate_plan,
        coalesced_blocks,
        ([], [], []),
    )
    return TwoStepDerenderPlan(
        t_lines=tuple(t_lines),
        t_clean_lines=tuple(t_clean_list),
        clean_hunks=tuple(clean_hunks_list),
        conflict_clusters=tuple(clusters_list),
    )


# ==============================================================================
# Layer 5: Display Unified Diff Synthesis & Patch Entry Point
# ==============================================================================


def _format_range_unified(start: int, stop: int) -> str:
    """Formats a 0-based `[start, stop)` line interval as a 1-based unified diff header range."""
    length = stop - start
    if length == 1:
        return f"{start + 1}"
    if length == 0:
        return f"{start},0"
    return f"{start + 1},{length}"


def _format_display_opcode(
    tag: str,
    i1: int,
    i2: int,
    j1: int,
    j2: int,
    old_items: Sequence[Tuple[str, Tuple[str, ...], int]],
    new_items: Sequence[Tuple[str, Tuple[str, ...], int]],
) -> List[str]:
    """Formats a single `SequenceMatcher` opcode of display items into `' '`, `'-'`, and `'+'` diff lines."""
    if tag == "equal":
        return [
            f" {ln}"
            for k in range(i1, i2)
            for ln in old_items[k][1]
        ]
    removed = choose(
        tag in ("replace", "delete"),
        [f"-{ln}" for k in range(i1, i2) for ln in old_items[k][1]],
        [],
    )
    added = choose(
        tag in ("replace", "insert"),
        [f"+{ln}" for k in range(j1, j2) for ln in new_items[k][1]],
        [],
    )
    return [*removed, *added]


def build_display_unified_diff(
    plan: TwoStepDerenderPlan,
    fromfile: str,
    tofile: str,
    n: int = 3,
) -> str:
    """Synthesizes a human-readable unified diff preview from a `TwoStepDerenderPlan`.

    Display Semantics:
        - **Clean hunks**: Displayed as `-T / +H'` (template lines vs de-rendered replacement lines).
        - **Conflict clusters**: Displayed as `-R / +H` (rendered baseline lines vs host drift lines)
          while keeping `@@` line numbers anchored on `T` and `T_clean`.
        - **Why Sentinel Grouping is Used**: Each conflict cluster is represented as a single item
          with distinct old/new keys (`"\\x00__DRIFT_CONF_OLD_k__\\n"` vs `"\\x00__DRIFT_CONF_NEW_k__\\n"`)
          so `SequenceMatcher.get_grouped_opcodes(n)` treats the entire conflict cluster as an atomic
          `"replace"` opcode and only emits `' '` context lines from the outer non-conflicting
          template lines of `T`.
    """
    if not plan.has_conflicts:
        if plan.t_lines == plan.t_clean_lines:
            return ""
        return "".join(
            difflib.unified_diff(
                list(plan.t_lines),
                list(plan.t_clean_lines),
                fromfile=fromfile,
                tofile=tofile,
                n=n,
            )
        )

    def _acc_items(
        state: Tuple[
            int,
            int,
            List[Tuple[str, Tuple[str, ...], int]],
            List[Tuple[str, Tuple[str, ...], int]],
        ],
        indexed_cluster: Tuple[int, ConflictCluster],
    ) -> Tuple[
        int,
        int,
        List[Tuple[str, Tuple[str, ...], int]],
        List[Tuple[str, Tuple[str, ...], int]],
    ]:
        t_cur, tc_cur, old_acc, new_acc = state
        idx, c = indexed_cluster
        sub_old = [(ln, (ln,), 1) for ln in plan.t_lines[t_cur : c.t_start]]
        sub_new = [
            (ln, (ln,), 1) for ln in plan.t_clean_lines[tc_cur : c.t_clean_start]
        ]
        return (
            c.t_end,
            c.t_clean_end,
            [
                *old_acc,
                *sub_old,
                (f"\x00__DRIFT_CONF_OLD_{idx}__\n", c.r_lines, len(c.t_lines)),
            ],
            [
                *new_acc,
                *sub_new,
                (f"\x00__DRIFT_CONF_NEW_{idx}__\n", c.h_lines, len(c.h_lines)),
            ],
        )

    t_end_cur, tc_end_cur, head_old, head_new = reduce(
        _acc_items,
        enumerate(plan.conflict_clusters),
        (0, 0, [], []),
    )
    old_items = [
        *head_old,
        *((ln, (ln,), 1) for ln in plan.t_lines[t_end_cur:]),
    ]
    new_items = [
        *head_new,
        *((ln, (ln,), 1) for ln in plan.t_clean_lines[tc_end_cur:]),
    ]

    old_keys = [item[0] for item in old_items]
    new_keys = [item[0] for item in new_items]
    groups = list(
        difflib.SequenceMatcher(None, old_keys, new_keys, autojunk=False).get_grouped_opcodes(n)
    )
    if not groups:
        return ""

    old_prefix = reduce(
        lambda acc, item: [*acc, acc[-1] + item[2]], old_items, [0]
    )
    new_prefix = reduce(
        lambda acc, item: [*acc, acc[-1] + item[2]], new_items, [0]
    )

    header_lines = [f"--- {fromfile}\n", f"+++ {tofile}\n"]
    body_lines = [
        line
        for group in groups
        for line in [
            f"@@ -{_format_range_unified(old_prefix[group[0][1]], old_prefix[group[-1][2]])} "
            f"+{_format_range_unified(new_prefix[group[0][3]], new_prefix[group[-1][4]])} @@\n",
            *(
                op_line
                for tag, i1, i2, j1, j2 in group
                for op_line in _format_display_opcode(
                    tag, i1, i2, j1, j2, old_items, new_items
                )
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
