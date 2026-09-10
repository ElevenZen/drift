"""Primitive 10: Change Visualization (Diff A, B, and C)."""

import logging
import subprocess
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Union, Sequence

from .constants import DRIFT_GENERATED_FILES
from .workspace_config import WorkspaceConfig
from .result_models import DiffType
from .folder_diff import compare_folders
from .editor_utils import launch_side_by_side_editor

logger = logging.getLogger(__name__)


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
        cmd = ["git", "-C", str(repo_path), "diff", "--name-status", "HEAD", "--", f"{pkg}/"]
        for f in ignored_files:
            cmd.append(f":!{pkg}/{f}")
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        for line in res.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            status, rel_path_str = parts[0], parts[1]
            rel_path = Path(rel_path_str)
            if rel_path.name in ignored_files:
                continue

            working_file = repo_path / rel_path
            head_target = temp_dir / "head" / rel_path
            head_target.parent.mkdir(parents=True, exist_ok=True)

            show_res = subprocess.run(
                ["git", "-C", str(repo_path), "show", f"HEAD:{rel_path_str}"],
                capture_output=True,
                check=False
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
            elif status.startswith("A"):
                empty_left = temp_dir / "empty" / rel_path
                empty_left.parent.mkdir(parents=True, exist_ok=True)
                empty_left.touch()
                pairs.append((empty_left, working_file))
            else:
                pairs.append((head_target, working_file))

    return pairs


def collect_pending_delta_pairs(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    temp_dir: Path,
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
) -> List[Tuple[Path, Path]]:
    """Collects file pairs between install/ (left/deployed) and render/ (right/candidate)."""
    to_diff, _, _ = get_pending_delta_worklist(workspace_config, packages)
    pairs: List[Tuple[Path, Path]] = []

    for pkg, rel_install, rel_render in to_diff:
        install_pkg = workspace_config.drift_root_path / rel_install
        render_pkg = workspace_config.drift_root_path / rel_render

        diff = compare_folders(render_pkg, install_pkg, resolve_symlinks=False)
        for rel_f in diff.modified:
            if rel_f.name not in ignored_files:
                pairs.append((install_pkg / rel_f, render_pkg / rel_f))
        for rel_f in diff.added:
            if rel_f.name not in ignored_files:
                empty_left = temp_dir / "empty" / pkg / rel_f
                empty_left.parent.mkdir(parents=True, exist_ok=True)
                empty_left.touch()
                pairs.append((empty_left, render_pkg / rel_f))
        for rel_f in diff.deleted:
            if rel_f.name not in ignored_files:
                empty_right = temp_dir / "empty" / pkg / rel_f
                empty_right.parent.mkdir(parents=True, exist_ok=True)
                empty_right.touch()
                pairs.append((install_pkg / rel_f, empty_right))

    return pairs


def run_repo_diff(
    repo_path: Path,
    packages: Sequence[str],
    git_options: Sequence[str],
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES,
    repo_name: str = "repo"
) -> None:
    """Helper to run git diff within a specific repository for a set of packages."""
    if not repo_path.exists():
        logger.warning(f"Repository directory does not exist: {repo_path}")
        return

    for pkg in packages:
        # We use pathspecs after '--' to avoid revision ambiguity
        cmd = ["git", "-C", str(repo_path), "diff"] + list(git_options) + ["--", f"{pkg}/"]
        for f in ignored_files:
            cmd.append(f":!{pkg}/{f}")
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.stdout:
            sys.stdout.write(res.stdout)
        if res.stderr:
            sys.stderr.write(res.stderr)


def get_pending_delta_worklist(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str]
) -> Tuple[List[Tuple[str, Path, Path]], List[str], List[str]]:
    """
    Classifies packages based on their presence in render/ and install/ directories.
    Returns (to_diff_list, new_package_names, orphan_package_names).
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


def run_pending_delta_diff(
    workspace_config: WorkspaceConfig,
    packages: Sequence[str],
    git_options: Sequence[str],
    ignored_files: Sequence[str] = DRIFT_GENERATED_FILES
) -> None:
    """Helper to run git diff --no-index between render/ and install/ layers."""
    to_diff, new_pkgs, orphan_pkgs = get_pending_delta_worklist(workspace_config, packages)

    for pkg in new_pkgs:
        logger.info(f"✨ Package '{pkg}' is NEW (exists in render but not install).")
    for pkg in orphan_pkgs:
        logger.info(f"⚠️  Package '{pkg}' is ORPHAN (exists in install but not render).")

    if not to_diff:
        return

    # Change CWD to drift_root to use relative paths in diff headers
    old_cwd = os.getcwd()
    os.chdir(str(workspace_config.drift_root_path))

    try:
        base_cmd = ["git", "diff", "--no-index"] + list(git_options)
        for pkg, rel_install, rel_render in to_diff:
            cmd = base_cmd + [str(rel_install), str(rel_render), "--"]
            for f in ignored_files:
                cmd.append(f":!{f}")
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.stdout:
                sys.stdout.write(res.stdout)
            if res.stderr:
                sys.stderr.write(res.stderr)
    finally:
        os.chdir(old_cwd)


def run_primitive_diff(
    workspace_config: WorkspaceConfig,
    package_names: Sequence[str] = (),
    diff_type: DiffType = DiffType.PENDING,
    side_by_side: bool = False,
    stat: bool = False
) -> None:
    """
    Visualizes changes between configuration layers.
    1. Ensures repositories are up-to-date (Transient Render/Reverse-Sync).
    2. Executes appropriate git diff command or side-by-side editor.
    """
    # Identify target packages
    discovered_in_install = workspace_config.get_package_names_from_dir(workspace_config.install_path)
    discovered_in_src = workspace_config.get_package_names_from_source_dir()
    all_discovered = sorted(list(set(discovered_in_install) | set(discovered_in_src)))

    packages = workspace_config.get_packages(all_discovered, package_names)
    if not packages and package_names:
        logger.warning(f"No packages found matching: {', '.join(package_names)}")
        return

    # Update repositories to reflect latest state
    from .reverse_sync import run_primitive_1_reverse_sync
    from .render_package import run_primitive_2_render_packages

    if diff_type in (DiffType.SYSTEM, DiffType.PENDING):
        syncable = [pkg for pkg in packages if (workspace_config.install_path / pkg).is_dir()]
        if syncable:
            run_primitive_1_reverse_sync(workspace_config, package_names=syncable)
    if diff_type in (DiffType.TEMPLATE, DiffType.PENDING):
        renderable = [pkg for pkg in packages if (workspace_config.source_path / pkg).is_dir()]
        if renderable:
            run_primitive_2_render_packages(workspace_config, target_pkgs=renderable)

    if side_by_side:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            if diff_type == DiffType.TEMPLATE:
                logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
                pairs = collect_repo_diff_pairs(workspace_config.render_path, packages, temp_dir, DRIFT_GENERATED_FILES)
            elif diff_type == DiffType.SYSTEM:
                logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
                pairs = collect_repo_diff_pairs(workspace_config.install_path, packages, temp_dir, DRIFT_GENERATED_FILES)
            elif diff_type == DiffType.PENDING:
                logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
                pairs = collect_pending_delta_pairs(workspace_config, packages, temp_dir, DRIFT_GENERATED_FILES)
            else:
                pairs = []
            launch_side_by_side_editor(pairs)
        return

    # Standard terminal git diff
    git_options = ["--color=always"]
    if stat:
        git_options.append("--stat")

    if diff_type == DiffType.TEMPLATE:
        logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
        run_repo_diff(workspace_config.render_path, packages, git_options, DRIFT_GENERATED_FILES, "render repo")

    elif diff_type == DiffType.SYSTEM:
        logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
        run_repo_diff(workspace_config.install_path, packages, git_options, DRIFT_GENERATED_FILES, "install repo")

    elif diff_type == DiffType.PENDING:
        logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
        run_pending_delta_diff(workspace_config, packages, git_options, DRIFT_GENERATED_FILES)
