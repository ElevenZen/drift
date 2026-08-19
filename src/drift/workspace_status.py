"""Primitive for auditing and aggregating configuration status across active packages."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Tuple

from .workspace_config import WorkspaceConfig
from .render_package import run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync
from .folder_diff import compare_folders, FolderDiff
from .constants import MANAGED_CONFIG_FILES
from .git_utils import get_git_status_porcelain

logger = logging.getLogger(__name__)

class PackageStatus:
    """Aggregated status information for a single package."""
    def __init__(self, name: str):
        self.name = name
        self.template_status: str = "UNKNOWN"
        self.system_status: str = "UNKNOWN"
        self.pending_status: str = "UNKNOWN"
        # Diff-A: Git Status output for render changes
        self.template_changes: List[str] = []
        # Diff-B: Git Status output for system drifts
        self.system_changes: List[str] = []
        # Diff-C: Pending changes between render/ and install/
        self.pending_changes: FolderDiff = FolderDiff()


def calculate_pending_delta(
    render_pkg_dir: Path,
    install_pkg_dir: Path
) -> Tuple[str, FolderDiff]:
    """Calculates the pending delta between render/ and install/ for a package."""
    if render_pkg_dir.exists() and install_pkg_dir.exists():
        diff = compare_folders(render_pkg_dir, install_pkg_dir)
        # Filter out internally managed files from the pending delta view
        diff.added = [p for p in diff.added if p.name not in MANAGED_CONFIG_FILES]
        diff.modified = [p for p in diff.modified if p.name not in MANAGED_CONFIG_FILES]
        diff.deleted = [p for p in diff.deleted if p.name not in MANAGED_CONFIG_FILES]

        if not diff.added and not diff.modified and not diff.deleted:
            return "CLEAN", diff
        else:
            return "STAGED", diff
            
    elif render_pkg_dir.exists() and not install_pkg_dir.exists():
        from .file_utils import tree_relative_files
        diff = FolderDiff(added=tree_relative_files(render_pkg_dir))
        return "NEW", diff
        
    elif not render_pkg_dir.exists() and install_pkg_dir.exists():
        from .file_utils import tree_relative_files
        diff = FolderDiff(deleted=tree_relative_files(install_pkg_dir))
        return "ORPHAN", diff
        
    else:
        return "EMPTY", FolderDiff()


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

    # 2. For Status B: Reverse Sync System Drift
    # This updates install/ with system changes (uncommitted)
    run_primitive_1_reverse_sync(workspace_config, package_names=packages)
    
    # 3. For Status A: Render Template Changes
    # This updates render/ with src changes (uncommitted)
    run_primitive_2_render_packages(workspace_config, target_pkgs=packages)
    
    # 4. Collect and Aggregate Status for Each Package
    results = []
    for pkg in packages:
        status = PackageStatus(pkg)
        results.append(status)
        
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
        
        status.pending_status, status.pending_changes = calculate_pending_delta(render_pkg_dir, install_pkg_dir)
        
    return results
