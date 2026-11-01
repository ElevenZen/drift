---
name: functional-pipeline-refactoring
description: >-
  Use this skill when writing, auditing, or refactoring multi-stage data pipelines,
  sequence/interval algorithms, 2D matrix transformations, parsers, or diff/patch
  transformations in Drift. Provides concrete before/after functional refactoring
  patterns (filter/star_filter -> map -> zip -> starmap, SpliceSpan + splice_spans
  single-pass gap-interleaving, itertools.accumulate coordinate shifting,
  positive/zero interval partitioning with bisect, context-bound frozen dataclass
  pipeline records, 2D matrix transpose pipelines, and odd-arity choose/branch matching).
---

# Functional Pipeline & Sequence Algorithm Refactoring Guide

When implementing or auditing multi-stage transformations in Drift (such as `src/drift/adopt/`, `src/drift/render/`, or `src/drift/primitives/`), apply the following 8 patterns to eliminate nested comprehensions, positional tuple indexing, index loops, ad-hoc string boundary munging, and redundant passes.

---

## Pattern 1: Decompose Nested Comprehensions into Linear `filter/star_filter -> map -> zip -> starmap` Stages

Avoid multi-clause comprehensions that bind intermediate variables via `for y in [expr]` or interleave filtering and projection in a single expression. Break them into named, single-purpose functional stages, and always use the **tuple-unpacking triad (`star_filter`, `star_partition`, `starmap`)** when operating on `enumerate(...)` or `zip(...)` instead of indexing `item[0]` / `item[1]`.

### Before (Anti-Pattern: Nested Multi-Clause Comprehension & Positional Tuple Indexing)
```python
pos_clean_spans = [
    _DerenderSpan(
        t_start=t_s,
        t_end=t_e,
        r_start=e.r_start,
        r_end=e.r_end,
        h_start=e.dst_start,
        h_end=e.dst_end,
        is_conflict=False,
        replacement=repl,
    )
    for e in rh_pos
    if (e.unit_type != DiffUnitType.EQUAL or e.dst_end == len(h_tokens))
    and bisect_right(pos_conf_ends, e.r_start) == bisect_left(pos_conf_starts, e.r_end)
    for tr_sub in [tr_pos[bisect_right(tr_pos_ends, e.r_start) : bisect_left(tr_pos_starts, e.r_end)]]
    if tr_sub
    for t_s, t_e in [(tr_sub[0].src_start, tr_sub[-1].src_end)]
    for repl in [_extract_clean_pos_replacement(e, t_s, t_e, t_tokens, h_tokens)]
    if repl is not None
]
```

### After (Canonical: Linear `filter -> map -> zip -> star_filter -> starmap`)
```python
candidate_pos_rh = tuple(filter(
    lambda e: (
        (e.unit_type != DiffUnitType.EQUAL or e.dst_end == len(h_tokens))
        and bisect_right(pos_conf_ends, e.r_start) == bisect_left(pos_conf_starts, e.r_end)
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
valid_pos_clean = star_filter(
    lambda _e, _t_rng, repl: repl is not None,
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
    ),
    valid_pos_clean,
))
```

---

## Pattern 2: Single-Pass $O(N)$ Gap-Interleaving via `SpliceSpan` and `splice_spans`

When applying sorted, non-overlapping slice replacements `[start, end)` onto a sequence `seq` (e.g., sub-line token splicing, applying clean template spans, or splicing `diff3` conflict clusters), do **not** mutate a list in reverse, copy the sequence on each step of a `reduce()`, or re-implement gap slicing inline. Project domain spans onto `SpliceSpan(start, end, replacement)` (from `drift.utils.func_utils`) and call `splice_spans(seq, spans)`.

- Use `span.overlaps_closed(other)` (`max(a.start, b.start) <= min(a.end, b.end)`) when checking whether two spans touch or overlap on closed boundaries (including zero-width insertions).
- Add a `.to_splice_span()` conversion method on domain dataclasses (such as `_DerenderSpan` or `_TokenOpcode`) when projecting onto `SpliceSpan`.

### Canonical Usage
```python
from ..utils.func_utils import SpliceSpan, splice_spans

# 1. Project domain records to sorted, non-overlapping SpliceSpans
combined_spans = sorted(
    (*template_var_spans, *host_edit_spans),
    key=lambda span: (span.start, span.end),
)

# 2. Splice in a single O(N) gap-interleaving pass
derendered_tokens = splice_spans(r_line.tokens, combined_spans)
```

---

## Pattern 3: Prefix-Sum Coordinate Shifting via `itertools.accumulate`

When Step 1 applies subset replacements (changing sequence lengths by $\Delta_i = |\text{repl}_i| - (\text{end}_i - \text{start}_i)$) and Step 2 needs shifted coordinates $[\text{start}_j + \text{shift}_j, \text{end}_j + \text{shift}_j)$ in the updated sequence, compute cumulative shifts declaratively with `itertools.accumulate(..., initial=0)` and filter with `star_filter`.

### Canonical Template
```python
span_deltas = map(
    lambda s: choose(
        s.is_conflict,
        0,
        len(s.replacement) - (s.t_end - s.t_start),
    ),
    active_spans,
)
cumulative_shifts = accumulate(span_deltas, initial=0)
conflict_spans_with_shifts = star_filter(
    lambda span, _shift: span.is_conflict,
    zip(active_spans, cumulative_shifts),
)
conflict_clusters = tuple(starmap(
    lambda span, shift: ConflictCluster(
        t_start=span.t_start,
        t_end=span.t_end,
        t_clean_start=span.t_start + shift,
        t_clean_end=span.t_end + shift,
        ...,
    ),
    conflict_spans_with_shifts,
))
```

---

## Pattern 4: Partition Positive-Length Intervals from Zero-Length Point Operations (`partition` + `bisect`)

When projecting or merging intervals along a shared 1D coordinate axis (such as `R`):
1. **Partition First**: Split positive-length intervals (`start < end`) from zero-length point operations (`start == end`) using `partition()` (or `star_partition()` for paired items).
2. **Positive-Length Stream (`pos`)**: Forms a strictly monotonic sequence of intervals supporting $O(\log N)$ `bisect_left` / `bisect_right` range queries:
   - **Overlapping slice for `[q_start, q_end)`**: `pos[bisect_right(pos_ends, q_start) : bisect_left(pos_starts, q_end)]`.
   - **Strict disjointness check**: `bisect_right(conf_ends, q_start) == bisect_left(conf_starts, q_end)`.
   - **Strict-overlap interval merging**: Merge two positive intervals only when `cur_start < prev_end` (keep touching intervals `cur_start == prev_end` split when the boundary is a valid synchronization point).
3. **Zero-Length Stream (`zero`)**: Query or group point operations by their exact point coordinate `d` (`bisect_left(zero_pts, d) : bisect_right(zero_pts, d)`), checking strict interior enclosure via `bisect_right(conf_ends, d) < bisect_left(conf_starts, d)`.

---

## Pattern 5: Context-Bound Frozen Dataclasses with Semantic `@property` Methods

Never pass multi-element positional tuples (`raw[0]`, `raw[5]`, `pair[1][0]`) across internal pipeline stages, and avoid threading parent context objects repeatedly through helper closures.
1. Define a `@dataclass(frozen=True)` (e.g., `_RawHunk`, `_DerenderSpan`, `_TokenOpcode`).
2. **Bind Parent Context When Representing Slices**: When a pipeline record represents a slice of 1–2 parent objects (e.g., `_TokenOpcode` over `line_a: TokenizedLine` and `line_b: TokenizedLine`), store references to those parent objects on the dataclass so slice projections (`op.a_tokens`, `op.a_kinds`) and predicates (`op.is_space_only_equal`, `op.has_non_space_tokens`, `op.signature_tokens`) become zero-argument `@property` methods.
3. Attach stage-transition conversions as methods on the source dataclass (e.g., `span.to_clean_hunk(t_tokens)`, `span.to_splice_span()`, `op.to_a_splice_span()`).

---

## Pattern 6: Normalize Boundary Metadata at Ingestion (`keepends=False` + Explicit Flags)

Do not mix `keepends=True` (`"line\n"`) and `keepends=False` (`"line"`) across sibling models or pipeline stages, which forces callers to sprinkle `.rstrip("\r\n")` and custom `line_ending` parameters.
1. At tokenization/ingestion (`tokenize_line`, `parse_unified_patch_hunks`), strip trailing newlines immediately (`stripped_line: str`) and record boundary metadata as explicit boolean flags (`has_newline: bool`, `old_eof_newline: bool`, `new_eof_newline: bool`).
2. Keep all downstream line tuples (`CleanPatchHunk`, `ConflictCluster`, `TwoStepDerenderPlan`, `_DerenderSpan`) uniformly newline-stripped (`Tuple[str, ...]`).
3. Reconstruct trailing newlines only at the final serialization boundary (`_join_lines_with_eof_newline` or `difflib.unified_diff(..., lineterm="")`).

---

## Pattern 7: 2D Matrix Pipelines Without Index Loops (`map` + `zip(*matrix)` + `starmap`)

When computing row/column aggregations and elementwise transformations over a 2D matrix `Sequence[Sequence[T]]` (such as bidirectional signature uniqueness in `_filter_unique_skeleton_matrix`):
1. Compute row summaries via `row_counts = tuple(map(agg_fn, matrix))`.
2. Compute column summaries on the matrix transpose via `col_counts = tuple(map(agg_fn, zip(*matrix)))`.
3. Transform cells declaratively without `for i in range(m): for j in range(n):` index loops by nesting `starmap` over `zip(matrix, row_counts)` (outer) and `zip(row, col_counts)` (inner):

```python
row_sig_counts = tuple(map(_count_valid_signatures, raw_matrix))
col_sig_counts = tuple(map(_count_valid_signatures, zip(*raw_matrix)))
filtered_matrix = tuple(
    starmap(
        lambda row, row_counts: tuple(
            starmap(
                lambda match, col_counts: _nullify_ambiguous_match(
                    match, row_counts, col_counts
                ),
                zip(row, col_sig_counts),
            )
        ),
        zip(raw_matrix, row_sig_counts),
    )
)
```

---

## Pattern 8: Odd-Arity `choose()` and `branch()` `when-then-...-else` Matching

Never nest `choose(p1, v1, choose(p2, v2, default))` or `branch(p1, fn1, lambda: branch(p2, fn2, default_fn))`. Both `choose()` and `branch()` accept any odd number of arguments ($2k + 1 \ge 3$) and evaluate `(pred_k, branch_k)` pairs left-to-right like a `when-then-...-else` matching machine:

```python
# Eager value selection (when all candidate values are cheap and safe to evaluate):
unit_type = choose(
    sub_tag == "equal", DiffUnitType.SINGLE_EDIT,
    sub_tag == "delete", DiffUnitType.DELETE,
    sub_tag == "insert", DiffUnitType.INSERT,
    (si2 - si1 == 1) and (sj2 - sj1 == 1), DiffUnitType.SINGLE_EDIT,
    DiffUnitType.BLOCK_EDIT,
)

# Lazy branch execution (when branches compute slices, recurse, or have side effects):
return branch(
    tag == "equal",
    lambda: tuple(map(_make_equal_unit, range(i2 - i1))),
    tag == "delete",
    lambda: (DiffUnit(relation, DiffUnitType.DELETE, i1, i2, j1, j2),),
    tag == "insert",
    lambda: (DiffUnit(relation, DiffUnitType.INSERT, i1, i2, j1, j2),),
    lambda: _fission_replace_block(src_tokens, dst_tokens, i1, i2, j1, j2, relation),
)
```

---

## Refactoring Audit Checklist

Before finalizing any pipeline module, verify:
- [ ] Are there any multi-clause comprehensions (`for ... for ... if ...`) that should be decomposed into `filter/star_filter -> map -> zip -> starmap`?
- [ ] Are any `filter()` or `partition()` calls indexing tuple elements (`pair[0]`, `item[2]`) instead of unpacking via `star_filter()` or `star_partition()`?
- [ ] Are disjoint sequence slice replacements using `SpliceSpan` + `splice_spans` rather than manual gap-slicing or list mutation?
- [ ] Are any multi-branch conditionals nesting `choose(..., choose(...))` instead of using flat odd-arity `choose(p1, v1, p2, v2, ..., default)` or `branch(...)`?
- [ ] Are any intermediate records represented as positional tuples with index lookups (`item[0]`, `item[3]`) instead of a context-bound `@dataclass(frozen=True)` with semantic `@property` methods?
- [ ] Do all pure internal pipeline functions return immutable `Tuple[...]` instead of `List[...]`?
- [ ] Are string/path representations normalized once at the ingestion boundary so internal helpers never call `.rstrip("\r\n")` defensively?
- [ ] Have all `AGENT_MODULE.md` and `docs/internals/*.md` files been synchronized with any signature or model changes?
