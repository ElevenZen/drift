"""Primitive for auditing and aggregating configuration status across active packages."""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Union

from .workspace_config import WorkspaceConfig
from .render_package import run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync
from .folder_diff import compare_folders, FolderDiff
from .constants import MANAGED_CONFIG_FILES
from .git_utils import get_git_status_porcelain
from .result_models import (
    SerializableModel,
    StatusResult,
    PackageStatusSummary,
    DiffType,
    DiffResult,
    PackageDiffDetail,
    FileDiffDetail,
)

logger = logging.getLogger(__name__)


@dataclass
class PackageStatus(SerializableModel):
    """Aggregated status information for a single package."""
    name: str
    template_status: str = "UNKNOWN"
    system_status: str = "UNKNOWN"
    pending_status: str = "UNKNOWN"
    template_changes: List[str] = field(default_factory=list)
    system_changes: List[str] = field(default_factory=list)
    pending_changes: FolderDiff = field(default_factory=FolderDiff)

    def format_text(self) -> str:
        """Formats the human-readable status block for this package."""
        lines = [f"Package: {self.name}"]
        lines.append(f"  [A] Template: {self.template_status}")
        for change in self.template_changes:
            lines.append(f"      {change}")
        lines.append(f"  [B] System:   {self.system_status}")
        for change in self.system_changes:
            lines.append(f"      {change}")
        lines.append(f"  [Δ] Pending:  {self.pending_status}")
        if self.pending_status not in ("CLEAN", "EMPTY"):
            plus = len(self.pending_changes.added)
            tilde = len(self.pending_changes.modified)
            minus = len(self.pending_changes.deleted)
            lines.append(f"      (+{plus}, ~{tilde}, -{minus} files)")
        return "\n".join(lines)

    def to_summary(self) -> PackageStatusSummary:
        """Converts package status into a serializable summary for JSON output."""
        return PackageStatusSummary(
            name=self.name,
            template_status=self.template_status,
            system_drift_status=self.system_status,
            staging_status=self.pending_status,
            drifted_files=self.system_changes,
            pending_files=[str(p) for p in (self.pending_changes.added + self.pending_changes.modified + self.pending_changes.deleted)]
        )


@dataclass
class WorkspaceStatusResult(SerializableModel):
    """Container representing the aggregated status of the drift workspace across packages."""
    command: str = "status"
    packages: List[PackageStatus] = field(default_factory=list)

    def __iter__(self):
        return iter(self.packages)

    def __len__(self):
        return len(self.packages)

    def __getitem__(self, index):
        return self.packages[index]

    @property
    def overall_status(self) -> str:
        overall = "CLEAN"
        for s in self.packages:
            if s.system_status == "DRIFTED":
                return "DRIFTED"
            elif s.pending_status not in ("CLEAN", "EMPTY") and overall == "CLEAN":
                overall = "PENDING"
            elif s.template_status == "MODIFIED" and overall == "CLEAN":
                overall = "MODIFIED"
        return overall

    def format_text(self) -> str:
        """Formats the human-readable workspace status output."""
        if not self.packages:
            return ""
        return "\n" + "\n\n".join(pkg.format_text() for pkg in self.packages)

    def to_status_result(self) -> StatusResult:
        """Converts to standardized StatusResult model."""
        return StatusResult(
            command=self.command,
            overall_status=self.overall_status,
            packages=[pkg.to_summary() for pkg in self.packages]
        )

    def to_diff_result(self, diff_type: DiffType = DiffType.PENDING) -> DiffResult:
        """Extracts and converts pending changes into DiffResult model for structured diff."""
        diff_pkgs = []
        for s in self.packages:
            files_detail = []
            for p in s.pending_changes.added:
                files_detail.append(FileDiffDetail(path=str(p), change_type="added"))
            for p in s.pending_changes.modified:
                files_detail.append(FileDiffDetail(path=str(p), change_type="modified"))
            for p in s.pending_changes.deleted:
                files_detail.append(FileDiffDetail(path=str(p), change_type="deleted"))
            has_ch = bool(files_detail)
            diff_pkgs.append(PackageDiffDetail(package=s.name, has_changes=has_ch, files=files_detail))
        return DiffResult(command="diff", diff_type=diff_type, packages=diff_pkgs)

    def to_dict(self) -> dict:
        return self.to_status_result().to_dict()

    def to_json(self, indent: int = 2) -> str:
        return self.to_status_result().to_json(indent=indent)


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
) -> WorkspaceStatusResult:
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
        return WorkspaceStatusResult(packages=[])

    logger.info("🔍 Auditing configuration status across active packages...")

    # 2. For Status B: Reverse Sync System Drift
    # This updates install/ with system changes (uncommitted)
    syncable_pkgs = [
        pkg for pkg in packages
        if (workspace_config.install_path / pkg).is_dir()
    ]
    if syncable_pkgs:
        run_primitive_1_reverse_sync(workspace_config, package_names=syncable_pkgs)
    
    # 3. For Status A: Render Template Changes
    # This updates render/ with src changes (uncommitted)
    renderable_pkgs = [
        pkg for pkg in packages
        if (workspace_config.source_path / pkg).is_dir()
    ]
    if renderable_pkgs:
        run_primitive_2_render_packages(workspace_config, target_pkgs=renderable_pkgs)
    
    # 4. Collect and Aggregate Status for Each Package
    results: List[PackageStatus] = []
    for pkg in packages:
        status = PackageStatus(name=pkg)
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
        
    return WorkspaceStatusResult(packages=results)
