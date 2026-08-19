"""Primitive for auditing and aggregating configuration status across active packages."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Dict

from .workspace_config import WorkspaceConfig
from .render_package import run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync
from .folder_diff import compare_folders, FolderDiff

logger = logging.getLogger(__name__)

class PackageStatus:
    """Aggregated status information for a single package."""
    def __init__(self, name: str):
        self.name = name
        self.template_status: str = "UNKNOWN"
        self.system_status: str = "UNKNOWN"
        self.pending_status: str = "UNKNOWN"
        self.template_changes: List[str] = []
        self.system_changes: List[str] = []
        self.pending_changes: FolderDiff = FolderDiff()

def get_git_status_porcelain(repo_path: Path, pkg_path: Optional[str] = None) -> List[str]:
    """Returns the output of git status --porcelain for a given repository and package path."""
    if not repo_path.exists():
        return []
    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    if pkg_path:
        cmd.append(pkg_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip().splitlines()
    except subprocess.CalledProcessError:
        return []

def run_primitive_status(
    workspace_config: WorkspaceConfig,
    target_pkgs: Optional[List[str]] = None
) -> List[PackageStatus]:
    """
    Orchestrates auditing configuration status across active packages.
    1. Reverse Sync (System -> install/)
    2. Render (src/ -> render/)
    3. Compare (render/ vs install/)
    """
    # 1. Identify packages to audit
    # We want to check everything that exists in src or install
    discovered_in_install = workspace_config.get_package_names_from_dir(workspace_config.install_path)
    discovered_in_src = workspace_config.get_package_names_from_source_dir()
    all_discovered = sorted(list(set(discovered_in_install) | set(discovered_in_src)))
    
    packages = workspace_config.get_packages(all_discovered, target_pkgs)
    if not packages:
        logger.info("No active packages selected for status audit.")
        return []

    logger.info("🔍 Auditing configuration status across active packages...")

    # 2. Status B: System Drift (Reverse Sync)
    # This updates install/ with system changes (uncommitted)
    run_primitive_1_reverse_sync(workspace_config, package_names=packages)
    
    # 3. Status A: Template Status (Render)
    # This updates render/ with src changes (uncommitted)
    run_primitive_2_render_packages(workspace_config, target_pkgs=packages)
    
    # 4. Status Δ: Pending Delta (Compare render/ vs install/)
    results = []
    for pkg in packages:
        status = PackageStatus(pkg)
        
        # A. Template Status (render/ repo status)
        git_status_a = get_git_status_porcelain(workspace_config.render_path, f"{pkg}/")
        if not git_status_a:
            status.template_status = "CLEAN"
        else:
            status.template_status = "MODIFIED"
            status.template_changes = git_status_a
            
        # B. System Status (install/ repo status)
        git_status_b = get_git_status_porcelain(workspace_config.install_path, f"{pkg}/")
        if not git_status_b:
            status.system_status = "CLEAN"
        else:
            status.system_status = "DRIFTED"
            status.system_changes = git_status_b
            
        # Δ. Pending Delta (render/pkg/ vs install/pkg/)
        render_pkg_dir = workspace_config.render_path / pkg
        install_pkg_dir = workspace_config.install_path / pkg
        
        if render_pkg_dir.exists() and install_pkg_dir.exists():
            status.pending_changes = compare_folders(render_pkg_dir, install_pkg_dir)
            # Filter out internally managed files from the pending delta view
            from .constants import IGNORED_FILENAMES
            status.pending_changes.added = [p for p in status.pending_changes.added if p.name not in IGNORED_FILENAMES]
            status.pending_changes.modified = [p for p in status.pending_changes.modified if p.name not in IGNORED_FILENAMES]
            status.pending_changes.deleted = [p for p in status.pending_changes.deleted if p.name not in IGNORED_FILENAMES]

            if not status.pending_changes.added and not status.pending_changes.modified and not status.pending_changes.deleted:
                status.pending_status = "CLEAN"
            else:
                status.pending_status = "STAGED"
        elif render_pkg_dir.exists() and not install_pkg_dir.exists():
            status.pending_status = "NEW"
            from .file_utils import tree_relative_files
            status.pending_changes.added = tree_relative_files(render_pkg_dir)
        elif not render_pkg_dir.exists() and install_pkg_dir.exists():
            status.pending_status = "ORPHAN"
            from .file_utils import tree_relative_files
            status.pending_changes.deleted = tree_relative_files(install_pkg_dir)
        else:
            status.pending_status = "EMPTY"
            
        results.append(status)
        
    return results
