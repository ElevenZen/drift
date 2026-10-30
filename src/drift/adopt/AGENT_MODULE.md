# `src/drift/adopt/` — Layer 2–3 Drift Adoption & 3-Way Template De-Rendering Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 2–3 (3-Way Template De-Rendering, Cross-Platform Patching, Planning & Single-Package Adoption Engine)**
* **Core Responsibility**: Discovers uncommitted host drifts in `install/<pkg>/`, partitions paths that have already converged with `render/<pkg>/`, translates host edits on rendered files (`H`) back into template-compatible unified patches against source templates (`T`) via 3-way hunk-map de-rendering (`T <-> R <-> H`), and executes interactive or automated single-package adoption into `src/<pkg>/`.
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../models/adopt_models.py`](../models/adopt_models.py), [`../config/`](../config/), and [`../hooks/`](../hooks/).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Layer 1a-1 — Word Tokenization, Hunk Fission & Single-Line Calculus**: [`adopt_fission.py`](adopt_fission.py)
2. **Layer 1a-2 — Orthogonal 3-Way Classification & Two-Step De-Rendering**: [`adopt_derender.py`](adopt_derender.py)
3. **Layer 1b — Unified Patch & Two-Step Conflict Engine**: [`adopt_patch.py`](adopt_patch.py)
4. **Layer 1c — Drift Discovery, Convergence & Planning**: [`adopt_plan.py`](adopt_plan.py)
5. **Layer 2 — Atomic Single-File Reconciliation Actions**: [`adopt_actions.py`](adopt_actions.py)
6. **Layer 3 — Single-Item Interactive & Non-Interactive Dispatchers**: [`adopt_handlers.py`](adopt_handlers.py)
7. **Layer 4 — Single-Package Adoption Execution & Status Derivation**: [`adopt_package.py`](adopt_package.py)

---

## 3. Module & Public API Catalog

### [`adopt_fission.py`](adopt_fission.py) — Layer 1a-1: Hunk Fission & Single-Line Token Calculus
* `tokenize_line(line) -> TokenizedLine`: Lossless Neovim-style 3-class Unicode tokenization (`TokenKind.WORD`, `TokenKind.PUNCT`, `TokenKind.SPACE`) with cached `word_set` for $O(1)$ disjointness pruning.
* `_coalesce_space_bridged_opcodes(raw_opcodes, line_a) -> List[_TokenOpcode]`: Coalesces non-`"equal"` `_TokenOpcode` spans bridged solely by whitespace so multi-word template expressions/values remain a single variable slot.
* `compute_skeleton_match(line_a, line_b) -> SkeletonMatch`: Evaluates the valid skeleton gate ($N_{\text{word}} \ge 1$ or $N_{\text{punct}} \ge 2$) and slot-normalized score $S = E_{\text{skel}} / (E_{\text{skel}} + N_{\text{slots}})$.
* `find_monotonic_line_matches(lines_a, lines_b) -> List[Tuple[int, int]]`: Applies bidirectional uniqueness filtering (loop-unrolling guard) and 2D DP maximum monotonic matching.
* `fission_diff_units(src_tokens, dst_tokens, relation) -> List[DiffUnit]`: Replaces matched line pairs with unique sentinels and runs a second-pass `SequenceMatcher` to split coarse `"replace"` blocks into `SINGLE_EDIT`, `BLOCK_EDIT`, `INSERT`, and `DELETE` `DiffUnit` instances.
* `try_derender_single_line(t_tok, r_tok, h_tok) -> Optional[str]`, `refine_single_line_units(tr_units, rh_units, t_tokens, r_tokens, h_tokens) -> List[DiffUnit]`: Evaluates closed token intervals $\max(p_1, c_1) \le \min(p_2, c_2)$ on $1 \leftrightarrow 1 \leftrightarrow 1$ units to distinguish Case 1.2 (`SINGLE_SKEL_EDIT`) from Case 1.3 (`SINGLE_EDIT` conflict).

### [`adopt_derender.py`](adopt_derender.py) — Layer 1a-2: Orthogonal 3-Way De-Rendering & Two-Step Plan
* `split_rh_deletes_at_tr_boundaries(tr_units, rh_units) -> Tuple[Tuple[DiffUnit, ...], Tuple[DiffUnit, ...]]`: Splits `RH_DELETE` units across positive-length `TR` boundaries (Case 2.1/2.2) and marks strictly enclosed `TR_DELETE` points ($d_1 < r_1 < d_2$, Case 2.5b) as conflicts.
* `_classify_orthogonal_units(tr_units, rh_units)`: Evaluates the $(T \to R) \times (R \to H)$ Orthogonal Classification Matrix (`0.1`–`4.2`, including Cases `3.1`–`3.5`).
* `_merge_positive_conflict_r_ranges`, `_build_positive_conflict_spans`, `_build_zero_conflict_spans`, `_build_clean_edit_spans`, `_resolve_derender_spans`: Partitions `TR` and `RH` units into positive/zero $R$-length sequences via `partition`, merges positive conflict $R$-intervals, groups exterior conflicting zero-length units at each $R[d]$ into standalone zero-length conflict envelopes, and resolves all `_DerenderSpan`s via `bisect`.
* `_apply_clean_spans_to_template`, `_anchor_conflict_clusters_to_t_clean`, `build_two_step_derender_plan(t_lines, r_lines, h_lines) -> TwoStepDerenderPlan`: Pure 3-way de-rendering pipeline synthesizing `t_clean_lines`, `clean_hunks: Tuple[CleanPatchHunk, ...]`, and `conflict_clusters` anchored to shifted $[t_{\text{clean\_start}}, t_{\text{clean\_end}})$ coordinates via `itertools.accumulate`.
* `build_display_unified_diff(plan, fromfile, tofile) -> str`: Splices Step 2 `diff3` conflict blocks onto `t_clean_lines` via `splice_conflict_clusters_diff3` to form `t_merged_lines` and runs standard `difflib.unified_diff(t_lines, t_merged_lines)`.
* `read_rendered_baseline_lines(render_pkg_dir, rel_rendered_path) -> List[str]`: Reads `R` from `render/<pkg>/<rel_path>`, or raises `SafeguardError` advising `drift adopt --with-render` when missing.
* `generate_derendered_patch(target_file, patch_from, render_pkg_dir, rel_rendered_path, src_rel_path, src_match=None) -> AdoptPatch`: Returns the structured `AdoptPatch` for a templated modification or rename.

### [`adopt_patch.py`](adopt_patch.py) — Layer 1b: Unified Patch & Two-Step Conflict Engine
* `generate_install_git_patch(install_base, pkg_rel_path, old_rel_path=None) -> str`: Runs `git -C <install_base> diff HEAD -- <paths>`.
* `generate_source_to_install_patch(src_file, install_file) -> str`: Runs `git diff --no-index -- <src_file> <install_file>` when an untracked addition in `install/` (not in `install/` `HEAD`) is promoted to a static file modification.
* `parse_unified_patch_hunks(patch_content) -> Tuple[CleanPatchHunk, ...]`, `_check_hunk_matches(src_lines, hunk, src_has_eof_nl=True) -> bool`, `_check_patch_hunks_conflict(src_file, clean_hunks) -> bool`: Purely parses unified diff text at the planning stage into structured 0-based `CleanPatchHunk` instances (with newline-stripped `old_lines`/`new_lines` and `old_eof_newline`/`new_eof_newline` flags, raising `ValueError` on malformed diff text) and probes for conflicts in pure Python without external `patch` or `git merge-file` subprocesses.
* `check_patch_conflicts(src_file, patch_content: AdoptPatch) -> bool`: Returns `True` if `patch_content.conflict_clusters` is non-empty or if `patch_content.clean_hunks` has any mismatched hunk against `src_file`.
* `_format_hunk_2way_conflict`, `_apply_hunk_to_lines`, `_apply_step1_clean_hunks`: Applies Step 1 `CleanPatchHunk`s in reverse order directly in memory, writing standard 2-way conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) when `accept_conflicts=True` on static files (`not patch_content.conflict_clusters`).
* `_format_conflict_cluster_diff3`, `splice_conflict_clusters_diff3(t_clean_lines, conflict_clusters)`, `_apply_step2_conflict_clusters(src_file, conflict_clusters)`: Purely splices Step 2 newline-stripped `diff3` conflict markers (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`) onto `t_clean_lines` at $[t_{\text{clean\_start}}, t_{\text{clean\_end}})$ in a single $O(N)$ pass, shared between `apply_source_patch` and `build_display_unified_diff`.
* `apply_source_patch(src_file, patch_content: AdoptPatch, accept_conflicts=False) -> bool`: Executes Two-Step Patching in pure Python (`Step 1` $\to T_{\text{clean}}$, followed by `Step 2` `ConflictCluster` `diff3` injection when `accept_conflicts=True`), raising `SafeguardError` if Step 1 conflicts while `patch_content.conflict_clusters` is non-empty.

### [`adopt_plan.py`](adopt_plan.py) — Layer 1c: Drift Discovery, Category Routing & Planning
* `get_drifted_packages(workspace_config) -> List[str]`, `_resolve_package_source_context(...)`, `_temporary_stage_package_index(...)`, `_normalize_keep_file_drifts(...)`, `_collect_package_git_status(...)`: Stage 1 raw Git status collection and `.drift_keep` normalization.
* `find_source_path_conflict(render_engines, src_dir_to_render, rel_path) -> Optional[RenderSourceMatch]`: Delegates to `render_engines.find_conflict_in_source_dir`, returning any `RenderSourceMatch` (`status == "match"` for leaf file/template collisions or `status == "block"` for blocking directories, intermediate files/templates, or broken symlinks).
* `resolve_source_file_path(render_engines, src_dir_to_render, rel_path) -> Optional[RenderSourceMatch]`: Resolves rendered filenames back to source files/templates via `find_conflict_in_source_dir`, returning the `RenderSourceMatch` strictly when `match.status == "match"`.
* `check_is_drifted_path_converged(install_pkg_dir, render_pkg_dir, rel_path, expect_src_exists=False) -> bool`, `_is_path_converged_with_render(...)`: Detects whether `install/<pkg>/<rel>` already matches `render/<pkg>/<rel>`.
* `_route_package_drifts(install_pkg_dir, render_pkg_dir, src_dir_to_render, render_engines, diff) -> PackageDriftFiles`: Stage 2 category router that routes normalized `GitStatusChanges` into `converged_files`, `additions`, `deletions`, `modifications`, and `renames` (keeping only renames where `old` exists in `src/` and `new` is absent in `src/`, decomposing all other renames, promoting additions with existing `src/` matches to modifications, and promoting modifications without `src/` matches to additions).
* `assert_source_file_clean(src_file, pkg, force=False) -> None`, `_assert_deletion_source_clean(...)`: Enforces the Git cleanliness safeguard on target source files and directories during Stage 3 (`plan_package_adopt`), raising `DirtySourceFileError` if any target source has uncommitted modifications and `force=False`.
* `_build_static_adopt_patch`, `_precompute_modification_patch`, `_precompute_rename_patch`, `_precompute_package_drift_patches`, `plan_package_adopt(workspace_config, pkg, skip_patches=False, pre_source_already_triggered=False, force=False) -> PackageAdoptPlan`: Stage 3 patch pre-computation and 3-stage planning entry point.

### [`adopt_actions.py`](adopt_actions.py) — Layer 2: Atomic Single-File Reconciliation Actions
* `_sync_file_mode(target_file, patch_from)`, `ensure_empty_keep_file(keep_file)`, `adopt_addition(pkg_dir, install_pkg_dir, rel_path)`, `ignore_addition(pkg_dir, install_pkg_dir, rel_path)`, `adopt_deletion(render_engines, src_dir_to_render, rel_path)`.
* `patch_and_edit(target_file, patch_content: AdoptPatch, accept_conflicts=False, open_editor=False) -> bool`.
* `adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, patch_content: AdoptPatch, accept_conflicts=False) -> Optional[Path]`.
* `fallback_over_render(src_file, static_file)`, `fallback_side_by_side(src_file, install_file) -> bool`.

### [`adopt_handlers.py`](adopt_handlers.py) — Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
* `handle_single_addition(...) -> ItemAdoptOutcome`, `handle_single_deletion(...) -> ItemAdoptOutcome`.
* `_prompt_rename_clean_interactive(...)`, `_prompt_rename_conflict_interactive(...)`, `handle_rename_non_interactive(...)`, `handle_rename_interactive(...)`, `handle_single_rename(...) -> ItemAdoptOutcome`.
* `_prompt_templated_modification_clean_interactive(...)`, `_prompt_static_modification_clean_interactive(...)`, `_prompt_modification_conflict_interactive(...)`, `handle_modification_non_interactive(...)`, `handle_modification_interactive(...)`, `handle_single_modification(...) -> ItemAdoptOutcome`.

### [`adopt_package.py`](adopt_package.py) — Layer 4: Single-Package Adoption Execution
* `_derive_package_adopt_status(...) -> AdoptStatus`, `_derive_adopt_result_status(...) -> AdoptStatus`.
* `stage_converged_package_drifts(workspace_config, plan) -> List[Path]`, `_dry_run_package_adopt(pkg_plan) -> PackageAdoptResult`.
* `execute_package_adopt(workspace_config, plan, options=None) -> PackageAdoptResult`: Reconciles divergent drifts into `src/<pkg>/` using pre-computed `AdoptPatch` entries in `plan.mod_patches` (raising `SafeguardError` if any modification or rename entry is missing) and stages resolved paths in `install/<pkg>/` without committing.
* `adopt_one_package_drifts(workspace_config, pkg, options=None) -> PackageAdoptResult`.

---

## 4. Local Invariants & Modification Rules
1. **Pure-Python Cross-Platform Patch Uniformity**: `adopt_patch.py` parses and applies unified diffs directly in memory without external `patch` or `git merge-file` subprocesses. Step 1 static-file conflicts write 2-way conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`), while Step 2 template `ConflictCluster`s write 3-way `diff3` markers (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`).
2. **Two-Step De-Rendering Isolation ($T \to T_{\text{clean}} \to T_{\text{merged}}$) & Step 1 Conflict Guard**: Clean operations (`0.2, 1.2, 2.3, 2.4, 3.1`) must be applied in Step 1 (`clean_patch`) to produce $T_{\text{clean}}$ before Step 2 injects `ConflictCluster` `diff3` blocks at $[t_{\text{clean\_start}}, t_{\text{clean\_end}})$. If `patch_content.conflict_clusters` is non-empty and Step 1 conflicts against `src_file`, `apply_source_patch` raises `SafeguardError`.
3. **Stage-Only Boundary in Layer 4**: `execute_package_adopt` and `adopt_one_package_drifts` must only stage resolved files (`git add -- <paths>`) in `install/`; committing staged changes belongs strictly to Layer 5 (`primitives/adopt_repo.py`).

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/template_adopt_derendering_draft.md`](../../../docs/features/template_adopt_derendering_draft.md), [`docs/features/template_adopt_architecture_plan.md`](../../../docs/features/template_adopt_architecture_plan.md), [`docs/features/drift_adoption_and_fcd.md`](../../../docs/features/drift_adoption_and_fcd.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_adopt.py
  ```
