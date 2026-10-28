"""Drift Adoption Engine (`drift.adopt`): 3-Way Template De-Rendering, Patching, Planning, and Execution.

Submodules (ordered bottom-up by dependency layer):
- `adopt_derender` (Layer 1a): 3-way hunk-map template de-rendering (`T <-> R <-> H`).
- `adopt_patch` (Layer 1b): Unified diff generation, header adjustment, conflict probing, and
  cross-platform patch application (`patch` + `git merge-file`).
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
    build_template_hunk_map,
    derender_host_lines,
    generate_derendered_patch,
    read_rendered_baseline_lines,
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
    check_patch_has_hunks,
    generate_install_git_patch,
    generate_retargeted_patch,
    rewrite_patch_target_headers,
)
from .adopt_plan import (
    assert_source_file_clean,
    check_is_drifted_path_converged,
    get_drifted_packages,
    plan_package_adopt,
    resolve_source_file_path,
)

__all__ = [
    # Layer 1a: 3-Way Template De-Rendering
    "build_template_hunk_map",
    "derender_host_lines",
    "read_rendered_baseline_lines",
    "generate_derendered_patch",
    # Layer 1b: Unified Patch Engine
    "check_patch_has_hunks",
    "generate_install_git_patch",
    "rewrite_patch_target_headers",
    "generate_retargeted_patch",
    "check_patch_conflicts",
    "apply_source_patch",
    # Layer 1c: Drift Discovery & Planning
    "get_drifted_packages",
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
