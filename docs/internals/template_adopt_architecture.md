# 3-Way Template De-Rendering: Domain Models, Module Architecture & Pipeline Dataflow

> [!NOTE]
> This document specifies the domain dataclasses, context-bound pipeline records, module boundaries, and end-to-end functional codeflow implementing Drift's 3-Way Template De-Rendering Engine in `src/drift/`. For the underlying mathematical derivations, orthogonal classification matrix (`Cases 0.1–4.2`), and complexity proofs, see the companion specification **[3-Way Template De-Rendering: Mathematical Model & Symbolic Algorithms](template_adopt_derendering.md)**.

* **Companion Mathematical Specification**: [`docs/internals/template_adopt_derendering.md`](template_adopt_derendering.md)
* **Bidirectional Drift Adoption Overview**: [`docs/internals/drift_adoption_and_fcd.md`](drift_adoption_and_fcd.md)
* **Target Subpackages**: [`src/drift/models/adopt_models.py`](../../src/drift/models/adopt_models.py), [`src/drift/adopt/`](../../src/drift/adopt/AGENT_MODULE.md), [`src/drift/utils/func_utils.py`](../../src/drift/utils/func_utils.py)

---

## 1. Module Allocation & Layered Responsibilities

Following Drift's architectural principles—**Separation of Gathering & Manipulation**, **Decomposed Pure Functional Stage Pipelines**, **Structured & Typed Return Boundaries**, and **Planning-Stage Safeguards vs. Simple Execution Handlers**—the adoption and de-rendering subsystem is organized into focused layers:

| Layer | Module | Core Responsibility |
| :--- | :--- | :--- |
| **Layer 0 (Utils)** | [`utils/func_utils.py`](../../src/drift/utils/func_utils.py) | Odd-arity `choose`/`branch` matching, `partition`/`star_partition`, `star_filter`, and generic $O(N)$ gap-interleaving `SpliceSpan` + `splice_spans`. |
| **Layer 0 (Models)** | [`models/adopt_models.py`](../../src/drift/models/adopt_models.py) | Immutable domain models: `TokenKind`, `TokenizedLine`, `SkeletonMatch`, `DiffRelation`, `DiffUnitType`, `DiffUnit`, `CleanPatchHunk`, `ConflictCluster`, `TwoStepDerenderPlan`, `AdoptPatch`, and `PackageAdoptPlan`. |
| **Layer 1a-1** | [`adopt/adopt_fission.py`](../../src/drift/adopt/adopt_fission.py) | Word-level tokenization (`tokenize_line`), space-bridge coalescing, slot-normalized skeleton similarity (`compute_skeleton_match`), transpose-based uniqueness filtering (`_filter_unique_skeleton_matrix`), 2D DP matching (`find_monotonic_line_matches`), second-pass sentinel fission (`fission_diff_units`), and $1 \leftrightarrow 1 \leftrightarrow 1$ token calculus (`try_derender_single_line`, `refine_single_line_units`). |
| **Layer 1a-2** | [`adopt/adopt_derender.py`](../../src/drift/adopt/adopt_derender.py) | `RH_DELETE` boundary splitting (`split_rh_deletes_at_tr_boundaries`), $R$-axis `bisect` orthogonal classification (`_classify_orthogonal_units`), positive/zero-length envelope closure (`_resolve_derender_spans`), `build_two_step_derender_plan`, `build_display_unified_diff`, and `generate_derendered_patch`. |
| **Layer 1b** | [`adopt/adopt_patch.py`](../../src/drift/adopt/adopt_patch.py) | Unified diff extraction (`generate_install_git_patch`, `generate_retargeted_patch`), pure-Python hunk parsing (`parse_unified_patch_hunks`), conflict probing (`check_patch_conflicts`), and pure-Python Two-Step Patching (`apply_source_patch`, `splice_conflict_clusters_diff3`). |
| **Layer 1c** | [`adopt/adopt_plan.py`](../../src/drift/adopt/adopt_plan.py) | Drift discovery (`get_drifted_packages`), `.drift_keep` normalization, convergence partitioning, and `plan_package_adopt` (populating `PackageAdoptPlan.mod_patches: Dict[str, AdoptPatch]`). |
| **Layers 2–4** | [`adopt/adopt_actions.py`](../../src/drift/adopt/adopt_actions.py), [`adopt_handlers.py`](../../src/drift/adopt/adopt_handlers.py), [`adopt_package.py`](../../src/drift/adopt/adopt_package.py) | Atomic file reconciliation actions (`patch_and_edit`, `adopt_rename`, editor fallbacks), interactive (`-i`) and non-interactive item dispatchers, and single-package execution (`execute_package_adopt`). |

---

## 2. Domain Models & Context-Bound Pipeline Records

All domain and internal pipeline records use `@dataclass(frozen=True)` (or `SerializableModel` dataclasses) with newline-stripped `Tuple[str, ...]` line representations (`keepends=False`) and explicit boolean boundary flags.

### 2.1 Lexical & Skeleton Models (`src/drift/models/adopt_models.py`)

```python
class TokenKind(str, Enum):
    """Neovim-style three-way character class partition for lossless line tokenization."""
    WORD = "word"          # \w+ (Unicode letters, digits, underscore)
    PUNCT = "punct"        # [^\w\s] (single punctuation / operator character)
    SPACE = "space"        # \s+ (contiguous horizontal whitespace)


@dataclass(frozen=True)
class TokenizedLine:
    """Losslessly tokenized line with pre-extracted word set for O(1) disjointness pruning."""
    raw_line: str                      # Original line including trailing newline
    stripped_line: str                 # Line without trailing \r\n
    line_ending: str                   # Trailing "\n", "\r\n", or ""
    tokens: Tuple[str, ...]            # Lossless token tuple: "".join(tokens) == stripped_line
    token_kinds: Tuple[TokenKind, ...] # Parallel token category tuple
    word_set: FrozenSet[str]           # Set of WORD tokens for O(1) intersection pre-filter

    @property
    def has_newline(self) -> bool:
        """True if the original raw line ended with a trailing newline sequence (`\\n` or `\\r\\n`)."""
        return bool(self.line_ending)


@dataclass(frozen=True)
class SkeletonMatch:
    """Result of comparing two TokenizedLines for shared static skeleton."""
    is_valid: bool                     # True if structural gate and score >= 0.5 pass
    score: float                       # Slot-normalized similarity E_skel / (E_skel + N_slots)
    signature: Tuple[str, ...]         # Matched non-space tokens interspersed with "\x00" slot markers
```

---

### 2.2 Unified Atomic Alignment Unit (`DiffRelation`, `DiffUnitType`, `DiffUnit`)

Representing both $T \to R$ (`DiffRelation.TR`) and $R \to H$ (`DiffRelation.RH`) with a single `DiffUnit` dataclass and a 6-member `DiffUnitType` enum allows `fission_diff_units` and all $R$-axis `bisect` algorithms to operate uniformly on both relations:

```python
class DiffRelation(str, Enum):
    TR = "tr"  # src = Template (T), dst = Rendered (R)
    RH = "rh"  # src = Rendered (R), dst = Host (H)


class DiffUnitType(str, Enum):
    EQUAL = "equal"                        # 1 <-> 1: identical line
    SINGLE_EDIT = "single_edit"            # 1 <-> 1: line edit with shared skeleton
    SINGLE_SKEL_EDIT = "single_skel_edit"  # 1 <-> 1: RH skeleton-only edit preserving TR variable slots (Case 1.2)
    BLOCK_EDIT = "block_edit"              # M <-> N (M>=1, N>=1): irreducible block replacement
    INSERT = "insert"                      # 0 <-> N (N>=1): pure insertion
    DELETE = "delete"                      # M <-> 0 (M>=1): pure deletion


@dataclass(frozen=True)
class DiffUnit:
    """Unified atomic alignment unit for both T -> R (TR) and R -> H (RH)."""
    relation: DiffRelation
    unit_type: DiffUnitType
    src_start: int
    src_end: int
    dst_start: int
    dst_end: int
    is_conflict: bool = False
    derendered_lines: Tuple[str, ...] = ()  # Populated for RH SINGLE_SKEL_EDIT (Case 1.2)

    @property
    def r_start(self) -> int:
        """Start coordinate on the shared Rendered (R) axis."""
        return choose(self.relation == DiffRelation.TR, self.dst_start, self.src_start)

    @property
    def r_end(self) -> int:
        """End coordinate on the shared Rendered (R) axis."""
        return choose(self.relation == DiffRelation.TR, self.dst_end, self.src_end)

    @property
    def src_len(self) -> int:
        return self.src_end - self.src_start

    @property
    def dst_len(self) -> int:
        return self.dst_end - self.dst_start

    @property
    def is_positive_length(self) -> bool:
        """True if this unit occupies a positive line interval `[r_start, r_end)` on the `R` axis."""
        return self.r_end > self.r_start
```

* **Architectural Benefits of Unified `DiffUnit`**:
  1. **100% Reuse of Stage 1 Fission**: `fission_diff_units(src_tokens, dst_tokens, relation)` runs identical skeleton matrix construction, uniqueness filtering, 2D DP, and second-pass sentinel alignment for both `TR` and `RH`.
  2. **Transparent $R$-Axis Projection**: `u.r_start` and `u.r_end` project `TR` (`dst`) and `RH` (`src`) onto the shared $R$ coordinate axis without branching in callers.
  3. **Zero Extra Enum Needed for Case 2.5b**: Because `DiffUnit` carries `is_conflict: bool`, when an `RH_DELETE` strictly encloses a zero-length `TR_DELETE` ($d_1 < r_1 < d_2$), `split_rh_deletes_at_tr_boundaries` marks the enclosed `TR_DELETE` with `is_conflict=True` while leaving the split `RH_DELETE` pieces `is_conflict=False`.

---

### 2.3 Two-Step Plan & Structured Patch Models (`CleanPatchHunk`, `ConflictCluster`, `TwoStepDerenderPlan`, `AdoptPatch`)

Why store structured `CleanPatchHunk` and `ConflictCluster` tuples inside `AdoptPatch` instead of a flat unified diff string?
1. **Two Distinct Coordinate Spaces ($T$ vs. $T_{\text{clean}}$)**: Step 1 clean edits apply to $T$ and change its line count by $\Delta L$, producing $T_{\text{clean}}$; Step 2 conflict clusters apply to the shifted coordinates $[t_{\text{clean\_start}}, t_{\text{clean\_end}})$ in $T_{\text{clean}}$ so adjacent clean edits are never swallowed inside conflict markers.
2. **Three Independent Line Dimensions ($M \neq N \neq Q$)**: A 2-way unified diff hunk header `@@ -old,N +new,Q @@` only encodes 2 dimensions and cannot simultaneously represent $M$ template lines in $T$, $N$ rendered lines in $R$, and $Q$ host lines in $H$ when a multi-line loop or conditional block conflicts ($M \neq N$).

```python
@dataclass
class CleanPatchHunk(SerializableModel):
    """A single 0-based Step 1 slice replacement on source file `T[t_start:t_end]`."""
    t_start: int
    t_end: int
    old_lines: Tuple[str, ...] = ()
    new_lines: Tuple[str, ...] = ()
    old_eof_newline: bool = True
    new_eof_newline: bool = True

    @property
    def is_old_eof_nl_valid(self) -> bool:
        return bool(self.old_lines)

    @property
    def is_new_eof_nl_valid(self) -> bool:
        return bool(self.new_lines)


@dataclass
class ConflictCluster(SerializableModel):
    """A converged Step 2 conflict cluster over (T, R, H) with shifted coordinates in T_clean."""
    t_start: int
    t_end: int
    t_clean_start: int
    t_clean_end: int
    t_lines: Tuple[str, ...] = ()      # Template lines T[t_start:t_end] (M lines, for <<<<<<<)
    r_lines: Tuple[str, ...] = ()      # Rendered lines R[r_start:r_end] (N lines, for |||||||)
    h_lines: Tuple[str, ...] = ()      # Host lines H[h_start:h_end] (Q lines, for =======)


@dataclass
class TwoStepDerenderPlan(SerializableModel):
    """Complete pure-computation output of the 3-way template de-rendering engine."""
    t_lines: Tuple[str, ...] = ()
    t_clean_lines: Tuple[str, ...] = ()
    clean_hunks: Tuple[CleanPatchHunk, ...] = ()
    conflict_clusters: Tuple[ConflictCluster, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflict_clusters)


@dataclass
class AdoptPatch(SerializableModel):
    """Unified patch & target container for both modifications and renames (static or templated)."""
    src_match: Optional[RenderSourceMatch] = None
    patch_from: Path = field(default_factory=Path)
    has_conflict: bool = False
    clean_hunks: Tuple[CleanPatchHunk, ...] = ()
    conflict_clusters: Tuple[ConflictCluster, ...] = ()
    display_diff: str = ""

    @property
    def is_templated(self) -> bool:
        return bool(self.src_match is not None and self.src_match.is_templated)

    @property
    def has_changes(self) -> bool:
        return bool(self.clean_hunks) or bool(self.conflict_clusters)
```

In `PackageAdoptPlan`, `mod_patches: Dict[str, AdoptPatch]` stores both modified files (keyed by `rel_mod.as_posix()`) and renamed files (keyed by `get_rename_patch_key(old_rel, new_rel)` using `"//->//"`), eliminating parallel dictionaries.

---

### 2.4 Generic Sequence Splicing & Internal Context-Bound Pipeline Records

To eliminate positional tuple indexing (`item[0]`, `raw[5]`) and duplicated gap-slicing loops across internal pipeline stages, Drift uses four specialized frozen dataclasses:

1. **`SpliceSpan[T]` and `splice_spans(seq, spans)` ([`src/drift/utils/func_utils.py`](../../src/drift/utils/func_utils.py))**:
   * Represents a 1D slice replacement `seq[start:end] -> replacement: Tuple[T, ...]`.
   * `span.overlaps_closed(other)` checks `max(self.start, other.start) <= min(self.end, other.end)` for closed-boundary collision detection.
   * `splice_spans(seq, spans)` slices unchanged gaps between sorted, non-overlapping spans (`prev_ends = (0, ...)`, `next_starts = (..., len(seq))`) and interleaves `(*gap, *span.replacement)` in a single $O(N)$ pass. Reused across `try_derender_single_line` (token splicing), `_apply_clean_spans_to_template` (Step 1 line splicing), and `splice_conflict_clusters_diff3` (Step 2 `diff3` splicing).
2. **`_TokenOpcode` and `_DpCell` ([`src/drift/adopt/adopt_fission.py`](../../src/drift/adopt/adopt_fission.py))**:
   * `_TokenOpcode` binds `line_a: TokenizedLine` and `line_b: TokenizedLine` directly onto each token diff opcode `(tag, a_start, a_end, b_start, b_end)`.
   * Exposes zero-argument `@property` projections (`a_tokens`, `b_tokens`, `a_kinds`, `b_kinds`, `is_equal`, `is_space_only_equal`, `has_non_space_tokens`, `signature_tokens`) and `merge_to(end_op)` so downstream `filter()` / `map()` / `reduce()` pipelines never thread `(line_a, line_b)` through closures.
3. **`_DerenderSpan` ([`src/drift/adopt/adopt_derender.py`](../../src/drift/adopt/adopt_derender.py))**:
   * Internal resolved span over `(t_start, t_end, r_start, r_end, h_start, h_end, is_conflict, replacement, old_eof_newline, new_eof_newline)`.
   * Provides `.to_clean_hunk(t_tokens) -> CleanPatchHunk` and `.to_splice_span() -> SpliceSpan[str]`.
4. **`_RawHunk` ([`src/drift/adopt/adopt_patch.py`](../../src/drift/adopt/adopt_patch.py))**:
   * Immutable reducer state for `parse_unified_patch_hunks` with `@property def is_count_valid(self) -> bool` and `.to_clean_hunk() -> CleanPatchHunk`.

---

## 3. Detailed Module Pipelines & Call Chains

### 3.1 Layer 1a-1: `src/drift/adopt/adopt_fission.py`

Organized bottom-up into 4 internal layers:
1. **Layer 1 — Lossless Word-Level Line Tokenization**:
   * `_classify_token(token: str) -> TokenKind`: Classifies regex matches into `SPACE`, `WORD` (`isalnum()` or `"_"`), or `PUNCT`.
   * `tokenize_line(line: str) -> TokenizedLine`: Separates `stripped_line` and `line_ending`, extracts `tokens` and `token_kinds`, and builds `word_set` via `itertools.compress`.
2. **Layer 2 — Slot-Normalized Skeleton Similarity & Monotonic 2D DP Line Matching**:
   * `_extract_token_opcodes(line_a, line_b) -> Tuple[_TokenOpcode, ...]`: Runs token-level `SequenceMatcher(..., autojunk=False)` and binds `(line_a, line_b)` to each `_TokenOpcode`.
   * `_coalesce_space_bridged_opcodes(opcodes) -> Tuple[_TokenOpcode, ...]`: Folds over `opcodes` via `reduce`, merging adjacent non-equal opcodes (`merge_one`) or non-equal opcodes bridged by a whitespace-only equal span (`merge_two`, `acc[-1].is_space_only_equal`) via `op.merge_to()`.
   * `_compute_skeleton_score(opcodes, both_lack_words) -> float`: Partitions `opcodes` into `(equal_ops, non_equal_ops)`, checks the structural gate (`n_word >= 1` or `n_punct >= 2`), and returns $E_{\text{skel}} / (E_{\text{skel}} + N_{\text{slots}})$.
   * `compute_skeleton_match(line_a, line_b) -> SkeletonMatch`: Performs the $O(1)$ `word_set.isdisjoint` prune, coalesces token opcodes, checks `score >= _SIMILARITY_THRESHOLD` (`0.5`), and builds `signature` from `op.signature_tokens`.
   * `_filter_unique_skeleton_matrix(raw_matrix) -> Tuple[Tuple[SkeletonMatch, ...], ...]`: Computes `row_sig_counts` via `map(_count_valid_signatures, raw_matrix)` and `col_sig_counts` on the transpose `map(_count_valid_signatures, zip(*raw_matrix))`, then transforms cells via nested `starmap` + `zip` with zero index loops.
   * `find_monotonic_line_matches(a_lines, b_lines) -> Tuple[Tuple[int, int], ...]`: Solves the 2D DP recurrence via nested `reduce(_dp_row, ...)` and `reduce(_dp_cell, ...)`.
3. **Layer 3 — Second-Pass Sentinel Hunk Fission**:
   * `_diff_sequences_to_units(a_seq, b_seq, expand_opcode) -> Tuple[DiffUnit, ...]`: Shared helper that runs `SequenceMatcher` and flattens expanded opcodes via `chain.from_iterable(starmap(...))`.
   * `_fission_replace_block` & `_fission_multiline_replace_block`: Handles $1 \leftrightarrow 1$ replace blocks directly via `compute_skeleton_match`, and decomposes $M \leftrightarrow N$ blocks by substituting matched lines with `"\x00__DRIFT_MATCH_k__\n"` sentinels and delegating to `_diff_sequences_to_units` + `_expand_sub_opcode_to_units`.
   * `fission_diff_units(src_tokens, dst_tokens, relation) -> Tuple[DiffUnit, ...]`: Top-level fission entry point for both `DiffRelation.TR` and `DiffRelation.RH`.
4. **Layer 4 — Single-Line (`1 <-> 1 <-> 1`) Closed-Interval Token Calculus**:
   * `try_derender_single_line(t_line, r_line, h_line) -> Optional[str]`: Projects `TR` variable slots and `RH` host edits from `_extract_token_opcodes` onto `r_line` as `SpliceSpan`s, checks `any(starmap(lambda h_edit, v_slot: h_edit.overlaps_closed(v_slot), product(host_edits, var_intervals)))`, and splices clean edits via `splice_spans`.
   * `refine_single_line_units(tr_units, rh_units, t_tokens, r_tokens, h_tokens) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]`: Indexes `SINGLE_EDIT` units on $R$ via `star_filter`, partitions results with `star_partition`, upgrades clean `RH` units to `SINGLE_SKEL_EDIT`, and marks colliding `TR` and `RH` units with `is_conflict=True`.

---

### 3.2 Layer 1a-2: `src/drift/adopt/adopt_derender.py`

Organized bottom-up into 4 internal layers:
1. **Layer 1 — Boundary-Aligned `RH_DELETE` Normalization**:
   * `split_rh_deletes_at_tr_boundaries(tr_units, rh_units) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]`: Uses `bisect_right` / `bisect_left` on positive-length `TR` start coordinates to split boundary-crossing `RH_DELETE` units, and marks strictly enclosed zero-length `TR_DELETE` units ($d_1 < r_1 < d_2$, **Case 2.5b**) with `is_conflict=True`.
2. **Layer 2 — $R$-Axis Orthogonal Classification (`Cases 0.1–4.2`)**:
   * `_classify_orthogonal_units(tr_units, rh_units) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]`: Partitions `tr_units` into `tr_pos` ($r_{\text{start}} < r_{\text{end}}$) and `tr_del` ($r_{\text{start}} = r_{\text{end}}$), evaluates each `rh_unit` via `bisect` range queries (`_eval_rh_insert_conflicts` for `RH_INSERT`, `_eval_rh_edit_or_delete_conflicts` for positive-length edits/deletes), and propagates conflict flags to both `TR` and `RH` units.
3. **Layer 3 — Conflict Envelope Closure & Clean Span Extraction**:
   * `_resolve_derender_spans(classified_tr, classified_rh, t_tokens, h_tokens) -> List[_DerenderSpan]`:
     * `_build_positive_conflict_spans`: Merges strictly overlapping positive conflict intervals (`cur_s < prev_e`) and expands each interval to a synchronized fixed point across `tr_pos` and `rh_pos` (`_expand_r_range_to_pos_units`).
     * `_build_zero_conflict_spans`: Groups standalone zero-length conflict points $d$ outside positive conflict interiors.
     * `_build_clean_edit_spans`: Extracts non-conflicting positive-length edits (including EOF-only newline drift on `RH_EQUAL`) and non-conflicting zero-length `RH_INSERT`s using linear `filter -> map -> zip -> star_filter -> starmap` stages.
4. **Layer 4 — Two-Step Derender Plan & Display Unified Diff Synthesis**:
   * `_apply_clean_spans_to_template(t_stripped_lines, clean_spans) -> Tuple[str, ...]`: Delegates to `splice_spans(t_stripped_lines, tuple(map(lambda s: s.to_splice_span(), clean_spans)))`.
   * `_anchor_conflict_clusters_to_t_clean(active_spans, t_tokens, r_tokens, h_tokens) -> Tuple[ConflictCluster, ...]`: Computes prefix-sum coordinate shifts via `itertools.accumulate(span_deltas, initial=0)`, filters conflicts with `star_filter`, and builds `ConflictCluster` records.
   * `build_two_step_derender_plan`, `build_display_unified_diff`, `read_rendered_baseline_lines`, and `generate_derendered_patch`: Assemble the final `TwoStepDerenderPlan` and `AdoptPatch`.

---

### 3.3 Layer 1b: `src/drift/adopt/adopt_patch.py`

Executes patch parsing, conflict probing, and Two-Step Patching entirely in pure Python:
* **`parse_unified_patch_hunks(patch_content: str) -> Tuple[CleanPatchHunk, ...]`**:
  Parses unified diff text into `CleanPatchHunk` records via a pure `reduce(_parse_patch_line, ...)` state machine over `_RawHunk`. Raises `SafeguardError` on malformed `@@` headers, unexpected diff body lines, or hunk line-count mismatches (`not raw.is_count_valid`).
* **`check_patch_conflicts(src_file: Path, patch_content: AdoptPatch) -> bool`**:
  Returns `True` immediately if `patch_content.conflict_clusters` is non-empty; otherwise verifies in memory (`_check_patch_hunks_conflict`) whether every `CleanPatchHunk` matches `src_file[t_start:t_end]` and its EOF newline state.
* **`splice_conflict_clusters_diff3(t_clean_lines, conflict_clusters) -> Tuple[str, ...]`**:
  Projects each `ConflictCluster` onto `SpliceSpan(start=c.t_clean_start, end=c.t_clean_end, replacement=_format_conflict_cluster_diff3(c))` and calls `splice_spans` in a single $O(L)$ pass.
* **`apply_source_patch(src_file: Path, patch_content: AdoptPatch, accept_conflicts: bool = False) -> bool`**:
  * **When `patch_content.conflict_clusters` is non-empty (3-Way Template Conflicts)**:
    1. **Fail-Fast Step 1 Precondition Guard**: Verifies `_check_patch_hunks_conflict(src_file, patch_content.clean_hunks)` is `False`; if `src_file` drifted from the planned baseline, raises `SafeguardError` immediately so Step 2 shifted coordinates `[t_clean_start, t_clean_end)` are never applied to a misaligned file.
    2. If `accept_conflicts=False`, returns `False` without modifying `src_file`.
    3. **Step 1 ($T \to T_{\text{clean}}$)**: Calls `_apply_step1_clean_hunks` to apply `patch_content.clean_hunks` in reverse hunk order and update EOF newline state.
    4. **Step 2 ($T_{\text{clean}} \to T_{\text{merged}}$)**: Calls `_apply_step2_conflict_clusters` $\to$ `splice_conflict_clusters_diff3` to write `<<<<<<<` / `|||||||` / `=======` / `>>>>>>>` conflict markers into `src_file`.
  * **When `patch_content.conflict_clusters` is empty (Clean Template or Static File)**:
    Applies `patch_content.clean_hunks` via `_apply_step1_clean_hunks` (injecting 2-way `<<<<<<<` / `=======` / `>>>>>>>` markers if a static hunk mismatches and `accept_conflicts=True`).

---

## 4. End-to-End Cross-Module Dataflow

1. **Planning Phase (`adopt_plan.py` $\to$ `adopt_derender.py` $\to$ `adopt_fission.py`)**:
   * `plan_package_adopt` discovers drifted files in `install/<pkg>/` and invokes `_precompute_package_drift_patches`.
   * For each modified file (`rel_mod.as_posix()`) and renamed file (`get_rename_patch_key(old_rel, new_rel)`):
     * **Templated source (`src_match.is_templated=True`)**: Calls `generate_derendered_patch`, which reads $(T, R, H)$, runs `build_two_step_derender_plan` + `build_display_unified_diff`, and returns a populated `AdoptPatch(src_match, patch_from, has_conflict, clean_hunks, conflict_clusters, display_diff)`.
     * **Static source (`src_match is None` or `is_templated=False`)**: Extracts raw unified diff via `generate_install_git_patch` / `generate_retargeted_patch`, parses `clean_hunks = parse_unified_patch_hunks(raw_diff)`, probes `has_conflict = check_patch_conflicts(target_file, temp_patch)`, and stores the resulting `AdoptPatch`.
   * All pre-computed patches are stored in `PackageAdoptPlan.mod_patches: Dict[str, AdoptPatch]`.
2. **Dry-Run & Interactive Preview Phase (`PackageAdoptPlan.format_text` & `adopt_handlers.py`)**:
   * Reads `patch.has_conflict`, `patch.src_match`, and `patch.display_diff` directly from `plan.mod_patches[key]` with zero disk I/O or subprocess calls.
3. **Execution Phase (`adopt_package.py` $\to$ `adopt_handlers.py` $\to$ `adopt_actions.py` $\to$ `adopt_patch.py`)**:
   * `execute_package_adopt` looks up each item's `AdoptPatch` in `plan.mod_patches` (raising `SafeguardError` if missing).
   * `patch_and_edit` invokes `apply_source_patch(target_file, patch, accept_conflicts=...)` to execute Step 1 ($T \to T_{\text{clean}}$) and Step 2 ($T_{\text{clean}} \to T_{\text{merged}}$) and syncs POSIX file permissions.
