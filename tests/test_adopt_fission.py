"""Comprehensive unit tests for Layer 1a-1 (`drift.adopt.adopt_fission`).

Covers all four internal layers of `adopt_fission.py`:
    - Layer 1: Lossless Word-Level Line Tokenization (`_classify_token`, `tokenize_line`)
    - Layer 2: Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching
      (`_TokenOpcode`, `_extract_token_opcodes`, `_coalesce_space_bridged_opcodes`,
      `_compute_skeleton_score`, `compute_skeleton_match`, `_filter_unique_skeleton_matrix`,
      `find_monotonic_line_matches`)
    - Layer 3: Second-Pass Sentinel Hunk Fission (`_diff_sequences_to_units`,
      `_expand_sub_opcode_to_units`, `_fission_multiline_replace_block`,
      `_fission_replace_block`, `_expand_raw_opcode_to_units`, `fission_diff_units`)
    - Layer 4: Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus
      (`try_derender_single_line`, `refine_single_line_units`)
"""

from io import StringIO
import unittest
from unittest.mock import patch

from drift.adopt.adopt_fission import (
    _TokenOpcode,
    _classify_token,
    _coalesce_space_bridged_opcodes,
    _compute_skeleton_score,
    _expand_raw_opcode_to_units,
    _expand_sub_opcode_to_units,
    _extract_token_opcodes,
    _filter_unique_skeleton_matrix,
    _fission_multiline_replace_block,
    _fission_replace_block,
    compute_skeleton_match,
    find_monotonic_line_matches,
    fission_diff_units,
    refine_single_line_units,
    tokenize_line,
    try_derender_single_line,
)
from drift.core.constants import set_test_mode
from drift.models.adopt_models import (
    DiffRelation,
    DiffUnit,
    DiffUnitType,
    SkeletonMatch,
    TokenKind,
)

set_test_mode(True)


class TestAdoptFission(unittest.TestCase):
    """Unit test suite for `src/drift/adopt/adopt_fission.py`."""

    def setUp(self) -> None:
        set_test_mode(True)
        self.stdout_patcher = patch("sys.stdout", StringIO())
        self.stderr_patcher = patch("sys.stderr", StringIO())
        self.stdout_patcher.start()
        self.stderr_patcher.start()

    def tearDown(self) -> None:
        self.stdout_patcher.stop()
        self.stderr_patcher.stop()

    # ==========================================================================
    # Layer 1: Lossless Word-Level Line Tokenization
    # ==========================================================================

    def test_layer1_classify_token_word_space_punct_and_unicode(self) -> None:
        """Verifies `_classify_token` across ASCII words, underscores, Unicode, whitespace, and punctuation."""
        for word_tok in ("alpha", "123", "_", "_private_var", "v1_2", "变量", "переменная", "café"):
            self.assertEqual(_classify_token(word_tok), TokenKind.WORD, msg=f"Failed for {word_tok!r}")

        for space_tok in (" ", "   ", "\t", " \t "):
            self.assertEqual(_classify_token(space_tok), TokenKind.SPACE, msg=f"Failed for {space_tok!r}")

        for punct_tok in ("=", "{", "}", "$", "-", ".", ":", ";", "/", "#", '"', "'"):
            self.assertEqual(_classify_token(punct_tok), TokenKind.PUNCT, msg=f"Failed for {punct_tok!r}")

    def test_layer1_tokenize_line_lossless_reconstruction_and_line_endings(self) -> None:
        """Verifies `tokenize_line` preserves exact tokens, line endings (`\\n`, `\\r\\n`, `""`), and `word_set`."""
        samples = (
            "",
            "\n",
            "\r\n",
            "   \t  \n",
            "{{}}\n",
            'export API_KEY="${SECRET_KEY}" # inline comment\n',
            "server = {{ host_name }}:8080;\r\n",
            "no_trailing_newline = true",
        )
        for raw in samples:
            tok = tokenize_line(raw)
            self.assertEqual(tok.raw_line, raw)
            self.assertEqual("".join(tok.tokens) + tok.line_ending, raw)
            self.assertEqual(len(tok.tokens), len(tok.token_kinds))
            self.assertEqual(tok.has_newline, raw.endswith(("\n", "\r\n")))
            expected_words = {
                t for t, k in zip(tok.tokens, tok.token_kinds) if k == TokenKind.WORD
            }
            self.assertEqual(tok.word_set, expected_words)

    # ==========================================================================
    # Layer 2: Slot-Normalized Skeleton Similarity & Monotonic 2D DP Matching
    # ==========================================================================

    def test_layer2_token_opcode_properties_and_merge_to(self) -> None:
        """Verifies `_TokenOpcode` slice projections, semantic predicates, `signature_tokens`, and `merge_to`."""
        # Include literal token "SPACE" (which has TokenKind.WORD) to lock down kind-based filtering
        line_a = tokenize_line("SPACE = {{ val }}\n")
        line_b = tokenize_line("SPACE = 42\n")
        ops = _extract_token_opcodes(line_a, line_b)

        # First opcode is equal: ("SPACE", " ", "=", " ")
        first_op = ops[0]
        self.assertTrue(first_op.is_equal)
        self.assertFalse(first_op.is_space_only_equal)
        self.assertTrue(first_op.has_non_space_tokens)
        self.assertEqual(first_op.a_tokens, ("SPACE", " ", "=", " "))
        self.assertEqual(first_op.b_tokens, ("SPACE", " ", "=", " "))
        # "SPACE" is a WORD token and must be retained in signature_tokens, while " " is filtered out
        self.assertEqual(first_op.signature_tokens, ("SPACE", "="))

        # Construct a space-only equal opcode and a space-only non-equal opcode
        space_eq = _TokenOpcode("equal", 1, 2, 1, 2, line_a, line_b)
        self.assertTrue(space_eq.is_space_only_equal)
        self.assertFalse(space_eq.has_non_space_tokens)
        self.assertEqual(space_eq.signature_tokens, ())

        space_replace = _TokenOpcode("replace", 1, 2, 1, 2, line_a, line_b)
        self.assertFalse(space_replace.is_equal)
        self.assertFalse(space_replace.is_space_only_equal)
        self.assertFalse(space_replace.has_non_space_tokens)
        # Non-equal opcode containing only whitespace does NOT emit a "\x00" slot marker
        self.assertEqual(space_replace.signature_tokens, ())

        # Coalescing non-equal opcodes across the internal space of `{{ val }}` via `merge_to`
        coalesced = _coalesce_space_bridged_opcodes(ops)
        self.assertEqual(len(coalesced), 2)
        slot_op = coalesced[1]
        self.assertEqual(slot_op.tag, "replace")
        self.assertEqual("".join(slot_op.a_tokens), "{{ val }}")
        self.assertEqual("".join(slot_op.b_tokens), "42")
        self.assertTrue(slot_op.has_non_space_tokens)
        self.assertEqual(slot_op.signature_tokens, ("\x00",))

    def test_layer2_coalesce_space_bridged_opcodes_multi_bridge_and_non_space_barrier(self) -> None:
        """Verifies `_coalesce_space_bridged_opcodes` merges chained space bridges but stops at non-space tokens."""
        # 1. Multi-word template tag vs multi-word rendered value: all internal space bridges coalesce into 1 slot
        t_multi = tokenize_line("title = {{ app name prod }}\n")
        r_multi = tokenize_line("title = Drift Config Manager\n")
        ops_multi = _coalesce_space_bridged_opcodes(_extract_token_opcodes(t_multi, r_multi))
        non_eq_multi = [op for op in ops_multi if not op.is_equal]
        self.assertEqual(len(non_eq_multi), 1)
        self.assertEqual("".join(non_eq_multi[0].a_tokens), "{{ app name prod }}")
        self.assertEqual("".join(non_eq_multi[0].b_tokens), "Drift Config Manager")

        # 2. Two distinct slots separated by static punctuation/words must remain 2 separate slots
        t_two = tokenize_line("addr = {{ host }}:{{ port }}\n")
        r_two = tokenize_line("addr = 127.0.0.1:8080\n")
        ops_two = _coalesce_space_bridged_opcodes(_extract_token_opcodes(t_two, r_two))
        non_eq_two = [op for op in ops_two if not op.is_equal]
        self.assertEqual(len(non_eq_two), 2)
        self.assertEqual("".join(non_eq_two[0].a_tokens), "{{ host }}")
        self.assertEqual("".join(non_eq_two[1].a_tokens), "{{ port }}")

    def test_layer2_compute_skeleton_score_and_match_boundaries(self) -> None:
        """Verifies `_compute_skeleton_score` and `compute_skeleton_match` across gates and threshold `0.5`."""
        # 1. Empty / blank lines -> invalid
        self.assertFalse(compute_skeleton_match(tokenize_line(""), tokenize_line("x = 1")).is_valid)
        self.assertFalse(compute_skeleton_match(tokenize_line("\n"), tokenize_line("\n")).is_valid)

        # 2. Disjoint word sets when at least one line has words -> O(1) prune
        self.assertFalse(
            compute_skeleton_match(
                tokenize_line("alpha = 1\n"),
                tokenize_line("beta = 2\n"),
            ).is_valid
        )
        # Also verify _compute_skeleton_score returns 0.0 when n_word == 0 and both_lack_words=False
        ops_no_shared_word = _coalesce_space_bridged_opcodes(
            _extract_token_opcodes(
                tokenize_line("alpha = 1\n"),
                tokenize_line("beta = 2\n"),
            )
        )
        self.assertEqual(_compute_skeleton_score(ops_no_shared_word, both_lack_words=False), 0.0)

        # 3. Exact threshold boundary S = 0.50: 1 word (1.0) + 0 punct, 1 slot (1.0) -> 1.0 / 2.0 = 0.5
        m_exact = compute_skeleton_match(
            tokenize_line("return {{ val }}\n"),
            tokenize_line("return 0\n"),
        )
        self.assertTrue(m_exact.is_valid)
        self.assertAlmostEqual(m_exact.score, 0.5)
        self.assertEqual(m_exact.signature, ("return", "\x00"))

        # 4. Typical key = {{ value }}: 1 word (1.0) + 1 punct (0.25) = 1.25, 1 slot -> 1.25 / 2.25
        m_kv = compute_skeleton_match(
            tokenize_line("key = {{ value }}\n"),
            tokenize_line("key = v1\n"),
        )
        self.assertTrue(m_kv.is_valid)
        self.assertAlmostEqual(m_kv.score, 1.25 / 2.25)
        self.assertEqual(m_kv.signature, ("key", "=", "\x00"))

        # 5. Sub-threshold line: 1 word ('export', 1.0) + 1 punct ('=', 0.25) = 1.25, 2 slots -> 1.25 / 3.25 < 0.5
        m_sub = compute_skeleton_match(
            tokenize_line("export A = {{ a }}\n"),
            tokenize_line("export B = 1\n"),
        )
        self.assertFalse(m_sub.is_valid)
        self.assertEqual(m_sub.score, 0.0)

        # 6. Wordless lines (`both_lack_words=True`):
        #    - Fewer than 2 shared puncts -> gate fails (0.0)
        ops_one_punct = _coalesce_space_bridged_opcodes(
            _extract_token_opcodes(tokenize_line(";\n"), tokenize_line(";\n"))
        )
        self.assertEqual(_compute_skeleton_score(ops_one_punct, both_lack_words=True), 0.0)
        self.assertFalse(compute_skeleton_match(tokenize_line(";\n"), tokenize_line(";\n")).is_valid)

        #    - 2 shared puncts with 1 slot -> E_skel = 0.5, N_slots = 1 -> S = 0.5 / 1.5 = 1/3 < 0.5 (invalid)
        self.assertFalse(
            compute_skeleton_match(tokenize_line("[{{}}]\n"), tokenize_line("[!]\n")).is_valid
        )

        #    - 4 shared puncts with 1 slot -> E_skel = 1.0, N_slots = 1 -> S = 1.0 / 2.0 = 0.5 (valid!)
        m_punct_valid = compute_skeleton_match(
            tokenize_line("[[{{}}]]\n"),
            tokenize_line("[[!]]\n"),
        )
        self.assertTrue(m_punct_valid.is_valid)
        self.assertAlmostEqual(m_punct_valid.score, 0.5)
        self.assertEqual(m_punct_valid.signature, ("[", "[", "\x00", "]", "]"))

        # 7. Whitespace-only difference -> N_slots = 0, score = 1.0, no "\x00" in signature
        m_ws = compute_skeleton_match(
            tokenize_line("key = 1\n"),
            tokenize_line("key   =   1\n"),
        )
        self.assertTrue(m_ws.is_valid)
        self.assertAlmostEqual(m_ws.score, 1.0)
        self.assertEqual(m_ws.signature, ("key", "=", "1"))

    def test_layer2_filter_unique_skeleton_matrix_all_four_rules(self) -> None:
        """Verifies all 4 bidirectional signature uniqueness rules in `_filter_unique_skeleton_matrix`."""
        # Rule 1: Invalid cells (is_valid=False) are ignored when counting signatures
        valid_cell = SkeletonMatch(is_valid=True, score=0.8, signature=("k", "=", "\x00"))
        invalid_dup = SkeletonMatch(is_valid=False, score=0.0, signature=("k", "=", "\x00"))
        filtered_r1 = _filter_unique_skeleton_matrix(((valid_cell, invalid_dup),))
        self.assertTrue(filtered_r1[0][0].is_valid)
        self.assertFalse(filtered_r1[0][1].is_valid)

        # Rule 2: Row uniqueness (`1 -> N` loop-unrolling guard)
        t_loop = (tokenize_line("server = {{ item }}\n"),)
        r_loop = (
            tokenize_line("server = node1\n"),
            tokenize_line("server = node2\n"),
            tokenize_line("server = node3\n"),
        )
        self.assertEqual(find_monotonic_line_matches(t_loop, r_loop), ())

        # Rule 3: Column uniqueness (`M -> 1` conditional branch ambiguity guard)
        t_branch = (
            tokenize_line("{% if prod %}\n"),
            tokenize_line("server = {{ prod_host }}\n"),
            tokenize_line("{% else %}\n"),
            tokenize_line("server = {{ dev_host }}\n"),
            tokenize_line("{% endif %}\n"),
        )
        r_branch = (tokenize_line("server = 10.0.0.1\n"),)
        self.assertEqual(find_monotonic_line_matches(t_branch, r_branch), ())

        # Rule 4: Per-signature counting preserves unique diagonal matches despite shared prefixes
        a_prefix = (
            tokenize_line("set_header X_Real_IP {{ ip }};\n"),
            tokenize_line("set_header X_Forwarded {{ fwd }};\n"),
            tokenize_line("set_header X_Host {{ host }};\n"),
        )
        b_prefix = (
            tokenize_line("set_header X_Real_IP 1.2.3.4;\n"),
            tokenize_line("set_header X_Forwarded https;\n"),
            tokenize_line("set_header X_Host example.com;\n"),
        )
        raw_mat = tuple(
            tuple(compute_skeleton_match(a_line, b_line) for b_line in b_prefix)
            for a_line in a_prefix
        )
        self.assertTrue(raw_mat[0][1].is_valid)
        self.assertEqual(raw_mat[0][1].signature, ("set_header", "\x00", ";"))
        self.assertEqual(raw_mat[0][0].signature, ("set_header", "X_Real_IP", "\x00", ";"))

        filtered_mat = _filter_unique_skeleton_matrix(raw_mat)
        self.assertTrue(filtered_mat[0][0].is_valid)
        self.assertTrue(filtered_mat[1][1].is_valid)
        self.assertTrue(filtered_mat[2][2].is_valid)
        self.assertFalse(filtered_mat[0][1].is_valid)
        self.assertFalse(filtered_mat[0][2].is_valid)
        self.assertEqual(
            find_monotonic_line_matches(a_prefix, b_prefix),
            ((0, 0), (1, 1), (2, 2)),
        )

    def test_layer2_find_monotonic_line_matches_dp_non_crossing_and_empty_inputs(self) -> None:
        """Verifies 2D DP selects maximum-weight strictly monotonic pairs and handles empty inputs."""
        line_x = tokenize_line("x = {{ a }}\n")
        self.assertEqual(find_monotonic_line_matches((), (line_x,)), ())
        self.assertEqual(find_monotonic_line_matches((line_x,), ()), ())

        # Crossing candidates:
        # a_lines[0] matches b_lines[2] (1 pair, score ~0.556)
        # a_lines[1] matches b_lines[0] and a_lines[2] matches b_lines[1] (2 non-crossing pairs, total score ~1.111)
        # DP must choose ((1, 0), (2, 1)) over ((0, 2))!
        a_lines = (
            tokenize_line("gamma = {{ g }}\n"),
            tokenize_line("alpha = {{ a }}\n"),
            tokenize_line("beta = {{ b }}\n"),
        )
        b_lines = (
            tokenize_line("alpha = 1\n"),
            tokenize_line("beta = 2\n"),
            tokenize_line("gamma = 3\n"),
        )
        self.assertEqual(
            find_monotonic_line_matches(a_lines, b_lines),
            ((1, 0), (2, 1)),
        )

    # ==========================================================================
    # Layer 3: Second-Pass Sentinel Hunk Fission
    # ==========================================================================

    def test_layer3_fission_replace_block_single_and_multiline_sentinel_fission(self) -> None:
        """Verifies `_fission_replace_block`, `_fission_multiline_replace_block`, and `_expand_sub_opcode_to_units`."""
        # 1. 1 <-> 1 replace block: valid skeleton -> SINGLE_EDIT; unrelated -> BLOCK_EDIT
        t_single = (tokenize_line("port = {{ port }}\n"),)
        r_single_match = (tokenize_line("port = 8080\n"),)
        r_single_unrelated = (tokenize_line("# completely different comment\n"),)

        self.assertEqual(
            _fission_replace_block(t_single, r_single_match, 0, 1, 0, 1, DiffRelation.TR),
            (DiffUnit(DiffRelation.TR, DiffUnitType.SINGLE_EDIT, 0, 1, 0, 1),),
        )
        self.assertEqual(
            _fission_replace_block(t_single, r_single_unrelated, 0, 1, 0, 1, DiffRelation.TR),
            (DiffUnit(DiffRelation.TR, DiffUnitType.BLOCK_EDIT, 0, 1, 0, 1),),
        )

        # 2. Multi-line replace block with zero skeleton matches -> single BLOCK_EDIT
        src_unmatched = (tokenize_line("{% raw %}\n"), tokenize_line("{% endraw %}\n"))
        dst_unmatched = (tokenize_line("line_one\n"), tokenize_line("line_two\n"), tokenize_line("line_three\n"))
        self.assertEqual(
            _fission_multiline_replace_block(
                src_unmatched, dst_unmatched, 0, 2, 0, 3, DiffRelation.TR
            ),
            (DiffUnit(DiffRelation.TR, DiffUnitType.BLOCK_EDIT, 0, 2, 0, 3),),
        )

        # 3. Multi-line replace block with sentinel matches interleaved with DELETE, INSERT, and BLOCK_EDIT:
        #    src[0]: deleted header comment -> DELETE [0:1] <-> [0:0]
        #    src[1] <-> dst[0]: skeleton match `host = {{ h }}` <-> `host = localhost` -> SINGLE_EDIT [1:2] <-> [0:1]
        #    dst[1]: inserted line `inserted_flag = true` -> INSERT [2:2] <-> [1:2]
        #    src[2] <-> dst[2]: skeleton match `port = {{ p }}` <-> `port = 9000` -> SINGLE_EDIT [2:3] <-> [2:3]
        #    src[3:5] <-> dst[3:4]: unmatched trailing lines -> BLOCK_EDIT [3:5] <-> [3:4]
        src_mixed = (
            tokenize_line("# old header\n"),
            tokenize_line("host = {{ h }}\n"),
            tokenize_line("port = {{ p }}\n"),
            tokenize_line("{% if tls %}\n"),
            tokenize_line("{% endif %}\n"),
        )
        dst_mixed = (
            tokenize_line("host = localhost\n"),
            tokenize_line("inserted_flag = true\n"),
            tokenize_line("port = 9000\n"),
            tokenize_line("tls_enabled\n"),
        )
        self.assertEqual(
            _fission_multiline_replace_block(src_mixed, dst_mixed, 0, 5, 0, 4, DiffRelation.TR),
            (
                DiffUnit(DiffRelation.TR, DiffUnitType.DELETE, 0, 1, 0, 0),
                DiffUnit(DiffRelation.TR, DiffUnitType.SINGLE_EDIT, 1, 2, 0, 1),
                DiffUnit(DiffRelation.TR, DiffUnitType.INSERT, 2, 2, 1, 2),
                DiffUnit(DiffRelation.TR, DiffUnitType.SINGLE_EDIT, 2, 3, 2, 3),
                DiffUnit(DiffRelation.TR, DiffUnitType.BLOCK_EDIT, 3, 5, 3, 4),
            ),
        )

        # 4. _expand_sub_opcode_to_units with identical raw lines inside a second-pass "equal" span
        ident_line = tokenize_line("same = 1\n")
        sub_eq_units = _expand_sub_opcode_to_units(
            "equal", 0, 1, 0, 1, (ident_line,), (ident_line,), 0, 0, DiffRelation.RH
        )
        self.assertEqual(
            sub_eq_units,
            (DiffUnit(DiffRelation.RH, DiffUnitType.EQUAL, 0, 1, 0, 1),),
        )

    def test_layer3_fission_diff_units_contiguous_partition_invariant(self) -> None:
        """Verifies `fission_diff_units` produces a gapless, monotonic partition of `src` and `dst`."""
        t_tokens = tuple(
            map(
                tokenize_line,
                (
                    "[section]\n",
                    "host = {{ host }}\n",
                    "port = {{ port }}\n",
                    "{% for s in servers %}\n",
                    "node = {{ s }}\n",
                    "{% endfor %}\n",
                    "footer = 1\n",
                ),
            )
        )
        r_tokens = tuple(
            map(
                tokenize_line,
                (
                    "[section]\n",
                    "host = 10.0.0.1\n",
                    "port = 8080\n",
                    "node = alpha\n",
                    "node = beta\n",
                    "footer = 1\n",
                    "extra_generated = true\n",
                ),
            )
        )
        units = fission_diff_units(t_tokens, r_tokens, DiffRelation.TR)

        # Verify gapless coverage from 0 to len(t_tokens) and 0 to len(r_tokens)
        self.assertEqual(units[0].src_start, 0)
        self.assertEqual(units[0].dst_start, 0)
        self.assertEqual(units[-1].src_end, len(t_tokens))
        self.assertEqual(units[-1].dst_end, len(r_tokens))
        for prev_u, next_u in zip(units[:-1], units[1:]):
            self.assertEqual(prev_u.src_end, next_u.src_start)
            self.assertEqual(prev_u.dst_end, next_u.dst_start)

        # Also verify direct `_expand_raw_opcode_to_units` for "delete" and "insert"
        del_u = _expand_raw_opcode_to_units("delete", 2, 4, 2, 2, t_tokens, r_tokens, DiffRelation.TR)
        ins_u = _expand_raw_opcode_to_units("insert", 2, 2, 2, 5, t_tokens, r_tokens, DiffRelation.TR)
        self.assertEqual(del_u, (DiffUnit(DiffRelation.TR, DiffUnitType.DELETE, 2, 4, 2, 2),))
        self.assertEqual(ins_u, (DiffUnit(DiffRelation.TR, DiffUnitType.INSERT, 2, 2, 2, 5),))

    # ==========================================================================
    # Layer 4: Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus
    # ==========================================================================

    def test_layer4_try_derender_single_line_disjoint_vs_closed_boundary_collisions(self) -> None:
        """Verifies `try_derender_single_line` across fast paths, disjoint edits, and closed-interval overlaps."""
        # 1. Fast path: no variable slots in T <-> R -> returns H stripped line
        self.assertEqual(
            try_derender_single_line(
                tokenize_line("static = 1\n"),
                tokenize_line("static = 1\n"),
                tokenize_line("static = 2\n"),
            ),
            "static = 2",
        )
        # 2. Fast path: no host edits in R <-> H -> returns T stripped line
        self.assertEqual(
            try_derender_single_line(
                tokenize_line("key = {{ val }}\n"),
                tokenize_line("key = 123\n"),
                tokenize_line("key = 123\n"),
            ),
            "key = {{ val }}",
        )

        # 3. Clean disjoint sub-line edit (Case 1.2) with space-bridged multi-word variable value
        #    T: `greeting = "{{ hello_msg }}" # v1`
        #    R: `greeting = "hello world" # v1`
        #    H: `greeting = "hello world" # v2`
        self.assertEqual(
            try_derender_single_line(
                tokenize_line('greeting = "{{ hello_msg }}" # v1\n'),
                tokenize_line('greeting = "hello world" # v1\n'),
                tokenize_line('greeting = "hello world" # v2\n'),
            ),
            'greeting = "{{ hello_msg }}" # v2',
        )

        # 4. Clean disjoint sub-line edit with multiple variable slots and middle static edit
        #    T: `url = http://{{ host }}:{{ port }}/v1`
        #    R: `url = http://localhost:8080/v1`
        #    H: `url = https://localhost:8080/v2`
        self.assertEqual(
            try_derender_single_line(
                tokenize_line("url = http://{{ host }}:{{ port }}/v1\n"),
                tokenize_line("url = http://localhost:8080/v1\n"),
                tokenize_line("url = https://localhost:8080/v2\n"),
            ),
            "url = https://{{ host }}:{{ port }}/v2",
        )

        # 5. Direct overlap on variable value (Case 1.3 -> None)
        self.assertIsNone(
            try_derender_single_line(
                tokenize_line("port = {{ port }}\n"),
                tokenize_line("port = 8080\n"),
                tokenize_line("port = 9090\n"),
            )
        )

        # 6. Zero-width insertion immediately adjacent to variable slot right boundary (p_1 == c_2 -> None)
        #    In R: `key = 8080`, tokens: `('key', ' ', '=', ' ', '8080')`, variable slot is [4, 5].
        #    In H: `key = 8080_suffix` or `key = 8080!` where `!` inserts at [5, 5], touching c_2 = 5!
        self.assertIsNone(
            try_derender_single_line(
                tokenize_line("key = {{ port }}\n"),
                tokenize_line("key = 8080\n"),
                tokenize_line("key = 8080!\n"),
            )
        )

        # 7. Zero-width insertion immediately adjacent to variable slot left boundary (p_2 == c_1 -> None)
        #    In H: `key = !8080` where `!` inserts at [4, 4], touching c_1 = 4!
        self.assertIsNone(
            try_derender_single_line(
                tokenize_line("key = {{ port }}\n"),
                tokenize_line("key = 8080\n"),
                tokenize_line("key = !8080\n"),
            )
        )

    def test_layer4_refine_single_line_units_upgrades_clean_and_flags_conflicts(self) -> None:
        """Verifies `refine_single_line_units` across no-op, clean `SINGLE_SKEL_EDIT`, and colliding `SINGLE_EDIT`."""
        t_tokens = (
            tokenize_line("static = 1\n"),
            tokenize_line("clean_key = {{ val }} # old\n"),
            tokenize_line("conflict_key = {{ secret }}\n"),
        )
        r_tokens = (
            tokenize_line("static = 1\n"),
            tokenize_line("clean_key = 42 # old\n"),
            tokenize_line("conflict_key = prod_secret\n"),
        )
        h_tokens = (
            tokenize_line("static = 1\n"),
            tokenize_line("clean_key = 42 # new\n"),
            tokenize_line("conflict_key = overridden_secret\n"),
        )

        # 1. When no shared R index has SINGLE_EDIT in both TR and RH, units are returned unchanged
        tr_no_shared = (DiffUnit(DiffRelation.TR, DiffUnitType.EQUAL, 0, 3, 0, 3),)
        rh_no_shared = (DiffUnit(DiffRelation.RH, DiffUnitType.SINGLE_EDIT, 1, 2, 1, 2),)
        self.assertEqual(
            refine_single_line_units(tr_no_shared, rh_no_shared, t_tokens, r_tokens, h_tokens),
            (tr_no_shared, rh_no_shared),
        )

        # 2. Mixed batch: r=0 is EQUAL, r=1 is clean 1<->1<->1, r=2 is colliding 1<->1<->1
        tr_units = fission_diff_units(t_tokens, r_tokens, DiffRelation.TR)
        rh_units = fission_diff_units(r_tokens, h_tokens, DiffRelation.RH)
        refined_tr, refined_rh = refine_single_line_units(
            tr_units, rh_units, t_tokens, r_tokens, h_tokens
        )

        # r=0: untouched EQUAL in both
        self.assertEqual(refined_tr[0].unit_type, DiffUnitType.EQUAL)
        self.assertFalse(refined_tr[0].is_conflict)
        self.assertEqual(refined_rh[0].unit_type, DiffUnitType.EQUAL)
        self.assertFalse(refined_rh[0].is_conflict)

        # r=1: clean sub-line edit -> TR stays non-conflicting SINGLE_EDIT; RH upgrades to SINGLE_SKEL_EDIT
        self.assertEqual(refined_tr[1].unit_type, DiffUnitType.SINGLE_EDIT)
        self.assertFalse(refined_tr[1].is_conflict)
        self.assertEqual(refined_rh[1].unit_type, DiffUnitType.SINGLE_SKEL_EDIT)
        self.assertFalse(refined_rh[1].is_conflict)
        self.assertEqual(refined_rh[1].derendered_lines, ("clean_key = {{ val }} # new",))

        # r=2: overlapping variable edit -> both TR and RH marked is_conflict=True
        self.assertEqual(refined_tr[2].unit_type, DiffUnitType.SINGLE_EDIT)
        self.assertTrue(refined_tr[2].is_conflict)
        self.assertEqual(refined_rh[2].unit_type, DiffUnitType.SINGLE_EDIT)
        self.assertTrue(refined_rh[2].is_conflict)
        self.assertEqual(refined_rh[2].derendered_lines, ())


if __name__ == "__main__":
    unittest.main()
