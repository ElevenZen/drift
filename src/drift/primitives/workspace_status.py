"""Primitive for auditing and aggregating configuration status across active packages."""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Sequence

from ..config.workspace_config import WorkspaceConfig
from ..render.render_package import run_primitive_2_render_packages
from .reverse_sync import run_primitive_1_reverse_sync
from ..core.folder_diff import compare_folders, FolderDiff
from ..core.constants import DRIFT_GENERATED_FILES
from ..utils.git_utils import parse_git_status_porcelain, GitStatusDiff
from ..core.state_registry import load_state_registry
from ..core.result_models import PackageStatus, StatusResult

logger = logging.getLogger(__name__)

# Backward-compatibility alias
WorkspaceStatusResult = StatusResult


def audit_repo_package_status(
    repo_path: Path,
    pkg: str,
    dirty_label: str
) -> Tuple[str, Optional[GitStatusDiff]]:
    """Audits a package's git repository status in render/ or install/."""
    pkg_dir = repo_path / pkg
    if not pkg_dir.exists():
        return "EMPTY", None

    res_tracked = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", f"{pkg}/"],
        capture_output=True, text=True, check=False
    )
    if not res_tracked.stdout.strip():
        return "NEW", None

    git_status = parse_git_status_porcelain(repo_path, pkg)
    if not git_status.has_changes:
        return "CLEAN", None
    return dirty_label, git_status


def calculate_pending_delta(
    render_pkg_dir: Path,
    install_pkg_dir: Path
) -> Tuple[str, Optional[FolderDiff]]:
    """Calculates the pending delta between render/ and install/ for a package."""
    if render_pkg_dir.exists() and install_pkg_dir.exists():
        diff = compare_folders(render_pkg_dir, install_pkg_dir)
        # Filter out internally generated synthetic files from the pending delta view
        diff.added = [p for p in diff.added if p.name not in DRIFT_GENERATED_FILES]
        diff.modified = [p for p in diff.modified if p.name not in DRIFT_GENERATED_FILES]
        diff.deleted = [p for p in diff.deleted if p.name not in DRIFT_GENERATED_FILES]

        if not diff.added and not diff.modified and not diff.deleted:
            return "CLEAN", None
        else:
            return "STAGED", diff

    elif render_pkg_dir.exists() and not install_pkg_dir.exists():
        return "NEW", None

    elif not render_pkg_dir.exists() and install_pkg_dir.exists():
        return "ORPHAN", None

    else:
        return "EMPTY", None


def build_list_only_status(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = ()
) -> StatusResult:
    """Builds fast status result querying only StateRegistry metadata without render/diff."""
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    if target_pkgs:
        selected_pkgs = list(target_pkgs)
    else:
        selected_pkgs = sorted([
            pkg for pkg, s in state_registry.packages.items()
            if s.state == "installed"
        ])

    results: List[PackageStatus] = []
    for pkg in selected_pkgs:
        status = PackageStatus(
            name=pkg,
            template_status="UNKNOWN",
            system_status="UNKNOWN",
            pending_status="UNKNOWN",
        )
        pkg_state = state_registry.packages.get(pkg)
        if pkg_state:
            status.state = pkg_state.state
            status.target_directory = str(pkg_state.target_directory) if pkg_state.target_directory else None
            status.install_method = pkg_state.install_method.value if pkg_state.install_method else None
            status.last_deployed = pkg_state.last_deployed
            status.deployed_files_count = len(pkg_state.deployed_files)
        results.append(status)

    return StatusResult(
        command="status",
        overall_status="UNKNOWN",
        list_only=True,
        packages=results
    )


def build_full_status(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = ()
) -> StatusResult:
    """Runs complete status audit across active packages with reverse-sync and render."""
    discovered_in_install = workspace_config.get_package_names_from_dir(workspace_config.install_path)
    discovered_in_src = workspace_config.get_package_names_from_source_dir()
    all_discovered = sorted(list(set(discovered_in_install) | set(discovered_in_src)))

    packages = workspace_config.filter_given_packages_by_target(
        available_packages=all_discovered,
        target_packages=target_pkgs or None,
    )
    if not packages:
        logger.info("No active packages selected for status audit.")
        return StatusResult(packages=[])

    logger.info("🔍 Auditing configuration status across active packages...")

    # 1. For Status B: Reverse Sync System Drift
    syncable_pkgs = [
        pkg for pkg in packages
        if (workspace_config.install_path / pkg).is_dir()
    ]
    if syncable_pkgs:
        run_primitive_1_reverse_sync(workspace_config, package_names=syncable_pkgs)

    # 2. For Status A: Render Template Changes
    renderable_pkgs = [
        pkg for pkg in packages
        if (workspace_config.source_path / pkg).is_dir()
    ]
    if renderable_pkgs:
        run_primitive_2_render_packages(workspace_config, target_pkgs=renderable_pkgs)

    # 3. Query State Registry
    state_file = workspace_config.install_path / "state.toml"
    state_registry = load_state_registry(state_file)

    # 4. Collect and Aggregate Status for Each Package
    results: List[PackageStatus] = []
    overall = "CLEAN"
    for pkg in packages:
        status = PackageStatus(name=pkg)
        results.append(status)

        pkg_state = state_registry.packages.get(pkg)
        if pkg_state:
            status.state = pkg_state.state
            status.target_directory = str(pkg_state.target_directory) if pkg_state.target_directory else None
            status.install_method = pkg_state.install_method.value if pkg_state.install_method else None
            status.last_deployed = pkg_state.last_deployed
            status.deployed_files_count = len(pkg_state.deployed_files)

        # Status A: Template Status
        status.template_status, status.template_changes = audit_repo_package_status(
            workspace_config.render_path, pkg, dirty_label="MODIFIED"
        )

        # Status B: System Status
        status.system_status, status.system_changes = audit_repo_package_status(
            workspace_config.install_path, pkg, dirty_label="DRIFTED"
        )

        # Status Δ: Pending Delta
        render_pkg_dir = workspace_config.render_path / pkg
        install_pkg_dir = workspace_config.install_path / pkg
        status.pending_status, status.pending_changes = calculate_pending_delta(
            render_pkg_dir, install_pkg_dir
        )

        # Compute overall status
        if status.system_status == "DRIFTED":
            overall = "DRIFTED"
        elif status.pending_status not in ("CLEAN", "EMPTY", "NEW", "ORPHAN") and overall == "CLEAN":
            overall = "PENDING"
        elif status.template_status == "MODIFIED" and overall == "CLEAN":
            overall = "MODIFIED"

    return StatusResult(
        command="status",
        overall_status=overall,
        list_only=False,
        packages=results
    )


def run_primitive_status(
    workspace_config: WorkspaceConfig,
    target_pkgs: Sequence[str] = (),
    list_only: bool = False
) -> StatusResult:
    """Orchestrates configuration status audit or fast metadata listing."""
    if list_only:
        return build_list_only_status(workspace_config, target_pkgs=target_pkgs)
    return build_full_status(workspace_config, target_pkgs=target_pkgs)
