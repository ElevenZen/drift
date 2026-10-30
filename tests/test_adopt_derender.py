"""Granular and matrix unit tests for Layer 1a-2 (`drift.adopt.adopt_derender`).

Tests each sub-stage of `adopt_derender.py` in isolation on synthetic `DiffUnit` topologies
as well as end-to-end across `build_two_step_derender_plan`, `build_display_unified_diff`,
and `apply_source_patch`.
"""

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from drift.adopt.adopt_derender import (
    _DerenderSpan,
    _anchor_conflict_clusters_to_t_clean,
    _apply_clean_spans_to_template,
    _build_clean_edit_spans,
    _build_positive_conflict_spans,
    _build_zero_conflict_spans,
    _classify_orthogonal_units,
    _eval_rh_edit_or_delete_conflicts,
    _eval_rh_insert_conflicts,
    _eval_rh_unit_conflicts,
    _extract_clean_pos_replacement,
    _get_interior_cut_points,
    _is_clean_rh_edit_or_delete,
    _merge_positive_conflict_r_ranges,
    _resolve_derender_spans,
    _split_single_rh_delete,
    build_display_unified_diff,
    build_two_step_derender_plan,
    read_rendered_baseline_lines,
    split_rh_deletes_at_tr_boundaries,
)
from drift.adopt.adopt_fission import tokenize_line
from drift.adopt.adopt_patch import (
    apply_source_patch,
    parse_unified_patch_hunks,
    splice_conflict_clusters_diff3,
)
from drift.core.constants import set_test_mode
from drift.core.exceptions import SafeguardError
from drift.models.adopt_models import (
    AdoptPatch,
    CleanPatchHunk,
    ConflictCluster,
    DiffRelation,
    DiffUnit,
    DiffUnitType,
)

set_test_mode(True)


def _tr(
    unit_type: DiffUnitType,
    t_start: int,
    t_end: int,
    r_start: int,
    r_end: int,
    is_conflict: bool = False,
) -> DiffUnit:
    """Helper to construct a synthetic `TR` (`T -> R`) `DiffUnit`."""
    return DiffUnit(
        relation=DiffRelation.TR,
        unit_type=unit_type,
        src_start=t_start,
        src_end=t_end,
        dst_start=r_start,
        dst_end=r_end,
        is_conflict=is_conflict,
    )


def _rh(
    unit_type: DiffUnitType,
    r_start: int,
    r_end: int,
    h_start: int,
    h_end: int,
    is_conflict: bool = False,
    derendered_lines: tuple = (),
) -> DiffUnit:
    """Helper to construct a synthetic `RH` (`R -> H`) `DiffUnit`."""
    return DiffUnit(
        relation=DiffRelation.RH,
        unit_type=unit_type,
        src_start=r_start,
        src_end=r_end,
        dst_start=h_start,
        dst_end=h_end,
        is_conflict=is_conflict,
        derendered_lines=derendered_lines,
    )


class TestAdoptDerender(unittest.TestCase):
    """Exhaustive unit tests for `src/drift/adopt/adopt_derender.py`."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.stdout_patcher = patch("sys.stdout", StringIO())
        self.stderr_patcher = patch("sys.stderr", StringIO())
        self.stdout_patcher.start()
        self.stderr_patcher.start()

    def tearDown(self) -> None:
        self.stdout_patcher.stop()
        self.stderr_patcher.stop()

    def test_layer1_split_rh_deletes_at_tr_boundaries_and_case_2_5b(self) -> None:
        """Verifies Layer 1 boundary splitting of `RH_DELETE` units and Case 2.5a vs 2.5b tagging."""
        tr_starts = [0, 1, 2, 4, 4, 6]
        # _get_interior_cut_points returns only points strictly inside (d1, d2)
        self.assertEqual(_get_interior_cut_points(1, 6, tr_starts), [2, 4, 4])
        self.assertEqual(_get_interior_cut_points(0, 1, tr_starts), [])
        self.assertEqual(_get_interior_cut_points(2, 4, tr_starts), [])

        # Non-DELETE RH units are never split even if they span multiple cut points
        rh_blk = _rh(DiffUnitType.BLOCK_EDIT, 0, 6, 0, 2)
        self.assertEqual(_split_single_rh_delete(rh_blk, [0, 1, 2, 4, 6]), [rh_blk])

        # Single RH_DELETE spanning [0, 4) across cut points [1, 2] splits into [0,1), [1,2), [2,4)
        rh_del = _rh(DiffUnitType.DELETE, 0, 4, 0, 0)
        self.assertEqual(
            _split_single_rh_delete(rh_del, [0, 1, 2, 4]),
            [
                _rh(DiffUnitType.DELETE, 0, 1, 0, 0),
                _rh(DiffUnitType.DELETE, 1, 2, 0, 0),
                _rh(DiffUnitType.DELETE, 2, 4, 0, 0),
            ],
        )

        # Case 2.5a vs Case 2.5b:
        # - tr_del_left at r=1 (left boundary of RH_DELETE[1:3]) -> Case 2.5a (NOT conflict)
        # - tr_del_inside at r=2 (strictly inside RH_DELETE[1:3]) -> Case 2.5b (CONFLICT)
        # - tr_del_right at r=3 (right boundary of RH_DELETE[1:3]) -> Case 2.5a (NOT conflict)
        tr_units = (
            _tr(DiffUnitType.EQUAL, 0, 1, 0, 1),
            _tr(DiffUnitType.DELETE, 1, 2, 1, 1),  # at d1=1 (Case 2.5a)
            _tr(DiffUnitType.EQUAL, 2, 3, 1, 2),
            _tr(DiffUnitType.DELETE, 3, 5, 2, 2),  # at r=2 strictly inside (1, 3) (Case 2.5b)
            _tr(DiffUnitType.EQUAL, 5, 6, 2, 3),
            _tr(DiffUnitType.DELETE, 6, 7, 3, 3),  # at d2=3 (Case 2.5a)
        )
        rh_units = (
            _rh(DiffUnitType.EQUAL, 0, 1, 0, 1),
            _rh(DiffUnitType.DELETE, 1, 3, 1, 1),
        )
        updated_tr, split_rh = split_rh_deletes_at_tr_boundaries(tr_units, rh_units)

        self.assertFalse(updated_tr[1].is_conflict)  # r=1 left boundary -> clean
        self.assertTrue(updated_tr[3].is_conflict)   # r=2 strictly enclosed -> conflict
        self.assertFalse(updated_tr[5].is_conflict)  # r=3 right boundary -> clean
        self.assertEqual(
            split_rh,
            (
                _rh(DiffUnitType.EQUAL, 0, 1, 0, 1),
                _rh(DiffUnitType.DELETE, 1, 2, 1, 1),
                _rh(DiffUnitType.DELETE, 2, 3, 1, 1),
            ),
        )

    def test_layer2_orthogonal_matrix_all_cases_synthetic_diff_units(self) -> None:
        """Exhaustively tests Layer 2 Orthogonal Classification (`Cases 0.1–4.2`) on synthetic `DiffUnit`s."""
        # --- Row 0: RH_EQUAL (Cases 0.1–0.5) ---
        for tr_u in (
            _tr(DiffUnitType.EQUAL, 0, 1, 0, 1),
            _tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1),
            _tr(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 1),
            _tr(DiffUnitType.INSERT, 0, 0, 0, 1),
        ):
            c_tr, c_rh = _classify_orthogonal_units(
                (tr_u,), (_rh(DiffUnitType.EQUAL, 0, 1, 0, 1),)
            )
            self.assertFalse(c_tr[0].is_conflict)
            self.assertFalse(c_rh[0].is_conflict)

        # --- Row 1: RH_SINGLE_EDIT / RH_SINGLE_SKEL_EDIT (Cases 1.1–1.4) ---
        # Case 1.1: TR_EQUAL + RH_SINGLE_EDIT -> Clean
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1),
                [_tr(DiffUnitType.EQUAL, 0, 1, 0, 1)],
                [],
            )
        )
        # Case 1.2: TR_SINGLE_EDIT + RH_SINGLE_SKEL_EDIT (exact match) -> Clean
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.SINGLE_SKEL_EDIT, 0, 1, 0, 1, derendered_lines=("x = ${V}",)),
                [_tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1)],
                [],
            )
        )
        # Case 1.3: TR_SINGLE_EDIT + RH_SINGLE_EDIT -> Conflict
        self.assertFalse(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=False),
                [_tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1)],
                [],
            )
        )
        # Case 1.4: TR_BLOCK_EDIT or TR_INSERT + RH_SINGLE_EDIT / RH_SINGLE_SKEL_EDIT -> Conflict
        for tr_type in (DiffUnitType.BLOCK_EDIT, DiffUnitType.INSERT):
            for rh_type in (DiffUnitType.SINGLE_EDIT, DiffUnitType.SINGLE_SKEL_EDIT):
                self.assertFalse(
                    _is_clean_rh_edit_or_delete(
                        _rh(rh_type, 0, 1, 0, 1),
                        [_tr(tr_type, 0, 1, 0, 1)],
                        [],
                    )
                )

        # --- Row 2: RH_DELETE (Cases 2.1–2.5b) ---
        # Case 2.1: RH_DELETE on TR_EQUAL or exact 1-line TR_SINGLE_EDIT -> Clean
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 1, 0, 0),
                [_tr(DiffUnitType.EQUAL, 0, 1, 0, 1)],
                [],
            )
        )
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 1, 0, 0),
                [_tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1)],
                [],
            )
        )
        # Case 2.2: Complete RH_DELETE over multi-line TR_BLOCK_EDIT [0, 3) -> Clean
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 3, 0, 0),
                [_tr(DiffUnitType.BLOCK_EDIT, 0, 4, 0, 3)],
                [],
            )
        )
        # Case 2.3: Partial RH_DELETE [0, 2) inside TR_BLOCK_EDIT [0, 3) -> Conflict
        self.assertFalse(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 2, 0, 0),
                [_tr(DiffUnitType.BLOCK_EDIT, 0, 4, 0, 3)],
                [],
            )
        )
        # Case 2.4: Complete or partial RH_DELETE over TR_INSERT (0 -> N) -> Conflict
        self.assertFalse(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 2, 0, 0),
                [_tr(DiffUnitType.INSERT, 0, 0, 0, 2)],
                [],
            )
        )
        self.assertFalse(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.DELETE, 0, 1, 0, 0),
                [_tr(DiffUnitType.INSERT, 0, 0, 0, 2)],
                [],
            )
        )

        # --- Row 3: RH_INSERT (Cases 3.1–3.5) ---
        # Setup TR axis:
        # idx 0: TR_EQUAL [0, 1)
        # idx 1: TR_DELETE [1, 1) (0-length at r=1)
        # idx 2: TR_BLOCK_EDIT [1, 3)
        # idx 3: TR_INSERT [3, 5)
        tr_for_ins = (
            _tr(DiffUnitType.EQUAL, 0, 1, 0, 1),
            _tr(DiffUnitType.DELETE, 1, 3, 1, 1),
            _tr(DiffUnitType.BLOCK_EDIT, 3, 6, 1, 3),
            _tr(DiffUnitType.INSERT, 6, 6, 3, 5),
        )
        starts_ins = [u.r_start for u in tr_for_ins]
        ends_ins = [u.r_end for u in tr_for_ins]

        # Case 3.1: RH_INSERT at r=0 (exterior boundary before TR_EQUAL, no TR_DELETE) -> Clean
        self.assertEqual(_eval_rh_insert_conflicts(0, 0, starts_ins, ends_ins), (set(), set()))
        # Case 3.3: RH_INSERT at r=1 (touches TR_DELETE at idx 1) -> Conflict with {1}
        self.assertEqual(_eval_rh_insert_conflicts(0, 1, starts_ins, ends_ins), ({1}, {0}))
        # Case 3.2: RH_INSERT at r=2 (strictly inside TR_BLOCK_EDIT [1, 3) at idx 2) -> Conflict with {2}
        self.assertEqual(_eval_rh_insert_conflicts(0, 2, starts_ins, ends_ins), ({2}, {0}))
        # Case 3.5 (left boundary of TR_INSERT / right boundary of TR_BLOCK_EDIT at r=3, no TR_DELETE) -> Clean
        self.assertEqual(_eval_rh_insert_conflicts(0, 3, starts_ins, ends_ins), (set(), set()))
        # Case 3.4: RH_INSERT at r=4 (strictly inside TR_INSERT [3, 5) at idx 3) -> Conflict with {3}
        self.assertEqual(_eval_rh_insert_conflicts(0, 4, starts_ins, ends_ins), ({3}, {0}))
        # Case 3.5 (right boundary of TR_INSERT at r=5, no TR_DELETE) -> Clean
        self.assertEqual(_eval_rh_insert_conflicts(0, 5, starts_ins, ends_ins), (set(), set()))

        # --- Row 4: RH_BLOCK_EDIT (Cases 4.1–4.2) ---
        # Case 4.1: RH_BLOCK_EDIT across two TR_EQUAL units with no enclosed TR_DELETE -> Clean
        self.assertTrue(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 3),
                [_tr(DiffUnitType.EQUAL, 0, 1, 0, 1), _tr(DiffUnitType.EQUAL, 1, 2, 1, 2)],
                [],
            )
        )
        # Case 4.2a: RH_BLOCK_EDIT straddling TR_EQUAL and TR_SINGLE_EDIT -> Conflict
        self.assertFalse(
            _is_clean_rh_edit_or_delete(
                _rh(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 3),
                [_tr(DiffUnitType.EQUAL, 0, 1, 0, 1), _tr(DiffUnitType.SINGLE_EDIT, 1, 2, 1, 2)],
                [],
            )
        )
        # Case 4.2b: RH_BLOCK_EDIT over TR_EQUALs that strictly encloses a 0-length TR_DELETE at r=1 -> Conflict!
        tr_with_enclosed_del = (
            _tr(DiffUnitType.EQUAL, 0, 1, 0, 1),
            _tr(DiffUnitType.DELETE, 1, 3, 1, 1),
            _tr(DiffUnitType.EQUAL, 3, 4, 1, 2),
        )
        rh_blk_enclosing = _rh(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 2)
        c_tr_set, c_rh_set = _eval_rh_edit_or_delete_conflicts(
            0,
            rh_blk_enclosing,
            tr_with_enclosed_del,
            [u.r_start for u in tr_with_enclosed_del],
            [u.r_end for u in tr_with_enclosed_del],
        )
        self.assertEqual(c_tr_set, {0, 1, 2})
        self.assertEqual(c_rh_set, {0})

    def test_layer3_positive_and_zero_conflict_envelope_merging_and_splitting(self) -> None:
        """Verifies positive conflict interval merging (strict overlap only) and zero-length conflict envelopes."""
        # 1. Touching positive conflict intervals [0, 1) and [1, 2) must NOT be merged:
        tr_touching = (
            _tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=True),
            _tr(DiffUnitType.SINGLE_EDIT, 1, 2, 1, 2, is_conflict=True),
        )
        rh_touching = (
            _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=True),
            _rh(DiffUnitType.SINGLE_EDIT, 1, 2, 1, 2, is_conflict=True),
        )
        self.assertEqual(
            _merge_positive_conflict_r_ranges(tr_touching, rh_touching),
            ((0, 1), (1, 2)),
        )

        # 2. Strictly overlapping staggered conflicts: TR[0:2], RH[1:3], TR[2:4] -> merged into single (0, 4)
        tr_staggered = (
            _tr(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 2, is_conflict=True),
            _tr(DiffUnitType.BLOCK_EDIT, 2, 4, 2, 4, is_conflict=True),
        )
        rh_staggered = (
            _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=False),
            _rh(DiffUnitType.BLOCK_EDIT, 1, 3, 1, 3, is_conflict=True),
            _rh(DiffUnitType.SINGLE_EDIT, 3, 4, 3, 4, is_conflict=False),
        )
        merged_ranges = _merge_positive_conflict_r_ranges(tr_staggered, rh_staggered)
        self.assertEqual(merged_ranges, ((0, 4),))
        pos_spans = _build_positive_conflict_spans(
            merged_ranges,
            tr_staggered,
            [u.r_start for u in tr_staggered],
            [u.r_end for u in tr_staggered],
            rh_staggered,
            [e.r_start for e in rh_staggered],
            [e.r_end for e in rh_staggered],
        )
        self.assertEqual(
            pos_spans,
            (_DerenderSpan(t_start=0, t_end=4, r_start=0, r_end=4, h_start=0, h_end=4, is_conflict=True),),
        )

        # 3. Zero-length conflicts:
        # - At r=1 (strictly inside positive conflict [0, 4)): absorbed into [0, 4), NOT in zero_spans
        # - At r=0 (left boundary of [0, 4)): NOT strictly inside, emitted as zero conflict span at r=0
        # - At r=4 (right boundary of [0, 4)): NOT strictly inside, emitted as zero conflict span at r=4
        tr_zero = (
            _tr(DiffUnitType.DELETE, 0, 1, 0, 0, is_conflict=True),
            _tr(DiffUnitType.DELETE, 2, 3, 1, 1, is_conflict=True),  # strictly inside (0, 4)
        )
        rh_zero = (
            _rh(DiffUnitType.INSERT, 0, 0, 0, 1, is_conflict=True),
            _rh(DiffUnitType.INSERT, 4, 4, 4, 6, is_conflict=True),
        )
        zero_spans = _build_zero_conflict_spans(
            tr_zero,
            rh_zero,
            [0],
            [4],
            (_tr(DiffUnitType.BLOCK_EDIT, 1, 5, 0, 4, is_conflict=True),),
            [0],
            (_rh(DiffUnitType.BLOCK_EDIT, 0, 4, 1, 4, is_conflict=True),),
            [0],
        )
        self.assertEqual(len(zero_spans), 2)
        # At r=0: combines tr_zero[0] (T[0:1]) and rh_zero[0] (H[0:1])
        self.assertEqual(
            zero_spans[0],
            _DerenderSpan(t_start=0, t_end=1, r_start=0, r_end=0, h_start=0, h_end=1, is_conflict=True),
        )
        # At r=4: no tr_zero at r=4, so t_start=t_end=5 (resolved from tr_pos[0].src_end), H[4:6]
        self.assertEqual(
            zero_spans[1],
            _DerenderSpan(t_start=5, t_end=5, r_start=4, r_end=4, h_start=4, h_end=6, is_conflict=True),
        )

    def test_layer3_clean_edit_spans_and_eof_newline_transitions(self) -> None:
        """Verifies clean edit span resolution, EOF-newline-only drift, and conflict-envelope filtering."""
        # 1. EOF-newline-only drift on RH_EQUAL: T has '\n', H drops '\n'
        t_nl = ["line1\n", "line2\n"]
        r_nl = ["line1\n", "line2\n"]
        h_no_nl = ["line1\n", "line2"]
        plan_drop_nl = build_two_step_derender_plan(t_nl, r_nl, h_no_nl)
        self.assertFalse(plan_drop_nl.has_conflicts)
        self.assertEqual(len(plan_drop_nl.clean_hunks), 1)
        self.assertEqual(
            plan_drop_nl.clean_hunks[0],
            CleanPatchHunk(
                t_start=1,
                t_end=2,
                old_lines=("line2",),
                new_lines=("line2",),
                old_eof_newline=True,
                new_eof_newline=False,
            ),
        )

        # 2. EOF-newline-only drift: T lacks '\n', R has '\n', H has '\n' (so R -> H is RH_EQUAL at EOF!)
        t_no_nl = ["line1\n", "line2"]
        plan_add_nl = build_two_step_derender_plan(t_no_nl, r_nl, r_nl)
        self.assertFalse(plan_add_nl.has_conflicts)
        self.assertEqual(len(plan_add_nl.clean_hunks), 1)
        self.assertEqual(
            plan_add_nl.clean_hunks[0],
            CleanPatchHunk(
                t_start=1,
                t_end=2,
                old_lines=("line2",),
                new_lines=("line2",),
                old_eof_newline=False,
                new_eof_newline=True,
            ),
        )

        # 3. Multiple clean RH_INSERT units at the same R point (BOF r=0 and EOF r=1)
        t_toks = [tokenize_line("body = 1\n")]
        h_toks = [
            tokenize_line("ins_a\n"),
            tokenize_line("ins_b\n"),
            tokenize_line("body = 1\n"),
            tokenize_line("tail_no_nl"),
        ]
        tr_pos = (_tr(DiffUnitType.EQUAL, 0, 1, 0, 1),)
        rh_pos = (_rh(DiffUnitType.EQUAL, 0, 1, 2, 3),)
        rh_zero = (
            _rh(DiffUnitType.INSERT, 0, 0, 0, 1),
            _rh(DiffUnitType.INSERT, 0, 0, 1, 2),
            _rh(DiffUnitType.INSERT, 1, 1, 3, 4),
        )
        clean_spans = _build_clean_edit_spans(
            rh_pos,
            rh_zero,
            [],
            [],
            tr_pos,
            [0],
            [1],
            t_toks,
            h_toks,
        )
        self.assertEqual(len(clean_spans), 2)
        # First zero span coalesces both RH_INSERTs at r=0 -> H[0:2] = ("ins_a", "ins_b")
        self.assertEqual(clean_spans[0].t_start, 0)
        self.assertEqual(clean_spans[0].t_end, 0)
        self.assertEqual(clean_spans[0].replacement, ("ins_a", "ins_b"))
        self.assertTrue(clean_spans[0].new_eof_newline)
        # Second zero span at r=1 (EOF) captures H[3:4] = ("tail_no_nl",) with new_eof_newline=False
        self.assertEqual(clean_spans[1].t_start, 1)
        self.assertEqual(clean_spans[1].t_end, 1)
        self.assertEqual(clean_spans[1].replacement, ("tail_no_nl",))
        self.assertFalse(clean_spans[1].new_eof_newline)

    def test_layer4_and_layer5_cumulative_coordinate_shifting_and_patch_roundtrip(self) -> None:
        """Verifies `_anchor_conflict_clusters_to_t_clean` across interleaved length deltas and full patch round-trip."""
        # Construct an interleaved sequence of clean & conflict _DerenderSpans on a 10-line template T:
        # - Span 0 (Clean insert at T[1:1]): +2 lines (shift after = +2)
        # - Span 1 (Conflict at T[2:4]): 2 lines in T -> should shift to T_clean[4:6]
        # - Span 2 (Clean delete at T[5:8]): -3 lines (shift after = +2 - 3 = -1)
        # - Span 3 (Clean replace at T[8:9]): 1 line -> 4 lines (+3 lines, shift after = -1 + 3 = +2)
        # - Span 4 (Conflict at T[9:10]): 1 line in T -> should shift to T_clean[11:12]
        t_stripped = tuple(f"t_{i}" for i in range(10))
        r_stripped = tuple(f"r_{i}" for i in range(10))
        h_stripped = tuple(f"h_{i}" for i in range(12))

        active_spans = (
            _DerenderSpan(1, 1, 1, 1, 1, 3, is_conflict=False, replacement=("ins_1", "ins_2")),
            _DerenderSpan(2, 4, 2, 4, 3, 4, is_conflict=True),
            _DerenderSpan(5, 8, 5, 8, 5, 5, is_conflict=False, replacement=()),
            _DerenderSpan(8, 9, 8, 9, 5, 9, is_conflict=False, replacement=("rep_a", "rep_b", "rep_c", "rep_d")),
            _DerenderSpan(9, 10, 9, 10, 9, 12, is_conflict=True),
        )
        clean_spans = tuple(s for s in active_spans if not s.is_conflict)
        t_clean = _apply_clean_spans_to_template(t_stripped, clean_spans)
        clusters = _anchor_conflict_clusters_to_t_clean(
            active_spans, t_stripped, r_stripped, h_stripped
        )

        self.assertEqual(len(clusters), 2)
        # Cluster 0: original T[2:4], shifted by +2 -> T_clean[4:6]
        self.assertEqual((clusters[0].t_start, clusters[0].t_end), (2, 4))
        self.assertEqual((clusters[0].t_clean_start, clusters[0].t_clean_end), (4, 6))
        self.assertEqual(t_clean[clusters[0].t_clean_start : clusters[0].t_clean_end], ("t_2", "t_3"))

        # Cluster 1: original T[9:10], shifted by (+2 - 3 + 3 = +2) -> T_clean[11:12]
        self.assertEqual((clusters[1].t_start, clusters[1].t_end), (9, 10))
        self.assertEqual((clusters[1].t_clean_start, clusters[1].t_clean_end), (11, 12))
        self.assertEqual(t_clean[clusters[1].t_clean_start : clusters[1].t_clean_end], ("t_9",))

        # End-to-end verification with build_two_step_derender_plan, build_display_unified_diff, and apply_source_patch
        t_raw = [
            "static_0 = 0\n",
            "var_1 = {{ v1 }};\n",
            "static_2 = 2\n",
            "{% for x in items %}\n",
            "loop_item = {{ x }}\n",
            "{% endfor %}\n",
            "footer = 3\n",
        ]
        r_raw = [
            "static_0 = 0\n",
            "var_1 = 100;\n",
            "static_2 = 2\n",
            "loop_item = a\n",
            "loop_item = b\n",
            "footer = 3\n",
        ]
        # Host:
        # 1. Edits skeleton of var_1 (Case 1.2 clean) -> 'var_1 = 100; # ok\n'
        # 2. Deletes static_2 (Case 2.1 clean)
        # 3. Inserts a line inside the loop output (Case 3.2 conflict on {% for %} block)
        # 4. Appends a line after footer (Case 3.1 clean)
        h_raw = [
            "static_0 = 0\n",
            "var_1 = 100; # ok\n",
            "loop_item = a\n",
            "loop_item = inserted_mid\n",
            "loop_item = b\n",
            "footer = 3\n",
            "extra_end = 4\n",
        ]
        plan = build_two_step_derender_plan(t_raw, r_raw, h_raw)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(len(plan.clean_hunks), 3)
        self.assertEqual(len(plan.conflict_clusters), 1)

        display_diff = build_display_unified_diff(plan, "a/src/conf.j2", "b/src/conf.j2")
        parsed_display_hunks = parse_unified_patch_hunks(display_diff)
        self.assertTrue(len(parsed_display_hunks) >= 1)

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_file = tmp_path / "conf.j2"
            src_file.write_text("".join(t_raw), encoding="utf-8")

            patch_obj = AdoptPatch(
                has_conflict=True,
                clean_hunks=plan.clean_hunks,
                conflict_clusters=plan.conflict_clusters,
                display_diff=display_diff,
            )
            self.assertTrue(apply_source_patch(src_file, patch_obj, accept_conflicts=True))
            merged_on_disk = src_file.read_text(encoding="utf-8").splitlines()
            expected_merged = splice_conflict_clusters_diff3(
                plan.t_clean_lines, plan.conflict_clusters
            )
            self.assertEqual(tuple(merged_on_disk), expected_merged)

            # Also verify read_rendered_baseline_lines safeguard on missing or symlinked baseline
            with self.assertRaises(SafeguardError):
                read_rendered_baseline_lines(tmp_path, Path("nonexistent.conf"))
            sym_path = tmp_path / "sym.conf"
            sym_path.symlink_to(src_file)
            with self.assertRaises(SafeguardError):
                read_rendered_baseline_lines(tmp_path, Path("sym.conf"))

    def test_layer3_zero_conflict_sandwiched_between_two_touching_positive_conflicts(self) -> None:
        """Verifies canonical `(r_start, r_end)` ordering when a zero-length conflict at `R[1:1]` sits
        between two touching positive-length conflicts `[0, 1)` and `[1, 2)`.
        """
        t_tokens = [tokenize_line(f"t{i}\n") for i in range(4)]
        h_tokens = [tokenize_line(f"h{i}\n") for i in range(3)]

        # TR:
        # - [0, 1) -> R[0, 1): conflicting TR_SINGLE_EDIT
        # - [1, 3) -> R[1, 1): conflicting TR_DELETE (at r=1)
        # - [3, 4) -> R[1, 2): conflicting TR_SINGLE_EDIT
        classified_tr = (
            _tr(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=True),
            _tr(DiffUnitType.DELETE, 1, 3, 1, 1, is_conflict=True),
            _tr(DiffUnitType.SINGLE_EDIT, 3, 4, 1, 2, is_conflict=True),
        )
        # RH:
        # - R[0, 1) -> H[0, 1): conflicting RH_SINGLE_EDIT
        # - R[1, 1) -> H[1, 2): conflicting RH_INSERT (at r=1)
        # - R[1, 2) -> H[2, 3): conflicting RH_SINGLE_EDIT
        classified_rh = (
            _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=True),
            _rh(DiffUnitType.INSERT, 1, 1, 1, 2, is_conflict=True),
            _rh(DiffUnitType.SINGLE_EDIT, 1, 2, 2, 3, is_conflict=True),
        )

        spans = _resolve_derender_spans(classified_tr, classified_rh, t_tokens, h_tokens)
        self.assertEqual(
            spans,
            [
                _DerenderSpan(t_start=0, t_end=1, r_start=0, r_end=1, h_start=0, h_end=1, is_conflict=True),
                _DerenderSpan(t_start=1, t_end=3, r_start=1, r_end=1, h_start=1, h_end=2, is_conflict=True),
                _DerenderSpan(t_start=3, t_end=4, r_start=1, r_end=2, h_start=2, h_end=3, is_conflict=True),
            ],
        )

    def test_layer3_clean_rh_unit_shadowed_by_overlapping_positive_conflict_envelope(self) -> None:
        """Verifies that a non-conflicting `RH` unit overlapping a multi-line `TR_BLOCK_EDIT` conflict
        envelope is absorbed into the conflict envelope and NOT emitted as a Step 1 clean span.
        """
        t_tokens = [tokenize_line("block_t1\n"), tokenize_line("block_t2\n")]
        h_tokens = [tokenize_line("edited_r1\n"), tokenize_line("r2_no_nl")]

        # TR has a single 2-line TR_BLOCK_EDIT covering R[0:2], marked is_conflict=True
        # RH has RH_SINGLE_EDIT on R[0:1] (conflict) and RH_EQUAL on R[1:2] (at EOF without newline)
        classified_tr = (
            _tr(DiffUnitType.BLOCK_EDIT, 0, 2, 0, 2, is_conflict=True),
        )
        classified_rh = (
            _rh(DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1, is_conflict=True),
            _rh(DiffUnitType.EQUAL, 1, 2, 1, 2, is_conflict=False),
        )

        spans = _resolve_derender_spans(classified_tr, classified_rh, t_tokens, h_tokens)
        # Only the single positive conflict envelope [0, 2) should be emitted;
        # RH_EQUAL[1:2] must NOT produce a Step 1 clean span!
        self.assertEqual(
            spans,
            [
                _DerenderSpan(t_start=0, t_end=2, r_start=0, r_end=2, h_start=0, h_end=2, is_conflict=True),
            ],
        )

    def test_layer3_tr_insert_all_rh_interactions(self) -> None:
        """Verifies all `TR_INSERT` ($0 \to N$) interactions across `RH_EQUAL`, `RH_INSERT`, `RH_DELETE`,
        `RH_SINGLE_EDIT`, and `RH_BLOCK_EDIT`.
        """
        t_tokens = [tokenize_line("anchor = 1\n")]

        # 1. TR_INSERT[0:2] at T[0:0] + RH_DELETE[0:2] (Case 2.4 complete deletion of generated lines) -> Conflict
        tr_ins = (_tr(DiffUnitType.INSERT, 0, 0, 0, 2), _tr(DiffUnitType.EQUAL, 0, 1, 2, 3))
        rh_del_gen = (_rh(DiffUnitType.DELETE, 0, 2, 0, 0), _rh(DiffUnitType.EQUAL, 2, 3, 0, 1))
        c_tr, c_rh = _classify_orthogonal_units(tr_ins, rh_del_gen)
        self.assertTrue(c_tr[0].is_conflict)
        self.assertTrue(c_rh[0].is_conflict)
        spans_del_gen = _resolve_derender_spans(c_tr, c_rh, t_tokens, [tokenize_line("anchor = 1\n")])
        self.assertEqual(
            spans_del_gen,
            [_DerenderSpan(t_start=0, t_end=0, r_start=0, r_end=2, h_start=0, h_end=0, is_conflict=True)],
        )

        # 2. TR_INSERT[0:2] at T[0:0] + RH_INSERT at r=0 (left boundary, Case 3.5) and r=2 (right boundary, Case 3.5) -> Clean!
        h_tokens_ins = [
            tokenize_line("before_gen\n"),
            tokenize_line("gen1\n"),
            tokenize_line("gen2\n"),
            tokenize_line("after_gen\n"),
            tokenize_line("anchor = 1\n"),
        ]
        rh_ins_boundaries = (
            _rh(DiffUnitType.INSERT, 0, 0, 0, 1),
            _rh(DiffUnitType.EQUAL, 0, 1, 1, 2),
            _rh(DiffUnitType.EQUAL, 1, 2, 2, 3),
            _rh(DiffUnitType.INSERT, 2, 2, 3, 4),
            _rh(DiffUnitType.EQUAL, 2, 3, 4, 5),
        )
        c_tr_b, c_rh_b = _classify_orthogonal_units(tr_ins, rh_ins_boundaries)
        self.assertTrue(all(not u.is_conflict for u in c_tr_b))
        self.assertTrue(all(not e.is_conflict for e in c_rh_b))
        spans_boundaries = _resolve_derender_spans(c_tr_b, c_rh_b, t_tokens, h_tokens_ins)
        self.assertEqual(len(spans_boundaries), 2)
        self.assertEqual(spans_boundaries[0].replacement, ("before_gen",))
        self.assertEqual(spans_boundaries[1].replacement, ("after_gen",))
        self.assertEqual((spans_boundaries[0].t_start, spans_boundaries[0].t_end), (0, 0))
        self.assertEqual((spans_boundaries[1].t_start, spans_boundaries[1].t_end), (0, 0))


if __name__ == "__main__":
    unittest.main()
