"""Primitive 7 & Stage 1: Bidirectional Drift Adoption & Workspace Sync."""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .workspace_config import WorkspaceConfig
from .constants import CONFIG_DIR_NAME
from .git_utils import (
    get_git_status_porcelain,
    has_uncommitted_modifications,
    is_git_tracked,
    run_command
)
from .file_utils import remove_file_or_dir

logger = logging.getLogger(__name__)


def get_drifted_packages(workspace_config: WorkspaceConfig) -> List[str]:
    """Scans the install/ repository status to identify which packages have uncommitted drifts."""
    lines = get_git_status_porcelain(workspace_config.install_path)
    drifted = set()
    for line in lines:
        if len(line) < 4:
            continue
        path_str = line[3:].strip()
        parts = Path(path_str).parts
        if not parts:
            continue
        pkg_name = parts[0]
        # Ignore standard state metadata in install/ (state.toml, .stow-local-ignore) as they are not user packages
        if pkg_name in ["state.toml", ".stow-local-ignore"]:
            continue
        src_pkg_dir = workspace_config.source_path / pkg_name
        if not src_pkg_dir.is_dir():
            continue
        drifted.add(pkg_name)
    return sorted(list(drifted))


def check_source_cleanliness(workspace_config: WorkspaceConfig, pkg: str, force: bool) -> None:
    """Verifies that the target package source directory is clean before adopting drifts."""
    src_pkg_dir = workspace_config.source_path / pkg
    # Scopes git check to src/<pkg> inside workspace git root
    if has_uncommitted_modifications(workspace_config.drift_root, src_pkg_dir):
        if force:
            logger.warning(f"⚠️  [FORCE] Bypassing Git cleanliness safeguard. Overriding uncommitted changes in 'src/{pkg}/'.")
            return
        rel_dirty = src_pkg_dir.relative_to(workspace_config.drift_root)
        raise RuntimeError(
            f"The source directory of package '{pkg}' has uncommitted modifications!\n"
            f"Adopting system configurations into a dirty package directory is unsafe.\n"
            f"Please commit or stash your changes in '{rel_dirty}/' before running 'drift adopt {pkg}'."
        )


def get_package_drifts(install_base: Path, pkg: str) -> Tuple[List[Path], List[Path], List[Path], List[Tuple[Path, Path]]]:
    """Returns lists of additions, deletions, modifications, and renames relative to the package directory."""
    lines = get_git_status_porcelain(install_base, pkg)
    additions = []
    deletions = []
    modifications = []
    renames = []
    for line in lines:
        if len(line) < 4:
            continue

        status = line[:2]
        path_str = line[3:].strip()

        # Handle Renames (R status), which are reported as "old_path -> new_path"
        if status.strip().startswith("R") and " -> " in path_str:
            # includes "R " and "RM" status
            old_path_str, new_path_str = path_str.split(" -> ", 1)
            try:
                old_rel_path = Path(old_path_str).relative_to(pkg)
                new_rel_path = Path(new_path_str).relative_to(pkg)
                renames.append((old_rel_path, new_rel_path))
            except ValueError:
                pass
            continue

        # Parse relative path inside the package folder
        try:
            rel_path = Path(path_str).relative_to(pkg)
        except ValueError:
            continue
        
        if "?" in status or "A" in status:
            additions.append(rel_path)
        elif "D" in status:
            deletions.append(rel_path)
        elif "M" in status:
            modifications.append(rel_path)
            
    return sorted(additions), sorted(deletions), sorted(modifications), sorted(renames, key=lambda x: x[1])


def generate_unified_patch(install_base: Path,
                           pkg_rel_path: Path,
                           old_rel_path: Optional[Path] = None) -> str:
    """Generates the unified patch string for a given drifted file in install/."""
    cmd = ["git", "-C", str(install_base), "diff", "HEAD", "--"]
    if old_rel_path:
        cmd.append(str(old_rel_path))
    cmd.append(str(pkg_rel_path))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate unified patch for {pkg_rel_path}: {e}")
        return ""


def check_patch_conflicts(src_file: Path, patch_content: str) -> bool:
    """Runs a dry-run of patch to determine if there are conflicts applying the patch to src_file."""
    if not patch_content.strip():
        return False
    cmd = ["patch", "--dry-run", "--no-backup-if-mismatch", str(src_file)]
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, check=True)
        return False
    except subprocess.CalledProcessError:
        return True


def apply_source_patch(src_file: Path, patch_content: str, accept_conflicts: bool = False) -> bool:
    """Applies a patch to a source file, supporting merge markers if accept_conflicts is True."""
    cmd = ["patch", "--no-backup-if-mismatch"]
    if accept_conflicts:
        cmd.append("--merge")
    cmd.append(str(src_file))
    
    try:
        subprocess.run(cmd, input=patch_content, capture_output=True, text=True, check=True)
        # Clean up any rejected file if generated
        rej_file = src_file.with_suffix(src_file.suffix + ".rej")
        if rej_file.exists():
            rej_file.unlink()
        return True
    except subprocess.CalledProcessError:
        rej_file = src_file.with_suffix(src_file.suffix + ".rej")
        if rej_file.exists():
            rej_file.unlink()
        return False


def test_file_conflict(src_file: Path, install_file: Path, install_base: Path, pkg_rel_path: Path) -> bool:
    """Evaluates patch conflicts for a single modified file."""
    patch_content = generate_unified_patch(install_base, pkg_rel_path)
    return check_patch_conflicts(src_file, patch_content)


def resolve_source_file_path(workspace_config: WorkspaceConfig, pkg: str, rel_path: Path) -> Optional[Path]:
    """Resolves the physical file path inside src/pkg_dir using find_source_file_for_rendered_names."""
    src_pkg_dir = workspace_config.source_path / pkg
    
    # Locate a file in src/pkg/rel_path.parent that renders to rel_path.name
    match_info = workspace_config.find_source_file_for_rendered_names(
        src_pkg_dir / rel_path.parent,
        [rel_path.name]
    )
    if match_info:
        return match_info.path
    return None


# --- Section F: Reconciling Sub-Actions ---

def adopt_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Copies a wild host-side added file into the declarative source folder."""
    dest = pkg_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(install_pkg_dir / rel_path, dest)


def ignore_addition(pkg_dir: Path, install_pkg_dir: Path, rel_path: Path) -> None:
    """Unlinks the file from install base and registers the relative path pattern in .drift_ignore."""
    install_file = install_pkg_dir / rel_path
    if install_file.exists() or install_file.is_symlink():
        remove_file_or_dir(install_file)
            
    install_base = install_pkg_dir.parent
    rel_install_base = Path(install_pkg_dir.name) / rel_path
    subprocess.run(["git", "-C", str(install_base), "rm", "--cached", "-f", "--", str(rel_install_base)], capture_output=True)

    # Append pattern to .drift_ignore
    ignore_file = pkg_dir / ".drift_ignore"
    pattern = rel_path.as_posix()
    content = f"\n{pattern}\n"
    with open(ignore_file, "a", encoding="utf-8") as f:
        f.write(content)


def adopt_deletion(workspace_config: WorkspaceConfig, pkg: str, rel_path: Path) -> None:
    """Symmetrically deletes the corresponding file from declarative source folder."""
    pkg_dir = workspace_config.source_path / pkg
    src_file = resolve_source_file_path(workspace_config, pkg, rel_path)
    if src_file and (src_file.exists() or src_file.is_symlink()):
        remove_file_or_dir(src_file)


def adopt_modification(src_file: Path, patch_content: str) -> bool:
    """Directly applies clean patch or overwrites the modified configuration file."""
    success = apply_source_patch(src_file, patch_content, accept_conflicts=False)
    if not success:
        logger.error(f"Failed to apply patch to template file {src_file.name}. Please check manually.")
    return success


def generate_adjusted_patch(
    install_base: Path,
    pkg: str,
    new_rel_path: Path,
    old_rel_path: Optional[Path] = None,
    target_src_filename: Optional[str] = None
) -> str:
    """Generates a unified patch and adjusts its diff headers to point to a target source file name."""
    patch_content = generate_unified_patch(
        install_base,
        Path(pkg) / new_rel_path,
        old_rel_path=(Path(pkg) / old_rel_path if old_rel_path else None)
    )
    if not patch_content.strip() or not target_src_filename:
        return patch_content

    lines = patch_content.splitlines()
    adjusted_lines = []
    for line in lines:
        if (line.startswith("diff --git") or 
            line.startswith("similarity index") or 
            line.startswith("rename from") or 
            line.startswith("rename to") or 
            line.startswith("index ")):
            continue
        if line.startswith("--- "):
            adjusted_lines.append(f"--- a/{target_src_filename}")
            continue
        if line.startswith("+++ "):
            adjusted_lines.append(f"+++ b/{target_src_filename}")
            continue
        adjusted_lines.append(line)
    return "\n".join(adjusted_lines) + "\n"


def adopt_rename(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    patch_content: str,
    accept_conflicts: bool = False
) -> None:
    """Symmetrically renames the source template file and applies any content patch."""
    pkg_dir = workspace_config.source_path / pkg
    old_src_file = resolve_source_file_path(workspace_config, pkg, old_rel_path)
    if old_src_file and old_src_file.exists():
        new_src_name = workspace_config.make_new_template_name(old_src_file.name,
                                                               new_rel_path.name)
        new_src_file = pkg_dir / new_rel_path.parent / new_src_name

        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old_src_file, new_src_file)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in package '{pkg}'. Creating a new template file for '{new_rel_path}'.")
        new_src_file = pkg_dir / new_rel_path
        new_src_file.parent.mkdir(parents=True, exist_ok=True)
        new_src_file.touch()
        
    if patch_content.strip():
        if not apply_source_patch(new_src_file, patch_content, accept_conflicts=accept_conflicts):
            logger.error(f"Failed to apply patch to renamed template file {new_src_file.name}. Please check manually.")


def fallback_over_render(src_file: Path, static_file: Path) -> None:
    """Backs up the original template to .bak and overwrites it with static file content (freezing template)."""
    bak_file = src_file.with_suffix(src_file.suffix + ".bak")
    shutil.copy2(src_file, bak_file)
    shutil.copy2(static_file, src_file)
    logger.warning(f"⚠️  [FREEZE] Overwrote template '{src_file.name}' with static content. Original template backed up to '{bak_file.name}'.")


def fallback_conflict_editor(src_file: Path, patch_content: str) -> None:
    """Uses patch --merge to write conflict markers into the template, then opens default $EDITOR."""
    apply_source_patch(src_file, patch_content, accept_conflicts=True)
    editor = os.environ.get("EDITOR", "vim")
    logger.info(f"📝 Launching editor '{editor}' to resolve conflicts in '{src_file}'...")
    subprocess.run([editor, str(src_file)], check=True)


def fallback_side_by_side(src_file: Path, install_file: Path) -> None:
    """Launches editor to display both template and the final compiled/static drift as side-by-side reference."""
    editor = os.environ.get("EDITOR", "vim")
    if editor in ["vim", "nvim", "code"]:
        cmd = [editor, "-d", str(src_file), str(install_file)]
    elif editor == 'emacs':
        cmd = ['emacs', '--eval', f'(ediff-files "{str(src_file)}" "{str(install_file)}")']
    else:
        cmd = [editor, str(src_file), str(install_file)]
    logger.info(f"📝 Launching editor '{editor}' for side-by-side template edit...")
    subprocess.run(cmd, check=True)


# --- The Dry-Run Engine ---

def dry_run_adopt(
    workspace_config: WorkspaceConfig,
    pkg: str,
    additions: List[Path],
    deletions: List[Path],
    modifications: List[Path],
    renames: List[Tuple[Path, Path]]
) -> None:
    """Prints a clear preview of all drift changes and potential conflicts."""
    logger.info(f"\n🔍 [DRY RUN] Previewing drift adoption for package '{pkg}':")
    
    pkg_dir = workspace_config.source_path / pkg
    install_pkg_dir = workspace_config.install_path / pkg

    if additions:
        logger.info("   [+] Additions (will be copied to source):")
        for file in additions:
            logger.info(f"       + {file}")

    if deletions:
        logger.info("   [-] Deletions (will be removed from source):")
        for file in deletions:
            logger.info(f"       - {file}")

    if renames:
        logger.info("   [R] Renames (will rename the template in source):")
        for old_file, new_file in renames:
            logger.info(f"       R {old_file} -> {new_file}")

    if modifications:
        logger.info("   [~] Modifications:")
        for file in modifications:
            src_file = resolve_source_file_path(workspace_config, pkg, file)
            if not src_file:
                logger.info(f"       ~ {file} [Static Overwrite]")
                continue
                
            # If the resolved source file is templated, check for conflicts
            is_templated = src_file.suffix in [".sh", ".toml", ".json", ".conf"] or ".envst" in src_file.name or ".mustache" in src_file.name
            if is_templated:
                install_file = install_pkg_dir / file
                pkg_rel_path = Path(pkg) / file
                has_conflict = test_file_conflict(src_file, install_file, workspace_config.install_path, pkg_rel_path)
                if has_conflict:
                    logger.info(f"       ~ {file} [CONFLICTS with template: {src_file.name}]")
                else:
                    logger.info(f"       ~ {file} [Patch applies cleanly to: {src_file.name}]")
            else:
                logger.info(f"       ~ {file} [Static Overwrite of: {src_file.name}]")


# --- The Main Dispatcher ---

def handle_single_addition(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool
) -> bool:
    """Handles drift reconciliation for a single file addition."""
    target_existing_src = resolve_source_file_path(workspace_config, pkg, rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot adopt addition '{rel_path}' because the target already exists in source. Skipping.")
            return False
        else:
            print(f"\n⚠️  [CONFLICT] Target file '{rel_path}' already exists in source!")
            print("Reconciliation options:")
            print("[1] Discard addition / Restore (restores original on host next deployment)")
            print("[2] Skip file")
            choice = input("Select option [1-2]: ").strip()
            if choice == "1":
                return True
        return False

    pkg_dir: Path = workspace_config.source_path / pkg
    if not interactive:
        adopt_addition(pkg_dir, install_pkg_dir, rel_path)
        return True
    else:
        print(f"\nFound untracked file addition inside Fully-Controlled Directory: {rel_path}")
        print("Reconciliation options:")
        print("[1] Adopt and copy into source package")
        print("[2] Ignore file (appends pattern to package .drift_ignore)")
        print("[3] Discard file (stages file to install/ database so it is deleted on next deploy)")
        print("[4] Skip file")
        choice = input("Select option [1-4]: ").strip()
        if choice == "1":
            adopt_addition(pkg_dir, install_pkg_dir, rel_path)
            return True
        elif choice == "2":
            ignore_addition(pkg_dir, install_pkg_dir, rel_path)
            return True
        elif choice == "3":
            return True
        else:
            return False


def handle_single_deletion(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool
) -> bool:
    """Handles drift reconciliation for a single file deletion."""
    target_existing_src = resolve_source_file_path(workspace_config, pkg, rel_path)
    if target_existing_src is None:
        if not interactive:
            logger.warning(f"⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        else:
            print(f"\n⚠️  [SKIP] Cannot adopt deletion '{rel_path}' because the target does not exist in source. Skipping.")
        return True

    if not interactive:
        adopt_deletion(workspace_config, pkg, rel_path)
        return True
    else:
        print(f"\nFound host file deletion: {rel_path}")
        print("Reconciliation options:")
        print("[1] Adopt deletion (deletes source file/template)")
        print("[2] Discard deletion / Restore (restores file in next deployment)")
        print("[3] Skip file")
        choice = input("Select option [1-3]: ").strip()
        if choice == "1":
            adopt_deletion(workspace_config, pkg, rel_path)
            return True
        elif choice == "2":
            return True
        else:
            return False


def handle_rename_non_interactive(
    workspace_config: WorkspaceConfig,
    pkg: str,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    old_src_file: Optional[Path],
    patch_content: str,
    has_patch_conflict: bool,
    accept_conflicts: bool
) -> bool:
    """Processes a rename drift non-interactively."""
    if not has_patch_conflict:
        adopt_rename(workspace_config, pkg, install_pkg_dir, old_rel_path, new_rel_path, patch_content)
        return True
    else:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into renamed template file: '{old_src_file.name if old_src_file else ''}'")
            adopt_rename(workspace_config, pkg, install_pkg_dir, old_rel_path, new_rel_path, patch_content, accept_conflicts=True)
            return True
        else:
            logger.error(f"❌ [CONFLICT] Cannot apply system diff cleanly onto renamed template file '{old_src_file.name if old_src_file else ''}'. Skipping.")
            logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
            return False


def handle_rename_interactive(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_dir: Path,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    old_src_file: Optional[Path],
    patch_content: str,
    has_patch_conflict: bool
) -> bool:
    """Processes a rename drift interactively."""
    print(f"\nFound host file rename: {old_rel_path} -> {new_rel_path}")
    if not has_patch_conflict:
        print("Reconciliation options:")
        print("[1] Adopt rename (renames template file and applies patch)")
        print("[2] Discard rename / Restore (restores file rename in next deployment)")
        print("[3] Skip file")
        choice = input("Select option [1-3]: ").strip()
        if choice == "1":
            adopt_rename(workspace_config, pkg, install_pkg_dir, old_rel_path, new_rel_path, patch_content)
            return True
        elif choice == "2":
            return True
        else:
            return False
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto renamed template file '{old_src_file.name if old_src_file else ''}'!")
        print("Choose fallback resolution strategy:")
        print("[1] Over-render & Freeze (overwrites template, original saved to .bak)")
        print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
        print("[3] Open Side-by-Side Reference (opens template and static drift side-by-side)")
        print("[4] Discard rename / Restore")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice in ["1", "2", "3"]:
            # Perform rename first
            if old_src_file and old_src_file.exists():
                new_src_name = workspace_config.make_new_template_name(old_src_file.name,
                                                                       new_rel_path.name)
                new_src_file = pkg_dir / new_rel_path.parent / new_src_name
                new_src_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(old_src_file, new_src_file)
            else:
                new_src_file = pkg_dir / new_rel_path
                new_src_file.parent.mkdir(parents=True, exist_ok=True)
                new_src_file.touch()

            if choice == "1":
                fallback_over_render(new_src_file, install_pkg_dir / new_rel_path)
            elif choice == "2":
                # Adjust patch to new file name
                adjusted_patch = generate_adjusted_patch(
                    install_pkg_dir.parent,
                    pkg,
                    new_rel_path,
                    old_rel_path=old_rel_path,
                    target_src_filename=new_src_file.name
                )
                fallback_conflict_editor(new_src_file, adjusted_patch)
            elif choice == "3":
                fallback_side_by_side(new_src_file, install_pkg_dir / new_rel_path)
            return True
        elif choice == "4":
            return True
        else:
            return False


def handle_single_rename(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_dir: Path,
    install_pkg_dir: Path,
    old_rel_path: Path,
    new_rel_path: Path,
    interactive: bool,
    accept_conflicts: bool
) -> bool:
    """Handles drift reconciliation for a single file/template rename."""
    # Check if the target already exists in source
    target_existing_src = resolve_source_file_path(workspace_config, pkg, new_rel_path)
    if target_existing_src is not None:
        if not interactive:
            logger.error(f"❌ [CONFLICT] Cannot rename '{old_rel_path}' to '{new_rel_path}' because the target already exists in source. Skipping.")
        else:
            print(f"\n⚠️  [CONFLICT] Target file '{new_rel_path}' already exists in source!")
            print("Reconciliation options:")
            print("[1] Discard rename / Restore (restores original on host next deployment)")
            print("[2] Skip file")
            choice = input("Select option [1-2]: ").strip()
            if choice == "1":
                return True
        return False

    old_src_file = resolve_source_file_path(workspace_config, pkg, old_rel_path)
    has_patch_conflict = False
    if old_src_file and old_src_file.exists():
        patch_content = generate_adjusted_patch(
            workspace_config.install_path,
            pkg,
            new_rel_path,
            old_rel_path=old_rel_path,
            target_src_filename=old_src_file.name
        )
        has_patch_conflict = check_patch_conflicts(old_src_file, patch_content)
    else:
        logger.warning(f"⚠️  Old source file for '{old_rel_path}' not found in package '{pkg}'. Treating as new file addition.")
        return handle_single_addition(workspace_config, pkg, install_pkg_dir, new_rel_path, interactive)

    if not interactive:
        return handle_rename_non_interactive(
            workspace_config, pkg, install_pkg_dir, old_rel_path, new_rel_path,
            old_src_file, patch_content, has_patch_conflict, accept_conflicts
        )
    else:
        return handle_rename_interactive(
            workspace_config, pkg, pkg_dir, install_pkg_dir, old_rel_path, new_rel_path,
            old_src_file, patch_content, has_patch_conflict
        )


def handle_modification_non_interactive(
    src_file: Path,
    install_file: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool,
    accept_conflicts: bool
) -> bool:
    """Processes a modification drift non-interactively."""
    if not has_conflict:
        if is_templated:
            has_content_hunks = any(line.startswith("@@") for line in patch_content.splitlines())
            if has_content_hunks:
                adopt_modification(src_file, patch_content)
            try:
                src_file.chmod(install_file.stat().st_mode)
            except Exception:
                pass
        else:
            shutil.copy2(install_file, src_file)
        return True
    else:
        if accept_conflicts:
            logger.warning(f"⚠️  Applying conflicting patch into file: '{src_file.name}'")
            apply_source_patch(src_file, patch_content, accept_conflicts=True)
            try:
                src_file.chmod(install_file.stat().st_mode)
            except Exception:
                pass
            return True
        else:
            logger.error(f"❌ [CONFLICT] Cannot apply system diff cleanly onto file '{src_file.name}'. Skipping.")
            logger.error("   Run 'drift adopt --interactive' or pass '--accept-conflicts' to resolve.")
            return False


def handle_modification_interactive(
    src_file: Path,
    install_file: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    patch_content: str,
    is_templated: bool,
    has_conflict: bool
) -> bool:
    """Processes a modification drift interactively."""
    if not has_conflict:
        if is_templated:
            print(f"\nFound modified templated file (Patch applies cleanly): {rel_path}")
            print("Reconciliation options:")
            print("[1] Adopt modifications (applies patch cleanly to template)")
            print("[2] Discard modifications / Restore (restores file in next deployment)")
            print("[3] Skip file")
            choice = input("Select option [1-3]: ").strip()
            if choice == "1":
                has_content_hunks = any(line.startswith("@@") for line in patch_content.splitlines())
                if has_content_hunks:
                    adopt_modification(src_file, patch_content)
                try:
                    src_file.chmod(install_file.stat().st_mode)
                except Exception:
                    pass
                return True
            elif choice == "2":
                return True
            else:
                return False
        else:
            print(f"\nFound modified static config file (Patch applies cleanly): {rel_path}")
            print("Reconciliation options:")
            print("[1] Adopt modifications (overwrites source file)")
            print("[2] Discard modifications / Restore (restores file in next deployment)")
            print("[3] Skip file")
            choice = input("Select option [1-3]: ").strip()
            if choice == "1":
                shutil.copy2(install_file, src_file)
                return True
            elif choice == "2":
                return True
            else:
                return False
    else:
        print(f"\n⚠️  [PATCH CONFLICT] Could not automatically apply system diff onto file '{src_file.name}'!")
        print("Choose a fallback resolution strategy:")
        print("[1] Over-render & Freeze (overwrites template/file, original saved to .bak)")
        print("[2] Open Merge Conflict Editor (writes conflict markers and opens editor)")
        print("[3] Open Side-by-Side Reference (opens file and static drift side-by-side)")
        print("[4] Discard modifications / Restore (restores file in next deployment)")
        print("[5] Skip file")
        choice = input("Select option [1-5]: ").strip()
        if choice == "1":
            fallback_over_render(src_file, install_file)
            return True
        elif choice == "2":
            fallback_conflict_editor(src_file, patch_content)
            return True
        elif choice == "3":
            fallback_side_by_side(src_file, install_file)
            return True
        elif choice == "4":
            return True
        else:
            return False


def handle_single_modification(
    workspace_config: WorkspaceConfig,
    pkg: str,
    pkg_dir: Path,
    install_pkg_dir: Path,
    rel_path: Path,
    interactive: bool,
    accept_conflicts: bool
) -> bool:
    """Handles drift reconciliation for a single file/template modification."""
    src_file = resolve_source_file_path(workspace_config, pkg, rel_path)
    pkg_rel_path = Path(pkg) / rel_path
    install_file = install_pkg_dir / rel_path

    if not src_file:
        # Symmetrically handle static file as an addition so the user has full choice in interactive mode.
        return handle_single_addition(workspace_config, pkg,
                                      install_pkg_dir, rel_path, interactive)

    is_templated = ".envst" in src_file.name or ".mustache" in src_file.name
    patch_content = generate_unified_patch(workspace_config.install_path, pkg_rel_path)

    # Check if there are content diff hunks in the patch
    has_content_hunks = any(line.startswith("@@") for line in patch_content.splitlines())

    if not is_templated:
        # Because the src folder is git clean, so we can safely overwrite.
        # For static files, adopting simply overwrites src_file with install_file (including permissions)
        has_conflict = False
    elif not has_content_hunks:
        # For templated files with only mode/permission changes, apply mode cleanly without patch conflict
        has_conflict = False
    else:
        # For templated files with text changes, check if patch applies cleanly
        has_conflict = check_patch_conflicts(src_file, patch_content)

    if not interactive:
        return handle_modification_non_interactive(
            src_file, install_file, patch_content, is_templated, has_conflict, accept_conflicts
        )
    else:
        return handle_modification_interactive(
            src_file, install_file, install_pkg_dir, rel_path, patch_content, is_templated, has_conflict
        )


def adopt_single_package(
    workspace_config: WorkspaceConfig,
    pkg: str,
    interactive: bool = False,
    accept_conflicts: bool = False,
    dry_run: bool = False
) -> bool:
    """Adopt drifts for a single package according to interactive or non-interactive choices.
    
    Returns True if all drifts in the package were resolved cleanly and can be committed;
    returns False if any file was skipped or had unresolved conflicts.
    """
    if not dry_run:
        # Pre-stage all changes in the install repository under the package subdirectory so that git rename detection operates correctly.
        subprocess.run(["git", "-C", str(workspace_config.install_path), "add", "--all", pkg], capture_output=True)

    additions, deletions, modifications, renames = get_package_drifts(workspace_config.install_path, pkg)
    
    if not additions and not deletions and not modifications and not renames:
        logger.info(f"✨ Package '{pkg}' has no drifts.")
        if not dry_run:
            subprocess.run(["git", "-C", str(workspace_config.install_path), "restore", "--staged", "--", pkg], capture_output=True)
        return True

    if dry_run:
        dry_run_adopt(workspace_config, pkg, additions, deletions, modifications, renames)
        return True

    pkg_dir = workspace_config.source_path / pkg
    install_pkg_dir = workspace_config.install_path / pkg

    # Trigger pre_source hook before adopting drifts into source directory
    from .lifecycle_hooks import trigger_pre_source_lifecycle_hook
    trigger_pre_source_lifecycle_hook(workspace_config, pkg, load_envs=True)

    skipped_files = []

    # 1. Process Additions
    for rel_path in additions:
        resolved = handle_single_addition(workspace_config, pkg, install_pkg_dir, rel_path, interactive)
        if not resolved:
            skipped_files.append(rel_path)

    # 2. Process Deletions
    for rel_path in deletions:
        resolved = handle_single_deletion(workspace_config, pkg, install_pkg_dir, rel_path, interactive)
        if not resolved:
            skipped_files.append(rel_path)

    # 3. Process Renames
    for old_rel_path, new_rel_path in renames:
        resolved = handle_single_rename(
            workspace_config, pkg, pkg_dir, install_pkg_dir,
            old_rel_path, new_rel_path, interactive, accept_conflicts
        )
        if not resolved:
            skipped_files.append(old_rel_path)
            skipped_files.append(new_rel_path)

    # 4. Process Modifications
    for rel_path in modifications:
        resolved = handle_single_modification(
            workspace_config, pkg, pkg_dir, install_pkg_dir, rel_path, interactive, accept_conflicts
        )
        if not resolved:
            skipped_files.append(rel_path)

    # Unstage any skipped/failed files so they remain as uncommitted local drift in install/
    if skipped_files:
        for rel_path in skipped_files:
            rel_spec = (Path(pkg) / rel_path).as_posix()
            subprocess.run([
                "git", "-C", str(workspace_config.install_path),
                "restore", "--staged", "--", rel_spec
            ], capture_output=True)
        return False

    return True


def run_primitive_adopt_drifts(
    workspace_config: WorkspaceConfig,
    package_names: Optional[List[str]] = None,
    interactive: bool = False,
    accept_conflicts: bool = False,
    force: bool = False,
    dry_run: bool = False
) -> List[str]:
    """High-level orchestrator for adopting system drifts back to declarative templates."""
    # 1. Discovery
    if not package_names:
        package_names = get_drifted_packages(workspace_config)
        if not package_names:
            logger.info("✨ No drifted packages found in local state database.")
            return []

    # 2. Check cleanliness guard for each package
    for pkg in package_names:
        check_source_cleanliness(workspace_config, pkg, force=force)

    # 3. Process each package
    resolved_packages = []
    for pkg in package_names:
        is_resolved = adopt_single_package(
            workspace_config=workspace_config,
            pkg=pkg,
            interactive=interactive,
            accept_conflicts=accept_conflicts,
            dry_run=dry_run
        )
        if is_resolved:
            resolved_packages.append(pkg)

    # 4. Commit resolved packages in install base
    if resolved_packages and not dry_run:
        from .git_utils import commit_repo_changes
        commit_repo_changes(
            repo_path=workspace_config.install_path,
            commit_message=f"Adopt: Resolved and locked drifts for package(s) {', '.join(resolved_packages)}",
            target_pkgs=resolved_packages,
            repo_name="install repo"
        )

    return resolved_packages
