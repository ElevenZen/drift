"""Primitive 11: Resource Import (Add files/folders to package)."""

import logging
import shutil
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

from .workspace_config import WorkspaceConfig, RenderEngineConfig
from .file_utils import (
    translate_dot_prefixes_reverse,
    is_relative_to,
    resolve_system_target,
    tree_relative_files
)
from .constants import PACKAGE_CONFIG_FILE_NAME, MANAGED_CONFIG_FILES
from .ignore import DriftIgnore
from .folder_diff import compare_folders

logger = logging.getLogger(__name__)

def get_package_target_directory_from_source(
    workspace_config: WorkspaceConfig,
    src_pkg_dir: Path,
    package_name: str
) -> Path:
    """Resolves the target directory for a package, handling config templates."""
    from .package_config import load_package_config_from_source_dir
    try:
        pkg_config = load_package_config_from_source_dir(
            package_dir=src_pkg_dir,
            package_name=package_name,
            workspace_config=workspace_config
        )
        target_base = pkg_config.get_target_directory(workspace_config)
    except Exception as e:
        logger.warning(f"Failed to load package configuration in {src_pkg_dir}: {e}. Using defaults.")
        target_base = workspace_config.default_target_path

    return target_base.resolve()

def generate_import_worklist(
    workspace_config: WorkspaceConfig,
    target_base: Path,
    import_paths: List[Path],
    ignore_handler: DriftIgnore
) -> List[Tuple[Path, Path]]:
    """
    Generates a list of (absolute_source_path, target_relative_path) for all files to be imported.
    Handles directory expansion and respects ignore rules.
    """
    worklist: List[Tuple[Path, Path]] = []
    
    # We use a persistent temp dir for all directory comparisons in this run
    with tempfile.TemporaryDirectory(prefix="drift-import-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        
        for path in import_paths:
            abs_import = path.resolve()
            if not abs_import.exists():
                raise FileNotFoundError(f"Import path does not exist: {path}")

            if not is_relative_to(abs_import, target_base):
                raise ValueError(f"Import path '{abs_import}' is not inside package target directory '{target_base}'")
            
            rel_root_target = abs_import.relative_to(target_base)
            # repo_prefix is the translated path of the import root in the repo (e.g. .config -> dot-config)
            repo_prefix = translate_dot_prefixes_reverse(rel_root_target)

            # Scoped ignore handler is a duck-type that offsets paths to match package-root-relative patterns
            class ScopedIgnore:
                def match_path(self, rel_repo: Path) -> bool:
                    # rel_repo is already dot-prefixed by compare_folders(translate_mode="reverse")
                    return ignore_handler.match_path(repo_prefix / rel_repo)

            scoped_ignore: Any = ScopedIgnore()

            # Use compare_folders to get a clean list of files from system.
            # translate_mode="reverse" ensures the ignore handler receives repo-style paths.
            diff = compare_folders(
                abs_import, 
                tmp_dir, 
                ignore_handler=scoped_ignore,
                resolve_symlinks=True,
                translate_mode="reverse"
            )
            
            for rel_path in diff.added:
                # rel_path is relative to abs_import (system style, e.g. leading dots)
                full_rel_target = rel_root_target / rel_path if rel_path != Path("") else rel_root_target
                worklist.append((abs_import / rel_path, full_rel_target))
                
    return worklist

def run_primitive_11_add_resources(
    workspace_config: WorkspaceConfig,
    package_name: str,
    import_paths: List[Path],
    dry_run: bool = False
) -> None:
    """
    Orchestrates importing multiple resources into a package.
    1. Resolves package target directory.
    2. Identifies all files to import, respecting ignores.
    3. Performs global conflict check before any copy.
    4. Executes the import with dot-prefix translation.
    """
    # 1. Resolve package source directory
    src_pkg_dir = workspace_config.source_path / package_name
    if not src_pkg_dir.exists():
        raise FileNotFoundError(f"Package '{package_name}' source directory not found: {src_pkg_dir}")

    # Trigger pre_source hook before reading/writing source directory
    from .lifecycle_hooks import trigger_pre_source_hook
    trigger_pre_source_hook(workspace_config, package_name)

    # 2. Resolve target directory and ignores
    target_base = get_package_target_directory_from_source(workspace_config, src_pkg_dir, package_name)
    ignore_handler = DriftIgnore.load_from_dir(src_pkg_dir)

    # 3. Generate global worklist of files to import
    full_worklist = generate_import_worklist(workspace_config, target_base, import_paths, ignore_handler)

    if not full_worklist:
        logger.info(f"No resources to import into '{package_name}'.")
        return

    # 4. Global Conflict Check Phase
    for src_on_system, rel_target in full_worklist:
        conflict = workspace_config.find_conflict_in_source_dir(src_pkg_dir, rel_target)
        if conflict:
            rel_conflict = conflict.path.relative_to(workspace_config.drift_root)
            raise RuntimeError(f"Conflict detected: '{src_on_system}' would overwrite existing source '{rel_conflict}'")

    # 5. Execution Phase
    for src_on_system, rel_target in full_worklist:
        rel_src = translate_dot_prefixes_reverse(rel_target)
        dest_path = src_pkg_dir / rel_src
        
        if dry_run:
            logger.info(f"🔍 [DRY RUN] Would import '{src_on_system}' to '{dest_path.relative_to(workspace_config.drift_root)}'")
            continue

        logger.info(f"📥 Importing: {src_on_system}")
        logger.debug(f"   -> {dest_path.relative_to(workspace_config.drift_root)}")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_on_system, dest_path)

    if dry_run:
        return

    logger.info(f"✨ Successfully imported {len(full_worklist)} file(s) into package '{package_name}'.")
