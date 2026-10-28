"""Drift Discovery, Convergence Partitioning, and Package Adoption Planning (Layer 1c).

Architecture & Call Chain Overview:
===================================
Discovers uncommitted drifts in `install/`, partitions paths that have already converged
with `render/`, resolves source template counterparts, and pre-computes unified or
3-way de-rendered patches:

1. Drift Discovery & Source Path Resolution:
   - `get_drifted_packages`: Scans `install/` Git status for drifted packages.
   - `resolve_source_file_path`: Maps rendered filenames back to source templates via
     `RenderEngineRegistry`.
   - `assert_source_file_clean`: Guards against overwriting uncommitted modifications
     in `src/<pkg>/` unless `--force` is active.
2. Convergence Detection & `.drift_keep` Normalization:
   - `check_is_drifted_path_converged`, `_check_source_path_exists`,
     `_is_path_converged_with_render`: Compares `install/<pkg>/<rel>` against
     `render/<pkg>/<rel>`.
   - `_normalize_keep_file_drifts`, `_translate_keep_to_dir`,
     `_partition_converged_package_drifts`: Normalizes `.drift_keep` stub files and
     splits converged vs divergent drifts into `PackageDriftFiles`.
3. Patch Pre-Computation & Planning:
   - `_precompute_modification_patch`, `_precompute_rename_patch`,
     `_precompute_package_drift_patches`: Routes templated files through
     `generate_derendered_patch` and static files through `generate_install_git_patch` /
     `generate_retargeted_patch`.
   - `plan_package_adopt`: Builds a complete `PackageAdoptPlan` for a single package.
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
from ..core.exceptions import DirtySourceFileError
from ..models.adopt_models import (
    PackageAdoptPlan,
    PackageDriftFiles,
    get_rename_patch_key,
)
from ..utils.file_inspect import contents_differ, permissions_differ
from ..utils.func_utils import branch, choose, partition, when_none, when_not_none
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
    generate_retargeted_patch,
    rewrite_patch_target_headers,
)

logger = logging.getLogger(__name__)


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


def resolve_source_file_path(
    render_engines: RenderEngineRegistry,
    src_dir_to_render: Path,
    rel_path: Path,
) -> Optional[RenderSourceMatch]:
    """Resolves the source file or template match inside src_dir_to_render using find_source_file_for_rendered_names."""
    return render_engines.find_source_file_for_rendered_names(
        src_dir_to_render / rel_path.parent,
        [rel_path.name],
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
    """Splits .drift_keep renames into deletions+additions and warns on corrupt .drift_keep files.

    - If a .drift_keep file is renamed, it is treated as a deletion of the old path and an addition
      of the new path.
    - Modifications to .drift_keep files are kept in the dataflow so they are automatically discarded
      (reset to 0 bytes and staged) downstream during adoption.
    - Non-empty additions to .drift_keep files are kept in additions so they are treated downstream
      as empty directory additions (and truncated to 0 bytes in install/).
    """
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


def _partition_converged_package_drifts(
    install_pkg_dir: Path,
    render_pkg_dir: Path,
    src_dir_to_render: Path,
    render_engines: RenderEngineRegistry,
    diff: GitStatusChanges,
) -> PackageDriftFiles:
    """Partitions normalized GitStatusChanges into a PackageDriftFiles container."""
    def _is_conv(p: Path, is_del: bool = False) -> bool:
        # .drift_keep files in render/ repo will always be empty,
        # so any non-empty .drift_keep file in install/ repo is considered divergent.
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


def _precompute_modification_patch(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    rel_mod: Path,
    src_match: Optional[RenderSourceMatch] = None,
) -> str:
    """Pre-computes a 3-way de-rendered patch for templated modifications or a raw unified patch."""
    if rel_mod.name == DRIFT_KEEP_FILE_NAME:
        return ""
    resolved_match = when_none(
        src_match,
        resolve_source_file_path(render_engines, src_dir_to_render, rel_mod),
    )
    return branch(
        resolved_match is not None and resolved_match.is_templated,
        lambda: generate_derendered_patch(
            target_file=resolved_match.path,  # type: ignore[union-attr]
            patch_from=install_base / pkg / rel_mod,
            render_pkg_dir=render_pkg_dir,
            rel_rendered_path=rel_mod,
            src_rel_path=to_relative_path(resolved_match.path, src_dir_to_render),  # type: ignore[union-attr]
        ),
        lambda: generate_install_git_patch(install_base, Path(pkg) / rel_mod),
    )


def _precompute_rename_patch(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    old_rel: Path,
    new_rel: Path,
) -> str:
    """Pre-computes a 3-way de-rendered patch for templated renames or a retargeted unified patch."""
    old_src_match = resolve_source_file_path(render_engines, src_dir_to_render, old_rel)
    src_match = when_none(
        old_src_match,
        lambda: resolve_source_file_path(render_engines, src_dir_to_render, new_rel),
    )
    rel_rendered = choose(old_src_match is not None, old_rel, new_rel)
    src_rel_path: Path = when_none(
        when_not_none(src_match, lambda m: to_relative_path(m.path, src_dir_to_render)),
        new_rel,
    )
    return branch(
        src_match is None,
        lambda: rewrite_patch_target_headers(
            generate_install_git_patch(install_base, Path(pkg) / new_rel),
            src_rel_path,
        ),
        lambda: branch(
            src_match.is_templated, # type: ignore[union-attr]
            lambda: generate_derendered_patch(
                target_file=src_match.path,  # type: ignore[union-attr]
                patch_from=install_base / pkg / new_rel,
                render_pkg_dir=render_pkg_dir,
                rel_rendered_path=rel_rendered,
                src_rel_path=src_rel_path,
            ),
            lambda: generate_retargeted_patch(
                install_base=install_base,
                pkg=pkg,
                new_rel_path=new_rel,
                old_rel_path=old_rel,
                patch_target_path=src_rel_path,
            )
        )
    )


def _precompute_package_drift_patches(
    install_base: Path,
    pkg: str,
    src_dir_to_render: Path,
    render_pkg_dir: Path,
    render_engines: RenderEngineRegistry,
    drifts: PackageDriftFiles,
    mod_targets: Optional[Dict[str, Optional[RenderSourceMatch]]] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Pre-computes unified or 3-way de-rendered diff patches for divergent modifications and renames."""
    resolved_targets = mod_targets or {}
    mod_patches = {
        rel_mod.as_posix(): _precompute_modification_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            rel_mod=rel_mod,
            src_match=resolved_targets.get(rel_mod.as_posix()),
        )
        for rel_mod in drifts.modifications
    }
    rename_patches = {
        get_rename_patch_key(old_rel, new_rel): _precompute_rename_patch(
            install_base=install_base,
            pkg=pkg,
            src_dir_to_render=src_dir_to_render,
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            old_rel=old_rel,
            new_rel=new_rel,
        )
        for old_rel, new_rel in drifts.renames
    }
    return mod_patches, rename_patches


def _precompute_modification_conflicts(
    drifts: PackageDriftFiles,
    mod_targets: Dict[str, Optional[RenderSourceMatch]],
    mod_patches: Dict[str, str],
) -> Dict[str, bool]:
    """Evaluates patch conflicts for modifications against their pre-computed patches."""
    def _has_conflict(rel_mod: Path) -> bool:
        key = rel_mod.as_posix()
        match = mod_targets.get(key)
        return branch(
            match is not None,
            lambda: check_patch_conflicts(match.path, mod_patches.get(key, "")),  # type: ignore[union-attr]
            lambda: False,
        )

    return {rel_mod.as_posix(): _has_conflict(rel_mod) for rel_mod in drifts.modifications}


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
            render_pkg_dir=render_pkg_dir,
            render_engines=render_engines,
            pre_source_already_triggered=pre_source_already_triggered,
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
        mod_targets: Dict[str, Optional[RenderSourceMatch]] = {
            rel_mod.as_posix(): branch(
                rel_mod.name == DRIFT_KEEP_FILE_NAME,
                lambda: None,
                lambda rel_mod=rel_mod: resolve_source_file_path(
                    render_engines, src_dir_to_render, rel_mod
                ),
            )
            for rel_mod in drifts.modifications
        }
        mod_patches, rename_patches = branch(
            skip_patches,
            lambda: ({}, {}),
            lambda: _precompute_package_drift_patches(
                install_base=install_base,
                pkg=pkg,
                src_dir_to_render=src_dir_to_render,
                render_pkg_dir=render_pkg_dir,
                render_engines=render_engines,
                drifts=drifts,
                mod_targets=mod_targets,
            ),
        )
        mod_conflicts: Dict[str, bool] = branch(
            skip_patches,
            lambda: {},
            lambda: _precompute_modification_conflicts(drifts, mod_targets, mod_patches),
        )

    return PackageAdoptPlan(
        package=pkg,
        src_pkg_dir=src_pkg_dir,
        src_dir_to_render=src_dir_to_render,
        install_pkg_dir=install_pkg_dir,
        render_pkg_dir=render_pkg_dir,
        render_engines=render_engines,
        drifts=drifts,
        mod_patches=mod_patches,
        rename_patches=rename_patches,
        mod_targets=mod_targets,
        mod_conflicts=mod_conflicts,
        pre_source_already_triggered=pre_source_already_triggered,
    )
