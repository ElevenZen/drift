"""Drift Adoption Engine (`drift.adopt`): 3-Way Template De-Rendering, Patching, Planning, and Execution.

Submodules (ordered bottom-up by dependency layer):
- `adopt_fission` (Layer 1a-1): Word tokenization, skeleton similarity, bidirectional uniqueness
  filtering, 2D DP matching, sentinel fission, and `1 <-> 1 <-> 1` closed token interval calculus.
- `adopt_derender` (Layer 1a-2): `RH_DELETE` boundary splitting, R-axis orthogonal classification
  (`0.1`-`4.2`), synchronized block conflict envelope closure, `TwoStepDerenderPlan`, and `AdoptPatch`.
- `adopt_patch` (Layer 1b): Unified diff generation, header adjustment, conflict probing, and
  two-step cross-platform patch application (`patch` + `ConflictCluster` `diff3` injection).
- `adopt_plan` (Layer 1c): Drift discovery, `.drift_keep` normalization, convergence partitioning,
  source resolution, cleanliness guard, and `plan_package_adopt`.
- `adopt_actions` (Layer 2): Atomic single-file reconciliation actions (`adopt_addition`,
  `ignore_addition`, `adopt_deletion`, `patch_and_edit`, `adopt_rename`, fallbacks).
- `adopt_handlers` (Layer 3): Interactive and non-interactive single-item dispatchers.
- `adopt_package` (Layer 4): Single-package adoption execution and status derivation.
"""

from .adopt_actions import (
    adopt_addition,
    adopt_deletion,
    adopt_rename,
    ensure_empty_keep_file,
    fallback_over_render,
    fallback_side_by_side,
    ignore_addition,
    patch_and_edit,
)
from .adopt_derender import (
    build_display_unified_diff,
    build_two_step_derender_plan,
    generate_derendered_patch,
    read_rendered_baseline_lines,
    split_rh_deletes_at_tr_boundaries,
)
from .adopt_fission import (
    compute_skeleton_match,
    find_monotonic_line_matches,
    fission_diff_units,
    refine_single_line_units,
    tokenize_line,
    try_derender_single_line,
)
from .adopt_handlers import (
    handle_modification_interactive,
    handle_modification_non_interactive,
    handle_rename_interactive,
    handle_rename_non_interactive,
    handle_single_addition,
    handle_single_deletion,
    handle_single_modification,
    handle_single_rename,
)
from .adopt_package import (
    _derive_adopt_result_status,
    _derive_package_adopt_status,
    _dry_run_package_adopt,
    adopt_one_package_drifts,
    execute_package_adopt,
    stage_converged_package_drifts,
)
from .adopt_patch import (
    apply_source_patch,
    check_patch_conflicts,
    generate_install_git_patch,
    generate_source_to_install_patch,
    parse_unified_patch_hunks,
    splice_conflict_clusters_diff3,
)
from .adopt_plan import (
    assert_source_file_clean,
    check_is_drifted_path_converged,
    find_source_path_conflict,
    get_drifted_packages,
    plan_package_adopt,
    resolve_source_file_path,
)

__all__ = [
    # Layer 1a-1: Hunk Fission & Single-Line Token Calculus
    "tokenize_line",
    "compute_skeleton_match",
    "find_monotonic_line_matches",
    "fission_diff_units",
    "try_derender_single_line",
    "refine_single_line_units",
    # Layer 1a-2: Orthogonal 3-Way De-Rendering & Two-Step Plan
    "split_rh_deletes_at_tr_boundaries",
    "build_two_step_derender_plan",
    "build_display_unified_diff",
    "read_rendered_baseline_lines",
    "generate_derendered_patch",
    # Layer 1b: Unified Patch Engine
    "generate_install_git_patch",
    "generate_source_to_install_patch",
    "parse_unified_patch_hunks",
    "check_patch_conflicts",
    "splice_conflict_clusters_diff3",
    "apply_source_patch",
    # Layer 1c: Drift Discovery & Planning
    "get_drifted_packages",
    "find_source_path_conflict",
    "resolve_source_file_path",
    "check_is_drifted_path_converged",
    "assert_source_file_clean",
    "plan_package_adopt",
    # Layer 2: Single-File Reconciliation Actions
    "ensure_empty_keep_file",
    "adopt_addition",
    "ignore_addition",
    "adopt_deletion",
    "patch_and_edit",
    "adopt_rename",
    "fallback_over_render",
    "fallback_side_by_side",
    # Layer 3: Interactive & Non-Interactive Single-Item Dispatchers
    "handle_single_addition",
    "handle_single_deletion",
    "handle_rename_non_interactive",
    "handle_rename_interactive",
    "handle_single_rename",
    "handle_modification_non_interactive",
    "handle_modification_interactive",
    "handle_single_modification",
    # Layer 4: Single-Package Adoption Execution
    "_derive_package_adopt_status",
    "_derive_adopt_result_status",
    "stage_converged_package_drifts",
    "_dry_run_package_adopt",
    "execute_package_adopt",
    "adopt_one_package_drifts",
]
