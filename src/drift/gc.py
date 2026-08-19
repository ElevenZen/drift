"""Primitive 9: Garbage Collection (Orphan Cleanup)."""

import logging
from typing import List, Optional

from .workspace_config import WorkspaceConfig
from .state_registry import load_state_registry
from .uninstall_repo import run_primitive_7_uninstall_packages

logger = logging.getLogger(__name__)

def run_primitive_9_garbage_collect_orphans(
    workspace_config: WorkspaceConfig,
    dry_run: bool = False
) -> List[str]:
    """
    Identifies and uninstalls orphan packages.
    An orphan is a package present in the state registry but disabled in workspace config.
    """
    state_file = workspace_config.install_path / "state.toml"
    if not state_file.exists():
        return []
        
    registry = load_state_registry(state_file)
    
    orphans = []
    for pkg in registry.packages:
        if not workspace_config.is_package_enabled(pkg):
            orphans.append(pkg)
            
    if not orphans:
        return []
        
    if dry_run:
        logger.info(f"🔍 [DRY RUN] Would garbage collect orphan package(s): {', '.join(orphans)}")
        return orphans

    logger.info(f"♻️  [GARBAGE COLLECTION] Discovering and cleaning orphan package(s): {', '.join(orphans)}")
    
    # Force uninstallation of orphans because they are disabled by definition
    run_primitive_7_uninstall_packages(workspace_config, orphans, force=True)
    
    return orphans
