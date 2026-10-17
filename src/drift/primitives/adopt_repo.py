"""Primitive 7 & Stage 1: Bidirectional Drift Adoption & Workspace Sync.

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Primitive Entry Point & Orchestration
    run_primitive_adopt_drifts(workspace_config, package_names, options)
        1. Preparation (reverse-sync + scoped render + converged/divergent planning):
            prepare_adopt [Layer 5]
                -> _sync_and_render_before_adopt [Layer 5]
                -> plan_package_adopt [Layer 1]
        2. Dry-Run Simulation (if options.dry_run=True):
            _dry_run_package_adopt [Layer 4] (pure simulation; formatted via PackageAdoptPlan.format_text / AdoptResult.format_text)
        3. Execution (stages converged files directly + reconciles divergent drifts):
            execute_adopt [Layer 5] -> execute_package_adopt [Layer 4]
        4. State Synchronization & Commit:
            commit_staged_repo_changes (commits only staged adopted paths in install/ repo)

    resolve_and_commit_converged_drifts(workspace_config, package_names, dry_run) -> Dict[str, List[Path]]
        Dedicated entry point that runs adoption planning without patch generation, rendering,
        or reverse-sync (prepare_adopt(..., options=AdoptOptions(no_render=True, no_reverse_sync=True, skip_patches=True)))
        and executes only the converged-files plan (execute_converged_drifts). Used by deploy_repo after reverse-sync.

    execute_converged_drifts(workspace_config, plan) -> Dict[str, List[Path]]
        Stages and commits only the planned converged_files in install/ repo.

    prepare_adopt(workspace_config, package_names, options) -> AdoptPlan
        Synchronizes host changes into install/ (unless options.no_reverse_sync), renders drifted
        packages (unless options.no_render), and builds a multi-package AdoptPlan containing PackageAdoptPlans.

    execute_adopt(workspace_config, plan) -> AdoptResult
        Executes adoption across packages using plan.options, stages resolved files, and commits staged changes in install/ repo.

Layer 4: Single-Package Drift Adoption & Planning
    stage_converged_package_drifts(workspace_config, plan) -> List[Path]
        Stages planned converged_files for a single package in install/<pkg>/.

    adopt_one_package_drifts(workspace_config, pkg, options) -> PackageAdoptResult
        Coordinates single-package planning and execution.
        NOTE: adopt_one_package_drifts does NOT commit the install/ repo; it only reconciles source files
        and stages adopted paths in the install/ index. Committing staged changes is performed at Layer 5
        orchestration (execute_adopt / commit_staged_repo_changes) or by the caller.

    execute_package_adopt(workspace_config, plan, options) -> PackageAdoptResult
        1. Pre-Source Hook (if divergent drifts exist and not plan.pre_source_already_triggered):
            trigger_pre_source_hook
        2. Stage Converged Files Directly:
            plan.converged_files -> resolved_paths
        3. Process Divergent Drift Categories (Additions, Deletions, Renames, Modifications):
            handle_single_addition [Layer 3] -> ItemAdoptOutcome
            handle_single_deletion [Layer 3] -> ItemAdoptOutcome
            handle_single_rename [Layer 3] (reads pre-computed patch) -> ItemAdoptOutcome
            handle_single_modification [Layer 3] (reads pre-computed patch) -> ItemAdoptOutcome
        4. Staging Resolved Files & Deriving AdoptStatus:
            git -C <install_path> add -- <resolved_paths>
            _derive_package_adopt_status -> AdoptStatus (UP_TO_DATE, SUCCESS, PARTIAL, SKIPPED, FAILED)
            NOTE: Only successfully adopted/converged files are staged. Does NOT commit the install/ repo.

Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
    handle_single_addition
        Verifies target file cleanliness, evaluates collisions with existing source files, and delegates to adopt_addition / ignore_addition / discard / skip.
    handle_single_deletion
        Verifies target file cleanliness, validates target presence in source, and delegates to adopt_deletion / discard / skip.
    handle_single_rename -> handle_rename_non_interactive / handle_rename_interactive
        Verifies old source file cleanliness, generates adjusted patch headers, evaluates conflict status, and routes to adopt_rename / freeze / merge editor / side-by-side.
    handle_single_modification -> handle_modification_non_interactive / handle_modification_interactive
        Verifies target source file cleanliness, evaluates template vs. static modifications, checks patch conflicts, logs/prints diffs, and routes to patch_and_edit / freeze / side-by-side.

Layer 2: Single-File Reconciliation Actions
    _sync_file_mode(src_file, install_file)
        Synchronizes file permissions and executable bits from install/ onto src_file.
    adopt_addition(pkg_dir, install_pkg_dir, rel_path)
        Copies a wild host-side added file into the declarative source folder under src/.
    ignore_addition(pkg_dir, install_pkg_dir, rel_path)
        Unlinks file from install/ base, un-tracks it in install/ Git index, and registers pattern into .drift_ignore.
    adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg)
        Symmetrically deletes the matching source template/file from declarative source directory.
    patch_and_edit(src_file, patch_content, install_file, accept_conflicts, open_editor)
        Unified patch application engine: applies unified content diffs (invoking patch only when '@@' diff hunks exist),
        synchronizes file mode/permissions from install_file via _sync_file_mode, and optionally opens $EDITOR.
    adopt_rename(render_engines, src_dir_to_render, old_rel_path, new_rel_path, patch_content, has_patch_conflict, install_file, accept_conflicts)
        Symmetrically renames template files in src/ using copy-and-patch with atomic cleanup, applies content patches,
        synchronizes permissions, and returns the resolved new_src_file Path.
    fallback_over_render(src_file, static_file)
        Backs up original template to .bak and overwrites it with static content from install/ (freezing template).
    fallback_side_by-side(src_file, install_file)
        Launches visual side-by-side diff in $EDITOR (nvim, vim, code, emacs) between source template and live static drift.

Layer 1: Inspection & Git Patch Primitives
    get_drifted_packages
        Parses git status porcelain across install/ to discover all packages with local host modifications.
    plan_package_adopt(workspace_config, pkg, skip_patches, pre_source_already_triggered) -> PackageAdoptPlan
        1. Resolve Package Source Context:
            _resolve_package_source_context -> (src_dir_to_render, render_engines)
        2. Temporary Staging Context Manager (if package has Git changes):
            _temporary_stage_package_index (git add --all <pkg> -> yield -> git restore --staged -- <pkg>)
        3. Parse & Normalize .drift_keep Drifts:
            parse_git_status_porcelain -> _normalize_keep_file_drifts -> GitStatusChanges
        4. Partition Converged vs. Divergent Paths:
            _partition_converged_package_drifts -> PackageDriftFiles
                -> _is_path_converged_with_render
                    -> _check_source_path_exists (resolves template/source counterpart for deletions)
                    -> check_is_drifted_path_converged (compares content & permissions between install/ and render/)
                -> _translate_keep_to_dir (translates .drift_keep additions/deletions to parent directories)
        5. Pre-compute Unified Diff Patches (unless skip_patches=True):
            _precompute_package_drift_patches -> (mod_patches, rename_patches)
                -> generate_unified_patch (for divergent modifications)
                -> get_rename_patch_key & generate_adjusted_patch (for divergent renames)
    check_is_drifted_path_converged
        Checks whether a drifted package-relative path in install/<pkg>/ has converged with render/<pkg>/.
    assert_source_file_clean
        Enforces scoped Git cleanliness safeguard on target source file before applying modifications.
    get_rename_patch_key
        Generates a collision-free dictionary key ('<old_posix>//->//<new_posix>') for rename patches.
    generate_unified_patch
        Executes git diff HEAD to extract raw unified diff patches for drifted files in install/.
    generate_adjusted_patch
        Rewrites unified diff headers to align install/ file paths with target template paths in src/.
    check_patch_conflicts
        Runs patch --dry-run for patches containing '@@' diff hunks (returns False early for hunkless/mode-only diffs).
    apply_source_patch
        Executes patch tool against source files for patches containing '@@' diff hunks with optional --merge conflict support.
    test_file_conflict
        Convenience wrapper checking patch conflicts for a single modified file.
    resolve_source_file_path
        Resolves physical source template paths using RenderEngineRegistry suffix mapping.
-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 0: Adoption Data Structures & Plans (in core/adopt_models.py)
    Layer 1: Inspection & Git Patch Primitives
    Layer 2: Single-File Reconciliation Actions
    Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
    Layer 4: Single-Package Drift Adoption
    Layer 5: Public Primitive Entry Point
===============================================================================
"""

import logging
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..config.package_config import PackageConfig
from ..config.render_engine_config import RenderEngineRegistry
from ..core.constants import DRIFT_IGNORE_FILE_NAME, DRIFT_KEEP_FILE_NAME
from ..core.adopt_models import (
    AdoptStatus,
    ItemAdoptOutcome,
    get_rename_patch_key,
    AdoptOptions,
    PackageDriftFiles,
    PackageAdoptPlan,
    AdoptPlan,
    PackageAdoptResult,
    AdoptResult,
)
from ..render.render_package import RenderOptions, run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync
from ..utils.config_utils import partition
from ..utils.git_utils import (
    GitStatusChanges,
    parse_git_status_porcelain,
    has_uncommitted_modifications,
    get_drift_root,
    commit_staged_repo_changes,
)
from ..utils.file_ops import remove_file_or_empty_dir, remove_tree, copy_file, ensure_dir
from ..utils.file_inspect import contents_differ, permissions_differ
from ..utils.path_utils import to_relative_path, to_relative_posix
from ..hooks.lifecycle_hooks import HookExecFlags, trigger_pre_source_hook
from ..utils.editor_utils import launch_single_file_editor, launch_side_by_side_editor

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Inspection & Git Patch Primitives
# =====================================================================

def get_drifted_packages(workspace_config: WorkspaceConfig) -> List[str]:
    """Scans the install/ repository status to identify which packages have uncommitted drifts."""
    diff = parse_git_status_porcelain(workspace_config.install_path)
    changed_names = {p.parts[0] for p in diff.all_paths() if p.parts}

    installed_packages = set(workspace_config.get_package_names_from_dir(workspace_config.install_path))
    source_packages = set(workspace_config.get_package_names_from_source_dir())

    drifted = (changed_names & installed_packages) & source_packages
    return sorted(drifted)


def check_is_drifted_path_converged(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    rel_path: Path,
    src_exists: bool = False,
) -> bool:
    """Checks whether a drifted package-relative path in install/<pkg>/ has converged with render/<pkg>/."""
    install_file = install_pkg_dir / rel_path
    render_file = render_pkg_dir / rel_path

    install_exists = install_file.exists() or install_file.is_symlink()
    render_exists = render_file.exists() or render_file.is_symlink()

    if not install_exists and not render_exists:
        return not src_exists

    if not install_exists or not render_exists:
        return False

    if install_file.is_symlink() or render_file.is_symlink():
        return False

    if install_file.is_dir() and render_file.is_dir():
        return True

    if install_file.is_file() and render_file.is_file():
        if rel_path.name == DRIFT_KEEP_FILE_NAME:
            return install_file.stat().st_size == 0 and render_file.stat().st_size == 0
        return (
            not contents_differ(render_file, install_file)
            and not permissions_differ(render_file, install_file)
        )

    return False


def _check_source_path_exists(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> bool:
    """Checks whether rel_path or its template counterpart exists in src_dir_to_render."""
    target_rel = (
        rel_path.parent
        if rel_path.name == DRIFT_KEEP_FILE_NAME and rel_path.parent != Path(".")
        else rel_path
    )
    direct_path = src_dir_to_render / target_rel
    if direct_path.exists() or direct_path.is_symlink():
        return True
    return resolve_source_file_path(render_engines, src_dir_to_render, target_rel) is not None


def _is_path_converged_with_render(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    rel_path: Path,
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    check_src_for_deletion: bool = False,
) -> bool:
    """Evaluates whether a package-relative path in install/<pkg>/ has converged with render/<pkg>/."""
    if not render_pkg_dir.is_dir() or not install_pkg_dir.is_dir():
        return False
    src_exists = (
        _check_source_path_exists(render_engines, src_dir_to_render, rel_path)
        if check_src_for_deletion
        else False
    )
    return check_is_drifted_path_converged(
        install_pkg_dir,
        render_pkg_dir,
        rel_path,
        src_exists=src_exists,
    )


def assert_source_file_clean(
    src_file: Path,
    pkg: str,
    force: bool = False
) -> None:
    """Verifies that a target source file is clean before adopting drifts into it."""
    if not src_file.exists():
        return

    try:
        drift_root = get_drift_root(src_file.parent)
    except Exception:
        drift_root = src_file.parent

    if not has_uncommitted_modifications(drift_root, src_file):
        return

    if force:
        logger.warning(
            f"⚠️  [FORCE] Bypassing Git cleanliness safeguard for '{src_file.name}'. Overwriting uncommitted modifications."
        )
        return

    rel_dirty = to_relative_path(src_file, drift_root)

    raise RuntimeError(
        f"The source file '{rel_dirty}' has uncommitted local modifications!\n"
        f"Adopting system drift would overwrite uncommitted changes in this file.\n\n"
        f"👉 Run 'drift adopt {pkg} --force' (or -f) to overwrite existing file modifications.\n"
        f"👉 Or commit / stash your changes before adopting."
    )


def check_patch_has_hunks(patch_content: str) -> bool:
    """Read-only check returning True if patch_content contains at least one unified diff hunk header ('@@ ')."""
    return any(line.startswith("@@ ") or line == "@@" for line in patch_content.splitlines())


def generate_unified_patch(install_base: Path,
                           pkg_rel_path: Path,
                           old_rel_path: Optional[Path] = None) -> str:
    """Generates the unified patch string for a given drifted file in install/."""
    cmd = ["git", "-C", str(install_base), "diff", "HEAD", "--"]
    if old_rel_path:
        cmd.append(old_rel_path.as_posix())
    cmd.append(pkg_rel_path.as_posix())
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return res.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate unified patch for {pkg_rel_path}: {e}")
        return ""


def generate_adjusted_patch(
    install_base: Path,
    pkg: str,
    new_rel_path: Path,
    old_rel_path: Optional[Path] = None,
    target_src_filename: Optional[str] = None
) -> str:
    """Generates a unified patch and adjusts its diff headers to point to a target source file name.

    Header lines are only stripped or rewritten before the first '@@' hunk header so that
    diff body lines starting with '--- ' (deleted '-- ' lines) or '+++ ' (added '++ ' lines)
    are preserved verbatim.
    """
    patch_content = generate_unified_patch(
        install_base,
        Path(pkg) / new_rel_path,
        old_rel_path=(Path(pkg) / old_rel_path if old_rel_path else None)
    )
    if not patch_content.strip() or not target_src_filename:
        return patch_content

    lines = patch_content.splitlines()
    adjusted_lines = []
    in_hunk = False
    for line in lines:
        if not in_hunk:
            if line.startswith("@@ ") or line == "@@":
                in_hunk = True
                adjusted_lines.append(line)
            elif line.startswith("--- "):
                adjusted_lines.append(f"--- a/{target_src_filename}")
            elif line.startswith("+++ "):
                adjusted_lines.append(f"+++ b/{target_src_filename}")
            # All other pre-hunk lines (diff --git, index, similarity index, rename from/to,
            # old/new/deleted file mode) are Git extended headers and are skipped.
            continue
        adjusted_lines.append(line)
    return "\n".join(adjusted_lines) + "\n"


def check_patch_conflicts(src_file: Path, patch_content: str) -> bool:
    """Runs a dry-run of patch to determine if there are conflicts applying the patch to src_file.

    Note: The external patch tool (especially BSD patch on macOS) requires diff hunk blocks
    (lines starting with '@@') and errors if passed a hunkless diff (e.g. mode-only git diffs).
    If patch_content lacks '@@' hunks, returns False (no conflicts) immediately without calling patch.
    """
    if not check_patch_has_hunks(patch_content):
        return False
    cmd = ["patch", "--dry-run", "--no-backup-if-mismatch", str(src_file)]
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return False
    except subprocess.CalledProcessError:
        return True


def apply_source_patch(src_file: Path, patch_content: str, accept_conflicts: bool = False) -> bool:
    """Applies a patch to a source file, supporting merge markers if accept_conflicts is True.

    Note: The external patch command operates strictly on text diff hunks ('@@' blocks).
    If patch_content has no '@@' hunks (e.g. file mode/permission-only drift), this function
    returns True immediately without executing the external patch binary, allowing file permissions
    to be applied via _sync_file_mode.
    """
    if not check_patch_has_hunks(patch_content):
        return True
    cmd = ["patch", "--no-backup-if-mismatch"]
    if accept_conflicts:
        cmd.append("--merge")
    cmd.append(str(src_file))
    
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        return True
    except subprocess.CalledProcessError:
        return False
    finally:
        rej_file = src_file.with_suffix(src_file.suffix + ".rej")
        if rej_file.exists():
            rej_file.unlink()


def test_file_conflict(src_file: Path, install_file: Path, install_base: Path, pkg_rel_path: Path) -> bool:
    """Evaluates patch conflicts for a single modified file against its '@@' diff hunks."""
    patch_content = generate_unified_patch(install_base, pkg_rel_path)
    return check_patch_conflicts(src_file, patch_content)


def resolve_source_file_path(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path
) -> Optional[Path]:
    """Resolves the physical file path inside src_dir_to_render using find_source_file_for_rendered_names."""
    match_info = render_engines.find_source_file_for_rendered_names(
        src_dir_to_render / rel_path.parent,
        [rel_path.name]
    )
    if match_info:
        return match_info.path
    return None


@contextmanager
def _temporary_stage_package_index(install_base: Path, pkg: str) -> Iterator[None]:
    """Temporarily stages a package in install/ for rename & diff inspection, restoring index on exit."""
    res_add = subprocess.run(
        ["git", "-C", str(install_base), "add", "--all", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_add.returncode != 0:
        logger.warning(
            f"Pre-staging install changes for '{pkg}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
        )
    try:
        yield
    finally:
        res_rst = subprocess.run(
            ["git", "-C", str(install_base), "restore", "--staged", "--", pkg],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_rst.returncode != 0:
            logger.debug(
                f"Unstaging install changes for '{pkg}' exited with code {res_rst.returncode}: {res_rst.stderr.strip()}"
            )


def _normalize_keep_file_drifts(
    diff: GitStatusChanges,
    install_pkg_dir: Path,
) -> GitStatusChanges:
    """Splits .drift_keep renames into deletions+additions and strips/warns on corrupt .drift_keep files."""
    sorted_renames = sorted(diff.renamed, key=lambda r: r.new_path)
    keep_renames, renames_after_keep = partition(
        lambda r: r.old_path.name == DRIFT_KEEP_FILE_NAME or r.new_path.name == DRIFT_KEEP_FILE_NAME,
        sorted_renames,
    )
    raw_deletions = list(diff.deleted) + [r.old_path for r in keep_renames]
    raw_additions = list(diff.added) + [r.new_path for r in keep_renames]

    keep_mods, raw_modifications = partition(
        lambda rel_mod: rel_mod.name == DRIFT_KEEP_FILE_NAME,
        diff.modified,
    )
    for rel_mod in keep_mods:
        logger.warning(
            f"⚠️  [CORRUPT] Unexpected modification of keep file '{rel_mod}'. "
            f"'.drift_keep' must always be an empty stub file."
        )

    for rel_add in raw_additions:
        if rel_add.name == DRIFT_KEEP_FILE_NAME:
            keep_path = install_pkg_dir / rel_add
            if keep_path.is_file() and keep_path.stat().st_size > 0:
                logger.warning(
                    f"⚠️  [CORRUPT] Keep file '{rel_add}' is not empty (size={keep_path.stat().st_size} bytes). "
                    f"'.drift_keep' must always be 0 bytes."
                )

    return GitStatusChanges(
        added=raw_additions,
        modified=raw_modifications,
        deleted=raw_deletions,
        renamed=renames_after_keep,
    )


def _translate_keep_to_dir(p: Path) -> Path:
    """Translates a .drift_keep path to its parent directory path."""
    return p.parent if p.name == DRIFT_KEEP_FILE_NAME and p.parent != Path(".") else p


def _partition_converged_package_drifts(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    src_dir_to_render: Path,
    render_engines: RenderEngineRegistry,
    diff: GitStatusChanges,
) -> PackageDriftFiles:
    """Partitions normalized GitStatusChanges into a PackageDriftFiles container."""
    def _is_conv(p: Path, is_del: bool = False) -> bool:
        return _is_path_converged_with_render(
            install_pkg_dir,
            render_pkg_dir,
            p,
            render_engines=render_engines,
            src_dir_to_render=src_dir_to_render,
            check_src_for_deletion=is_del,
        )

    conv_adds, div_adds_raw = partition(lambda p: _is_conv(p, is_del=False), diff.added)
    conv_dels, div_dels_raw = partition(lambda p: _is_conv(p, is_del=True), diff.deleted)
    conv_mods, modifications = partition(lambda p: _is_conv(p, is_del=False), diff.modified)
    conv_renames, div_renames = partition(
        lambda r: _is_conv(r.old_path, is_del=True) and _is_conv(r.new_path, is_del=False),
        diff.renamed,
    )

    converged_files = sorted(
        set(conv_adds)
        | set(conv_dels)
        | set(conv_mods)
        | {p for r in conv_renames for p in (r.old_path, r.new_path)}
    )
    additions = sorted({_translate_keep_to_dir(p) for p in div_adds_raw})
    deletions = sorted({_translate_keep_to_dir(p) for p in div_dels_raw})
    renames = [(r.old_path, r.new_path) for r in div_renames]
    return PackageDriftFiles(
        additions=additions,
        deletions=deletions,
        modifications=modifications,
        renames=renames,
        converged_files=converged_files,
    )


def _precompute_package_drift_patches(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_engines: RenderEngineRegistry,
    drifts: PackageDriftFiles,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Pre-computes unified diff patches for divergent modifications and renames while staged in index."""
    mod_patches = {
        rel_mod.as_posix(): generate_unified_patch(install_base, Path(pkg) / rel_mod)
        for rel_mod in drifts.modifications
    }
    rename_patches = {
        get_rename_patch_key(old_rel, new_rel): generate_adjusted_patch(
            install_base=install_base,
            pkg=pkg,
            new_rel_path=new_rel,
            old_rel_path=old_rel,
            target_src_filename=(
                old_src_file.name
                if (old_src_file := resolve_source_file_path(render_engines, src_dir_to_render, old_rel))
                else old_rel.name
            ),
        )
        for old_rel, new_rel in drifts.renames
    }
    return mod_patches, rename_patches


def _resolve_package_source_context(
    workspace_config: WorkspaceConfig,
    pkg: str,
    dry_run: bool = True,
) -> Tuple[Path, RenderEngineRegistry]:
    """Resolves (src_dir_to_render, render_engines) for a package."""
    src_pkg_dir = workspace_config.source_path / pkg
    render_engines = workspace_config.render_engine_configs
    try:
        pkg_config = PackageConfig.from_source_dir(
            src_pkg_dir,
            workspace_config,
            dry_run=dry_run,
            silent=dry_run,
        )
        return (
            pkg_config.get_source_directory_to_render(src_pkg_dir),
            pkg_config.package_render_engines(workspace_config),
        )
    except Exception:
        return src_pkg_dir, render_engines


def plan_package_adopt(
    workspace_config: WorkspaceConfig,
    pkg: str,
    skip_patches: bool = False,
    pre_source_already_triggered: bool = False,
) -> PackageAdoptPlan:
    """Discovers and structures all system drifts for a single package into a PackageAdoptPlan.

    Includes the converged-files planning sub-process: any drifted paths in install/<pkg>/ that
    have already converged with render/<pkg>/ are partitioned into `plan.converged_files` and
    excluded from divergent additions, deletions, modifications, and renames.

    Temporarily stages the package in the install/ repository under a context manager to allow
    Git rename detection and unified patch pre-computation (unless skip_patches=True), immediately
    restoring the staged index so that the working index remains untouched until explicit execution.
    """
    install_base = workspace_config.install_path
    src_pkg_dir = workspace_config.source_path / pkg
    src_dir_to_render, render_engines = _resolve_package_source_context(
        workspace_config, pkg, dry_run=skip_patches
    )
    install_pkg_dir = install_base / pkg
    render_pkg_dir = workspace_config.render_path / pkg

    if not install_pkg_dir.is_dir() or not parse_git_status_porcelain(install_base, pkg).has_changes:
        return PackageAdoptPlan(
            package=pkg,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            render_engines=render_engines,
            pre_source_already_triggered=pre_source_already_triggered,
            status="PENDING",
        )

    with _temporary_stage_package_index(install_base, pkg):
        raw_diff = parse_git_status_porcelain(install_base, pkg)
        normalized_diff = _normalize_keep_file_drifts(raw_diff, install_pkg_dir)
        drifts = _partition_converged_package_drifts(
            install_pkg_dir=install_pkg_dir,
            render_pkg_dir=render_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            render_engines=render_engines,
            diff=normalized_diff,
        )
        mod_patches, rename_patches = (
            ({}, {})
            if skip_patches
            else _precompute_package_drift_patches(
                install_base, pkg, src_dir_to_render, render_engines, drifts
            )
        )

    return PackageAdoptPlan(
        package=pkg,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        render_engines=render_engines,
        drifts=drifts,
        mod_patches=mod_patches,
        rename_patches=rename_patches,
        pre_source_already_triggered=pre_source_already_triggered,
        status="PENDING",
    )


# =====================================================================
# Layer 2: Single-File Reconciliation Actions
# =====================================================================

def _sync_file_mode(src_file: Path, install_file: Path) -> None:
    """Synchronizes file mode/permissions from install_file onto src_file."""
    try:
        src_file.chmod(install_file.stat().st_mode)
    except Exception as e:
        logger.warning(f"Failed to synchronize file permissions from '{install_file}' to '{src_file}': {e}")


def adopt_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Copies a wild host-side added file or creates an empty directory in the declarative source folder."""
    dest = pkg_dir / rel_path
    src_install = install_pkg_dir / rel_path
    if src_install.is_dir():
        ensure_dir(dest)
    else:
        copy_file(src_install, dest)


def ignore_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Unlinks the file/directory from install base and registers pattern in .drift_ignore."""
    install_item = install_pkg_dir / rel_path
    if install_item.is_dir() and not install_item.is_symlink():
        remove_tree(install_item)
    elif install_item.exists() or install_item.is_symlink():
        remove_file_or_empty_dir(install_item)

    install_base = install_pkg_dir.parent
    rel_install_base = Path(install_pkg_dir.name) / rel_path
    # check=False is intentional: file may not be tracked in the git index yet.
    res_rm = subprocess.run(
        ["git", "-C", str(install_base), "rm", "-r", "--cached", "-f", "--", rel_install_base.as_posix()],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_rm.returncode != 0:
        logger.debug(f"git rm --cached exited with code {res_rm.returncode} for '{rel_install_base}': {res_rm.stderr.strip()}")

    # Append pattern to .drift_ignore
    ignore_file = pkg_dir / DRIFT_IGNORE_FILE_NAME
    pattern = (rel_path.as_posix() + "/") if install_item.is_dir() else rel_path.as_posix()
    with ignore_file.open("a", encoding="utf-8") as f:
        f.write(f"\n{pattern}\n")


def adopt_deletion(render_engines: RenderEngineRegistry, src_dir_to_render: Path, rel_path: Path, pkg: str) -> None:
    """Symmetrically deletes the corresponding file or directory from declarative source folder."""
    src_dir = src_dir_to_render / rel_path
    if src_dir.is_dir() and not src_dir.is_symlink():
        remove_tree(src_dir)
        return
    src_file = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if src_file and (src_file.exists() or src_file.is_symlink()):
        remove_file_or_empty_dir(src_file)


def patch_and_edit(
    src_file: Path,
    patch_content: str,
    install_file: Optional[Path] = None,
    accept_conflicts: bool = False,
    open_editor: bool = False
) -> bool:
    """Applies a patch (clean or with merge markers) to a source file, synchronizes file permissions, and optionally opens $EDITOR.

    Note: The external patch tool is executed via apply_source_patch only when '@@' diff hunks exist.
    File mode/permissions are synchronized separately via _sync_file_mode when install_file is provided.
    """
    if patch_content.strip():
        success = apply_source_patch(src_file, patch_content, accept_conflicts=accept_conflicts)
        if not success:
            logger.error(f"Failed to apply patch to template file {src_file.name}. Please check manually.")
            return False
    if install_file is not None and install_file.exists():
        _sync_file_mode(src_file, install_file)
    if open_editor:
        try:
            launch_single_file_editor(src_file)
        except RuntimeError as e:
            logger.warning(f"⚠️  Failed to open editor: {e}. Skipping file adoption.")
            return False
    return True


def adopt_rename(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    has_patch_conflict: bool = False,
    install_file: Optional[Path] = None,
    accept_conflicts: bool = False
) -> Optional[Path]:
    """Symmetrically renames the source template file and applies any content patch."""
    old_src_file = resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path)
    file_name = old_src_file.name if old_src_file else old_rel_path.name

    if has_patch_conflict:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into renamed template file: '{file_name}'")
        else:
            logger.error(f"❌ [CONFLICT] Cannot apply system diff cleanly onto renamed template file '{file_name}'. Skipping.")
            logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
            return None

    if old_src_file and old_src_file.exists():
        new_src_name = render_engines.make_new_template_name(old_src_file.name,
                                                             new_rel_path.name)
        new_src_file = src_dir_to_render / new_rel_path.parent / new_src_name

        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        copy_file(old_src_file, new_src_file)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in source directory. Creating a new template file for '{new_rel_path}'.")
        new_src_file = src_dir_to_render / new_rel_path
        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        new_src_file.touch()

    success = patch_and_edit(
        new_src_file,
        patch_content,
        install_file=install_file,
        accept_conflicts=accept_conflicts,
        open_editor=False
    )
    if not success:
        new_src_file.unlink(missing_ok=True)
        return None

    if old_src_file and old_src_file.exists() and old_src_file != new_src_file:
        old_src_file.unlink(missing_ok=True)

    return new_src_file


def fallback_over_render(src_file: Path, static_file: Path) -> None:
    """Backs up the original template to .bak and overwrites it with static file content (freezing template)."""
    bak_file = src_file.with_suffix(src_file.suffix + ".bak")
    copy_file(src_file, bak_file)
    copy_file(static_file, src_file)
    logger.warning(f"⚠️  [FREEZE] Overwrote template '{src_file.name}' with static content. Original template backed up to '{bak_file.name}'.")


def fallback_side_by_side(src_file: Path, install_file: Path) -> bool:
    """Launches editor to display both template and the final compiled/static drift as side-by-side reference."""
    try:
        launch_side_by_side_editor([(src_file, install_file)])
        return True
    except RuntimeError as e:
        logger.warning(f"⚠️  Failed to open side-by-side editor: {e}. Skipping file adoption.")
        return False


# =====================================================================
# Layer 3: Single-Item Interactive & Non-Interactive Dispatchers
# =====================================================================

def _prompt_addition_collision_interactive(rel_path: Path, incoming_is_dir: bool) -> ItemAdoptOutcome:
    """Prompts the user when an addition collides with an existing file/directory in source."""
    incoming_type = "directory" if incoming_is_dir else "file"
    existing_type = "file" if incoming_is_dir else "directory"
    print(f"\n⚠️  [CONFLICT] Cannot adopt {incoming_type} '{rel_path}' because a {existing_type} with the same name exists in source!")
    print("Reconciliation options:")
    print(f"[1] Discard addition / Restore (removes {incoming_type} on host next deployment)")
    print(f"[2] Skip {incoming_type}")
    choice = input("Select option [1-2]: ").strip()
    return ItemAdoptOutcome.ADOPTED if choice == "1" else ItemAdoptOutcome.SKIPPED


def _prompt_directory_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting an untracked directory addition."""
    print(f"\nFound untracked directory addition inside Fully-Controlled Directory: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt directory into source package (creates empty directory)")
    print("[2] Ignore directory (appends pattern to package .drift_ignore)")
    print("[3] Discard directory (removes directory on host next deployment)")
    print("[4] Skip directory")
    choice = input("Select option [1-4]: ").strip()
    if choice == "1":
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "3":
        return ItemAdoptOutcome.ADOPTED
    else:
        return ItemAdoptOutcome.SKIPPED


def _prompt_file_addition_interactive(
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting an untracked file addition."""
    print(f"\nFound untracked file addition inside Fully-Controlled Directory: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt and copy into source package")
    print("[2] Ignore file (appends pattern to package .drift_ignore)")
    print("[3] Discard file (stages file to install/ database so it is deleted on next deploy)")
    print("[4] Skip file")
    choice = input("Select option [1-4]: ").strip()
    if choice == "1":
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "2":
        ignore_addition(src_pkg_dir, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "3":
        return ItemAdoptOutcome.ADOPTED
    else:
        return ItemAdoptOutcome.SKIPPED


def _handle_single_directory_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles adoption of an empty directory addition."""
    # 1. Pre-existing directory in source: already matches desired state, skip cleanly
    if (src_dir_to_render / rel_path).is_dir():
        logger.info(f"Directory '{rel_path}' already exists in source. Skipping addition.")
        return ItemAdoptOutcome.ADOPTED

    # 2. File / template collision check
    if (src_dir_to_render / rel_path).is_file() or resolve_source_file_path(render_engines, src_dir_to_render, rel_path) is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt directory '{rel_path}' because a file already exists at that path in source. Skipping.")
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=True)

    # 3. Clean directory addition
    assert_source_file_clean(src_dir_to_render / rel_path, force=force, pkg=install_pkg_dir.name)
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_directory_addition_interactive(src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path)


def _handle_single_file_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles adoption of a regular file addition."""
    # 1. Directory collision check
    if (src_dir_to_render / rel_path).is_dir():
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt file '{rel_path}' because a directory already exists at that path in source. Skipping.")
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    # 2. Existing file collision check
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt addition '{rel_path}' because the target already exists in source. Skipping.")
            return ItemAdoptOutcome.CONFLICT
        return _prompt_addition_collision_interactive(rel_path, incoming_is_dir=False)

    # 3. Clean file addition
    target_src_file = src_dir_to_render / rel_path
    assert_source_file_clean(target_src_file, force=force, pkg=install_pkg_dir.name)
    if not interactive:
        adopt_addition(src_dir_to_render, install_pkg_dir, rel_path)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_file_addition_interactive(src_pkg_dir, src_dir_to_render, install_pkg_dir, rel_path)


def handle_single_addition(
    render_engines: RenderEngineRegistry,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file or directory addition."""
    if (install_pkg_dir / rel_path).is_dir():
        return _handle_single_directory_addition(
            render_engines=render_engines,
            src_pkg_dir=src_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            install_pkg_dir=install_pkg_dir,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        )
    return _handle_single_file_addition(
        render_engines=render_engines,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        rel_path=rel_path,
        interactive=interactive,
        force=force,
    )


def _prompt_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting an empty directory deletion."""
    print(f"\nFound host empty directory deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (removes empty directory from source)")
    print("[2] Discard deletion / Restore (restores directory in next deployment)")
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "2":
        return ItemAdoptOutcome.ADOPTED
    else:
        return ItemAdoptOutcome.SKIPPED


def _prompt_non_empty_directory_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for handling a non-empty directory deletion."""
    src_dir = src_dir_to_render / rel_path
    print(f"\n⚠️  [NON-EMPTY] Host deleted directory '{rel_path}', but source directory contains files!")
    contained_items = (
        sorted(
            to_relative_posix(p, src_dir) + ("/" if p.is_dir() else "")
            for p in src_dir.rglob("*")
        )
        if src_dir.is_dir()
        else []
    )
    if contained_items:
        print("Existing contents in source directory:")
        for item in contained_items[:10]:
            print(f"  - {item}")
        if len(contained_items) > 10:
            print(f"  ... and {len(contained_items) - 10} more item(s)")

    print("Reconciliation options:")
    print("[1] Discard deletion / Restore (preserves source files and restores directory on next deployment)")
    print("[2] Adopt deletion (⚠️  DANGER: permanently deletes entire directory tree and ALL files from source!)")
    print("[3] Skip folder")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        return ItemAdoptOutcome.ADOPTED
    elif choice == "2":
        confirm = input(
            f"⚠️  DANGER: Are you sure you want to permanently delete source directory '{rel_path}' and all {len(contained_items)} item(s)? [y/N]: "
        ).strip().lower()
        if confirm in ("y", "yes"):
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return ItemAdoptOutcome.ADOPTED
        print("Aborted deletion adoption.")
        return ItemAdoptOutcome.SKIPPED
    else:
        return ItemAdoptOutcome.SKIPPED


def _prompt_file_deletion_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
) -> ItemAdoptOutcome:
    """Interactive menu for adopting a file deletion."""
    print(f"\nFound host file deletion: {rel_path}")
    print("Reconciliation options:")
    print("[1] Adopt deletion (deletes source file/template)")
    print("[2] Discard deletion / Restore (restores file in next deployment)")
    print("[3] Skip file")
    choice = input("Select option [1-3]: ").strip()
    if choice == "1":
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    elif choice == "2":
        return ItemAdoptOutcome.ADOPTED
    else:
        return ItemAdoptOutcome.SKIPPED


def _handle_single_directory_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a directory deletion."""
    is_empty = not any((src_dir_to_render / rel_path).iterdir())
    assert_source_file_clean(src_dir_to_render / rel_path, pkg=pkg, force=force)
    if not interactive:
        if is_empty:
            adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
            return ItemAdoptOutcome.ADOPTED
        else:
            logger.warning(
                f"⚠️  [DISCARD] Cannot adopt directory deletion '{rel_path}' because the source directory is not empty. "
                f"Discarding deletion (will restore directory on next deploy)."
            )
            return ItemAdoptOutcome.ADOPTED
    else:
        if is_empty:
            return _prompt_empty_directory_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)
        else:
            return _prompt_non_empty_directory_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)


def _handle_single_file_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a file deletion."""
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    if target_existing_src is None:
        if not interactive:
            logger.warning(f"⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        else:
            print(f"\n⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        return ItemAdoptOutcome.ADOPTED

    assert_source_file_clean(target_existing_src, pkg=pkg, force=force)
    if not interactive:
        adopt_deletion(render_engines, src_dir_to_render, rel_path, pkg=pkg)
        return ItemAdoptOutcome.ADOPTED
    return _prompt_file_deletion_interactive(render_engines, pkg, src_dir_to_render, rel_path)


def handle_single_deletion(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    rel_path: Path,
    interactive: bool,
    force: bool = False,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file or directory deletion."""
    target_src = src_dir_to_render / rel_path
    if target_src.is_dir() and not target_src.is_symlink():
        return _handle_single_directory_deletion(
            render_engines=render_engines,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            rel_path=rel_path,
            interactive=interactive,
            force=force,
        )
    return _handle_single_file_deletion(
        render_engines=render_engines,
        pkg=pkg,
        src_dir_to_render=src_dir_to_render,
        rel_path=rel_path,
        interactive=interactive,
        force=force,
    )


def handle_rename_non_interactive(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    has_patch_conflict: bool,
    accept_conflicts: bool,
    install_file: Optional[Path] = None
) -> ItemAdoptOutcome:
    """Processes a rename drift non-interactively."""
    if patch_content and patch_content.strip():
        logger.debug(f"Diff for rename '{old_rel_path}' -> '{new_rel_path}':\n{patch_content.strip()}")
    new_src_file = adopt_rename(
        render_engines,
        src_dir_to_render,
        old_rel_path,
        new_rel_path,
        patch_content,
        has_patch_conflict=has_patch_conflict,
        install_file=install_file,
        accept_conflicts=accept_conflicts,
    )
    return ItemAdoptOutcome.ADOPTED if new_src_file is not None else ItemAdoptOutcome.CONFLICT


def handle_rename_interactive(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    old_src_file: Path,
    patch_content: str,
    has_patch_conflict: bool
) -> ItemAdoptOutcome:
    """Processes a rename drift interactively."""
    print(f"\nFound host file rename: {old_rel_path} -> {new_rel_path}")
    if patch_content and patch_content.strip():
        print(f"{patch_content.strip()}\n")
    install_file = install_pkg_dir / new_rel_path
    if not has_patch_conflict:
        print("Reconciliation options (Patch applies cleanly):")
        print("[1] Adopt rename (renames template file and applies patch)")
        print("[2] Adopt rename and Edit in Editor (renames, applies patch, then opens template in $EDITOR)")
        print("[3] Open Side-by-Side Reference (renames template, opens template and live file side-by-side)")
        print("[4] Discard rename / Restore (restores file rename in next deployment)")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2", "3"]:
            new_src_file = adopt_rename(
                render_engines,
                src_dir_to_render,
                old_rel_path,
                new_rel_path,
                patch_content,
                has_patch_conflict=False,
                install_file=install_file,
                accept_conflicts=False,
            )
            if not new_src_file:
                return ItemAdoptOutcome.CONFLICT
            if choice == "2":
                try:
                    launch_single_file_editor(new_src_file)
                except RuntimeError as e:
                    logger.warning(f"⚠️  Failed to open editor: {e}.")
                    return ItemAdoptOutcome.SKIPPED
            elif choice == "3":
                return (
                    ItemAdoptOutcome.ADOPTED
                    if fallback_side_by_side(new_src_file, install_file)
                    else ItemAdoptOutcome.SKIPPED
                )
            return ItemAdoptOutcome.ADOPTED
        elif choice == "4":
            return ItemAdoptOutcome.ADOPTED
        else:
            return ItemAdoptOutcome.SKIPPED
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto renamed template file '{old_src_file.name}'!")
        print("Choose fallback resolution strategy:")
        print("[1] Over-render & Freeze (overwrites template, original saved to .bak)")
        print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
        print("[3] Open Side-by-Side Reference (opens template and static drift side-by-side)")
        print("[4] Discard rename / Restore")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2", "3"]:
            # note the patch_content is empty here because we only do rename in this function.
            new_src_file = adopt_rename(
                render_engines,
                src_dir_to_render,
                old_rel_path,
                new_rel_path,
                patch_content="",
                has_patch_conflict=False,
                install_file=install_file,
                accept_conflicts=False,
            )
            if not new_src_file:
                return ItemAdoptOutcome.CONFLICT

            if choice == "1":
                fallback_over_render(new_src_file, install_file)
                return ItemAdoptOutcome.ADOPTED
            elif choice == "2":
                # Adjust patch to new file name
                adjusted_patch = generate_adjusted_patch(
                    install_pkg_dir.parent,
                    pkg,
                    new_rel_path,
                    old_rel_path=old_rel_path,
                    target_src_filename=new_src_file.name
                )
                return (
                    ItemAdoptOutcome.ADOPTED
                    if patch_and_edit(
                        new_src_file,
                        adjusted_patch,
                        install_file=install_file,
                        accept_conflicts=True,
                        open_editor=True,
                    )
                    else ItemAdoptOutcome.SKIPPED
                )
            elif choice == "3":
                return (
                    ItemAdoptOutcome.ADOPTED
                    if fallback_side_by_side(new_src_file, install_file)
                    else ItemAdoptOutcome.SKIPPED
                )

        if choice == "4":
            return ItemAdoptOutcome.ADOPTED
        else:
            return ItemAdoptOutcome.SKIPPED


def handle_single_rename(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template rename."""
    # Check if the target already exists in source
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, new_rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot rename '{old_rel_path}' to '{new_rel_path}' because the target already exists in source. Skipping.")
            return ItemAdoptOutcome.CONFLICT
        print(f"\n⚠️  [CONFLICT] Target file '{new_rel_path}' already exists in source!")
        print("Reconciliation options:")
        print("[1] Discard rename / Restore (restores original on host next deployment)")
        print("[2] Skip file")
        choice = input("Select option [1-2]: ").strip()
        return ItemAdoptOutcome.ADOPTED if choice == "1" else ItemAdoptOutcome.SKIPPED

    old_src_file = resolve_source_file_path(render_engines, src_dir_to_render, old_rel_path)
    has_patch_conflict = False
    if old_src_file and old_src_file.exists():
        assert_source_file_clean(old_src_file, force=force, pkg=pkg)
        if precomputed_patch is not None:
            patch_content = precomputed_patch
        else:
            patch_content = generate_adjusted_patch(
                install_base,
                pkg,
                new_rel_path,
                old_rel_path=old_rel_path,
                target_src_filename=old_src_file.name
            )
        has_patch_conflict = check_patch_conflicts(old_src_file, patch_content)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in package '{pkg}'. Treating as new file addition.")
        return handle_single_addition(
            render_engines, src_pkg_dir, src_dir_to_render, install_pkg_dir, new_rel_path, interactive, force=force
        )

    if not interactive:
        return handle_rename_non_interactive(
            render_engines, src_dir_to_render, old_rel_path, new_rel_path,
            patch_content, has_patch_conflict, accept_conflicts,
            install_file=(install_pkg_dir / new_rel_path)
        )
    else:
        return handle_rename_interactive(
            render_engines, pkg, src_dir_to_render, install_pkg_dir, old_rel_path, new_rel_path,
            old_src_file, patch_content, has_patch_conflict
        )


def handle_modification_non_interactive(
    src_file: Path,
    install_file: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool,
    accept_conflicts: bool
) -> ItemAdoptOutcome:
    """Processes a modification drift non-interactively."""
    if patch_content and patch_content.strip():
        logger.debug(f"Diff for '{src_file.name}':\n{patch_content.strip()}")
    if not has_conflict:
        if is_templated:
            return (
                ItemAdoptOutcome.ADOPTED
                if patch_and_edit(
                    src_file,
                    patch_content,
                    install_file=install_file,
                    accept_conflicts=False,
                    open_editor=False,
                )
                else ItemAdoptOutcome.CONFLICT
            )
        else:
            copy_file(install_file, src_file)
            _sync_file_mode(src_file, install_file)
            return ItemAdoptOutcome.ADOPTED
    else:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into file: '{src_file.name}'")
            return (
                ItemAdoptOutcome.ADOPTED
                if patch_and_edit(
                    src_file,
                    patch_content,
                    install_file=install_file,
                    accept_conflicts=True,
                    open_editor=False,
                )
                else ItemAdoptOutcome.CONFLICT
            )
        else:
            logger.error(f"❌ [CONFLICT] Cannot apply system diff cleanly onto file '{src_file.name}'. Skipping.")
            logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
            return ItemAdoptOutcome.CONFLICT


def handle_modification_interactive(
    src_file: Path,
    install_file: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool
) -> ItemAdoptOutcome:
    """Processes a modification drift interactively."""
    if not has_conflict:
        if is_templated:
            print(f"\nFound modified templated file (Patch applies cleanly): {rel_path}")
            if patch_content and patch_content.strip():
                print(f"{patch_content.strip()}\n")
            print("Reconciliation options:")
            print("[1] Adopt modifications (applies patch cleanly to template)")
            print("[2] Adopt and Edit in Editor (applies patch, then opens template in $EDITOR)")
            print("[3] Open Side-by-Side Reference (opens template and live file side-by-side for manual merge)")
            print("[4] Discard modifications / Restore (restores file in next deployment)")
            print("[5] Skip file")
            choice = input("Select option [1-5]: ").strip()
            if choice in ["1", "2"]:
                return (
                    ItemAdoptOutcome.ADOPTED
                    if patch_and_edit(
                        src_file,
                        patch_content,
                        install_file=install_file,
                        accept_conflicts=False,
                        open_editor=(choice == "2"),
                    )
                    else ItemAdoptOutcome.SKIPPED
                )
            elif choice == "3":
                return (
                    ItemAdoptOutcome.ADOPTED
                    if fallback_side_by_side(src_file, install_file)
                    else ItemAdoptOutcome.SKIPPED
                )
            elif choice == "4":
                return ItemAdoptOutcome.ADOPTED
            else:
                return ItemAdoptOutcome.SKIPPED
        else:
            print(f"\nFound modified static config file (Patch applies cleanly): {rel_path}")
            if patch_content and patch_content.strip():
                print(f"{patch_content.strip()}\n")
            print("Reconciliation options:")
            print("[1] Adopt modifications (overwrites source file)")
            print("[2] Adopt and Edit in Editor (overwrites source file, then opens in $EDITOR)")
            print("[3] Open Side-by-Side Reference (opens source file and live file side-by-side for manual merge)")
            print("[4] Discard modifications / Restore (restores file in next deployment)")
            print("[5] Skip file")
            choice = input("Select option [1-5]: ").strip()
            if choice in ["1", "2"]:
                copy_file(install_file, src_file)
                _sync_file_mode(src_file, install_file)
                if choice == "2":
                    try:
                        launch_single_file_editor(src_file)
                    except RuntimeError as e:
                        logger.warning(f"⚠️  Failed to open editor: {e}.")
                        return ItemAdoptOutcome.SKIPPED
                return ItemAdoptOutcome.ADOPTED
            elif choice == "3":
                return (
                    ItemAdoptOutcome.ADOPTED
                    if fallback_side_by_side(src_file, install_file)
                    else ItemAdoptOutcome.SKIPPED
                )
            elif choice == "4":
                return ItemAdoptOutcome.ADOPTED
            else:
                return ItemAdoptOutcome.SKIPPED
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto file '{src_file.name}'!")
        if patch_content and patch_content.strip():
            print(f"{patch_content.strip()}\n")
        print("Choose a fallback resolution strategy:")
        print("[1] Over-render & Freeze (overwrites template/file, original saved to .bak)")
        print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
        print("[3] Open Side-by-Side Reference (opens file and static drift side-by-side)")
        print("[4] Discard modifications / Restore (restores file in next deployment)")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice == "1":
            fallback_over_render(src_file, install_file)
            return ItemAdoptOutcome.ADOPTED
        elif choice == "2":
            return (
                ItemAdoptOutcome.ADOPTED
                if patch_and_edit(
                    src_file,
                    patch_content,
                    install_file=install_file,
                    accept_conflicts=True,
                    open_editor=True,
                )
                else ItemAdoptOutcome.SKIPPED
            )
        elif choice == "3":
            return (
                ItemAdoptOutcome.ADOPTED
                if fallback_side_by_side(src_file, install_file)
                else ItemAdoptOutcome.SKIPPED
            )
        elif choice == "4":
            return ItemAdoptOutcome.ADOPTED
        else:
            return ItemAdoptOutcome.SKIPPED


def handle_single_modification(
    render_engines: RenderEngineRegistry,
    pkg: str,
    src_pkg_dir: Path,
    src_dir_to_render: Path,
    install_pkg_dir: Path,
    install_base: Path,
    rel_path: Path,
    interactive: bool,
    accept_conflicts: bool,
    force: bool = False,
    precomputed_patch: Optional[str] = None,
) -> ItemAdoptOutcome:
    """Handles drift reconciliation for a single file/template modification."""
    src_file = resolve_source_file_path(render_engines, src_dir_to_render, rel_path)
    pkg_rel_path = Path(pkg) / rel_path
    install_file = install_pkg_dir / rel_path

    if not src_file:
        # Symmetrically handle static file as an addition so the user has full choice in interactive mode.
        return handle_single_addition(
            render_engines, src_pkg_dir, src_dir_to_render,
            install_pkg_dir, rel_path, interactive, force=force
        )

    assert_source_file_clean(src_file, force=force, pkg=pkg)

    is_templated = render_engines.find_engine_for_file(src_file.name) is not None
    if precomputed_patch is not None:
        patch_content = precomputed_patch
    else:
        patch_content = generate_unified_patch(install_base, pkg_rel_path)

    # Check if there are content diff hunks in the patch
    has_content_hunks = check_patch_has_hunks(patch_content)

    if not is_templated:
        # For static files, adopting simply overwrites src_file with install_file (including permissions)
        has_conflict = False
    elif not has_content_hunks:
        # For templated files with only mode/permission changes, apply mode cleanly without patch conflict
        has_conflict = False
    else:
        # For templated files with text changes, check if patch applies cleanly
        has_conflict = check_patch_conflicts(src_file, patch_content)

    if not interactive:
        return handle_modification_non_interactive(
            src_file, install_file, patch_content, is_templated, has_conflict, accept_conflicts
        )
    else:
        return handle_modification_interactive(
            src_file, install_file, install_pkg_dir, rel_path, patch_content, is_templated, has_conflict
        )


# =====================================================================
# Layer 4: Single-Package Drift Adoption
# =====================================================================

def _derive_package_adopt_status(
    has_drifts: bool,
    has_adopted_changes: bool,
    has_unresolved_files: bool,
) -> AdoptStatus:
    """Derives the 5-state AdoptStatus for a single package."""
    if not has_drifts:
        return AdoptStatus.UP_TO_DATE
    if has_adopted_changes and not has_unresolved_files:
        return AdoptStatus.SUCCESS
    if has_adopted_changes and has_unresolved_files:
        return AdoptStatus.PARTIAL
    return AdoptStatus.SKIPPED


def _derive_adopt_result_status(package_results: Sequence[PackageAdoptResult]) -> AdoptStatus:
    """Derives the aggregate 5-state AdoptStatus across all packages."""
    if any(p.status == AdoptStatus.FAILED for p in package_results):
        return AdoptStatus.FAILED
    if not package_results or all(p.status == AdoptStatus.UP_TO_DATE for p in package_results):
        return AdoptStatus.UP_TO_DATE
    any_adopted = any(p.has_adopted_changes for p in package_results)
    any_unresolved = any(p.has_unresolved_files for p in package_results)
    if any_adopted and not any_unresolved:
        return AdoptStatus.SUCCESS
    if any_adopted and any_unresolved:
        return AdoptStatus.PARTIAL
    return AdoptStatus.SKIPPED


def stage_converged_package_drifts(
    workspace_config: WorkspaceConfig,
    plan: PackageAdoptPlan,
) -> List[Path]:
    """Stages planned converged_files for a single package in install/<pkg>/."""
    if not plan.converged_files:
        return []

    stage_args = [(Path(plan.package) / p).as_posix() for p in plan.converged_files]
    res_add = subprocess.run(
        ["git", "-C", str(workspace_config.install_path), "add", "--", *stage_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if res_add.returncode != 0:
        logger.warning(
            f"Staging converged changes for '{plan.package}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
        )
        return []

    logger.info(
        f"✨ Auto-resolved {len(plan.converged_files)} converged file(s) in '{plan.package}': "
        f"{', '.join(p.as_posix() for p in plan.converged_files)}"
    )
    return list(plan.converged_files)


def _dry_run_package_adopt(pkg_plan: PackageAdoptPlan) -> PackageAdoptResult:
    """Builds a pure simulated PackageAdoptResult for a single package in dry-run mode."""
    if not pkg_plan.has_drifts:
        return PackageAdoptResult(
            package=pkg_plan.package,
            status=AdoptStatus.UP_TO_DATE,
            plan=pkg_plan,
        )
    return PackageAdoptResult(
        package=pkg_plan.package,
        adopted_additions=[p.as_posix() for p in pkg_plan.additions],
        adopted_deletions=[p.as_posix() for p in pkg_plan.deletions],
        adopted_modifications=[p.as_posix() for p in pkg_plan.modifications],
        adopted_renames=[f"{old.as_posix()} -> {new.as_posix()}" for old, new in pkg_plan.renames],
        adopted_converged=[p.as_posix() for p in pkg_plan.converged_files],
        status=AdoptStatus.SUCCESS,
        plan=pkg_plan,
    )


def execute_package_adopt(
    workspace_config: WorkspaceConfig,
    plan: PackageAdoptPlan,
    options: Optional[AdoptOptions] = None,
) -> PackageAdoptResult:
    """Executes adoption for a single PackageAdoptPlan, reconciling source files and staging adopted paths.

    Planned converged_files are staged directly without mutating src/, while divergent additions,
    deletions, renames, and modifications are reconciled into src/ and then staged.

    Note:
        This function stages resolved/adopted files ('git add -- <resolved_paths>') in the install/
        repository index, but does NOT commit them. Committing staged changes is performed at
        Layer 5 orchestration (e.g. execute_adopt) or via commit_staged_repo_changes.
    """
    opts = options or AdoptOptions()
    if not plan.has_drifts:
        logger.info(f"✨ Package '{plan.package}' has no drifts.")
        return PackageAdoptResult(
            package=plan.package,
            status=AdoptStatus.UP_TO_DATE,
            plan=plan,
        )

    # Trigger pre_source hook before adopting divergent drifts into source directory
    # (unless already triggered during the pre-adoption render step)
    if plan.has_divergent_drifts and not plan.pre_source_already_triggered:
        trigger_pre_source_hook(
            workspace_config,
            plan.package,
            flags=opts.get_hook_flags(settings=workspace_config.settings),
        )

    adopted_converged: List[str] = [p.as_posix() for p in plan.converged_files]
    adopted_additions: List[str] = []
    adopted_deletions: List[str] = []
    adopted_renames: List[str] = []
    adopted_modifications: List[str] = []
    skipped_files: List[str] = []
    conflicted_files: List[str] = []
    resolved_paths: List[Path] = list(plan.converged_files)

    if plan.converged_files:
        logger.info(
            f"✨ Auto-resolved {len(plan.converged_files)} converged file(s) in '{plan.package}': "
            f"{', '.join(p.as_posix() for p in plan.converged_files)}"
        )

    # 1. Process Additions
    for rel_path in plan.additions:
        outcome = handle_single_addition(
            plan.render_engines, plan.src_pkg_dir, plan.src_dir_to_render, plan.install_pkg_dir,
            rel_path, opts.interactive, force=opts.force
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_additions.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    # 2. Process Deletions
    for rel_path in plan.deletions:
        outcome = handle_single_deletion(
            plan.render_engines, plan.package, plan.src_dir_to_render, rel_path, opts.interactive, force=opts.force
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_deletions.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    # 3. Process Renames
    for old_rel_path, new_rel_path in plan.renames:
        patch_content = plan.rename_patches.get(get_rename_patch_key(old_rel_path, new_rel_path))
        outcome = handle_single_rename(
            plan.render_engines, plan.package, plan.src_pkg_dir, plan.src_dir_to_render,
            plan.install_pkg_dir, workspace_config.install_path,
            old_rel_path, new_rel_path, opts.interactive, opts.accept_conflicts, force=opts.force,
            precomputed_patch=patch_content,
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_renames.append(f"{old_rel_path.as_posix()} -> {new_rel_path.as_posix()}")
            resolved_paths.append(old_rel_path)
            resolved_paths.append(new_rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(old_rel_path.as_posix())
            conflicted_files.append(new_rel_path.as_posix())
        else:
            skipped_files.append(old_rel_path.as_posix())
            skipped_files.append(new_rel_path.as_posix())

    # 4. Process Modifications
    for rel_path in plan.modifications:
        patch_content = plan.mod_patches.get(rel_path.as_posix())
        outcome = handle_single_modification(
            plan.render_engines, plan.package, plan.src_pkg_dir, plan.src_dir_to_render,
            plan.install_pkg_dir, workspace_config.install_path,
            rel_path, opts.interactive, opts.accept_conflicts, force=opts.force,
            precomputed_patch=patch_content,
        )
        if outcome == ItemAdoptOutcome.ADOPTED:
            adopted_modifications.append(rel_path.as_posix())
            resolved_paths.append(rel_path)
        elif outcome == ItemAdoptOutcome.CONFLICT:
            conflicted_files.append(rel_path.as_posix())
        else:
            skipped_files.append(rel_path.as_posix())

    # 5. Staging ONLY resolved files (including converged_files) in install/ repository
    if resolved_paths:
        stage_args = [(Path(plan.package) / p).as_posix() for p in resolved_paths]
        res_add = subprocess.run(
            ["git", "-C", str(workspace_config.install_path), "add", "--", *stage_args],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_add.returncode != 0:
            logger.warning(
                f"Staging adopted changes for '{plan.package}' exited with code {res_add.returncode}: {res_add.stderr.strip()}"
            )

    has_adopted = bool(resolved_paths)
    has_unresolved = bool(skipped_files or conflicted_files)
    status = _derive_package_adopt_status(
        has_drifts=plan.has_drifts,
        has_adopted_changes=has_adopted,
        has_unresolved_files=has_unresolved,
    )
    return PackageAdoptResult(
        package=plan.package,
        adopted_additions=adopted_additions,
        adopted_modifications=adopted_modifications,
        adopted_deletions=adopted_deletions,
        adopted_renames=adopted_renames,
        adopted_converged=adopted_converged,
        skipped_files=skipped_files,
        conflicted_files=conflicted_files,
        status=status,
        plan=plan,
    )


def adopt_one_package_drifts(
    workspace_config: WorkspaceConfig,
    pkg: str,
    options: Optional[AdoptOptions] = None,
) -> PackageAdoptResult:
    """Adopts drifts for a single package using decoupled plan and execution phases.

    Note:
        adopt_one_package_drifts does NOT commit the install/ repo; it only reconciles source files
        and stages adopted paths in the install/ repository index. Committing staged changes is
        the responsibility of Layer 5 orchestration (execute_adopt / run_primitive_adopt_drifts)
        or explicit commit helpers (commit_staged_repo_changes).
    """
    opts = options or AdoptOptions()
    plan = plan_package_adopt(workspace_config, pkg, skip_patches=opts.skip_patches)
    if opts.dry_run:
        return _dry_run_package_adopt(plan)

    return execute_package_adopt(
        workspace_config=workspace_config,
        plan=plan,
        options=opts,
    )


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def _sync_and_render_before_adopt(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str],
    options: AdoptOptions,
) -> Tuple[List[str], List[str]]:
    """Synchronizes host changes into install/ and renders drifted source packages before adoption planning.

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Available source packages resolved via filter_source_packages_by_target.
        options: AdoptOptions controlling reverse-sync, render, and hook behavior.

    Returns:
        Tuple of (drifted_pkgs, rendered_pkgs), both sublists of package_names.
    """
    if not options.no_reverse_sync and package_names:
        run_primitive_1_reverse_sync(
            workspace_config,
            package_names=package_names,
            missing_ok=True,
        )

    drifted_set = set(get_drifted_packages(workspace_config))
    drifted_pkgs = list(filter(drifted_set.__contains__, package_names))
    if options.no_render or not drifted_pkgs:
        return drifted_pkgs, []

    hook_flags = options.get_hook_flags(settings=workspace_config.settings)
    render_res = run_primitive_2_render_packages(
        workspace_config,
        target_pkgs=drifted_pkgs,
        options=RenderOptions.resolve(hook_flags),
    )
    if hook_flags.no_hooks or hook_flags.dry_run:
        return drifted_pkgs, []

    rendered_set = {
        pkg_res.package
        for pkg_res in render_res.packages
        if pkg_res.status in ("SUCCESS", "UP_TO_DATE")
    }
    rendered_pkgs = list(filter(rendered_set.__contains__, drifted_pkgs))
    return drifted_pkgs, rendered_pkgs


def execute_converged_drifts(
    workspace_config: WorkspaceConfig,
    plan: AdoptPlan,
) -> Dict[str, List[Path]]:
    """Executes only the converged-files plan: stages converged files and commits them in install/."""
    converged_by_pkg: Dict[str, List[Path]] = {
        pkg_plan.package: staged_paths
        for pkg_plan in plan.plans
        if pkg_plan.converged_files
        and (staged_paths := stage_converged_package_drifts(workspace_config, pkg_plan))
    }

    if converged_by_pkg:
        converged_pkgs = sorted(converged_by_pkg.keys())
        pkg_word = "package" if len(converged_pkgs) == 1 else "packages"
        commit_staged_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Auto-resolved converged drifts for {pkg_word} {', '.join(converged_pkgs)}",
            repo_name="install repo",
        )

    return converged_by_pkg


def resolve_and_commit_converged_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    dry_run: bool = False,
) -> Dict[str, List[Path]]:
    """Dedicated entry point that plans adoption without reverse-sync, render, or patch generation, and executes only converged files.

    Not called by run_primitive_adopt_drifts (since normal adoption planning and execution
    already handle converged_files directly). Used by deploy_repo after reverse-sync to
    auto-resolve and commit converged files before checking for remaining divergent drift.
    """
    plan = prepare_adopt(
        workspace_config,
        package_names=package_names,
        options=AdoptOptions(
            dry_run=dry_run,
            no_render=True,
            no_reverse_sync=True,
            skip_patches=True,
        ),
    )
    if dry_run:
        return {
            pkg_plan.package: list(pkg_plan.converged_files)
            for pkg_plan in plan.plans
            if pkg_plan.converged_files
        }
    return execute_converged_drifts(workspace_config, plan)


def prepare_adopt(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    options: Optional[AdoptOptions] = None,
) -> AdoptPlan:
    """Synchronizes host changes, renders drifted templates, and creates an AdoptPlan."""
    opts = options or AdoptOptions()
    available_packages = workspace_config.filter_source_packages_by_target(
        target_packages=package_names or None
    )
    drifted_pkgs, rendered_pkgs = _sync_and_render_before_adopt(
        workspace_config,
        package_names=available_packages,
        options=opts,
    )

    resolved_packages = available_packages if package_names else drifted_pkgs
    rendered_pkg_set = set(rendered_pkgs)

    plans = [
        plan_package_adopt(
            workspace_config,
            pkg,
            skip_patches=opts.skip_patches,
            pre_source_already_triggered=(pkg in rendered_pkg_set),
        )
        for pkg in resolved_packages
    ]
    return AdoptPlan(plans=plans, options=opts)


def execute_adopt(
    workspace_config: WorkspaceConfig,
    plan: AdoptPlan,
) -> AdoptResult:
    """Executes adoption for all plans in AdoptPlan, staging adopted/converged files and committing changes."""
    opts = plan.options
    package_results: List[PackageAdoptResult] = [
        execute_package_adopt(
            workspace_config=workspace_config,
            plan=pkg_plan,
            options=opts,
        )
        for pkg_plan in plan.plans
    ]
    adopted_pkgs = [
        p.package for p in package_results
        if p.has_adopted_changes
    ]

    # Commit staged adopted changes in install base
    if adopted_pkgs:
        pkg_word = "package" if len(adopted_pkgs) == 1 else "packages"
        commit_staged_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Resolved and locked drifts for {pkg_word} {', '.join(adopted_pkgs)}",
            repo_name="install repo",
        )

    return AdoptResult(
        command="adopt",
        status=_derive_adopt_result_status(package_results),
        packages=package_results,
        dry_run=False,
    )


def run_primitive_adopt_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    options: Optional[AdoptOptions] = None,
) -> AdoptResult:
    """High-level orchestrator for adopting system drifts back to declarative templates (Primitive Adopt).

    Args:
        workspace_config: The workspace configuration instance.
        package_names: Specific package name(s) to adopt, or empty/omitted to discover all drifted packages.
        options: Optional AdoptOptions controlling interactive mode, conflict markers, force, dry_run,
            pre-adoption rendering, reverse-sync, and hook execution flags.

    Returns:
        AdoptResult containing detailed results for all adopted packages.
    """
    opts = options or AdoptOptions()

    # 1. Preparation (reverse-sync + scoped render + converged/divergent planning)
    plan = prepare_adopt(workspace_config, package_names=package_names, options=opts)
    if not plan.plans or not plan.has_drifts:
        if not opts.dry_run:
            if not package_names:
                logger.info("✨ No drifted packages found in local state database.")
            else:
                for p in plan.plans:
                    logger.info(f"✨ Package '{p.package}' has no drifts.")
        return AdoptResult(
            command="adopt",
            status=AdoptStatus.UP_TO_DATE,
            packages=[
                PackageAdoptResult(package=p.package, status=AdoptStatus.UP_TO_DATE, plan=p)
                for p in plan.plans
            ],
            dry_run=opts.dry_run,
        )

    # 2. Dry-run Mode
    if opts.dry_run:
        dry_run_pkgs = [_dry_run_package_adopt(pkg_plan) for pkg_plan in plan.plans]
        return AdoptResult(
            command="adopt",
            status=_derive_adopt_result_status(dry_run_pkgs),
            packages=dry_run_pkgs,
            dry_run=True,
        )

    # 3. Execution (stages converged_files directly and reconciles divergent drifts)
    return execute_adopt(
        workspace_config=workspace_config,
        plan=plan,
    )

