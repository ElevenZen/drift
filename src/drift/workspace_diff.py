"""Primitive 10: Change Visualization (Diff A, B, and C)."""

import logging
import subprocess
import os
from pathlib import Path
from typing import List, Optional, Tuple

from .workspace_config import WorkspaceConfig

logger = logging.getLogger(__name__)

def run_repo_diff(
    repo_path: Path,
    packages: List[str],
    git_options: List[str],
    managed_files: List[str],
    repo_name: str
) -> None:
    """Helper to run git diff within a specific repository for a set of packages."""
    if not repo_path.exists():
        logger.warning(f"Repository directory does not exist: {repo_path}")
        return

    for pkg in packages:
        # We use pathspecs after '--' to avoid revision ambiguity
        cmd = ["git", "-C", str(repo_path), "diff"] + git_options + ["--", f"{pkg}/"]
        for f in managed_files:
            cmd.append(f":!{pkg}/{f}")
        subprocess.run(cmd, check=False)

def get_pending_delta_worklist(
    workspace_config: WorkspaceConfig,
    packages: List[str]
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
        rel_install = workspace_config.install_directory / pkg
        rel_render = workspace_config.render_directory / pkg
        
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
    packages: List[str],
    git_options: List[str],
    managed_files: List[str]
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
        base_cmd = ["git", "diff", "--no-index"] + git_options
        for pkg, rel_install, rel_render in to_diff:
            cmd = base_cmd + [str(rel_install), str(rel_render), "--"]
            for f in managed_files:
                cmd.append(f":!{f}")
            subprocess.run(cmd, check=False)
    finally:
        os.chdir(old_cwd)

def run_primitive_diff(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    diff_type: str = "pending",  # "template" (A), "system" (B), "pending" (C)
    side_by_side: bool = False,
    stat: bool = False
) -> None:
    """
    Visualizes changes between configuration layers.
    1. Ensures repositories are up-to-date (Transient Render/Reverse-Sync).
    2. Executes appropriate git diff command.
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
    
    if diff_type in ("system", "pending"):
        run_primitive_1_reverse_sync(workspace_config, package_names=packages)
    if diff_type in ("template", "pending"):
        run_primitive_2_render_packages(workspace_config, target_pkgs=packages)

    # Prepare git options
    git_options = ["--color=always"]
    if side_by_side:
        git_options.append("--side-by-side")
    if stat:
        git_options.append("--stat")

    from .constants import MANAGED_CONFIG_FILES

    if diff_type == "template":
        logger.info("🔍 [Diff A] Visualizing Template Evolution (src/ -> render/)...")
        run_repo_diff(workspace_config.render_path, packages, git_options, MANAGED_CONFIG_FILES, "render repo")
            
    elif diff_type == "system":
        logger.info("🔍 [Diff B] Visualizing System Drift (System -> install/)...")
        run_repo_diff(workspace_config.install_path, packages, git_options, MANAGED_CONFIG_FILES, "install repo")
            
    elif diff_type == "pending":
        logger.info("🔍 [Diff Δ] Visualizing Pending Delta (render/ -> install/)...")
        run_pending_delta_diff(workspace_config, packages, git_options, MANAGED_CONFIG_FILES)
