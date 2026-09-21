"""Primitive 15: Change Visualization (Diff A, B, and Δ / Pending).

===============================================================================
Architecture & Call Chain Overview
===============================================================================

Layer 5: Primitive Entry Point
    run_primitive_15_workspace_diff(workspace_config, package_names, diff_type, side_by_side, stat)
        State Synchronization:
            run_primitive_1_reverse_sync
            run_primitive_2_render_packages

        Interactive / Visual Mode (side_by_side=True):
            run_side_by_side_diff(workspace_config, packages, diff_type) [Layer 4]
                collect_repo_diff_pairs(repo_path, packages, temp_dir) [Layer 2]
                collect_pending_delta_pairs(workspace_config, packages, temp_dir) [Layer 2]
                    get_pending_delta_worklist(workspace_config, packages) [Layer 1]
                launch_side_by_side_editor(pairs)

        Standard Terminal Mode (side_by_side=False):
            run_terminal_diff(workspace_config, packages, diff_type, stat) [Layer 4]
                run_repo_diff(repo_path, packages, git_options) [Layer 3]
                run_pending_delta_diff(workspace_config, packages, git_options) [Layer 3]
                    get_pending_delta_worklist(workspace_config, packages) [Layer 1]

-------------------------------------------------------------------------------
Layers (ordered bottom-up by dependency):
    Layer 1: Worklist Classification Helpers
        get_pending_delta_worklist
    Layer 2: Side-by-Side Diff Pair Collectors
        collect_repo_diff_pairs
        collect_pending_delta_pairs
    Layer 3: Terminal Git Diff Runners
        run_repo_diff
        run_pending_delta_diff
    Layer 4: Diff Strategy Dispatchers
        run_side_by_side_diff
        run_terminal_diff
    Layer 5: Public Primitive Entry Point
        run_primitive_15_workspace_diff
===============================================================================
"""

import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple, Sequence, Iterable

from ..core.constants import (
    DRIFT_GENERATED_FILES,
    DEFAULT_DIFF_EXCLUDE_PATTERNS,
)
from ..config.workspace_config import WorkspaceConfig
from ..core.result_models import DiffType, DiffResult, PackageDiffDetail, FileDiffDetail
from ..core.folder_diff import compare_folders
from ..utils.file_utils import is_editor_or_os_temporary_file
from ..utils.git_utils import parse_git_status_porcelain
from ..utils.editor_utils import launch_side_by_side_editor

logger = logging.getLogger(__name__)


# =====================================================================
# Layer 1: Worklist Classification Helpers
# =====================================================================

def get_pending_delta_worklist(
    workspace_config: WorkspaceConfig,
    packages: Iterable[str]
) -> Tuple[List[Tuple[str, Path, Path]], List[str], List[str]]:
    """
    Classifies packages based on their presence in render/ and install/ directories.
    Returns (to_diff_list, new_package_names, orphan_package_names).
    to_diff_list contains (package_names, install_pkg_dir, render_pkg_dir) .
    """
    to_diff = []
    new_pkgs = []
    orphan_pkgs = []

    for pkg in packages:
        # Use paths relative to drift_root for diffing
        rel_install = workspace_config.workspace.install_directory / pkg
        rel_render = workspace_config.workspace.render_directory / pkg

        abs_install = workspace_config.install_path / pkg
        abs_render = workspace_config.render_path / pkg

        if abs_install.exists() and abs_render.exists():
            to_diff.append((pkg, rel_install, rel_render))
        elif abs_render.exists():
            new_pkgs.append(pkg)
        elif abs_install.exists():
            orphan_pkgs.append(pkg)

    return to_diff, new_pkgs, orphan_pkgs


# =====================================================================
# Layer 2: Side-by-Side Diff Pair Collectors
# =====================================================================

def collect_repo_diff_pairs(
    repo_path: Path,
    packages: Sequence[str],
    temp_dir: Path,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> List[Tuple[Path, Path]]:
    """
    Finds modified, added, and deleted files in a git repo against HEAD,
    extracts the HEAD version into temp_dir for the left side,
    and pairs it with the working tree file for the right side.
    """
    pairs: List[Tuple[Path, Path]] = []
    if not repo_path.exists():
        return pairs

    for pkg in packages:
        pkg_dir = repo_path / pkg
        if not pkg_dir.exists():
            continue
        cmd = [
            "git", "-C", str(repo_path), "diff", "--name-status", "HEAD", "--",
            f"{pkg}/",
            *(f":!{pkg}/{f}" for f in ignored_files),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            status, rel_path_str = parts[0], parts[1]
            rel_path = Path(rel_path_str)
            if is_editor_or_os_temporary_file(rel_path):
                continue

            working_file = repo_path / rel_path

            if status.startswith("A"):
                empty_left = temp_dir / "empty" / rel_path
                empty_left.parent.mkdir(parents=True, exist_ok=True)
                empty_left.touch()
                pairs.append((empty_left, working_file))
            else:
                head_target = temp_dir / "head" / rel_path
                head_target.parent.mkdir(parents=True, exist_ok=True)

                show_res = subprocess.run(
                    ["git", "-C", str(repo_path), "show", f"HEAD:{rel_path_str}"],
                    capture_output=True,
                    check=False,
                )
                if show_res.returncode == 0:
                    head_target.write_bytes(show_res.stdout)
                else:
                    head_target.touch()

                if status.startswith("D"):
                    empty_right = temp_dir / "empty" / rel_path
                    empty_right.parent.mkdir(parents=True, exist_ok=True)
                    empty_right.touch()
                    pairs.append((head_target, empty_right))
                else:
                    pairs.append((head_target, working_file))

    return pairs


def collect_pending_delta_pairs(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    temp_dir: Path,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> List[Tuple[Path, Path]]:
    """
    Collects file pairs between install/ (left/deployed) and render/ (right/candidate).
    Calls compare_folders instead of 'git diff --no-index' to avoid content-based diffing and focus on file presence and structure.
    NEW packages (render only) appear as all-added pairs; ORPHAN packages (install only) as all-deleted pairs.
    """
    from ..utils.file_utils import tree_relative_files
    to_diff, new_pkgs, orphan_pkgs = get_pending_delta_worklist(workspace_config, packages)
    pairs: List[Tuple[Path, Path]] = []

    def is_valid_file(rel_f: Path) -> bool:
        return rel_f.name not in ignored_files and not is_editor_or_os_temporary_file(rel_f)

    for pkg in new_pkgs:
        render_pkg = workspace_config.render_path / pkg
        for rel_f in filter(is_valid_file, tree_relative_files(render_pkg)):
            empty_left = temp_dir / "empty" / pkg / rel_f
            empty_left.parent.mkdir(parents=True, exist_ok=True)
            empty_left.touch()
            pairs.append((empty_left, render_pkg / rel_f))

    for pkg in orphan_pkgs:
        install_pkg = workspace_config.install_path / pkg
        for rel_f in filter(is_valid_file, tree_relative_files(install_pkg)):
            empty_right = temp_dir / "empty" / pkg / rel_f
            empty_right.parent.mkdir(parents=True, exist_ok=True)
            empty_right.touch()
            pairs.append((install_pkg / rel_f, empty_right))

    for pkg, _, _ in to_diff:
        install_pkg = workspace_config.install_path / pkg
        render_pkg = workspace_config.render_path / pkg

        diff = compare_folders(render_pkg, install_pkg, resolve_symlinks=False)
        for rel_f in filter(is_valid_file, diff.modified):
            pairs.append((install_pkg / rel_f, render_pkg / rel_f))
        for rel_f in filter(is_valid_file, diff.added):
            empty_left = temp_dir / "empty" / pkg / rel_f
            empty_left.parent.mkdir(parents=True, exist_ok=True)
            empty_left.touch()
            pairs.append((empty_left, render_pkg / rel_f))
        for rel_f in filter(is_valid_file, diff.deleted):
            empty_right = temp_dir / "empty" / pkg / rel_f
            empty_right.parent.mkdir(parents=True, exist_ok=True)
            empty_right.touch()
            pairs.append((install_pkg / rel_f, empty_right))

    return pairs


# =====================================================================
# Layer 3: Terminal Git Diff Runners & Structured Diff Collectors
# =====================================================================

def collect_git_repo_diff_details(
    repo_path: Path,
    pkg: str,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> List[FileDiffDetail]:
    """Collects FileDiffDetail list for a package in a git repository against HEAD."""
    diff = parse_git_status_porcelain(repo_path, pkg, ignored_files=ignored_files)
    files: List[FileDiffDetail] = []
    for p in diff.added:
        files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="added"))
    for p in diff.modified:
        files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="modified"))
    for p in diff.deleted:
        files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="deleted"))
    for r in diff.renamed:
        files.append(FileDiffDetail(path=str(Path(pkg) / r.new_path), change_type="renamed", renamed_from=str(Path(pkg) / r.old_path)))
    return files


def collect_pending_folder_diff_details(
    workspace_config: WorkspaceConfig,
    pkg: str,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> List[FileDiffDetail]:
    """Collects FileDiffDetail list between install/ and render/ for a package."""
    render_pkg = workspace_config.render_path / pkg
    install_pkg = workspace_config.install_path / pkg
    files: List[FileDiffDetail] = []

    if render_pkg.exists() and install_pkg.exists():
        diff = compare_folders(render_pkg, install_pkg)
        for p in diff.added:
            if p.name not in ignored_files and not is_editor_or_os_temporary_file(p):
                files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="added"))
        for p in diff.modified:
            if p.name not in ignored_files and not is_editor_or_os_temporary_file(p):
                files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="modified"))
        for p in diff.deleted:
            if p.name not in ignored_files and not is_editor_or_os_temporary_file(p):
                files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="deleted"))
    elif render_pkg.exists() and not install_pkg.exists():
        from ..utils.file_utils import tree_relative_files
        for p in tree_relative_files(render_pkg):
            if p.name not in ignored_files and not is_editor_or_os_temporary_file(p):
                files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="added"))
    elif not render_pkg.exists() and install_pkg.exists():
        from ..utils.file_utils import tree_relative_files
        for p in tree_relative_files(install_pkg):
            if p.name not in ignored_files and not is_editor_or_os_temporary_file(p):
                files.append(FileDiffDetail(path=str(Path(pkg) / p), change_type="deleted"))

    return files


def run_repo_diff(
    repo_path: Path,
    packages: Sequence[str],
    git_options: Sequence[str],
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> bool:
    """Helper to run git diff within a specific repository for a set of packages.
    Returns True if any diff output was produced, False otherwise.
    """
    if not repo_path.exists():
        logger.warning(f"Repository directory does not exist: {repo_path}")
        return False

    had_output = False
    for pkg in packages:
        # We use pathspecs after '--' to avoid revision ambiguity
        cmd = [
            "git", "-C", str(repo_path), "diff",
            *git_options,
            "--", f"{pkg}/",
            *(f":!{pkg}/{f}" for f in ignored_files),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.stdout:
            sys.stdout.write(res.stdout)
            had_output = True
        if res.stderr:
            sys.stderr.write(res.stderr)
    return had_output


def run_pending_delta_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    git_options: Sequence[str],
    exclude_patterns: Sequence[str] = DEFAULT_DIFF_EXCLUDE_PATTERNS,
) -> bool:
    """Helper to run git diff --no-index between render/ and install/ layers.
    NEW packages diff as all-added against an empty directory; ORPHAN packages diff as all-deleted.
    Returns True if any diff output was produced, False otherwise.
    """
    to_diff, new_pkgs, orphan_pkgs = get_pending_delta_worklist(workspace_config, packages)

    had_output = False
    base_cmd = ["git", "diff", "--no-index", *git_options]

    if new_pkgs or orphan_pkgs:
        with tempfile.TemporaryDirectory() as empty_td:
            empty_dir = Path(empty_td)
            for pkg in new_pkgs:
                logger.info(f"✨ Package '{pkg}' is NEW (exists in render but not install).")
                cmd = [*base_cmd, str(empty_dir), str(workspace_config.render_path / pkg), "--", *exclude_patterns]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.stdout:
                    sys.stdout.write(res.stdout)
                    had_output = True
                if res.stderr:
                    sys.stderr.write(res.stderr)
            for pkg in orphan_pkgs:
                logger.info(f"⚠️  Package '{pkg}' is ORPHAN (exists in install but not render).")
                cmd = [*base_cmd, str(workspace_config.install_path / pkg), str(empty_dir), "--", *exclude_patterns]
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.stdout:
                    sys.stdout.write(res.stdout)
                    had_output = True
                if res.stderr:
                    sys.stderr.write(res.stderr)

    for _, rel_install, rel_render in to_diff:
        cmd = [*base_cmd, str(rel_install), str(rel_render), "--", *exclude_patterns]
        res = subprocess.run(
            cmd,
            cwd=str(workspace_config.drift_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.stdout:
            sys.stdout.write(res.stdout)
            had_output = True
        if res.stderr:
            sys.stderr.write(res.stderr)
    return had_output


# =====================================================================
# Layer 4: Diff Strategy Dispatchers
# =====================================================================

def run_side_by_side_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    diff_type: DiffType,
) -> None:
    """Collects file pairs and launches side-by-side visual diff editor."""
    with tempfile.TemporaryDirectory() as td:
        temp_dir = Path(td)
        if diff_type == DiffType.TEMPLATE:
            logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
            pairs = collect_repo_diff_pairs(workspace_config.render_path, packages, temp_dir)
        elif diff_type == DiffType.SYSTEM:
            logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
            pairs = collect_repo_diff_pairs(workspace_config.install_path, packages, temp_dir)
        elif diff_type == DiffType.PENDING:
            logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
            pairs = collect_pending_delta_pairs(workspace_config, packages, temp_dir)
        else:
            pairs = []
        if not pairs:
            logger.info("✨ No differences detected.")
            return
        launch_side_by_side_editor(pairs)


def run_terminal_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    diff_type: DiffType,
    stat: bool = False,
) -> None:
    """Dispatches standard terminal git diffs for Diff A, B, or Δ."""
    git_options = ["--color=always"]
    if stat:
        git_options.append("--stat")

    had_output = False
    if diff_type == DiffType.TEMPLATE:
        logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
        had_output = run_repo_diff(workspace_config.render_path, packages, git_options)

    elif diff_type == DiffType.SYSTEM:
        logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
        had_output = run_repo_diff(workspace_config.install_path, packages, git_options)

    elif diff_type == DiffType.PENDING:
        logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
        had_output = run_pending_delta_diff(workspace_config, packages, git_options)

    if not had_output:
        logger.info("✨ No differences detected.")


# =====================================================================
# Layer 5: Public Primitive Entry Point
# =====================================================================

def run_primitive_15_workspace_diff(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    diff_type: DiffType = DiffType.PENDING,
    side_by_side: bool = False,
    stat: bool = False,
    quiet: bool = False
) -> DiffResult:
    """
    Visualizes changes between configuration layers and returns structured DiffResult.
    1. Ensures repositories are up-to-date (Transient Render/Reverse-Sync).
    2. Executes appropriate git diff command or side-by-side editor if not quiet.
    3. Returns DiffResult containing package and file diff details.
    """
    # Identify target packages
    discovered_in_install = workspace_config.get_package_names_from_dir(workspace_config.install_path)
    discovered_in_src = workspace_config.get_package_names_from_source_dir()
    all_discovered = sorted(set(discovered_in_install) | set(discovered_in_src))

    packages = workspace_config.filter_given_packages_by_target(
        available_packages=all_discovered,
        target_packages=package_names or None,
    )
    if not packages and package_names:
        logger.warning(f"No packages found matching: {', '.join(package_names)}")
        return DiffResult(command="diff", diff_type=diff_type, packages=[])

    # Update repositories to reflect latest state
    from .reverse_sync import run_primitive_1_reverse_sync
    from ..render.render_package import run_primitive_2_render_packages

    if diff_type in (DiffType.SYSTEM, DiffType.PENDING):
        syncable = [pkg for pkg in packages if (workspace_config.install_path / pkg).is_dir()]
        if syncable:
            run_primitive_1_reverse_sync(workspace_config, package_names=syncable)
    if diff_type in (DiffType.TEMPLATE, DiffType.PENDING):
        renderable = [pkg for pkg in packages if (workspace_config.source_path / pkg).is_dir()]
        if renderable:
            run_primitive_2_render_packages(workspace_config, target_pkgs=renderable)

    # Collect structured diff details
    package_details: List[PackageDiffDetail] = []
    for pkg in packages:
        if diff_type == DiffType.TEMPLATE:
            files = collect_git_repo_diff_details(workspace_config.render_path, pkg)
        elif diff_type == DiffType.SYSTEM:
            files = collect_git_repo_diff_details(workspace_config.install_path, pkg)
        elif diff_type == DiffType.PENDING:
            files = collect_pending_folder_diff_details(workspace_config, pkg)
        else:
            files = []
        package_details.append(PackageDiffDetail(package=pkg, has_changes=bool(files), files=files))

    if not quiet:
        if side_by_side:
            run_side_by_side_diff(workspace_config, packages, diff_type)
        else:
            run_terminal_diff(workspace_config, packages, diff_type, stat=stat)

    return DiffResult(command="diff", diff_type=diff_type, packages=package_details)

