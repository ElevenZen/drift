"""Core action implementations for drift CLI backend triggers using pathlib."""

import logging
from pathlib import Path
from typing import Optional, List

from ..constants import CONFIG_DIR_NAME, GLOBAL_CONFIG_FILE_NAME
from ..workspace_config import WorkspaceConfig, load_workspace_config
from ..workspace_init import init_drift_workspace
from ..git_utils import get_drift_root

logger = logging.getLogger(__name__)

# Disable unused import warning for get_drift_root, as it may be used in CLI backends.
_ = get_drift_root


def load_workspace_config_default(drift_root: Path) -> WorkspaceConfig:
    config_path = drift_root / CONFIG_DIR_NAME / GLOBAL_CONFIG_FILE_NAME
    workspace_config = load_workspace_config(config_path)
    return workspace_config


def execute_init(drift_root: Path, force: bool = False, no_git_root: bool = False) -> None:
    """Core function to initialize a drift workspace, shared by both CLI backends."""
    init_drift_workspace(drift_root, force=force, no_git_root=no_git_root)


def execute_render(drift_root: Path, package_names: Optional[List[str]] = None) -> None:
    """Core function to execute template rendering, shared by both CLI backends."""
    from ..render_package import run_primitive_2_render_packages

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_2_render_packages(workspace_config, target_pkgs=package_names)


def execute_stage(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False) -> None:
    """Core function to execute staging from render to install, shared by both CLI backends."""
    from ..stage_repo import run_primitive_4_stage_render_to_install

    workspace_config = load_workspace_config_default(drift_root)
    changes = run_primitive_4_stage_render_to_install(workspace_config, target_pkgs=package_names, force=force)
    if not changes:
        logger.info("No changes staged. All files are up-to-date.")
    else:
        for pkg_change in changes:
            logger.info(f"Package '{pkg_change.package_name}' staged changes:")
            for file in pkg_change.added_files:
                logger.info(f"  [+] {file.as_posix()}")
            for file in pkg_change.modified_files:
                logger.info(f"  [*] {file.as_posix()}")
            for file in pkg_change.deleted_files:
                logger.info(f"  [-] {file.as_posix()}")


def execute_apply(drift_root: Path, package_names: Optional[List[str]] = None, force: bool = False) -> None:
    """Core function to execute state application (apply), shared by both CLI backends."""
    from ..install_repo import run_primitive_5_install_deployment

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_5_install_deployment(
        workspace_config=workspace_config,
        packages_to_redeploy=package_names,
        resolve_symlinks=True,
        force=force,
        package_changes=None
    )


def execute_render_commit(drift_root: Path, message: str, package_names: Optional[List[str]] = None) -> None:
    """Core function to execute committing render repository changes, shared by both CLI backends."""
    from ..render_package import run_primitive_3_commit_render_repo

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_3_commit_render_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_install_commit(drift_root: Path, message: str, package_names: Optional[List[str]] = None) -> None:
    """Core function to execute committing install repository changes, shared by both CLI backends."""
    from ..install_repo import run_primitive_6_commit_install_repo

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_6_commit_install_repo(workspace_config, commit_message=message, target_pkgs=package_names)


def execute_reverse_sync(drift_root: Path, package_names: Optional[List[str]] = None) -> None:
    """Core function to execute reverse sync (System -> install/), shared by both CLI backends."""
    from ..reverse_sync import run_primitive_1_reverse_sync

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_1_reverse_sync(workspace_config, package_names=package_names)


def execute_new(
    drift_root: Path,
    package_name: str,
    config_filename: Optional[str] = None,
    force: bool = False,
    target_directory: Optional[str] = None,
    install_method: Optional[str] = None
) -> None:
    """Core function to create a new package, shared by both CLI backends."""
    from ..new_package import run_primitive_10_create_new_package

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_10_create_new_package(
        workspace_config,
        package_name,
        config_filename=config_filename,
        force=force,
        target_directory=target_directory,
        install_method=install_method
    )


def execute_uninstall(drift_root: Path, package_names: List[str], force: bool = False, dry_run: bool = False, detach: bool = False) -> None:
    """Core function to uninstall or detach packages, shared by both CLI backends."""
    from ..uninstall_repo import run_primitive_7_uninstall_packages

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_7_uninstall_packages(workspace_config, package_names=package_names, force=force, dry_run=dry_run, detach=detach)


def execute_status(drift_root: Path, package_names: Optional[List[str]] = None) -> None:
    """Core function to audit workspace status, shared by both CLI backends."""
    from ..workspace_status import run_primitive_status
    
    workspace_config = load_workspace_config_default(drift_root)
    results = run_primitive_status(workspace_config, target_pkgs=package_names)
    
    # We use a simple print here, which can be enhanced by CLI backends if needed
    for s in results:
        print(f"\nPackage: {s.name}")
        
        # Template
        a_status = f"[A] Template: {s.template_status}"
        print(f"  {a_status}")
        if s.template_changes:
             for change in s.template_changes:
                 print(f"      {change}")
                 
        # System
        b_status = f"[B] System:   {s.system_status}"
        print(f"  {b_status}")
        if s.system_changes:
             for change in s.system_changes:
                 print(f"      {change}")
                 
        # Pending
        d_status = f"[Δ] Pending:  {s.pending_status}"
        print(f"  {d_status}")
        if s.pending_status != "CLEAN" and s.pending_status != "EMPTY":
            plus = len(s.pending_changes.added)
            tilde = len(s.pending_changes.modified)
            minus = len(s.pending_changes.deleted)
            print(f"      (+{plus}, ~{tilde}, -{minus} files)")


def execute_gc(drift_root: Path, dry_run: bool = False) -> None:
    """Core function to garbage collect orphans and purge databases, shared by both CLI backends."""
    from ..workspace_gc import run_primitive_9_purge_workspace_garbage

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_9_purge_workspace_garbage(workspace_config, dry_run=dry_run)


def execute_adopt(
    drift_root: Path,
    package_names: List[str],
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False
) -> None:
    """Core function to adopt system drifts back to source templates, shared by both CLI backends."""
    from ..adopt_repo import run_primitive_adopt_drifts

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_adopt_drifts(
        workspace_config=workspace_config,
        package_names=package_names,
        interactive=interactive,
        accept_conflicts=accept_conflicts,
        force=force,
        dry_run=dry_run
    )


def execute_diff(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    diff_type: str = "pending",
    side_by_side: bool = False,
    stat: bool = False
) -> None:
    """Core function to visualize changes, shared by both CLI backends."""
    from ..workspace_diff import run_primitive_diff
    
    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_diff(workspace_config, package_names=package_names, diff_type=diff_type, side_by_side=side_by_side, stat=stat)


def execute_add(
    drift_root: Path,
    package_name: str,
    import_paths: List[str],
    dry_run: bool = False
) -> None:
    """Core function to import resources into a package, shared by both CLI backends."""
    from ..add_resource import run_primitive_11_add_resources
    
    workspace_config = load_workspace_config_default(drift_root)
    paths = [Path(p) for p in import_paths]
    run_primitive_11_add_resources(workspace_config, package_name, paths, dry_run=dry_run)


def execute_rollback(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    force: bool = False
) -> None:
    """Core function to rollback failed deployments, shared by both CLI backends."""
    from ..rollback_repo import run_primitive_8_rollback_recovery
    
    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_8_rollback_recovery(
        workspace_config=workspace_config,
        package_names=package_names,
        force=force
    )


def execute_deploy(
    drift_root: Path,
    package_names: Optional[List[str]] = None,
    force: bool = False
) -> None:
    """Core function to execute transactional deploy workflow, shared by both CLI backends."""
    from ..deploy_repo import run_primitive_deploy_pipeline

    workspace_config = load_workspace_config_default(drift_root)
    run_primitive_deploy_pipeline(
        workspace_config=workspace_config,
        packages_to_deploy=package_names,
        force=force
    )


def execute_help(topic: Optional[str] = None) -> None:
    """Core function to display help documentation pages with paging fallback support."""
    from .help_docs import print_help_document
    print_help_document(topic)
