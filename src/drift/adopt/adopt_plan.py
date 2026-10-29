"""Drift Discovery, Category Routing, and Package Adoption Planning (Layer 1c).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Pipeline Architecture:
    1. Package Drift Discovery & Context Resolution (Read-Only):
        get_drifted_packages(workspace_config) [Layer 1]
            - Scans install/ Git status (parse_git_status_porcelain)
            - Intersects changed package directories with installed and source packages
            -> Returns sorted list of drifted package names

    2. Single-Package Adoption Planner:
        plan_package_adopt(workspace_config, pkg, skip_patches, pre_source_already_triggered, force) [Layer 4]
            - Source Context Resolution (_resolve_package_source_context [Layer 1])
                * Resolves (src_dir_to_render, render_engines) via PackageConfig.from_source_dir
            - Early Exit Guard: returns empty PackageAdoptPlan if install/<pkg>/ has no Git changes
            - Scoped Index Staging (with _temporary_stage_package_index(install_base, pkg) [Layer 1]):
                * Stages install/<pkg>/ ('git add --all <pkg>') so Git detects renames & diffs,
                  and restores the index ('git restore --staged -- <pkg>') on exit
                * Stage 1 — Collect & Normalize Raw Git Status:
                    _collect_package_git_status(install_base, install_pkg_dir, pkg) [Layer 1]
                        - parse_git_status_porcelain(install_base, pkg)
                        - _normalize_keep_file_drifts: splits .drift_keep renames into
                          deleted + added and warns on corrupt non-empty/modified .drift_keep files
                        -> Returns normalized GitStatusChanges
                * Stage 2 — Route Drift Categories:
                    _route_package_drifts(install_pkg_dir, render_pkg_dir, src_dir_to_render, render_engines, diff) [Layer 2]
                        - Rename Routing & Decomposition (via resolve_source_file_path):
                            * (old exists in src/ AND new missing in src/) -> kept in renames(old, new)
                            * All other renames decomposed:
                                - old -> deletions (if old exists in src/) or converged_files (if old is absent in src/)
                                - new -> routed as a present-file drift
                        - Deletion Routing (diff.deleted + decomposed old with src/):
                            * _is_path_converged_with_render(..., check_src_for_deletion=True) -> converged_files
                            * Divergent deletions -> deletions (_translate_keep_to_dir)
                        - Present-File Routing (diff.modified + diff.added + decomposed new):
                            * _is_path_converged_with_render(..., check_src_for_deletion=False) -> converged_files
                            * Corrupt .drift_keep modifications -> modifications
                            * Regular files with existing match in src/ (resolve_source_file_path is not None) -> modifications
                            * Files without match in src/ or directory .drift_keep additions -> additions (_translate_keep_to_dir)
                        -> Returns PackageDriftFiles(additions, deletions, modifications, renames, converged_files)
                * Stage 3 — Validate Source Cleanliness & Calculate Patches (when not skip_patches):
                    _precompute_package_drift_patches(install_base, pkg, src_dir_to_render, render_pkg_dir, render_engines, drifts, force) [Layer 3]
                        - Deletion Cleanliness (_assert_deletion_source_clean -> assert_source_file_clean)
                        - Modification Patch Calculation (_precompute_modification_patch):
                            * Asserts src_match exists and calls assert_source_file_clean(src_match.path)
                            * Templated -> generate_derendered_patch [adopt_derender]
                            * Static (untracked/promoted addition with '--- /dev/null') -> generate_source_to_install_patch [adopt_patch]
                            * Static (tracked in install/ HEAD) -> generate_install_git_patch [adopt_patch]
                            * _build_static_adopt_patch (parse_unified_patch_hunks + check_patch_conflicts)
                        - Rename Patch Calculation (_precompute_rename_patch):
                            * Asserts old_src_match exists and calls assert_source_file_clean(old_src_match.path)
                            * Templated -> generate_derendered_patch [adopt_derender]
                            * Static -> generate_install_git_patch [adopt_patch] -> _build_static_adopt_patch
                        -> Returns mod_patches: Dict[str, AdoptPatch]
            -> Returns PackageAdoptPlan(package, src_pkg_dir, src_dir_to_render, install_pkg_dir, render_pkg_dir, render_engines, drifts, mod_patches, pre_source_already_triggered)

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Discovery & Git Status Collection (Stage 1)
        get_drifted_packages
        _resolve_package_source_context
        _temporary_stage_package_index
        _normalize_keep_file_drifts
        _collect_package_git_status
    Layer 2: Source Resolution, Convergence Probing & Category Routing (Stage 2)
        find_source_path_conflict
        resolve_source_file_path
        _translate_keep_to_dir
        _check_source_path_exists
        check_is_drifted_path_converged
        _is_path_converged_with_render
        _route_package_drifts
    Layer 3: Source Cleanliness Validation & Patch Calculation (Stage 3)
        assert_source_file_clean
        _assert_deletion_source_clean
        _build_static_adopt_patch
        _precompute_modification_patch
        _precompute_rename_patch
        _precompute_package_drift_patches
    Layer 4: Package Adoption Planning Entry Point
        plan_package_adopt
===============================================================================
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import subprocess
from typing import Dict, Iterator, List, Optional, Tuple

from ..config.package_config import PackageConfig
from ..config.render_engine_config import RenderEngineRegistry, RenderSourceMatch
from ..config.workspace_config import WorkspaceConfig
from ..core.constants import DRIFT_KEEP_FILE_NAME
from ..core.exceptions import DirtySourceFileError, SafeguardError
from ..models.adopt_models import (
    AdoptPatch,
    PackageAdoptPlan,
    PackageDriftFiles,
    get_rename_patch_key,
)
from ..utils.file_inspect import contents_differ, permissions_differ
from ..utils.func_utils import branch, choose, partition
from ..utils.git_utils import (
    GitStatusChanges,
    get_git_toplevel,
    has_uncommitted_modifications,
    parse_git_status_porcelain,
)
from ..utils.path_utils import to_relative_path
from .adopt_derender import generate_derendered_patch
from .adopt_patch import (
    check_patch_conflicts,
    generate_install_git_patch,
    generate_source_to_install_patch,
    parse_unified_patch_hunks,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# Stage 1: Drift Discovery & Raw Git Status Collection
# ==============================================================================


def get_drifted_packages(workspace_config: WorkspaceConfig) -> List[str]:
    """Scans the install/ repository status to identify which packages have uncommitted drifts.
    Only those whose top-level directory names match both installed packages and source packages are returned.
    """
    diff = parse_git_status_porcelain(workspace_config.install_path)
    changed_names = {p.parts[0] for p in diff.all_paths() if p.parts}

    installed_packages = set(
        workspace_config.get_package_names_from_dir(workspace_config.install_path)
    )
    source_packages = set(workspace_config.get_package_names_from_source_dir())

    drifted = (changed_names & installed_packages) & source_packages
    return sorted(drifted)


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


@contextmanager
def _temporary_stage_package_index(install_base: Path, pkg: str) -> Iterator[None]:
    """Temporarily stages a package in install/ for rename & diff inspection, restoring index on exit."""
    res_add = subprocess.run(
        ["git", "-C", install_base.as_posix(), "add", "--all", pkg],
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
            ["git", "-C", install_base.as_posix(), "restore", "--staged", "--", pkg],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_rst.returncode != 0:
            logger.warning(
                f"Unstaging install changes for '{pkg}' exited with code {res_rst.returncode}: {res_rst.stderr.strip()}"
            )


def _normalize_keep_file_drifts(
    diff: GitStatusChanges,
    install_pkg_dir: Path,
) -> GitStatusChanges:
    """Splits .drift_keep renames into deletions+additions and warns on corrupt .drift_keep files."""
    sorted_renames = sorted(diff.renamed, key=lambda r: r.new_path)
    keep_renames, renames_after_keep = partition(
        lambda r: r.old_path.name == DRIFT_KEEP_FILE_NAME or r.new_path.name == DRIFT_KEEP_FILE_NAME,
        sorted_renames,
    )
    raw_deletions = list(diff.deleted) + [r.old_path for r in keep_renames]
    raw_additions = list(diff.added) + [r.new_path for r in keep_renames]

    keep_mods = [
        rel_mod for rel_mod in diff.modified if rel_mod.name == DRIFT_KEEP_FILE_NAME
    ]
    for rel_mod in keep_mods:
        logger.warning(
            f"⚠️  [CORRUPT] Unexpected modification of keep file '{rel_mod}'. "
            f"'.drift_keep' must always be an empty stub file."
        )

    non_empty_keeps = [
        (rel_add, (install_pkg_dir / rel_add).stat().st_size)
        for rel_add in raw_additions
        if rel_add.name == DRIFT_KEEP_FILE_NAME
        and (install_pkg_dir / rel_add).is_file()
        and (install_pkg_dir / rel_add).stat().st_size > 0
    ]
    for rel_add, size in non_empty_keeps:
        logger.warning(
            f"⚠️  [CORRUPT] Keep file '{rel_add}' is not empty (size={size} bytes). "
            f"'.drift_keep' must always be 0 bytes."
        )

    return GitStatusChanges(
        added=raw_additions,
        modified=list(diff.modified),
        deleted=raw_deletions,
        renamed=renames_after_keep,
    )


def _collect_package_git_status(
    install_base: Path,
    install_pkg_dir: Path,
    pkg: str,
) -> GitStatusChanges:
    """Collects and normalizes Git status changes for a package inside install/."""
    raw_diff = parse_git_status_porcelain(install_base, pkg)
    return _normalize_keep_file_drifts(raw_diff, install_pkg_dir)


# ==============================================================================
# Stage 2: Source Resolution, Convergence Probing & Category Routing
# ==============================================================================


def find_source_path_conflict(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> Optional[RenderSourceMatch]:
    """Finds a matching source file/template (status='match') or blocking path (status='block') inside src_dir_to_render."""
    return render_engines.find_conflict_in_source_dir(src_dir_to_render, rel_path)


def resolve_source_file_path(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> Optional[RenderSourceMatch]:
    """Resolves an exact source file or template match (status='match') inside src_dir_to_render."""
    match = render_engines.find_conflict_in_source_dir(src_dir_to_render, rel_path)
    return choose(match is not None and match.status == "match", match, None)


def _translate_keep_to_dir(p: Path) -> Path:
    """Translates a .drift_keep path to its parent directory path."""
    return choose(
        p.name == DRIFT_KEEP_FILE_NAME and p.parent != Path("."),
        p.parent,
        p,
    )


def _check_source_path_exists(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> bool:
    """Checks whether rel_path or its template counterpart exists in src_dir_to_render.
    If the rel_path is a .drift_keep file, checks the parent directory instead, since .drift_keep is a stub file.
    """
    target_rel = _translate_keep_to_dir(rel_path)
    return (
        find_source_path_conflict(render_engines, src_dir_to_render, target_rel)
        is not None
    )


def check_is_drifted_path_converged(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    rel_path: Path,
    expect_src_exists: bool = False,
) -> bool:
    """Checks whether a drifted package-relative path in install/<pkg>/ has converged with render/<pkg>/."""
    install_file = install_pkg_dir / rel_path
    render_file = render_pkg_dir / rel_path

    install_exists = install_file.exists() or install_file.is_symlink()
    render_exists = render_file.exists() or render_file.is_symlink()

    if not install_exists and not render_exists:
        return not expect_src_exists

    if not install_exists or not render_exists:
        return False

    if install_file.is_symlink() or render_file.is_symlink():
        return False

    if install_file.is_dir() and render_file.is_dir():
        return True

    if install_file.is_file() and render_file.is_file():
        if rel_path.name == DRIFT_KEEP_FILE_NAME:
            return (
                install_file.stat().st_size == 0
                and render_file.stat().st_size == 0
                and not permissions_differ(render_file, install_file)
            )
        return not contents_differ(render_file, install_file) and not permissions_differ(
            render_file, install_file
        )

    return False


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
    expect_src_exists = branch(
        check_src_for_deletion,
        lambda: _check_source_path_exists(render_engines, src_dir_to_render, rel_path),
        lambda: False,
    )
    return check_is_drifted_path_converged(
        install_pkg_dir,
        render_pkg_dir,
        rel_path,
        expect_src_exists=expect_src_exists,
    )


def _route_package_drifts(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    src_dir_to_render: Path,
    render_engines: RenderEngineRegistry,
    diff: GitStatusChanges,
) -> PackageDriftFiles:
    """Routes normalized GitStatusChanges into converged and divergent PackageDriftFiles categories.

    Routing Rules:
    1. Renames (`old -> new`):
       - `old` exists in `src/` and `new` does not exist in `src/`: kept as `renames(old, new)`.
       - Otherwise decomposed:
         * `old` routes to `deletions` if `old` exists in `src/`, or `converged_files` if `old` is absent in `src/`.
         * `new` routes as a present-file drift (`converged_files`, `modifications`, or `additions`).
    2. Deletions (`diff.deleted` + decomposed `old` where `old` exists in `src/`):
       - Converged with `render/` (absent in `install/`, `render/`, and `src/`) -> `converged_files`.
       - Otherwise -> `deletions` (with `.drift_keep` translated to its parent directory).
    3. Present-file drifts (`diff.added`, `diff.modified`, and decomposed `new`):
       - Converged with `render/` -> `converged_files`.
       - Corrupt `.drift_keep` modifications -> `modifications`.
       - Regular file with an existing match in `src/` (`resolve_source_file_path` is not None) -> `modifications`.
       - Otherwise (no match in `src/` or directory `.drift_keep` addition) -> `additions` (with `.drift_keep` translated to its parent directory).
    """
    def _has_src_file(p: Path) -> bool:
        return (
            p.name != DRIFT_KEEP_FILE_NAME
            and resolve_source_file_path(render_engines, src_dir_to_render, p) is not None
        )

    def _is_conv(p: Path, is_del: bool = False) -> bool:
        return _is_path_converged_with_render(
            install_pkg_dir,
            render_pkg_dir,
            p,
            render_engines=render_engines,
            src_dir_to_render=src_dir_to_render,
            check_src_for_deletion=is_del,
        )

    # 1. Route renames: keep only (old exists in src/ AND new missing in src/); decompose all others
    pure_renames, decomposed_renames = partition(
        lambda r: _has_src_file(r.old_path) and not _has_src_file(r.new_path),
        diff.renamed,
    )
    decomposed_old_with_src, decomposed_old_without_src = partition(
        _has_src_file,
        (r.old_path for r in decomposed_renames),
    )

    # 2. Route deletions
    conv_dels, div_dels = partition(
        lambda p: _is_conv(p, is_del=True),
        (*diff.deleted, *decomposed_old_with_src),
    )

    # 3. Route present-file drifts (modified + added + decomposed rename targets)
    present_candidates = (
        *diff.modified,
        *diff.added,
        *(r.new_path for r in decomposed_renames),
    )
    conv_present, div_present = partition(
        lambda p: _is_conv(p, is_del=False),
        present_candidates,
    )
    modified_keep_set = {
        p for p in diff.modified if p.name == DRIFT_KEEP_FILE_NAME
    }
    routed_mods, routed_adds = partition(
        lambda p: p in modified_keep_set or (
            (install_pkg_dir / p).is_file() and _has_src_file(p)
        ),
        div_present,
    )

    converged_files = sorted(
        set(conv_dels)
        | set(conv_present)
        | set(decomposed_old_without_src)
    )
    additions = sorted({_translate_keep_to_dir(p) for p in routed_adds})
    deletions = sorted({_translate_keep_to_dir(p) for p in div_dels})
    modifications = list(dict.fromkeys(routed_mods))
    renames = [(r.old_path, r.new_path) for r in pure_renames]

    return PackageDriftFiles(
        additions=additions,
        deletions=deletions,
        modifications=modifications,
        renames=renames,
        converged_files=converged_files,
    )


# ==============================================================================
# Stage 3: Source Cleanliness Validation & Patch Calculation
# ==============================================================================


def assert_source_file_clean(
    src_file: Path,
    pkg: str,
    force: bool = False,
) -> None:
    """Verifies that a target source file is clean before adopting drifts into it."""
    if not src_file.exists():
        return

    try:
        git_root = get_git_toplevel(src_file.parent)
    except Exception:
        git_root = src_file.parent

    if not has_uncommitted_modifications(git_root, src_file):
        return

    if force:
        logger.warning(
            f"⚠️  [FORCE] Bypassing Git cleanliness safeguard for '{src_file.name}'. Overwriting uncommitted modifications."
        )
        return

    rel_dirty = to_relative_path(src_file, git_root)

    raise DirtySourceFileError(
        f"The source file '{rel_dirty}' has uncommitted local modifications!\n"
        f"Adopting system drift would overwrite uncommitted changes in this file.\n\n"
        f"👉 Run 'drift adopt {pkg} --force' (or -f) to overwrite existing file modifications.\n"
        f"👉 Or commit / stash your changes before adopting.",
        packages=[pkg],
        source_file=src_file,
    )


def _assert_deletion_source_clean(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    pkg: str,
    rel_del: Path,
    force: bool = False,
) -> None:
    """Asserts that the source file or directory targeted by a deletion drift is clean in Git."""
    target_src = src_dir_to_render / rel_del
    if target_src.is_dir() and not target_src.is_symlink():
        assert_source_file_clean(target_src, pkg=pkg, force=force)
        return
    target_existing_src = resolve_source_file_path(render_engines, src_dir_to_render, rel_del)
    if target_existing_src is not None:
        assert_source_file_clean(target_existing_src.path, pkg=pkg, force=force)


def _build_static_adopt_patch(
    raw_diff: str,
    src_match: RenderSourceMatch,
    patch_from: Path = Path(),
) -> AdoptPatch:
    """Parses a static file's raw unified diff into structured CleanPatchHunks with conflict probing."""
    clean_hunks = parse_unified_patch_hunks(raw_diff)
    prelim_patch = AdoptPatch(
        src_match=src_match,
        patch_from=patch_from,
        clean_hunks=clean_hunks,
        conflict_clusters=(),
        display_diff=raw_diff,
    )
    has_conflict = branch(
        src_match.path.exists(),
        lambda: check_patch_conflicts(src_match.path, prelim_patch),
        lambda: False,
    )
    return AdoptPatch(
        src_match=src_match,
        patch_from=patch_from,
        has_conflict=has_conflict,
        clean_hunks=clean_hunks,
        conflict_clusters=(),
        display_diff=raw_diff,
    )


def _precompute_modification_patch(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    rel_mod: Path,
    force: bool = False,
) -> AdoptPatch:
    """Pre-computes a 3-way de-rendered or static AdoptPatch for a modified file."""
    patch_from = install_base / pkg / rel_mod
    if rel_mod.name == DRIFT_KEEP_FILE_NAME:
        return AdoptPatch(patch_from=patch_from)
    src_match = resolve_source_file_path(render_engines, src_dir_to_render, rel_mod)
    if src_match is None:
        raise SafeguardError(
            f"Cannot precompute modification patch for '{rel_mod}' in package '{pkg}': "
            f"source file for '{rel_mod}' does not exist."
        )
    assert_source_file_clean(src_match.path, pkg=pkg, force=force)
    src_rel_path = to_relative_path(src_match.path, src_dir_to_render)

    def _compute_static_diff() -> str:
        raw_git_diff = generate_install_git_patch(install_base, Path(pkg) / rel_mod)
        # check if the diff is a promoted addition (untracked in install/ HEAD) by looking for the /dev/null marker
        if (
            "\n--- /dev/null\n" in raw_git_diff
            or raw_git_diff.startswith("--- /dev/null\n")
        ):
            return generate_source_to_install_patch(
                src_file=src_match.path,
                install_file=patch_from,
            )
        return raw_git_diff

    return branch(
        src_match.is_templated,
        lambda: generate_derendered_patch(
            target_file=src_match.path,
            patch_from=patch_from,
            render_pkg_dir=render_pkg_dir,
            rel_rendered_path=rel_mod,
            src_rel_path=src_rel_path,
            src_match=src_match,
        ),
        lambda: _build_static_adopt_patch(
            _compute_static_diff(),
            src_match=src_match,
            patch_from=patch_from,
        ),
    )


def _precompute_rename_patch(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    old_rel: Path,
    new_rel: Path,
    force: bool = False,
) -> AdoptPatch:
    """Pre-computes a 3-way de-rendered or static AdoptPatch for a renamed source file."""
    patch_from = install_base / pkg / new_rel
    old_src_match = resolve_source_file_path(render_engines, src_dir_to_render, old_rel)
    if old_src_match is None:
        raise SafeguardError(
            f"Cannot precompute rename patch for '{old_rel} -> {new_rel}' in package '{pkg}': "
            f"source file for '{old_rel}' does not exist."
        )
    assert_source_file_clean(old_src_match.path, pkg=pkg, force=force)
    src_rel_path = to_relative_path(old_src_match.path, src_dir_to_render)
    return branch(
        old_src_match.is_templated,
        lambda: generate_derendered_patch(
            target_file=old_src_match.path,
            patch_from=patch_from,
            render_pkg_dir=render_pkg_dir,
            rel_rendered_path=old_rel,
            src_rel_path=src_rel_path,
            src_match=old_src_match,
        ),
        lambda: _build_static_adopt_patch(
            generate_install_git_patch(
                install_base,
                Path(pkg) / new_rel,
                old_rel_path=Path(pkg) / old_rel,
            ),
            src_match=old_src_match,
            patch_from=patch_from,
        ),
    )


def _precompute_package_drift_patches(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    drifts: PackageDriftFiles,
    force: bool = False,
) -> Dict[str, AdoptPatch]:
    """Asserts source cleanliness for divergent targets and pre-computes structured AdoptPatch objects."""
    for rel_del in drifts.deletions:
        _assert_deletion_source_clean(
            render_engines=render_engines,
            src_dir_to_render=src_dir_to_render,
            pkg=pkg,
            rel_del=rel_del,
            force=force,
        )
    mod_entries = {
        rel_mod.as_posix(): _precompute_modification_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            rel_mod=rel_mod,
            force=force,
        )
        for rel_mod in drifts.modifications
    }
    rename_entries = {
        get_rename_patch_key(old_rel, new_rel): _precompute_rename_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            old_rel=old_rel,
            new_rel=new_rel,
            force=force,
        )
        for old_rel, new_rel in drifts.renames
    }
    return {**mod_entries, **rename_entries}


# ==============================================================================
# Entry Point: Single-Package Adoption Planning
# ==============================================================================


def plan_package_adopt(
    workspace_config: WorkspaceConfig,
    pkg: str,
    skip_patches: bool = False,
    pre_source_already_triggered: bool = False,
    force: bool = False,
) -> PackageAdoptPlan:
    """Plans drift adoption for a single package via a 3-stage pipeline:
    1. Collect & normalize raw Git status in install/<pkg>/.
    2. Route drifts into converged_files, additions, deletions, modifications, and renames.
    3. Validate source file cleanliness and pre-compute unified/de-rendered AdoptPatch objects.
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
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            pre_source_already_triggered=pre_source_already_triggered,
        )

    with _temporary_stage_package_index(install_base, pkg):
        normalized_diff = _collect_package_git_status(
            install_base=install_base,
            install_pkg_dir=install_pkg_dir,
            pkg=pkg,
        )
        routed_drifts = _route_package_drifts(
            install_pkg_dir=install_pkg_dir,
            render_pkg_dir=render_pkg_dir,
            src_dir_to_render=src_dir_to_render,
            render_engines=render_engines,
            diff=normalized_diff,
        )
        mod_patches: Dict[str, AdoptPatch] = branch(
            skip_patches,
            lambda: {},
            lambda: _precompute_package_drift_patches(
                install_base=install_base,
                pkg=pkg,
                src_dir_to_render=src_dir_to_render,
                render_pkg_dir=render_pkg_dir,
                render_engines=render_engines,
                drifts=routed_drifts,
                force=force,
            ),
        )

    return PackageAdoptPlan(
        package=pkg,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        render_pkg_dir=render_pkg_dir,
        render_engines=render_engines,
        drifts=routed_drifts,
        mod_patches=mod_patches,
        pre_source_already_triggered=pre_source_already_triggered,
    )
