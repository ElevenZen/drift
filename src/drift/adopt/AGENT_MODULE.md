# `src/drift/adopt/` — Layer 2–3 Drift Adoption & 3-Way Template De-Rendering Module Map

## 1. Subpackage Purpose & Architectural Boundary
* **Architectural Layer**: **Layer 2–3 (3-Way Template De-Rendering, Cross-Platform Patching, Planning & Single-Package Adoption Engine)**
* **Core Responsibility**: Discovers uncommitted host drifts in `install/<pkg>/`, partitions paths that have already converged with `render/<pkg>/`, translates host edits on rendered files (`H`) back into template-compatible unified patches against source templates (`T`) via 3-way hunk-map de-rendering (`T <-> R <-> H`), and executes interactive or automated single-package adoption into `src/<pkg>/`.
* **Dependency Rules**:
  * **Allowed Imports**: [`../utils/`](../utils/), [`../core/`](../core/), [`../models/adopt_models.py`](../models/adopt_models.py), [`../config/`](../config/), and [`../hooks/`](../hooks/).
  * **Forbidden Imports**: Must **never** import from `primitives/` or `cli/`.

---

## 2. Internal Dependency Order (Bottom-Up)
1. **Layer 1a — 3-Way Template De-Rendering**: [`adopt_derender.py`](adopt_derender.py)
2. **Layer 1b — Unified Patch & Conflict Engine**: [`adopt_patch.py`](adopt_patch.py)
3. **Layer 1c — Drift Discovery, Convergence & Planning**: [`adopt_plan.py`](adopt_plan.py)
4. **Layer 2 — Atomic Single-File Reconciliation Actions**: [`adopt_actions.py`](adopt_actions.py)
5. **Layer 3 — Single-Item Interactive & Non-Interactive Dispatchers**: [`adopt_handlers.py`](adopt_handlers.py)
6. **Layer 4 — Single-Package Adoption Execution & Status Derivation**: [`adopt_package.py`](adopt_package.py)

---

## 3. Module & Public API Catalog

### [`adopt_derender.py`](adopt_derender.py) — Layer 1a: 3-Way Hunk-Map Template De-Rendering
* `build_template_hunk_map(t_lines, r_lines) -> List[TemplateHunk]`: Extracts non-`"equal"` opcodes between Template (`T`) and Rendered baseline (`R`).
* `_classify_template_hunk(hunk, r_h_opcodes) -> Tuple[TemplateHunkClassification, Optional[int]]`: Classifies each `TemplateHunk` against `R <-> H` opcodes into Case 1 (`TemplateHunkClassification.EQUAL`), Case 2 (`TemplateHunkClassification.EDIT`), or Case 3 (`TemplateHunkClassification.CONFLICT`).
* `_apply_subline_containment(h_block, edit_hunks, t_lines, r_lines) -> List[str]`: Handles Case 2a sub-line containment (where the rendered value still appears uniquely inside the host edit block, e.g. adding a trailing comma) vs Case 2b full replacement.
* `_format_conflict_base_lines(t_slice, r_slice) -> List[str]`: Formats Case 3 conflict baselines so `len(r_derendered) == len(t_lines)` and subsequent hunks maintain 1-to-1 line alignment with `T`.
* `derender_host_lines(t_lines, r_lines, h_lines) -> DerenderedPair`: Pure 3-way de-rendering function returning `DerenderedPair(r_derendered, h_derendered, r_conflict_indices, h_conflict_indices)`.
* `_build_anchored_unified_diff(pair, fromfile, tofile, n=3) -> str`: Builds a unified diff where Case 3 conflict spans never match as equal context lines, anchoring every diff hunk strictly on the outer template lines of `T`.
* `read_rendered_baseline_lines(render_pkg_dir, rel_rendered_path) -> List[str]`: Reads `R` from `render/<pkg>/<rel_path>`, or raises `SafeguardError` advising `drift adopt --with-render` when missing.
* `generate_derendered_patch(target_file, patch_from, render_pkg_dir, rel_rendered_path, src_rel_path) -> str`: Synthesizes a unified diff (`R_derendered -> H_derendered`) targeting `src_rel_path`.

### [`adopt_patch.py`](adopt_patch.py) — Layer 1b: Unified Patch & Cross-Platform Conflict Engine
* `check_patch_has_hunks(patch_content) -> bool`: Returns `True` if the diff contains `'@@'` hunk headers.
* `generate_install_git_patch(install_base, pkg_rel_path, old_rel_path=None) -> str`: Runs `git -C <install_base> diff HEAD -- <paths>`.
* `rewrite_patch_target_headers(patch_content, patch_target_path) -> str`: Pure helper that strips pre-hunk Git metadata and rewrites `--- a/` and `+++ b/` headers to target `patch_target_path`.
* `generate_retargeted_patch(install_base, pkg, new_rel_path, old_rel_path, patch_target_path) -> str`: Composes `generate_install_git_patch` with `rewrite_patch_target_headers` to target `patch_target_path`.
* `check_patch_conflicts(src_file, patch_content) -> bool`: Runs standard POSIX `patch --dry-run --no-backup-if-mismatch <src_file>`.
* `_parse_unified_patch_hunks(patch_content)`, `_apply_hunk_slice(lines, hunk, use_new)`, `_apply_patch_with_git_merge_file(src_file, patch_content) -> bool`: Synthesizes 3-way merge inputs (`current`, `base`, `other`) in a temporary directory and runs `git merge-file --diff3 -L "" -L "" -L ""` for platform-independent 3-way conflict markers (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`).
* `apply_source_patch(src_file, patch_content, accept_conflicts=False) -> bool`: Uses POSIX `patch --no-backup-if-mismatch` when `accept_conflicts=False` and delegates unconditionally to `_apply_patch_with_git_merge_file` when `accept_conflicts=True`.

### [`adopt_plan.py`](adopt_plan.py) — Layer 1c: Drift Discovery, Convergence & Planning
* `get_drifted_packages(workspace_config) -> List[str]`: Identifies drifted packages in `install/` that exist in both `install/` and `src/`.
* `resolve_source_file_path(render_engines, src_dir_to_render, rel_path) -> Optional[RenderSourceMatch]`: Resolves rendered filenames back to source files/templates via `RenderEngineRegistry`, returning `RenderSourceMatch` (with `.path` and `.is_templated`).
* `check_is_drifted_path_converged(install_pkg_dir, render_pkg_dir, rel_path, expect_src_exists=False) -> bool`, `_is_path_converged_with_render(...)`: Detects whether `install/<pkg>/<rel>` already matches `render/<pkg>/<rel>`.
* `assert_source_file_clean(src_file, pkg, force=False) -> None`: Raises `DirtySourceFileError` if `src_file` has uncommitted Git modifications and `force=False`.
* `_normalize_keep_file_drifts`, `_partition_converged_package_drifts`, `_precompute_modification_patch`, `_precompute_rename_patch`, `_precompute_package_drift_patches`, `_precompute_modification_conflicts`, `plan_package_adopt(workspace_config, pkg, skip_patches=False, pre_source_already_triggered=False) -> PackageAdoptPlan`.

### [`adopt_actions.py`](adopt_actions.py) — Layer 2: Atomic Single-File Reconciliation Actions
* `_sync_file_mode(target_file, patch_from)`, `ensure_empty_keep_file(keep_file)`, `adopt_addition(pkg_dir, install_pkg_dir, rel_path)`, `ignore_addition(pkg_dir, install_pkg_dir, rel_path)`, `adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg)`.
* `patch_and_edit(target_file, patch_content, patch_from, accept_conflicts=False, open_editor=False) -> bool`.
* `adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, patch_content, patch_from, has_patch_conflict=False, accept_conflicts=False) -> Optional[Path]`.
* `fallback_over_render(src_file, static_file)`, `fallback_side_by_side(src_file, install_file) -> bool`.

### [`adopt_handlers.py`](adopt_handlers.py) — Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
* `handle_single_addition(...) -> ItemAdoptOutcome`, `handle_single_deletion(...) -> ItemAdoptOutcome`.
* `handle_rename_non_interactive(...)`, `handle_rename_interactive(...)`, `handle_single_rename(...) -> ItemAdoptOutcome`.
* `handle_modification_non_interactive(...)`, `handle_modification_interactive(...)`, `handle_single_modification(...) -> ItemAdoptOutcome`.

### [`adopt_package.py`](adopt_package.py) — Layer 4: Single-Package Adoption Execution
* `_derive_package_adopt_status(...) -> AdoptStatus`, `_derive_adopt_result_status(...) -> AdoptStatus`.
* `stage_converged_package_drifts(workspace_config, plan) -> List[Path]`, `_dry_run_package_adopt(pkg_plan) -> PackageAdoptResult`.
* `execute_package_adopt(workspace_config, plan, options=None) -> PackageAdoptResult`: Reconciles divergent drifts into `src/<pkg>/` and stages resolved paths in `install/<pkg>/` without committing.
* `adopt_one_package_drifts(workspace_config, pkg, options=None) -> PackageAdoptResult`.

---

## 4. Local Invariants & Modification Rules
1. **Cross-Platform Patch Uniformity**: Never pass GNU-only `--merge` to `patch`. Always use POSIX `patch --no-backup-if-mismatch` when `accept_conflicts=False` and `_apply_patch_with_git_merge_file` (`git merge-file -L "" -L "" -L ""`) when `accept_conflicts=True`.
2. **3-Way De-Rendering Alignment**: In `derender_host_lines`, `R_derendered` must maintain 1-to-1 line alignment with `T` across Case 1, Case 2, and Case 3 hunks so unified diffs apply at exact template line numbers.
3. **Stage-Only Boundary in Layer 4**: `execute_package_adopt` and `adopt_one_package_drifts` must only stage resolved files (`git add -- <paths>`) in `install/`; committing staged changes belongs strictly to Layer 5 (`primitives/adopt_repo.py`).

---

## 5. Related Feature Docs & Targeted Tests
* **Feature Specifications**: [`docs/features/template_adopt_derendering.md`](../../../docs/features/template_adopt_derendering.md), [`docs/features/drift_adoption_and_fcd.md`](../../../docs/features/drift_adoption_and_fcd.md)
* **Targeted Unit Tests**:
  ```bash
  PYTHONPATH=src python3 -m unittest tests/test_adopt.py
  ```
